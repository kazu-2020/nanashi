//! The file tier of the base: one file for each segment, and a bounded cache of block records.
//!
//! A process has at most one `Tier` (`Tier::install`). A Store whose `Config` has the tier writes each new
//! segment to `<dir>/seg-<id>.bin` and keeps only the fences in RAM. A segment file goes when its last `Arc`
//! drops. The files are a cache of state, because the journal and the snapshot make every segment again. So
//! the tier does not fsync a file and does not read a file after a restart.
//!
//! File layout:
//! ```text
//! header   32 B: "NSEG", version u32 = 1, rows u64, blocks u32, footer offset u64
//! records  one for each block: values (8 B each), packed key offsets (words of 8 B), 16 zero bytes
//! footer   16 B for each block: base u64, start u32 (the record offset in words), width u8, 3 B pad
//! ```

use crate::packed::{block_of, pack_into, Block, Fences, BLOCK};
use crate::Result;
use std::collections::BTreeMap;
use std::fs::File;
use std::io::{self, BufWriter, Write};
use std::ops::Range;
use std::os::unix::fs::FileExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

const HEADER: usize = 32;
const SPARE: usize = 16;
const SHARDS: usize = 64;
pub(crate) const WRITE_BUF: usize = 1 << 20;

/// One block record in the cache. Readers pin a record by holding the `Arc`.
pub(crate) type Buf = Arc<Vec<u8>>;

/// The segment directory and the block cache of the process.
pub struct Tier {
    dir: PathBuf,
    cache: Cache,
    pub(crate) cache_bytes: usize,
    next_id: AtomicU64,
    /// The blocks that a sequential reader takes from the file with one read.
    pub(crate) read_window: usize,
}

static TIER: OnceLock<Tier> = OnceLock::new();

impl Tier {
    /// Makes `dir` the segment directory of the process, or gives the tier that is set. A second tier in the
    /// same process with a different directory, cache size or window is an error.
    pub fn install(dir: &Path, cache_bytes: usize, read_window: usize) -> Result<&'static Tier> {
        let read_window = read_window.max(1);
        let same = |t: &Tier| t.dir == dir && t.cache_bytes == cache_bytes && t.read_window == read_window;
        if TIER.get().is_some_and(|t| !same(t)) {
            return Err("segment_dir はプロセスで 1 つ".into());
        }
        std::fs::create_dir_all(dir).map_err(|e| format!("segment_dir {} を作れない: {e}", dir.display()))?;
        let t = TIER.get_or_init(|| Tier { dir: dir.to_path_buf(), cache: Cache::new(cache_bytes), cache_bytes, next_id: AtomicU64::new(0), read_window });
        if !same(t) {
            return Err("segment_dir はプロセスで 1 つ".into());
        }
        Ok(t)
    }

    pub(crate) fn writer(&'static self) -> io::Result<SegWriter> {
        self.writer_of(self.next_id.fetch_add(1, Ordering::Relaxed))
    }

    fn writer_of(&'static self, id: u64) -> io::Result<SegWriter> {
        let path = self.dir.join(format!("seg-{id}.bin"));
        // A file that exists is the live segment of another process on the same directory: an error, not a truncation
        let file = std::fs::OpenOptions::new().read(true).write(true).create_new(true).open(&path)?;
        let mut w = BufWriter::with_capacity(WRITE_BUF, file);
        w.write_all(&[0u8; HEADER])?;
        Ok(SegWriter { tier: self, w, path, id, blocks: Vec::new(), words: (HEADER / 8) as u64, rows: 0, finished: false })
    }

    /// The tier for the tests of this crate: a directory of the process in the temp directory, a cache of 64 KiB
    /// and a window of 2 blocks, so eviction and window boundaries happen on small stores.
    #[cfg(test)]
    pub(crate) fn for_tests() -> &'static Tier {
        Tier::install(&std::env::temp_dir().join(format!("nanashi-engine-test-{}", std::process::id())), 1 << 16, 2).unwrap()
    }
}

impl std::fmt::Debug for Tier {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Tier({})", self.dir.display())
    }
}

impl PartialEq for Tier {
    fn eq(&self, other: &Tier) -> bool {
        std::ptr::eq(self, other)
    }
}

/// A sharded, byte-bounded LRU cache of block records, keyed by (segment id, block).
struct Cache {
    shards: Vec<Mutex<Shard>>,
    bound: usize, // bytes for each shard
}

#[derive(Default)]
struct Shard {
    map: rustc_hash::FxHashMap<(u64, u32), (Buf, u64)>, // the record and its tick in `lru`
    lru: BTreeMap<u64, (u64, u32)>,                      // tick of the last use -> key
    bytes: usize,
    tick: u64,
}

impl Cache {
    fn new(bound: usize) -> Cache {
        Cache { shards: (0..SHARDS).map(|_| Mutex::new(Shard::default())).collect(), bound: bound / SHARDS }
    }

    fn shard(&self, key: (u64, u32)) -> &Mutex<Shard> {
        let h = (key.0 ^ ((key.1 as u64) << 32)).wrapping_mul(0x9E37_79B9_7F4A_7C15);
        &self.shards[(h >> 58) as usize]
    }

    /// The record of `key`. A miss runs `read` outside the lock and inserts the result.
    fn get(&self, key: (u64, u32), read: impl FnOnce() -> Vec<u8>) -> Buf {
        let shard = self.shard(key);
        if let Some(b) = shard.lock().unwrap().touch(key) {
            return b;
        }
        let buf = Arc::new(read());
        let mut s = shard.lock().unwrap();
        if let Some(b) = s.touch(key) {
            return b; // another reader inserted it first
        }
        s.insert(key, buf.clone(), self.bound);
        buf
    }
}

impl Shard {
    fn touch(&mut self, key: (u64, u32)) -> Option<Buf> {
        let e = self.map.get_mut(&key)?;
        self.lru.remove(&e.1);
        self.tick += 1;
        e.1 = self.tick;
        self.lru.insert(self.tick, key);
        Some(e.0.clone())
    }

    /// Inserts the record and evicts the least recently used ones past `bound`. A record larger than `bound`
    /// is not kept: the `Arc` of the reader holds it.
    fn insert(&mut self, key: (u64, u32), buf: Buf, bound: usize) {
        if buf.len() > bound {
            return;
        }
        self.tick += 1;
        self.bytes += buf.len();
        self.map.insert(key, (buf, self.tick));
        self.lru.insert(self.tick, key);
        while self.bytes > bound {
            let (_, old) = self.lru.pop_first().unwrap();
            let (b, _) = self.map.remove(&old).unwrap();
            self.bytes -= b.len();
        }
    }
}

/// The file of one segment. The last `Arc<Segment>` drop removes the file.
#[derive(Debug)]
pub(crate) struct SegFile {
    tier: &'static Tier,
    file: File,
    path: PathBuf,
    id: u64,
    end: u64, // the byte offset of the footer: the end of the records
}

impl SegFile {
    pub(crate) fn read_window(&self) -> usize {
        self.tier.read_window
    }

    #[cfg(test)]
    pub(crate) fn path(&self) -> &Path {
        &self.path
    }

    /// The end of the records (the byte offset of the footer).
    pub(crate) fn end(&self) -> u64 {
        self.end
    }

    /// Reads the bytes `r` of the file into `out`. A read error is a hardware error: the file exists while the segment lives.
    pub(crate) fn read_into(&self, r: Range<u64>, out: &mut Vec<u8>) {
        out.resize((r.end - r.start) as usize, 0);
        if let Err(e) = self.file.read_exact_at(out, r.start) {
            panic!("セグメントファイル {} を読めない: {e}", self.path.display());
        }
    }

    /// Block `b` of the segment (its record is the bytes `r`) through the cache.
    pub(crate) fn block(&self, b: usize, r: Range<u64>) -> Buf {
        self.tier.cache.get((self.id, b as u32), || {
            let mut v = Vec::new();
            self.read_into(r, &mut v);
            v
        })
    }
}

impl Drop for SegFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}

/// Writes the records of one segment file, one block at a time.
pub(crate) struct SegWriter {
    tier: &'static Tier,
    w: BufWriter<File>,
    path: PathBuf,
    id: u64,
    blocks: Vec<Block>,
    words: u64, // the word offset of the next record
    rows: usize,
    finished: bool,
}

impl SegWriter {
    /// Writes the record of one block: `keys` (sorted, at most `BLOCK`) and their values.
    pub(crate) fn block(&mut self, keys: &[u64], vals: &[f64]) -> io::Result<()> {
        debug_assert!(!keys.is_empty() && keys.len() == vals.len() && keys.len() <= BLOCK);
        let m = keys.len();
        let (blk, words) = block_of(keys, self.words as usize);
        let mut rec = [0u8; BLOCK * 16 + SPARE];
        for (o, v) in rec.as_chunks_mut::<8>().0.iter_mut().zip(vals) {
            *o = v.to_le_bytes();
        }
        pack_into(&mut rec[m * 8..(m + words) * 8], keys, &Block { start: 0, ..blk });
        self.w.write_all(&rec[..(m + words) * 8 + SPARE])?;
        self.blocks.push(blk);
        self.words += (m + words + SPARE / 8) as u64;
        self.rows += m;
        Ok(())
    }

    /// Writes the footer and the header. The segment must not be empty.
    pub(crate) fn finish(mut self) -> io::Result<(Fences, SegFile)> {
        let end = self.words * 8;
        let mut footer = Vec::with_capacity(self.blocks.len() * 16);
        for b in &self.blocks {
            footer.extend_from_slice(&b.base.to_le_bytes());
            footer.extend_from_slice(&b.start.to_le_bytes());
            footer.extend_from_slice(&[b.width, 0, 0, 0]);
        }
        self.w.write_all(&footer)?;
        self.w.flush()?;
        let file = self.w.get_ref();
        let mut header = [0u8; HEADER];
        header[..4].copy_from_slice(b"NSEG");
        header[4..8].copy_from_slice(&1u32.to_le_bytes());
        header[8..16].copy_from_slice(&(self.rows as u64).to_le_bytes());
        header[16..20].copy_from_slice(&(self.blocks.len() as u32).to_le_bytes());
        header[20..28].copy_from_slice(&end.to_le_bytes());
        file.write_all_at(&header, 0)?;
        let file = file.try_clone()?;
        self.finished = true;
        let fences = Fences::new(self.rows, std::mem::take(&mut self.blocks).into_boxed_slice());
        Ok((fences, SegFile { tier: self.tier, file, path: std::mem::take(&mut self.path), id: self.id, end }))
    }
}

impl Drop for SegWriter {
    fn drop(&mut self) {
        if !self.finished {
            let _ = std::fs::remove_file(&self.path);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_cache_keeps_its_bound_and_the_newest_record() {
        let c = Cache::new(SHARDS * 25);
        let reads = std::cell::Cell::new(0);
        let get = |b: u32| c.get((0, b), || { reads.set(reads.get() + 1); vec![b as u8; 10] });
        for b in 0..6 {
            assert_eq!(get(b)[0], b as u8);
        }
        assert_eq!(reads.get(), 6);
        get(5);
        assert_eq!(reads.get(), 6, "a hit");
        let total: usize = c.shards.iter().map(|s| s.lock().unwrap().bytes).sum();
        assert!(total <= 6 * 10);
        for s in &c.shards {
            let s = s.lock().unwrap();
            assert!(s.bytes <= c.bound, "{} bytes in a shard of bound {}", s.bytes, c.bound);
            assert_eq!(s.map.len(), s.lru.len());
        }
        let c = Cache::new(SHARDS * 5);
        let get = |b: u32| c.get((0, b), || { reads.set(reads.get() + 1); vec![b as u8; 10] });
        assert_eq!(get(1)[0], 1);
        assert_eq!(get(1)[0], 1);
        assert_eq!(reads.get(), 8, "a record larger than the bound of a shard is read every time");
        assert!(c.shards.iter().all(|s| s.lock().unwrap().bytes == 0));
    }

    #[test]
    fn a_second_tier_with_other_values_is_an_error() {
        let t = Tier::for_tests();
        assert!(Tier::install(&t.dir, 1 << 16, 2).is_ok());
        assert!(Tier::install(&t.dir.join("other"), 1 << 16, 2).is_err());
        assert!(Tier::install(&t.dir, 1 << 17, 2).is_err());
        assert!(Tier::install(&t.dir, 1 << 16, 3).is_err());
        assert!(!t.dir.join("other").exists(), "no directory is made for a refused tier");
        let path = t.dir.join("seg-999999.bin");
        std::fs::write(&path, b"x").unwrap();
        assert!(t.writer_of(999_999).is_err(), "a file of another process is not truncated");
        assert_eq!(std::fs::read(&path).unwrap(), b"x");
        std::fs::remove_file(&path).unwrap();
    }

    #[test]
    fn a_segment_file_round_trips_and_goes_with_its_drop() {
        let tier = Tier::for_tests();
        let keys: Vec<u64> = (0..2500u64).map(|i| i * 3).collect();
        let vals: Vec<f64> = keys.iter().map(|&k| k as f64 * 0.5).collect();
        let mut w = tier.writer().unwrap();
        for (k, v) in keys.chunks(BLOCK).zip(vals.chunks(BLOCK)) {
            w.block(k, v).unwrap();
        }
        let (fences, file) = w.finish().unwrap();
        let path = file.path().to_path_buf();
        assert_eq!(fences.blocks(), 3);
        let mut head = vec![0u8; HEADER];
        file.file.read_exact_at(&mut head, 0).unwrap();
        assert_eq!(&head[..4], b"NSEG");
        assert_eq!(u64::from_le_bytes(head[8..16].try_into().unwrap()), 2500);
        assert_eq!(u64::from_le_bytes(head[20..28].try_into().unwrap()), file.end());
        assert_eq!(std::fs::metadata(&path).unwrap().len(), file.end() + 3 * 16);
        // The last record: 452 values, their keys (width 11 -> 452 * 11 / 64 = 78 words) and the spare bytes
        let start = fences.block(2).start as u64 * 8;
        let buf = file.block(2, start..file.end());
        assert_eq!(buf.len(), 452 * 8 + 78 * 8 + SPARE);
        let mut out = [0u64; 452];
        crate::packed::decode_block(&Block { start: 0, ..*fences.block(2) }, &buf[452 * 8..], 0..452, &mut out);
        assert_eq!(&out[..], &keys[2048..]);
        assert_eq!(f64::from_le_bytes(buf[8..16].try_into().unwrap()), vals[2049]);
        drop(file);
        assert!(!path.exists(), "the file goes with the last reference");
    }
}
