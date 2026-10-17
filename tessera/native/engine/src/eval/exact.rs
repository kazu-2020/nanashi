//! The exact sum of f64 values (Shewchuk partials, the same algorithm as Python `math.fsum`).
//! A SUM gives the correctly rounded exact sum. Thus the result does not change with the order of the rows,
//! the aggregation path or the thread count.

use crate::CELL_BYTES;
use std::sync::atomic::{AtomicUsize, Ordering::Relaxed};

/// The exact value of a sum, as non-overlapping partials in increasing magnitude. All partials are not zero.
/// Most sums need 2 partials or less, so they stay inline (24 B). More partials go to `spill`.
#[derive(Clone, Default, Debug)]
pub(crate) struct ExactSum {
    p: [f64; 2], // the partials p[..n], where n is the number of values that are not 0.0
    spill: Option<Box<Spill>>,
}

// A Spill holds at most about 40 partials (non-overlapping partials below HUGE cover 2^-1074 to 2^960, 53 bits
// each). Only a group with more than 2 partials has one. A Meter counts it.
#[derive(Clone, Default, Debug)]
struct Spill {
    parts: Vec<f64>,
    special: f64, // the sum of the non-finite inputs; NaN for an input of HUGE or more
}

/// Counts the bytes of the Spills that are made, also from parallel workers. Over room, no Spill is made or grows:
/// the sums become wrong, and cells() is more than room, so the caller's budget check refuses the result.
pub(crate) struct Meter {
    made: AtomicUsize,
    room: usize,
}

impl Meter {
    pub(crate) fn new(room_cells: usize) -> Meter {
        Meter { made: AtomicUsize::new(0), room: room_cells.saturating_mul(CELL_BYTES) }
    }

    fn take(&self, bytes: usize) -> bool {
        self.made.fetch_add(bytes, Relaxed).saturating_add(bytes) <= self.room
    }

    /// A Meter with no limit, for a Spill that lives only for a short time.
    pub(crate) fn unbounded() -> Meter {
        Meter::new(usize::MAX)
    }

    /// The cells of the Spills made (or refused) until now.
    pub(crate) fn cells(&self) -> usize {
        self.made.load(Relaxed).div_ceil(CELL_BYTES)
    }
}

impl Spill {
    #[cold]
    fn push(&mut self, x: f64, m: &Meter) {
        let n = self.parts.len();
        if n == self.parts.capacity() {
            let more = n.max(4);
            if !m.take(more * std::mem::size_of::<f64>()) {
                return;
            }
            self.parts.reserve_exact(more);
        }
        self.parts.push(0.0);
        let k = grow(&mut self.parts, n, x);
        self.parts.truncate(k);
    }
}

/// The bytes of a new Spill with n partials (the Box and the Vec buffer).
fn spill_bytes(n: usize) -> usize {
    std::mem::size_of::<Spill>() + n * std::mem::size_of::<f64>()
}

/// A finite input with this magnitude or more makes the sum NaN. Below it, the partials cannot overflow
/// (that needs more than 2^64 inputs), so the result does not depend on the order of the inputs.
pub(crate) const HUGE: f64 = f64::from_bits((1023 + 960) << 52); // 2^960

/// One step of msum on buf[..n], in place (buf has room for n + 1 values). Returns the new n.
fn grow(buf: &mut [f64], n: usize, mut x: f64) -> usize {
    let mut i = 0;
    for j in 0..n {
        let y = buf[j];
        let (a, b) = if x.abs() < y.abs() { (y, x) } else { (x, y) };
        let hi = a + b;
        let lo = b - (hi - a);
        if lo != 0.0 {
            buf[i] = lo;
            i += 1;
        }
        x = hi;
    }
    if x != 0.0 {
        buf[i] = x;
        i += 1;
    }
    i
}

impl ExactSum {
    fn parts(&self) -> &[f64] {
        match &self.spill {
            Some(s) => &s.parts,
            None => &self.p[..(self.p[0] != 0.0) as usize + (self.p[1] != 0.0) as usize],
        }
    }

    fn special(&self) -> f64 {
        self.spill.as_ref().map_or(0.0, |s| s.special)
    }

    pub(crate) fn add(&mut self, x: f64, m: &Meter) {
        if x.is_nan() || x.abs() >= HUGE {
            if let Some(s) = self.spill_mut(m) {
                s.special += if x.is_infinite() { x } else { f64::NAN };
            }
            return;
        }
        self.push(x, m);
    }

    /// Add a finite value with no check (a partial or a rounded sum of inputs below HUGE).
    fn push(&mut self, x: f64, m: &Meter) {
        if let Some(s) = &mut self.spill {
            return s.push(x, m);
        }
        let n = self.parts().len();
        let mut buf = [self.p[0], self.p[1], 0.0];
        match grow(&mut buf, n, x) {
            k if k <= 2 => self.p = [if k > 0 { buf[0] } else { 0.0 }, if k > 1 { buf[1] } else { 0.0 }],
            k => self.make_spill(&buf[..k], m),
        }
    }

    #[cold]
    fn make_spill(&mut self, parts: &[f64], m: &Meter) {
        if m.take(spill_bytes(parts.len())) {
            self.spill = Some(Box::new(Spill { parts: parts.to_vec(), special: 0.0 }));
            self.p = [0.0; 2];
        }
    }

    fn spill_mut(&mut self, m: &Meter) -> Option<&mut Spill> {
        if self.spill.is_none() {
            let parts = self.parts().to_vec();
            if !m.take(spill_bytes(parts.len())) {
                return None;
            }
            self.spill = Some(Box::new(Spill { parts, special: 0.0 }));
            self.p = [0.0; 2];
        }
        self.spill.as_deref_mut()
    }

    pub(crate) fn merge(&mut self, o: &ExactSum, m: &Meter) {
        for &x in o.parts() {
            self.push(x, m);
        }
        if o.special() != 0.0 {
            if let Some(s) = self.spill_mut(m) {
                s.special += o.special();
            }
        }
    }

    /// The exact sum, correctly rounded (round half to even). The same as CPython `math_fsum`.
    pub(crate) fn finish(&self) -> f64 {
        let special = self.special();
        if special != 0.0 {
            return special;
        }
        let p = self.parts();
        let mut n = p.len();
        if n == 0 {
            return 0.0;
        }
        n -= 1;
        let mut hi = p[n];
        let mut lo = 0.0;
        while n > 0 {
            let x = hi;
            n -= 1;
            let y = p[n];
            hi = x + y;
            lo = y - (hi - x);
            if lo != 0.0 {
                break;
            }
        }
        // If the rest has the same sign as lo, the true sum is not exactly half way: round in its direction
        if n > 0 && ((lo < 0.0 && p[n - 1] < 0.0) || (lo > 0.0 && p[n - 1] > 0.0)) {
            let y = lo * 2.0;
            let x = hi + y;
            if y == x - hi {
                hi = x;
            }
        }
        hi
    }

    /// exact - finish(), if that is one f64 exactly. Otherwise (or for a non-finite sum) NaN.
    pub(crate) fn residual(&self) -> f64 {
        if self.special() != 0.0 {
            return f64::NAN;
        }
        // rest lives only in this call, so it is not counted
        let (mut rest, m) = (self.clone(), Meter::new(usize::MAX));
        rest.push(-self.finish(), &m);
        let r = rest.finish();
        rest.push(-r, &m);
        if rest.special() == 0.0 && rest.parts().is_empty() { r } else { f64::NAN }
    }
}

/// An exact sum that is fast when most values have near exponents (for example, amounts with 2 decimals).
/// A value whose lowest mantissa bit is 0 to WINDOW bits above `base` goes into a 128-bit integer. Other values
/// go to an ExactSum. `finish` adds the integer to that ExactSum exactly, so it gives the same bits as ExactSum.
#[derive(Clone, Default, Debug)]
pub(crate) struct FixSum {
    acc: i128,
    base: i32, // the exponent of bit 0 of acc
    n: u32,    // the values in acc. The first value sets base
    rest: ExactSum,
}

/// A value in acc is less than 2^(WINDOW + 53) = 2^100. Thus FLUSH values stay less than 2^126.
const WINDOW: i32 = 47;
const FLUSH: u32 = 1 << 26;
/// The first value puts base this number of bits below its lowest mantissa bit.
const BELOW: i32 = 24;
/// The limits of base. Then each 43-bit chunk of acc is an f64 from 2^-1022 to less than HUGE, with no rounding.
/// They also keep subnormal values, infinities, NaN and values of HUGE or more out of acc.
const BASE: std::ops::RangeInclusive<i32> = -1022..=831;
const CHUNK: u32 = 43;

impl FixSum {
    #[inline]
    pub(crate) fn add(&mut self, x: f64, m: &Meter) {
        let bits = x.to_bits();
        let low_exp = ((bits >> 52) & 0x7ff) as i32 - 1075;
        if self.n == 0 {
            self.base = (low_exp - BELOW).clamp(*BASE.start(), *BASE.end());
        }
        let shift = low_exp - self.base;
        if !(0..=WINDOW).contains(&shift) {
            return self.rest.add(x, m);
        }
        let v = (((bits & ((1 << 52) - 1)) | (1 << 52)) as i128) << shift;
        let sign = (bits as i64 >> 63) as i128; // -1 for a negative value
        self.acc += (v ^ sign) - sign;
        self.n += 1;
        if self.n >= FLUSH {
            put(&mut self.rest, self.acc, self.base, m);
            (self.acc, self.n) = (0, 0);
        }
    }

    pub(crate) fn merge(&mut self, o: &FixSum, m: &Meter) {
        if self.n == 0 {
            (self.acc, self.base, self.n) = (o.acc, o.base, o.n);
        } else if o.n > 0 {
            // A value shifted up by d bits counts 2^d in n, so |acc| stays below n * 2^(WINDOW + 53)
            let d = (o.base - self.base).unsigned_abs();
            let (lo, hi) = if o.base >= self.base { (self.n, o.n) } else { (o.n, self.n) };
            let n = (lo as u64).saturating_add((hi as u64) << d.min(32));
            if d <= 32 && n <= FLUSH as u64 {
                if o.base >= self.base {
                    self.acc += o.acc << d;
                } else {
                    (self.acc, self.base) = ((self.acc << d) + o.acc, o.base);
                }
                self.n = n as u32;
                debug_assert!(self.acc.unsigned_abs() < (self.n as u128) << (WINDOW + 53));
            } else {
                put(&mut self.rest, o.acc, o.base, m);
            }
        }
        self.rest.merge(&o.rest, m);
    }

    pub(crate) fn finish(&self) -> f64 {
        let (mut s, m) = (self.rest.clone(), Meter::unbounded());
        put(&mut s, self.acc, self.base, &m);
        s.finish()
    }
}

/// Add acc * 2^base to s exactly, as chunks of CHUNK bits.
fn put(s: &mut ExactSum, acc: i128, base: i32, m: &Meter) {
    let (neg, mut a, mut at) = (acc < 0, acc.unsigned_abs(), base);
    while a != 0 {
        let c = (a & ((1 << CHUNK) - 1)) as f64 * f64::from_bits(((at + 1023) as u64) << 52);
        s.push(if neg { -c } else { c }, m);
        a >>= CHUNK;
        at += CHUNK as i32;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sum(vs: &[f64]) -> ExactSum {
        let (mut s, m) = (ExactSum::default(), Meter::new(usize::MAX));
        vs.iter().for_each(|&v| s.add(v, &m));
        s
    }

    #[test]
    fn known_sums_match_python_fsum() {
        // The expected bits come from Python math.fsum
        let cases: [(&[f64], u64); 7] = [
            (&[0.1; 10], 0x3ff0000000000000),
            (&[1e100, 1.0, -1e100, 1e-100], 0x3ff0000000000000),
            (&[1.0, f64::powi(2.0, -53)], 0x3ff0000000000000), // a tie rounds to even
            (&[1.0, f64::powi(2.0, -53), f64::powi(2.0, -106)], 0x3ff0000000000001),
            (&[1.0, -f64::powi(2.0, -53), -f64::powi(2.0, -106)], 0x3fefffffffffffff),
            (&[0.1, 0.2, 0.3, -0.6], 0x3c80000000000000),
            (&[1e9 + 0.01, 0.001, 123.45, -2.5, 0.7], 0x41cdcd653cd49ba6),
        ];
        for (vs, bits) in cases {
            assert_eq!(sum(vs).finish().to_bits(), bits, "{vs:?}");
        }
    }

    #[test]
    fn order_and_merge_do_not_change_the_bits() {
        let vs = [0.1, 1e9 + 0.01, -2.5, 0.7, 1e-3, 123.45];
        let want = sum(&vs).finish().to_bits();
        let mut idx: Vec<usize> = (0..vs.len()).collect();
        // All permutations (Heap's algorithm)
        let mut c = vec![0; vs.len()];
        let mut i = 0;
        while i < vs.len() {
            if c[i] < i {
                idx.swap(if i % 2 == 0 { 0 } else { c[i] }, i);
                let perm: Vec<f64> = idx.iter().map(|&j| vs[j]).collect();
                assert_eq!(sum(&perm).finish().to_bits(), want, "{perm:?}");
                c[i] += 1;
                i = 0;
            } else {
                c[i] = 0;
                i += 1;
            }
        }
        let (mut a, b) = (sum(&vs[..2]), sum(&vs[2..]));
        a.merge(&b, &Meter::new(usize::MAX));
        assert_eq!(a.finish().to_bits(), want);
    }

    #[test]
    fn residual_round_trip() {
        let vs = [1e9 + 0.01, 0.001, 123.45, -2.5, 0.7, 0.1, 0.1, 0.1];
        let s = sum(&vs);
        let (hi, lo) = (s.finish(), s.residual());
        assert!(lo != 0.0 && lo.is_finite());
        // [hi, lo] holds the same exact value: adding the inputs back with a minus sign gives 0
        let mut back = sum(&[hi, lo]);
        let m = Meter::new(usize::MAX);
        vs.iter().for_each(|&v| back.add(-v, &m));
        assert!(back.parts().is_empty());
        assert_eq!(sum(&[hi, lo]).finish().to_bits(), hi.to_bits());
        assert_eq!(sum(&[1.0, 2.0]).residual(), 0.0);
        // 1 + 2^-60 + 2^-130 needs 3 doubles: the residual is not one f64
        assert!(sum(&[1.0, f64::powi(2.0, -60), f64::powi(2.0, -130)]).residual().is_nan());
    }

    #[test]
    fn spill_and_special_values() {
        let s = sum(&[1.0, f64::powi(2.0, -60), f64::powi(2.0, -130), -1.0]);
        assert_eq!(s.finish(), f64::powi(2.0, -60) + f64::powi(2.0, -130));
        assert_eq!(sum(&[1.0, f64::INFINITY]).finish(), f64::INFINITY);
        assert!(sum(&[f64::INFINITY, f64::NEG_INFINITY]).finish().is_nan());
        assert!(sum(&[f64::NAN, 1.0]).finish().is_nan());
        assert_eq!(HUGE, 2f64.powi(960));
        // A huge input gives NaN in every order (no order-dependent overflow)
        assert!(sum(&[f64::MAX, f64::MAX, -f64::MAX]).finish().is_nan());
        assert!(sum(&[f64::MAX, -f64::MAX, f64::MAX]).finish().is_nan());
        assert_eq!(sum(&[HUGE * 0.75, HUGE * 0.75, -HUGE * 0.75]).finish(), HUGE * 0.75);
        assert!(sum(&[1.0, f64::INFINITY]).residual().is_nan());
        assert_eq!(std::mem::size_of::<ExactSum>(), 24);
    }

    /// FixSum gives the same bits as ExactSum for values in and out of its window, split and merged.
    #[test]
    fn fix_sum_matches_exact_sum() {
        let m = Meter::new(usize::MAX);
        let mut x = 99u64;
        let mut next = || {
            x = x.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            x >> 11
        };
        let specials = [0.0, -0.0, 1e300, -1e-300, 5e-324, f64::MIN_POSITIVE, HUGE, -HUGE * 0.75, 1e16, -1e16, f64::INFINITY, f64::NEG_INFINITY, f64::NAN];
        for round in 0..400 {
            let n = 1 + next() as usize % 300;
            let vs: Vec<f64> = (0..n)
                .map(|_| match next() % 20 {
                    0 => specials[next() as usize % if round % 2 == 0 { 10 } else { specials.len() }],
                    1 => (next() % 1000) as f64 * 1e-7,
                    2 => -((next() % 100_000) as f64) * 1e5,
                    _ => (next() % 100_000) as f64 * 0.01 * if next() % 3 == 0 { -1.0 } else { 1.0 },
                })
                .collect();
            let want = sum(&vs).finish();
            let cut = next() as usize % (n + 1);
            let (mut a, mut b) = (FixSum::default(), FixSum::default());
            vs[..cut].iter().for_each(|&v| a.add(v, &m));
            vs[cut..].iter().for_each(|&v| b.add(v, &m));
            a.merge(&b, &m);
            let got = a.finish();
            assert!(got.to_bits() == want.to_bits() || (got.is_nan() && want.is_nan()), "{got} {want} {vs:?}");
        }
    }

    /// acc goes into rest before it can overflow: 2^27 + 3 values at the top of the window overflow 2^127.
    #[test]
    fn fix_sum_flushes_before_overflow() {
        let m = Meter::new(usize::MAX);
        let top = ((1u64 << 53) - 1) as f64 * 2f64.powi(-52 - BELOW + WINDOW);
        let (mut f, mut e) = (FixSum::default(), sum(&[1.0]));
        f.add(1.0, &m);
        for _ in 0..2 * FLUSH + 3 {
            f.add(top, &m);
            e.add(top, &m);
        }
        assert_eq!(f.finish().to_bits(), e.finish().to_bits());
    }

    /// A merge of sums with different bases shifts acc, and n keeps the bound: 2^19 merges of 2^8 values at the
    /// top of the window, shifted up by 8 bits, flush before acc overflows.
    #[test]
    fn fix_sum_merge_shifts_to_the_lower_base() {
        let m = Meter::new(usize::MAX);
        let top = ((1u64 << 53) - 1) as f64 * 2f64.powi(-52 - BELOW + WINDOW);
        let (mut f, mut e) = (FixSum::default(), sum(&[top / 256.0]));
        f.add(top / 256.0, &m); // the lower base
        let mut part = FixSum::default();
        (0..256).for_each(|_| part.add(top, &m));
        for _ in 0..(1 << 19) + 3 {
            f.merge(&part, &m);
            (0..256).for_each(|_| e.add(top, &m));
        }
        assert_eq!(f.finish().to_bits(), e.finish().to_bits());
        let (mut g, mut h) = (FixSum::default(), FixSum::default());
        g.add(top, &m);
        h.add(top / 256.0, &m);
        g.merge(&h, &m); // the higher base takes the lower one
        assert_eq!(g.finish().to_bits(), sum(&[top, top / 256.0]).finish().to_bits());
    }

    #[test]
    fn meter_counts_each_spill_and_stops_at_room() {
        let vs: Vec<f64> = (0..10).map(|j| 2f64.powi(60 * j)).collect();
        let (mut s, m) = (ExactSum::default(), Meter::new(usize::MAX));
        vs.iter().for_each(|&v| s.add(v, &m));
        // 3 partials make the Spill, and its buffer grows from 3 to 7 and 14
        assert_eq!(m.made.load(Relaxed), spill_bytes(3) + (4 + 7) * 8);
        assert_eq!((s.parts().len(), s.spill.as_ref().unwrap().parts.capacity()), (10, 14));
        let (mut s, m) = (ExactSum::default(), Meter::new(0));
        vs.iter().for_each(|&v| s.add(v, &m));
        assert!(s.spill.is_none());
        assert!(m.cells() > 0, "a refused Spill counts over room");
    }
}
