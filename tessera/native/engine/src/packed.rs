//! Sorted u64 keys in frame-of-reference blocks.
//!
//! The keys are split into blocks of `BLOCK` keys. A block keeps its first key as the base and the
//! offsets `key - base` bit-packed one after the other with the width of the largest offset. In the
//! retail model the width is 14 to 19 bits, so the keys use about a quarter of the memory of `Box<[u64]>`.

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
    /// `bytes` must hold 16 bytes from the byte of position `j`.
    #[inline(always)]
    fn key(&self, bytes: &[u8], j: usize) -> u64 {
        let width = self.width as usize;
        let bit = j * width;
        let o = self.start as usize * 8 + bit / 8;
        let sh = (bit % 8) as u32;
        // One unaligned 8-byte load holds the offset when the width is at most 56 bits.
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
        let mut blocks = Vec::with_capacity(keys.len().div_ceil(BLOCK));
        let mut start = 0usize;
        for chunk in keys.chunks(BLOCK) {
            let width = 64 - (chunk[chunk.len() - 1] - chunk[0]).leading_zeros() as usize;
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

    pub(crate) fn builder(capacity: usize) -> Builder {
        Builder { len: 0, blocks: Vec::with_capacity(capacity.div_ceil(BLOCK)), bytes: Vec::new(), buf: Vec::with_capacity(BLOCK) }
    }

    /// The key at `i`. `i` must be less than `len()`.
    #[inline]
    pub(crate) fn get(&self, i: usize) -> u64 {
        debug_assert!(i < self.len, "key index past the number of rows");
        self.blocks[i / BLOCK].key(&self.bytes, i % BLOCK)
    }

    /// Decodes the keys at positions `js` of block `b` into `out`. `out.len()` must equal `js.len()`.
    /// The loop has no per-key branch or table load, so it is much faster than `get` per key.
    pub(crate) fn decode(&self, b: usize, js: Range<usize>, out: &mut [u64]) {
        let blk = self.blocks[b];
        let width = blk.width as usize;
        debug_assert!(js.end <= BLOCK && js.len() == out.len());
        if width == 0 {
            out.fill(blk.base);
            return;
        }
        let bytes = &self.bytes[blk.start as usize * 8..];
        // One `unpack` for each width decodes 2 times faster than one loop with a variable width
        // (`bench_reads`: 0.010 s against 0.021 s for 18.9M keys).
        macro_rules! dispatch {
            ($($w:literal)*) => {
                match width {
                    $($w => unpack::<$w>(blk.base, bytes, js, out),)*
                    _ => {
                        for (o, j) in out.iter_mut().zip(js) {
                            *o = blk.key(&self.bytes, j);
                        }
                    }
                }
            };
        }
        dispatch!(1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32
            33 34 35 36 37 38 39 40 41 42 43 44 45 46 47 48 49 50 51 52 53 54 55 56);
    }

    /// The keys at the indexes in `r`, in order. It decodes block by block, so it is faster than `get`.
    pub(crate) fn range(&self, r: Range<usize>) -> Keys<'_> {
        debug_assert!(r.end <= self.len);
        Keys { keys: self, i: r.start, end: r.end, decode_at: r.start, buf: [0; BLOCK] }
    }

    pub(crate) fn iter(&self) -> impl Iterator<Item = u64> + '_ {
        self.range(0..self.len)
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

    pub(crate) fn find(&self, key: u64) -> Option<usize> {
        let i = self.partition_point(|k| k < key);
        (i < self.len && self.get(i) == key).then_some(i)
    }

    #[cfg(test)]
    fn width(&self, block: usize) -> u8 {
        self.blocks[block].width
    }

    /// Includes the block headers and the 16 spare bytes.
    pub(crate) fn bytes(&self) -> usize {
        self.bytes.len() + self.blocks.len() * std::mem::size_of::<Block>()
    }
}

/// Packs keys pushed one at a time, in sorted order and with no duplicates. It keeps only one block of
/// keys unpacked, so a merge into a new base does not hold a `Vec<u64>` of all keys. `finish` gives
/// the same bytes as `PackedKeys::new`.
pub(crate) struct Builder {
    len: usize,
    blocks: Vec<Block>,
    bytes: Vec<u8>,
    buf: Vec<u64>, // the keys of the block that is not yet packed
}

impl Builder {
    #[inline]
    pub(crate) fn push(&mut self, key: u64) {
        self.buf.push(key);
        if self.buf.len() == BLOCK {
            self.flush();
        }
    }

    fn flush(&mut self) {
        let chunk = &self.buf;
        let start = self.bytes.len() / 8;
        let width = 64 - (chunk[chunk.len() - 1] - chunk[0]).leading_zeros() as usize;
        let blk = Block { base: chunk[0], start: u32::try_from(start).expect("too many words"), width: width as u8 };
        self.bytes.resize((start + (chunk.len() * width).div_ceil(64)) * 8, 0);
        pack_into(&mut self.bytes[start * 8..], chunk, &blk);
        self.blocks.push(blk);
        self.len += chunk.len();
        self.buf.clear();
    }

    pub(crate) fn finish(mut self) -> PackedKeys {
        if !self.buf.is_empty() {
            self.flush();
        }
        self.bytes.resize(self.bytes.len() + 16, 0);
        PackedKeys { len: self.len, blocks: self.blocks.into_boxed_slice(), bytes: self.bytes.into_boxed_slice() }
    }
}

/// The iterator of `range`. It decodes one block at a time into `buf`.
pub(crate) struct Keys<'a> {
    keys: &'a PackedKeys,
    i: usize,
    end: usize,
    decode_at: usize, // the first index that is not yet in `buf`
    buf: [u64; BLOCK],
}

impl Iterator for Keys<'_> {
    type Item = u64;

    #[inline]
    fn next(&mut self) -> Option<u64> {
        if self.i >= self.end {
            return None;
        }
        let j = self.i % BLOCK;
        if self.i == self.decode_at {
            // Decode the rest of the block, but not past `end`.
            let hi = (self.end - (self.i - j)).min(BLOCK);
            self.keys.decode(self.i / BLOCK, j..hi, &mut self.buf[j..hi]);
            self.decode_at = self.i - j + hi;
        }
        self.i += 1;
        Some(self.buf[j])
    }

    fn size_hint(&self) -> (usize, Option<usize>) {
        let n = self.end - self.i;
        (n, Some(n))
    }
}

impl ExactSizeIterator for Keys<'_> {}

#[derive(Clone, Copy)]
pub(crate) enum Sorted<'a> {
    Packed(&'a PackedKeys),
    Cells(&'a [(u64, f64)]),
}

/// A cursor over sorted keys. `pos` is the row of the last read. Over packed keys it caches one decoded
/// block: a read within `BLOCK` rows of `pos` decodes the block of the row, a read farther away (a probe of a
/// search that jumps far) uses `get` for that row only.
pub(crate) struct Cursor<'a> {
    src: Sorted<'a>,
    pub(crate) pos: usize,
    block: usize,  // the decoded block in `buf`, or usize::MAX
    buf: Vec<u64>, // empty until the first decode
}

impl<'a> Cursor<'a> {
    pub(crate) fn new(src: Sorted<'a>) -> Cursor<'a> {
        Cursor { src, pos: 0, block: usize::MAX, buf: Vec::new() }
    }

    #[inline]
    pub(crate) fn len(&self) -> usize {
        match self.src {
            Sorted::Packed(p) => p.len(),
            Sorted::Cells(c) => c.len(),
        }
    }

    /// The key at row `i`, and `pos` becomes `i`.
    #[inline]
    pub(crate) fn key(&mut self, i: usize) -> u64 {
        let k = match self.src {
            Sorted::Cells(c) => c[i].0,
            Sorted::Packed(_) if i / BLOCK == self.block => self.buf[i % BLOCK],
            Sorted::Packed(p) if i.abs_diff(self.pos) > BLOCK => p.get(i),
            Sorted::Packed(p) => {
                let b = i / BLOCK;
                if self.buf.is_empty() {
                    self.buf = vec![0; BLOCK];
                }
                let m = (p.len() - b * BLOCK).min(BLOCK);
                p.decode(b, 0..m, &mut self.buf[..m]);
                self.block = b;
                self.buf[i % BLOCK]
            }
        };
        self.pos = i;
        k
    }

    /// The first row at or after `key`, searched near `pos` (an exponential search in both directions).
    /// If the next keys are close, this is faster than a binary search. `pos` becomes the row.
    #[inline]
    pub(crate) fn seek(&mut self, key: u64) -> usize {
        let from = self.pos;
        let i = if from == 0 || from > self.len() || self.key(from - 1) < key {
            self.gallop_from(from.min(self.len()), key)
        } else {
            // key(from - 1) >= key: go backwards
            let mut step = 1;
            let mut hi = from - 1; // key(hi) >= key
            let mut lo = hi.saturating_sub(1);
            while lo > 0 && self.key(lo) >= key {
                hi = lo;
                step *= 2;
                lo = lo.saturating_sub(step);
            }
            if self.key(lo) >= key {
                lo // lo == 0
            } else {
                self.bisect(lo + 1, hi, key) // key(lo) < key <= key(hi)
            }
        };
        self.pos = i;
        i
    }

    /// The first row at or after `pos` whose key is at least `key` (an exponential search, for a
    /// cursor that moves in key order). `pos` becomes the row.
    #[inline]
    pub(crate) fn gallop(&mut self, key: u64) -> usize {
        let i = self.gallop_from(self.pos, key);
        self.pos = i;
        i
    }

    fn gallop_from(&mut self, from: usize, key: u64) -> usize {
        let n = self.len();
        if from >= n || self.key(from) >= key {
            return from;
        }
        let mut step = 1;
        let mut lo = from; // key(lo) < key
        let mut hi = from + 1;
        while hi < n && self.key(hi) < key {
            lo = hi;
            step *= 2;
            hi = (hi + step).min(n);
        }
        self.bisect(lo + 1, hi, key)
    }

    /// The first row in `l..=h` whose key is at least `key`. `key(l - 1) < key`, and `h` is the end or `key(h) >= key`.
    fn bisect(&mut self, mut l: usize, mut h: usize, key: u64) -> usize {
        while l < h {
            let mid = (l + h) / 2;
            if self.key(mid) < key {
                l = mid + 1;
            } else {
                h = mid;
            }
        }
        l
    }
}

/// Decodes `out[i] = base + offset(js.start + i)` for a width `W` of 1 to 56 bits. The width is a
/// constant, so the compiler unrolls each group of 8 keys (`W` bytes) with fixed shifts.
fn unpack<const W: usize>(base: u64, bytes: &[u8], js: Range<usize>, mut out: &mut [u64]) {
    let mask = MASKS[W];
    let one = |j: usize| {
        let bit = j * W;
        base + ((u64::from_le_bytes(bytes[bit / 8..bit / 8 + 8].try_into().unwrap()) >> (bit % 8)) & mask)
    };
    let (mut j, end) = (js.start, js.end);
    while j % 8 != 0 && j < end {
        out[0] = one(j);
        out = &mut out[1..];
        j += 1;
    }
    while j + 8 <= end {
        let g = &bytes[j / 8 * W..j / 8 * W + W + 8];
        for (l, o) in out[..8].iter_mut().enumerate() {
            let bit = l * W;
            *o = base + ((u64::from_le_bytes(g[bit / 8..bit / 8 + 8].try_into().unwrap()) >> (bit % 8)) & mask);
        }
        out = &mut out[8..];
        j += 8;
    }
    for (o, j) in out.iter_mut().zip(j..end) {
        *o = one(j);
    }
}

fn pack_into(out: &mut [u8], chunk: &[u64], blk: &Block) {
    let width = blk.width as usize;
    if width == 0 {
        return; // every key equals the base
    }
    let mut words = vec![0u64; out.len() / 8];
    for (j, &k) in chunk.iter().enumerate() {
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

    #[test]
    fn builder_gives_the_same_bytes_as_new() {
        let mut seed = 0x9E37_79B9_7F4A_7C15;
        let random = sorted_random(&mut seed, 5000, 1 << 40, 45);
        let mut cases: Vec<Vec<u64>> = [0, 1, BLOCK - 1, BLOCK, BLOCK + 1].iter().map(|&n| (0..n as u64 * 3).step_by(3).collect()).collect();
        cases.push(random);
        cases.push(vec![5; 1]);
        for keys in cases {
            let want = PackedKeys::new(&keys, false);
            let mut b = PackedKeys::builder(keys.len());
            keys.iter().for_each(|&k| b.push(k));
            let got = b.finish();
            assert_eq!(got.len, want.len, "n={}", keys.len());
            assert_eq!(got.bytes, want.bytes, "n={}", keys.len());
            assert_eq!(got.blocks.len(), want.blocks.len());
            for (g, w) in got.blocks.iter().zip(&want.blocks) {
                assert_eq!((g.base, g.start, g.width), (w.base, w.start, w.width));
            }
            assert_eq!(got.iter().collect::<Vec<_>>(), keys);
        }
    }

    #[test]
    fn cursor_finds_the_same_rows_as_a_binary_search() {
        let mut seed = 0x9E37_79B9_7F4A_7C15;
        let keys = sorted_random(&mut seed, 20_000, 1 << 30, 24);
        let p = PackedKeys::new(&keys, false);
        let cells: Vec<(u64, f64)> = keys.iter().map(|&k| (k, 0.0)).collect();
        for src in [Sorted::Packed(&p), Sorted::Cells(&cells)] {
            let mut rd = Cursor::new(src);
            assert_eq!((0..keys.len()).map(|i| rd.key(i)).collect::<Vec<_>>(), keys, "a sequential read");
            // Near and far probes: steps of 1, a jump of 3 blocks, a jump to the other end, a step back.
            let mut q = keys[0];
            for step in [1u64, 1, 1 << 12, 1 << 16, 1 << 23, 1, 1 << 23, 3, 7] {
                q = q.wrapping_add(step << 6);
                let want = keys.partition_point(|&k| k < q);
                assert_eq!(rd.seek(q), want, "seek {q}");
                assert_eq!(rd.pos, want);
            }
            // Far seeks to exact keys: the probes of the search use `get`.
            for i in [0, 7000, 13, 19_999, 5000, 5001, 12_345] {
                assert_eq!(rd.seek(keys[i]), i, "seek to key {i}");
                assert_eq!(rd.seek(keys[i] + 1), i + 1, "seek past key {i}");
            }
            let mut back = Cursor::new(src);
            back.pos = keys.len();
            assert_eq!(back.seek(keys[5]), 5, "a seek backwards from the end");
            assert_eq!(back.seek(0), 0);
            assert_eq!(back.seek(u64::MAX), keys.len());
            let mut fwd = Cursor::new(src);
            for (i, &k) in keys.iter().enumerate().step_by(997) {
                assert_eq!(fwd.gallop(k), i);
                assert_eq!(fwd.gallop(k + 1), i + 1);
            }
            assert_eq!(fwd.gallop(u64::MAX), keys.len());
        }
        let mut empty = Cursor::new(Sorted::Cells(&[]));
        assert_eq!(empty.seek(5), 0);
        assert_eq!(empty.gallop(5), 0);
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
        time("packed iter zip vals", &|| p.iter().zip(&keys).fold(0, |a, (k, &v)| a.wrapping_add(k ^ v)));
        time("packed block decode zip vals", &|| {
            let mut buf = [0u64; BLOCK];
            let mut a = 0u64;
            for b in 0..n.div_ceil(BLOCK) {
                let m = BLOCK.min(n - b * BLOCK);
                p.decode(b, 0..m, &mut buf[..m]);
                for (&k, &v) in buf[..m].iter().zip(&keys[b * BLOCK..]) {
                    a = a.wrapping_add(k ^ v);
                }
            }
            a
        });
        time("packed generic decode", &|| {
            let mut buf = [0u64; BLOCK];
            (0..n.div_ceil(BLOCK)).fold(0, |a, b| {
                let m = BLOCK.min(n - b * BLOCK);
                let (blk, bytes) = (p.blocks[b], &p.bytes[p.blocks[b].start as usize * 8..]);
                let width = blk.width as usize;
                for (o, j) in buf[..m].iter_mut().zip(0..m) {
                    let bit = j * width;
                    *o = blk.base + ((u64::from_le_bytes(bytes[bit / 8..bit / 8 + 8].try_into().unwrap()) >> (bit % 8)) & MASKS[width]);
                }
                a.wrapping_add(buf[m - 1])
            })
        });
        time("packed block decode", &|| {
            let mut buf = [0u64; BLOCK];
            (0..n.div_ceil(BLOCK)).fold(0, |a, b| {
                let m = BLOCK.min(n - b * BLOCK);
                p.decode(b, 0..m, &mut buf[..m]);
                a.wrapping_add(buf[m - 1])
            })
        });
        time("packed new (par)", &|| PackedKeys::new(&keys, true).len() as u64);
    }
}
