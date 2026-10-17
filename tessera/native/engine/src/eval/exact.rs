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
