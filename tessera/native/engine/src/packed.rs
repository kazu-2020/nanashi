//! Sorted u64 keys in FastLanes-style frame-of-reference blocks.
//!
//! The keys are split into blocks of `BLOCK` keys. A block keeps its first key as the base and the
//! offsets `key - base` bit-packed with the width of the largest offset. On the benchmark data the
//! width is 14 to 19 bits, so the keys use about a quarter of the memory of `Box<[u64]>`.

#[allow(unused_imports)]
use crate::*;

pub(crate) const BLOCK: usize = 1024;

/// Immutable sorted keys with no duplicates, packed in blocks (see the module doc).
#[derive(Debug)]
pub(crate) struct PackedKeys {
    len: usize,
    bases: Box<[u64]>,    // the first key of each block
    widths: Box<[u8]>,    // bits per offset in each block (0 to 64)
    starts: Box<[usize]>, // the first word of each block in `words`
    words: Box<[u64]>,    // the packed offsets, plus one spare word so a read can always take 2 words
}

impl PackedKeys {
    /// Packs `keys`, which must be sorted and have no duplicates. `par` packs the blocks in parallel.
    pub(crate) fn new(keys: &[u64], par: bool) -> PackedKeys {
        let pack = |chunk: &[u64]| {
            let base = chunk[0];
            let span = chunk[chunk.len() - 1] - base;
            let width = 64 - span.leading_zeros() as u8;
            let mut words = vec![0u64; (chunk.len() * width as usize).div_ceil(64)];
            // Width 0 has no words: every key equals the base.
            for (j, &k) in chunk.iter().enumerate().filter(|_| width > 0) {
                let off = k - base;
                let bit = j * width as usize;
                let (w, sh) = (bit / 64, bit % 64);
                words[w] |= off << sh;
                if sh + width as usize > 64 {
                    words[w + 1] |= off >> (64 - sh);
                }
            }
            (base, width, words)
        };
        let blocks: Vec<(u64, u8, Vec<u64>)> =
            if par { keys.par_chunks(BLOCK).map(pack).collect() } else { keys.chunks(BLOCK).map(pack).collect() };
        let mut starts = Vec::with_capacity(blocks.len());
        let mut words = Vec::with_capacity(blocks.iter().map(|b| b.2.len()).sum::<usize>() + 1);
        for b in &blocks {
            starts.push(words.len());
            words.extend_from_slice(&b.2);
        }
        words.push(0);
        PackedKeys {
            len: keys.len(),
            bases: blocks.iter().map(|b| b.0).collect(),
            widths: blocks.iter().map(|b| b.1).collect(),
            starts: starts.into_boxed_slice(),
            words: words.into_boxed_slice(),
        }
    }

    pub(crate) fn len(&self) -> usize {
        self.len
    }

    /// The key at `i`. Panics if `i >= len()`.
    #[inline]
    pub(crate) fn get(&self, i: usize) -> u64 {
        assert!(i < self.len, "キーの番号が行数を超えている");
        let b = i / BLOCK;
        let width = self.widths[b] as usize;
        if width == 0 {
            return self.bases[b];
        }
        let bit = (i % BLOCK) * width;
        let w = self.starts[b] + bit / 64;
        let pair = self.words[w] as u128 | (self.words[w + 1] as u128) << 64;
        let off = (pair >> (bit % 64)) as u64 & (u64::MAX >> (64 - width));
        self.bases[b] + off
    }

    /// The keys in order.
    pub(crate) fn iter(&self) -> impl Iterator<Item = u64> + '_ {
        (0..self.len).map(move |i| self.get(i))
    }

    pub(crate) fn par_iter(&self) -> impl IndexedParallelIterator<Item = u64> + '_ {
        (0..self.len).into_par_iter().map(move |i| self.get(i))
    }

    /// The first index whose key does not satisfy `pred`. `pred` must be true for a prefix of the keys.
    pub(crate) fn partition_point(&self, mut pred: impl FnMut(u64) -> bool) -> usize {
        let nb = self.bases.partition_point(|&b| pred(b));
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
        self.widths[block]
    }

    /// The memory that the packed keys use (bytes).
    pub(crate) fn bytes(&self) -> usize {
        (self.words.len() + self.bases.len() + self.starts.len()) * 8 + self.widths.len()
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
