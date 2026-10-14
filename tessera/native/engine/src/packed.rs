//! Sorted u64 keys in FastLanes-style frame-of-reference blocks.
//!
//! The keys are split into blocks of `BLOCK` keys. A block keeps its first key as the base and the
//! offsets `key - base` bit-packed with the width of the largest offset. On the benchmark data the
//! width is 14 to 19 bits, so the keys use about a quarter of the memory of `Box<[u64]>`.

#[allow(unused_imports)]
use crate::*;
use std::ops::Range;

pub(crate) const BLOCK: usize = 1024;

/// The metadata of one block. It is 16 bytes, so a random read touches one cache line of metadata.
#[derive(Debug, Clone, Copy)]
struct Block {
    base: u64,  // the first key of the block
    start: u32, // the first word of the block in `bytes` (a word is 8 bytes)
    width: u8,  // bits per offset (0 to 64)
}

impl Block {
    /// The key at position `j` in the block. `bytes` must hold 16 bytes from the position's byte.
    #[inline(always)]
    fn key(&self, bytes: &[u8], j: usize) -> u64 {
        let width = self.width as usize;
        let bit = j * width;
        let o = self.start as usize * 8 + bit / 8;
        let sh = (bit % 8) as u32;
        // One unaligned 8-byte load holds the offset when the width is at most 56 bits. The branch
        // is on the block's width, so the predictor learns it.
        let off = if width <= 56 {
            u64::from_le_bytes(bytes[o..o + 8].try_into().unwrap()) >> sh
        } else {
            (u128::from_le_bytes(bytes[o..o + 16].try_into().unwrap()) >> sh) as u64
        };
        self.base + (off & MASKS[width])
    }
}

/// `MASKS[w]` keeps the low `w` bits. A table load is cheaper than a shift that must handle w = 0 and 64.
static MASKS: [u64; 65] = {
    let mut m = [0u64; 65];
    let mut w = 1;
    while w <= 64 {
        m[w] = u64::MAX >> (64 - w);
        w += 1;
    }
    m
};

/// Immutable sorted keys with no duplicates, packed in blocks (see the module doc).
#[derive(Debug)]
pub(crate) struct PackedKeys {
    len: usize,
    blocks: Box<[Block]>,
    bytes: Box<[u8]>,  // the packed offsets in little-endian words, plus 16 spare bytes for the last read window
}

impl PackedKeys {
    /// Packs `keys`, which must be sorted and have no duplicates. `par` packs the blocks in parallel.
    pub(crate) fn new(keys: &[u64], par: bool) -> PackedKeys {
        let width_of = |chunk: &[u64]| 64 - (chunk[chunk.len() - 1] - chunk[0]).leading_zeros() as usize;
        let mut blocks = Vec::with_capacity(keys.len().div_ceil(BLOCK));
        let mut start = 0usize;
        for chunk in keys.chunks(BLOCK) {
            let width = width_of(chunk);
            blocks.push(Block { base: chunk[0], start: u32::try_from(start).expect("too many words"), width: width as u8 });
            start += (chunk.len() * width).div_ceil(64);
        }
        // A block of width 0 has no words and starts at the spare bytes, so a read window fits there too.
        let mut bytes = vec![0u8; start * 8 + 16];
        // The byte ranges of the blocks do not overlap, so each block fills its own slice.
        let mut rest: &mut [u8] = &mut bytes;
        let mut parts = Vec::with_capacity(blocks.len());
        for w in blocks.windows(2) {
            let (head, tail) = rest.split_at_mut((w[1].start - w[0].start) as usize * 8);
            parts.push(head);
            rest = tail;
        }
        parts.push(rest);
        let slices: Vec<(&[u64], &Block)> = keys.chunks(BLOCK).zip(&blocks).collect();
        if par {
            parts.into_par_iter().zip(slices).for_each(|(out, (chunk, blk))| pack_into(out, chunk, blk));
        } else {
            parts.into_iter().zip(slices).for_each(|(out, (chunk, blk))| pack_into(out, chunk, blk));
        }
        PackedKeys { len: keys.len(), blocks: blocks.into_boxed_slice(), bytes: bytes.into_boxed_slice() }
    }

    pub(crate) fn len(&self) -> usize {
        self.len
    }

    /// The key at `i`. `i` must be less than `len()`. The slice bounds keep an index past the end safe.
    #[inline]
    pub(crate) fn get(&self, i: usize) -> u64 {
        debug_assert!(i < self.len, "key index past the number of rows");
        self.blocks[i / BLOCK].key(&self.bytes, i % BLOCK)
    }

    /// The keys at the indexes in `r`, in order. It decodes block by block, so it is faster than `get`.
    pub(crate) fn range(&self, r: Range<usize>) -> impl Iterator<Item = u64> + '_ {
        debug_assert!(r.end <= self.len);
        let (lo, hi) = (r.start, r.end);
        (lo / BLOCK..hi.div_ceil(BLOCK)).flat_map(move |b| {
            let blk = self.blocks[b];
            (lo.max(b * BLOCK)..hi.min((b + 1) * BLOCK)).map(move |i| blk.key(&self.bytes, i % BLOCK))
        })
    }

    /// The keys in order.
    pub(crate) fn iter(&self) -> impl Iterator<Item = u64> + '_ {
        self.range(0..self.len)
    }

    pub(crate) fn par_iter(&self) -> impl IndexedParallelIterator<Item = u64> + '_ {
        (0..self.len).into_par_iter().map(move |i| self.get(i))
    }

    /// The first index whose key does not satisfy `pred`. `pred` must be true for a prefix of the keys.
    pub(crate) fn partition_point(&self, mut pred: impl FnMut(u64) -> bool) -> usize {
        let nb = self.blocks.partition_point(|b| pred(b.base));
        if nb == 0 {
            return 0;
        }
        // The base of block nb - 1 satisfies pred and the base of block nb does not, so the answer
        // is in block nb - 1 or at its end.
        let (mut lo, mut hi) = ((nb - 1) * BLOCK + 1, (nb * BLOCK).min(self.len));
        while lo < hi {
            let mid = lo + (hi - lo) / 2;
            if pred(self.get(mid)) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        lo
    }

    /// The index of `key`, or None if it is not present.
    pub(crate) fn find(&self, key: u64) -> Option<usize> {
        let i = self.partition_point(|k| k < key);
        (i < self.len && self.get(i) == key).then_some(i)
    }

    #[cfg(test)]
    fn width(&self, block: usize) -> u8 {
        self.blocks[block].width
    }

    /// The memory that the packed keys use (bytes).
    pub(crate) fn bytes(&self) -> usize {
        self.bytes.len() + self.blocks.len() * std::mem::size_of::<Block>()
    }
}

/// Packs `chunk` into `out`, the bytes of the block `blk`.
fn pack_into(out: &mut [u8], chunk: &[u64], blk: &Block) {
    let width = blk.width as usize;
    let mut words = vec![0u64; out.len() / 8];
    // Width 0 has no words: every key equals the base.
    for (j, &k) in chunk.iter().enumerate().filter(|_| width > 0) {
        let off = k - blk.base;
        let bit = j * width;
        let (w, sh) = (bit / 64, bit % 64);
        words[w] |= off << sh;
        if sh + width > 64 {
            words[w + 1] |= off >> (64 - sh);
        }
    }
    for (dst, w) in out.as_chunks_mut::<8>().0.iter_mut().zip(&words) {
        *dst = w.to_le_bytes();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn check(keys: &[u64]) {
        for par in [false, true] {
            let p = PackedKeys::new(keys, par);
            assert_eq!(p.len(), keys.len());
            assert_eq!(p.iter().collect::<Vec<_>>(), keys);
            assert_eq!(p.par_iter().collect::<Vec<_>>(), keys);
            for (i, &k) in keys.iter().enumerate() {
                assert_eq!(p.get(i), k);
            }
            for lo in [0, 1, BLOCK - 1, BLOCK, BLOCK + 5, keys.len() / 2, keys.len()] {
                for hi in [lo, lo + 1, lo + BLOCK + 3, keys.len()] {
                    let (lo, hi) = (lo.min(keys.len()), hi.min(keys.len()));
                    if lo <= hi {
                        assert_eq!(p.range(lo..hi).collect::<Vec<_>>(), &keys[lo..hi], "range {lo}..{hi}");
                    }
                }
            }
            let probes = keys.iter().flat_map(|&k| [k.saturating_sub(1), k, k.saturating_add(1)]).chain([0, u64::MAX]);
            for q in probes {
                assert_eq!(p.partition_point(|k| k < q), keys.partition_point(|&k| k < q), "q={q}");
                assert_eq!(p.find(q), keys.binary_search(&q).ok(), "q={q}");
            }
        }
    }

    /// `n` sorted distinct values in `[lo, lo + 2^bits)` from a xorshift generator.
    fn sorted_random(seed: &mut u64, n: usize, lo: u64, bits: u32) -> Vec<u64> {
        let mut out: Vec<u64> = Vec::with_capacity(n);
        while out.len() < n {
            *seed ^= *seed << 13;
            *seed ^= *seed >> 7;
            *seed ^= *seed << 17;
            out.push(lo + (*seed >> (64 - bits)));
            out.sort_unstable();
            out.dedup();
        }
        out
    }

    #[test]
    fn packed_keys_match_a_plain_vec() {
        let mut seed = 0x9E37_79B9_7F4A_7C15;
        check(&[]);
        check(&[7]);
        check(&[u64::MAX]);
        check(&(0..BLOCK as u64 * 2 + 1).collect::<Vec<_>>());
        check(&sorted_random(&mut seed, 3001, 0, 20));
        check(&sorted_random(&mut seed, 5000, 1 << 40, 45));
        // Block 1 spans 64 bits: it starts near 0 and ends at u64::MAX.
        let mut wide = sorted_random(&mut seed, 2047, 0, 30);
        wide.push(u64::MAX);
        check(&wide);
        // Mixed widths across blocks, and a last block of one key (width 0).
        let mut mixed = sorted_random(&mut seed, 1024, 0, 14);
        mixed.extend(sorted_random(&mut seed, 1024, 1 << 20, 50));
        mixed.push(1 << 62);
        check(&mixed);
    }

    /// Micro-benchmark of the reads against a plain `Vec<u64>` on about 18M keys with 15 to 18
    /// bit offsets. Run: `cargo test --release -p nanashi-engine bench_reads -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn bench_reads() {
        use std::hint::black_box;
        use std::time::Instant;
        let n = 18 << 20;
        let mut seed = 0x9E37_79B9_7F4A_7C15u64;
        let mut next = move || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        let mut keys = Vec::with_capacity(n);
        let mut k = 1u64 << 40;
        for b in 0..n.div_ceil(BLOCK) {
            // The steps are up to 2^(w-10), so a block of 1024 keys spans about 2^(w-1) to 2^w.
            let w = 15 + (b % 4) as u32;
            for _ in 0..BLOCK.min(n - b * BLOCK) {
                k += 1 + (next() >> (74 - w));
                keys.push(k);
            }
        }
        let p = PackedKeys::new(&keys, true);
        let mut hist = [0usize; 65];
        for b in 0..n.div_ceil(BLOCK) {
            hist[p.width(b) as usize] += 1;
        }
        let widths: Vec<String> = hist.iter().enumerate().filter(|(_, &c)| c > 0).map(|(w, c)| format!("{w}:{c}")).collect();
        println!("keys {n}, bytes packed {} plain {}, widths {}", p.bytes(), keys.len() * 8, widths.join(" "));
        let idx: Vec<usize> = (0..10_000_000).map(|_| (next() % n as u64) as usize).collect();
        let time = |name: &str, f: &dyn Fn() -> u64| {
            let mut best = f64::MAX;
            for _ in 0..5 {
                let t = Instant::now();
                black_box(f());
                best = best.min(t.elapsed().as_secs_f64());
            }
            println!("{name:<28} {best:.4} s");
        };
        time("vec random 10M", &|| idx.iter().map(|&i| keys[i]).fold(0, u64::wrapping_add));
        time("packed random 10M", &|| idx.iter().map(|&i| p.get(i)).fold(0, u64::wrapping_add));
        time("vec sequential get", &|| (0..n).map(|i| keys[i]).fold(0, u64::wrapping_add));
        time("packed sequential get", &|| (0..n).map(|i| p.get(i)).fold(0, u64::wrapping_add));
        time("vec iter", &|| keys.iter().fold(0, |a, &k| a.wrapping_add(k)));
        time("packed iter", &|| p.iter().fold(0, u64::wrapping_add));
        time("vec par_iter", &|| keys.par_iter().map(|&k| k).reduce(|| 0, u64::wrapping_add));
        time("packed par_iter", &|| p.par_iter().reduce(|| 0, u64::wrapping_add));
        time("packed new (par)", &|| PackedKeys::new(&keys, true).len() as u64);
    }
}
