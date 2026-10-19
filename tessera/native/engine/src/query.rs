//! The read query (`Store::summarize`). It filters the cells of a Store and aggregates them to the kept
//! dimensions in one pass. The pass reads the base and the delta in place, in parallel, with no copy of the Store.

use crate::eval::{from_key, group, max_key, min_key, FixSum, Meter, DENSE_MAX};
use crate::packed::BLOCK;
use crate::*;
use std::ops::{Range, RangeInclusive};
use std::sync::Mutex;

/// The aggregations of a read (proto `Aggregation`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReadAgg {
    Sum,
    Avg,
    Min,
    Max,
    Count,
}

impl ReadAgg {
    /// The lower-case name, as in SQL and in the Python API.
    pub fn name(self) -> &'static str {
        match self {
            ReadAgg::Sum => "sum",
            ReadAgg::Avg => "avg",
            ReadAgg::Min => "min",
            ReadAgg::Max => "max",
            ReadAgg::Count => "count",
        }
    }

    pub fn from_name(name: &str) -> Option<ReadAgg> {
        [ReadAgg::Sum, ReadAgg::Avg, ReadAgg::Min, ReadAgg::Max, ReadAgg::Count].into_iter().find(|a| a.name() == name)
    }
}

/// One row for each group that has cells, in the order of the member numbers. `cols[j]` has the member numbers
/// of kept dimension j, in `metric_dims` order.
#[derive(Debug, PartialEq)]
pub struct Groups {
    pub cols: Vec<Vec<u32>>,
    pub vals: Vec<f64>,
}

/// A member test on one packing position.
struct Test {
    shift: u32,
    mask: u64,
    sel: Arc<Sel>,
}

/// A kept dimension. `remap` gives the rank of a member in the result (NONE if the filter drops the member).
struct Radix {
    dim: DimId,
    shift: u32,
    mask: u64,
    remap: Vec<u32>,
    member_of_rank: Vec<u32>,
}

const NONE: u32 = u32::MAX;
/// The bytes before and after the entries of a Dense table. They keep the small tables of 2 threads out of one
/// cache line.
const PAD: usize = 128;
/// The work items that one thread takes at a time. Small chunks balance the threads when one of them is slow.
const CHUNK: usize = 64;

/// The base rows of one block and the keys that the block owns. The blocks of all segments in key order make one
/// sequence. A block owns the keys from its first key (0 for the first block) to the first key of the next block
/// minus 1 (u64::MAX for the last block). Thus each key has one owner, and the scan reads each delta key one time,
/// also a key between two blocks or two segments.
#[derive(Clone, Debug, PartialEq)]
struct Item {
    seg: usize,
    block_in_segment: usize,
    rows: Range<usize>, // positions in the block
    keys: RangeInclusive<u64>,
}

#[derive(Debug, PartialEq)]
enum Group {
    /// An array of all the group combinations, one for each thread.
    Dense { slots: usize, tables: usize },
    /// Collect (group key, value) pairs, then sort them (agg::group).
    Sort,
}

/// The accumulator of one group in the Dense array. SUM and AVG use FixSum, which gives the bits of ExactSum.
trait Acc: Clone + Default + Send + Sync {
    fn add(&mut self, v: f64, m: &Meter);
    fn merge(&mut self, o: &Self, m: &Meter);
    fn value(&self, n: u64, agg: ReadAgg) -> f64;
}

impl Acc for FixSum {
    #[inline]
    fn add(&mut self, v: f64, m: &Meter) {
        FixSum::add(self, v, m)
    }
    fn merge(&mut self, o: &Self, m: &Meter) {
        FixSum::merge(self, o, m)
    }
    fn value(&self, n: u64, agg: ReadAgg) -> f64 {
        if agg == ReadAgg::Avg { self.finish() / n as f64 } else { self.finish() }
    }
}

#[derive(Clone)]
struct Lo(i64);
#[derive(Clone)]
struct Hi(i64);

impl Default for Lo {
    fn default() -> Lo {
        Lo(i64::MAX)
    }
}

impl Default for Hi {
    fn default() -> Hi {
        Hi(i64::MIN)
    }
}

impl Acc for Lo {
    #[inline]
    fn add(&mut self, v: f64, _: &Meter) {
        self.0 = self.0.min(min_key(v));
    }
    fn merge(&mut self, o: &Self, _: &Meter) {
        self.0 = self.0.min(o.0);
    }
    fn value(&self, _: u64, _: ReadAgg) -> f64 {
        from_key(self.0)
    }
}

impl Acc for Hi {
    #[inline]
    fn add(&mut self, v: f64, _: &Meter) {
        self.0 = self.0.max(max_key(v));
    }
    fn merge(&mut self, o: &Self, _: &Meter) {
        self.0 = self.0.max(o.0);
    }
    fn value(&self, _: u64, _: ReadAgg) -> f64 {
        from_key(self.0)
    }
}

impl Acc for () {
    #[inline]
    fn add(&mut self, _: f64, _: &Meter) {}
    fn merge(&mut self, _: &Self, _: &Meter) {}
    fn value(&self, n: u64, _: ReadAgg) -> f64 {
        n as f64
    }
}

impl Store {
    /// Filter the cells by r, keep the dimensions in `keep` and aggregate the other dimensions with agg. A
    /// dimension in keep or r that the Store does not have is ignored. SUM and AVG are exact (ExactSum), so the
    /// result does not change with the thread count. Err only if the work does not fit in b.
    pub fn summarize(&self, keep: &[DimId], r: &Restrict, agg: ReadAgg, cat: &Catalog, b: &Budget) -> Result<Groups> {
        match agg {
            ReadAgg::Sum | ReadAgg::Avg => self.summarize_with::<FixSum>(keep, r, agg, cat, b),
            ReadAgg::Min => self.summarize_with::<Lo>(keep, r, agg, cat, b),
            ReadAgg::Max => self.summarize_with::<Hi>(keep, r, agg, cat, b),
            ReadAgg::Count => self.summarize_with::<()>(keep, r, agg, cat, b),
        }
    }

    fn summarize_with<A: Acc>(&self, keep: &[DimId], r: &Restrict, agg: ReadAgg, cat: &Catalog, b: &Budget) -> Result<Groups> {
        // The remap and member_of_rank of each kept dimension: at most 8 B a member
        let kept = self.metric_dims.iter().filter(|d| keep.contains(d)).map(|&d| cat.dims[d].size as usize * 8).sum::<usize>();
        // The key ranges and the Items: each range gives one Item, plus one Item for each block border that it crosses
        let n = self.pack.dims.first().and_then(|&d| r.get(d)).map_or(1, |s| s.members.len());
        let blocks = self.base.segs.iter().map(|g| g.keys.fences().blocks()).sum::<usize>();
        let work = n.saturating_mul(size_of::<RangeInclusive<u64>>()).saturating_add(n.saturating_add(blocks).saturating_mul(size_of::<Item>()));
        b.take(kept.saturating_add(work).div_ceil(CELL_BYTES), "集計")?;
        let radix = radix(self, keep, r, cat);
        let tests = tests(&self.pack, r);
        let items = items(&self.base.segs, self.delta.is_empty(), &ranges(&self.pack, r));
        let rows = items.iter().map(|it| it.rows.len()).sum::<usize>() + self.delta.len();
        let slots = radix.iter().try_fold(1usize, |a, x| a.checked_mul(x.member_of_rank.len()));
        match choose(slots, size_of::<(A, u64)>(), items.len(), rows, &self.cfg) {
            Group::Dense { slots, tables } => self.dense::<A>(&radix, &tests, &items, slots, tables, agg, b),
            Group::Sort => self.sorted(&radix, &tests, &items, rows, agg, cat, b),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn dense<A: Acc>(&self, radix: &[Radix], tests: &[Test], items: &[Item], slots: usize, tables: usize, agg: ReadAgg, b: &Budget) -> Result<Groups> {
        let entry = size_of::<(A, u64)>();
        let pad = PAD.div_ceil(entry);
        let table_cells = ((slots + 2 * pad) * entry).saturating_mul(tables).div_ceil(CELL_BYTES);
        b.need(table_cells, "集計")?;
        let m = Meter::new(b.room().saturating_sub(table_cells));
        // A run holds at most one Spill for a short time, so the Meter of the table does not count it
        let short = Meter::unbounded();
        let fill = |t: &mut [(A, u64)], it: &Item| {
            // A run of rows of one group adds up in a local: one table write per row costs about 3 times more
            let (mut cur, mut run) = (NONE as usize, (A::default(), 0u64));
            scan(self, tests, it, |k, v| {
                let mut s = 0usize;
                for x in radix {
                    let rank = x.remap.get(((k >> x.shift) & x.mask) as usize).copied().unwrap_or(NONE);
                    if rank == NONE {
                        return;
                    }
                    s = s * x.member_of_rank.len() + rank as usize;
                }
                if s == cur {
                    run.0.add(v, &short);
                    run.1 += 1;
                    return;
                }
                if run.1 > 0 {
                    let e = &mut t[pad + cur];
                    e.0.merge(&run.0, &m);
                    e.1 += run.1;
                    run = (A::default(), 0);
                }
                cur = s;
                let e = &mut t[pad + s];
                e.0.add(v, &m);
                e.1 += 1;
            });
            if run.1 > 0 {
                let e = &mut t[pad + cur];
                e.0.merge(&run.0, &m);
                e.1 += run.1;
            }
        };
        // A thread locks only its own table, so the locks never wait
        let new = || vec![(A::default(), 0u64); slots + 2 * pad];
        let tabs: Vec<Vec<(A, u64)>> = if tables == 1 {
            let mut t = new();
            items.iter().for_each(|it| fill(&mut t, it));
            vec![t]
        } else {
            let tabs: Vec<Mutex<Vec<(A, u64)>>> = (0..tables).into_par_iter().map(|_| Mutex::new(new())).collect();
            // With a table for each chunk, each chunk takes its own table. Two thread indexes can give the same table
            let own = items.len().div_ceil(CHUNK) <= tables;
            items.par_chunks(CHUNK).enumerate().for_each(|(i, c)| {
                let k = if own { i } else { rayon::current_thread_index().unwrap_or(0) % tables };
                let mut t = tabs[k].lock().expect("no panic holds the table");
                c.iter().for_each(|it| fill(&mut t, it));
            });
            tabs.into_iter().map(|t| t.into_inner().expect("no panic holds the table")).collect()
        };
        let mut tabs = tabs.into_iter();
        let mut t = tabs.next().expect("tables > 0");
        for o in tabs {
            for (x, y) in t.iter_mut().zip(&o) {
                if y.1 > 0 {
                    x.0.merge(&y.0, &m);
                    x.1 += y.1;
                }
            }
        }
        let t = &t[pad..pad + slots];
        let groups = t.iter().filter(|e| e.1 > 0).count();
        // m.cells() is over the room if the Meter refused a Spill: then the sums are wrong
        b.need(table_cells + m.cells() + (groups * (4 * radix.len() + 8)).div_ceil(CELL_BYTES), "集計")?;
        let mut out = Groups { cols: radix.iter().map(|_| Vec::with_capacity(groups)).collect(), vals: Vec::with_capacity(groups) };
        let mut ranks = vec![0usize; radix.len()];
        for (s, e) in t.iter().enumerate().filter(|(_, e)| e.1 > 0) {
            let mut rest = s;
            for (j, x) in radix.iter().enumerate().rev() {
                ranks[j] = rest % x.member_of_rank.len();
                rest /= x.member_of_rank.len();
            }
            for ((c, x), &rank) in out.cols.iter_mut().zip(radix).zip(&ranks) {
                c.push(x.member_of_rank[rank]);
            }
            out.vals.push(e.0.value(e.1, agg));
        }
        Ok(out)
    }

    #[allow(clippy::too_many_arguments)]
    fn sorted(&self, radix: &[Radix], tests: &[Test], items: &[Item], rows: usize, agg: ReadAgg, cat: &Catalog, b: &Budget) -> Result<Groups> {
        let dims: Vec<DimId> = radix.iter().map(|x| x.dim).collect();
        let out = Packing::new(&dims, cat)?;
        let live_rows = rows.saturating_mul(2).saturating_add((items.len() * size_of::<Vec<(u64, f64)>>()).div_ceil(CELL_BYTES));
        b.need(live_rows, "集計")?;
        let pair = |k: u64| -> Option<u64> {
            let mut o = 0;
            for (j, x) in radix.iter().enumerate() {
                let rank = x.remap.get(((k >> x.shift) & x.mask) as usize).copied().unwrap_or(NONE);
                if rank == NONE {
                    return None;
                }
                o |= out.put(j, x.member_of_rank[rank as usize]);
            }
            Some(o)
        };
        // A part sized to its bound never grows, so the parts hold at most rows pairs
        let fill = |it: &Item| {
            let mut p = Vec::with_capacity(it.rows.len() + self.delta.range(it.keys.clone()).count());
            scan(self, tests, it, |k, v| p.extend(pair(k).map(|o| (o, v))));
            p
        };
        let parts: Vec<Vec<(u64, f64)>> = if self.cfg.par(rows) { items.par_iter().map(fill).collect() } else { items.iter().map(fill).collect() };
        let pairs = parts.concat();
        drop(parts);
        let a = match agg {
            ReadAgg::Sum => Agg::Sum,
            ReadAgg::Avg => Agg::Avg,
            ReadAgg::Min => Agg::Min,
            ReadAgg::Max => Agg::Max,
            ReadAgg::Count => Agg::Count,
        };
        let cube = group(&self.cfg, pairs, out.clone(), &[a], b)?.remove(0);
        let groups = cube.cells.len();
        b.need(groups + (groups * (4 * dims.len() + 8)).div_ceil(CELL_BYTES), "集計")?;
        let cols = (0..dims.len()).map(|j| cube.cells.iter().map(|&(k, _)| out.get(k, j)).collect()).collect();
        Ok(Groups { cols, vals: cube.cells.iter().map(|c| c.1).collect() })
    }
}

/// Pick the aggregation for `slots` groups (None: more than usize) with `entry` bytes each, over `items`
/// work items with `rows` rows.
fn choose(slots: Option<usize>, entry: usize, items: usize, rows: usize, cfg: &Config) -> Group {
    let Some(slots) = slots.filter(|&s| s <= DENSE_MAX) else { return Group::Sort };
    let tables = if cfg.par(rows) { rayon::current_num_threads().clamp(1, items.div_ceil(CHUNK).max(1)) } else { 1 };
    // The same rule as agg.rs Dense::fold: the arrays must not be larger than the (group, value) pairs of the sort
    let bytes = slots.saturating_mul(entry).saturating_mul(tables);
    if cfg.dense_always || bytes <= rows.saturating_mul(16) {
        Group::Dense { slots, tables }
    } else {
        Group::Sort
    }
}

/// The kept dimensions in metric_dims order (each copy of a repeated dimension).
fn radix(st: &Store, keep: &[DimId], r: &Restrict, cat: &Catalog) -> Vec<Radix> {
    let mut out = Vec::new();
    for (j, &d) in st.metric_dims.iter().enumerate() {
        if !keep.contains(&d) {
            continue;
        }
        let pos = st.pack.slot_of(&st.metric_dims, j).expect("a metric dimension is in the packing");
        let size = cat.dims[d].size;
        let member_of_rank: Vec<u32> = match r.get(d) {
            Some(sel) => sel.members.iter().copied().filter(|&m| m < size).collect(),
            None => (0..size).collect(),
        };
        let mut remap = vec![NONE; size as usize];
        for (rank, &m) in member_of_rank.iter().enumerate() {
            remap[m as usize] = rank as u32;
        }
        out.push(Radix { dim: d, shift: st.pack.shifts[pos], mask: st.pack.masks[pos], remap, member_of_rank });
    }
    out
}

/// A member test for each filtered packing position, except the partition dimension (`ranges` reads only
/// its members).
fn tests(pack: &Packing, r: &Restrict) -> Vec<Test> {
    let parted = pack.dims.first().is_some_and(|&d| r.get(d).is_some());
    checks(pack, r)
        .into_iter()
        .filter(|&(i, _)| !(parted && i == 0))
        .map(|(i, sel)| Test { shift: pack.shifts[i], mask: pack.masks[i], sel })
        .collect()
}

/// The key ranges to read: one for each selected member of the partition dimension, or all keys.
fn ranges(pack: &Packing, r: &Restrict) -> Vec<RangeInclusive<u64>> {
    let Some(sel) = pack.dims.first().and_then(|&d| r.get(d)) else { return vec![RangeInclusive::new(0, u64::MAX)] };
    let (shift, mask) = (pack.shifts[0], pack.masks[0]);
    sel.members.iter().filter(|&&m| m as u64 <= mask).map(|&m| (m as u64) << shift..=((m as u64) << shift) | ((1u64 << shift) - 1)).collect()
}

/// The work items of the key ranges (sorted, not overlapping). If the Store has no delta, an item with no base
/// rows is dropped.
fn items(segs: &[Arc<Segment>], no_delta: bool, ranges: &[RangeInclusive<u64>]) -> Vec<Item> {
    let mut out = Vec::new();
    for r in ranges {
        let (lo, hi) = (*r.start(), *r.end());
        if segs.is_empty() {
            if !no_delta {
                out.push(Item { seg: 0, block_in_segment: 0, rows: 0..0, keys: r.clone() });
            }
            continue;
        }
        let mut s = segs.partition_point(|g| g.keys.fences().first(0) <= lo).saturating_sub(1);
        let mut b = segs[s].keys.fences().owner(lo);
        'segs: loop {
            let keys = &segs[s].keys;
            let f = keys.fences();
            let (start, end) = (keys.partition_point(|k| k < lo), keys.partition_point(|k| k <= hi));
            loop {
                let own_lo = if s == 0 && b == 0 { 0 } else { f.first(b) };
                let next = if b + 1 < f.blocks() { Some(f.first(b + 1)) } else { segs.get(s + 1).map(|g| g.keys.fences().first(0)) };
                let (a, e) = (start.max(b * BLOCK), end.min((b + 1) * BLOCK));
                let rows = if a < e { a - b * BLOCK..e - b * BLOCK } else { 0..0 };
                if !rows.is_empty() || !no_delta {
                    out.push(Item { seg: s, block_in_segment: b, rows, keys: lo.max(own_lo)..=next.map_or(hi, |f| hi.min(f - 1)) });
                }
                if next.is_none_or(|f| f > hi) {
                    break 'segs;
                }
                b += 1;
                if b == f.blocks() {
                    break;
                }
            }
            (s, b) = (s + 1, 0);
        }
    }
    out
}

/// Give each cell of the item that passes the tests to visit (key, value). Base rows go in key order; a delta
/// value goes first. A delta key replaces (or removes) the base row with the same key.
#[inline]
fn scan(st: &Store, tests: &[Test], it: &Item, mut visit: impl FnMut(u64, f64)) {
    let mut buf = [0u64; BLOCK];
    let keys = &mut buf[..it.rows.len()];
    let mut vals = &[][..];
    if !keys.is_empty() {
        let block = st.base.segs[it.seg].block(it.block_in_segment);
        block.keys(it.rows.clone(), keys);
        vals = &block.vals[it.rows.clone()];
    }
    let pass = |k: u64| tests.iter().all(|t| t.sel.has(((k >> t.shift) & t.mask) as u32));
    let mut delta = st.delta.range(it.keys.clone()).peekable();
    if delta.peek().is_none() {
        for (&k, &v) in keys.iter().zip(vals) {
            if pass(k) {
                visit(k, v);
            }
        }
        return;
    }
    let mut skip = [false; BLOCK];
    for (&k, &Slot(v)) in delta {
        if let Ok(j) = keys.binary_search(&k) {
            skip[j] = true;
        }
        if let Some(v) = v.filter(|_| pass(k)) {
            visit(k, v);
        }
    }
    for (j, (&k, &v)) in keys.iter().zip(vals).enumerate() {
        if !skip[j] && pass(k) {
            visit(k, v);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::eval::{aggregate_rows, Rows};

    struct Rng(u64);

    impl Rng {
        fn below(&mut self, n: u64) -> u64 {
            self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            (self.0 >> 33) % n
        }
    }

    const SIZES: [u32; 3] = [200, 3, 13];

    fn catalog(cfg: Config) -> Catalog {
        let dims = SIZES.iter().enumerate().map(|(i, &size)| DimInfo { size, ordered: false, name: format!("d{i}") }).collect();
        Catalog { dims, maps: Vec::new(), cfg }
    }

    fn value(rng: &mut Rng, i: u64) -> f64 {
        match rng.below(100) {
            0 => 1e16,
            1 => -1e16,
            2 => 1e300,
            3 => -5e-324,
            4 => f64::INFINITY,
            5 => -0.0,
            _ => i as f64 * 0.01,
        }
    }

    /// A Store with about 4,700 base rows (5 blocks, in segments of about `cfg.seg_rows` rows) and a delta with overrides, removals and new keys.
    fn store(rng: &mut Rng, index: DimId, cat: &Catalog) -> Store {
        let (mut cols, mut vals) = (vec![Vec::new(); 3], Vec::new());
        for a in 0..SIZES[0] {
            for b in 0..SIZES[1] {
                for c in 0..SIZES[2] {
                    if rng.below(10) < 6 {
                        cols[0].push(a);
                        cols[1].push(b);
                        cols[2].push(c);
                        vals.push(value(rng, vals.len() as u64));
                    }
                }
            }
        }
        let slices: Vec<&[u32]> = cols.iter().map(|c| c.as_slice()).collect();
        let mut st = Store::new(&[0, 1, 2], Some(index), Kind::Num, cat).unwrap().with_rows(&slices, &vals).unwrap();
        assert!(st.base.segs.iter().map(|g| g.keys.fences().blocks()).sum::<usize>() > 3);
        for i in 0..300 {
            let key: Vec<u32> = SIZES.iter().map(|&s| rng.below(s as u64) as u32).collect();
            let v = if rng.below(3) == 0 { None } else { Some(value(rng, i)) };
            st.write(&key, v);
        }
        assert!(st.delta.len() > 200, "the delta stays");
        st
    }

    fn region(rng: &mut Rng) -> Restrict {
        let mut r = Restrict::all(3);
        for (d, &size) in SIZES.iter().enumerate() {
            if rng.below(2) == 0 {
                let ms = (0..size).filter(|_| rng.below(3) == 0).collect();
                r = r.with(d, Sel::new(ms, size));
            }
        }
        r
    }

    /// The same query through the aggregation of formulas (aggregate_rows over the read cells).
    fn want(st: &Store, keep: &[DimId], r: &Restrict, agg: Agg, cat: &Catalog) -> Vec<(Vec<u32>, u64)> {
        let dims: Vec<DimId> = st.metric_dims.iter().copied().filter(|d| keep.contains(d)).collect();
        let out = Packing::new(&dims, cat).unwrap();
        let proj = Proj::new(&st.pack, &out);
        let b = Budget::new(usize::MAX, 0);
        let cube = aggregate_rows(Rows::Owned(st.read(r)), out.clone(), &[agg], cat, r, &b, |xp, o, k| Some(proj.apply(xp, o, k))).unwrap().remove(0);
        cube.cells.iter().map(|&(k, v)| ((0..dims.len()).map(|j| out.get(k, j)).collect(), bits(v))).collect()
    }

    fn got(g: Groups) -> Vec<(Vec<u32>, u64)> {
        (0..g.vals.len()).map(|i| (g.cols.iter().map(|c| c[i]).collect(), bits(g.vals[i]))).collect()
    }

    /// One bit pattern for all NaN values: the sign of a NaN from inf - inf is not specified.
    fn bits(v: f64) -> u64 {
        if v.is_nan() { u64::MAX } else { v.to_bits() }
    }

    const AGGS: [(ReadAgg, Agg); 5] =
        [(ReadAgg::Sum, Agg::Sum), (ReadAgg::Avg, Agg::Avg), (ReadAgg::Min, Agg::Min), (ReadAgg::Max, Agg::Max), (ReadAgg::Count, Agg::Count)];

    /// summarize gives the same bits as the formula aggregation, on Stores with a delta, with and without a
    /// filter on the partition dimension, on the Dense and Sort paths, on 1 thread and on many tables.
    #[test]
    fn summarize_matches_the_formula_aggregation() {
        let mut rng = Rng(7);
        let (mut seen, mut segmented) = ((0, 0), 0);
        for round in 0..24 {
            let seg_rows = if round % 5 < 3 { 1500 } else { usize::MAX };
            let cfg = Config { compact_min: usize::MAX, par_min: if round % 2 == 0 { 0 } else { usize::MAX }, dense_always: round % 4 < 2, seg_rows, ..Config::default() };
            let cat = catalog(cfg);
            let st = store(&mut rng, [0, 2][round % 3 % 2], &cat);
            segmented += (st.base.segs.len() > 2) as usize;
            for _ in 0..6 {
                let r = region(&mut rng);
                let keep: Vec<DimId> = (0..3).filter(|_| rng.below(2) == 0).collect();
                let slots = keep.iter().map(|&d| r.get(d).map_or(SIZES[d] as usize, |s| s.members.len())).product();
                let path = choose(Some(slots), size_of::<(FixSum, u64)>(), 8, st.rows_hint(), &cfg);
                if matches!(path, Group::Dense { .. }) { seen.0 += 1 } else { seen.1 += 1 }
                for (a, f) in AGGS {
                    let b = Budget::new(usize::MAX, 0);
                    assert_eq!(got(st.summarize(&keep, &r, a, &cat, &b).unwrap()), want(&st, &keep, &r, f, &cat), "{a:?} keep={keep:?} round={round}");
                }
            }
        }
        assert!(seen.0 > 10 && seen.1 > 5, "both paths ran: {seen:?}");
        assert!(segmented > 10, "{segmented} rounds read a base of 3 or more segments");
    }

    /// 4 threads and 1 thread give the same bits.
    #[test]
    fn thread_count_does_not_change_the_bits() {
        let cat = catalog(Config { compact_min: usize::MAX, par_min: 0, seg_rows: 1500, ..Config::default() });
        let st = store(&mut Rng(11), 0, &cat);
        assert!(st.base.segs.len() > 2);
        let run = |threads: usize, keep: &[DimId], a: ReadAgg| {
            let pool = rayon::ThreadPoolBuilder::new().num_threads(threads).build().unwrap();
            pool.install(|| got(st.summarize(keep, &Restrict::all(3), a, &cat, &Budget::new(usize::MAX, 0)).unwrap()))
        };
        for keep in [vec![], vec![1], vec![0, 2]] {
            for (a, _) in AGGS {
                let one = run(1, &keep, a);
                assert!(!one.is_empty());
                assert_eq!(run(4, &keep, a), one, "{a:?} keep={keep:?}");
            }
        }
    }

    /// Each key of the key space has one owner item, also keys before the first and after the last base key, and
    /// keys between two segments.
    #[test]
    fn items_cover_each_key_once() {
        let keys: Vec<u64> = (0..3000).map(|i| 10 + i * 3).collect();
        let one = Base::from_parts(&[&keys]);
        let all = items(&one.segs, false, &[RangeInclusive::new(0, u64::MAX)]);
        assert_eq!(all.len(), 3);
        assert_eq!(all[0].keys, 0..=keys[1024] - 1);
        assert_eq!(all[2].keys, keys[2048]..=u64::MAX);
        assert_eq!(all.iter().map(|it| it.rows.len()).sum::<usize>(), 3000);
        let part = items(&one.segs, true, &[keys[1000]..=keys[1030], keys[2999] + 1..=u64::MAX]);
        assert_eq!(part, vec![Item { seg: 0, block_in_segment: 0, rows: 1000..1024, keys: keys[1000]..=keys[1024] - 1 }, Item { seg: 0, block_in_segment: 1, rows: 0..7, keys: keys[1024]..=keys[1030] }]);

        let two = Base::from_parts(&[&keys[..1500], &keys[1500..]]);
        let all = items(&two.segs, false, &[RangeInclusive::new(0, u64::MAX)]);
        let owners: Vec<(usize, usize, RangeInclusive<u64>)> = all.iter().map(|it| (it.seg, it.block_in_segment, it.keys.clone())).collect();
        assert_eq!(
            owners,
            [(0, 0, 0..=keys[1024] - 1), (0, 1, keys[1024]..=keys[1500] - 1), (1, 0, keys[1500]..=keys[2524] - 1), (1, 1, keys[2524]..=u64::MAX)]
        );
        assert_eq!(all.iter().map(|it| it.rows.len()).sum::<usize>(), 3000);
        let part = items(&two.segs, true, &[RangeInclusive::new(keys[1400], keys[1600])]);
        assert_eq!(part, vec![Item { seg: 0, block_in_segment: 1, rows: 376..476, keys: keys[1400]..=keys[1500] - 1 }, Item { seg: 1, block_in_segment: 0, rows: 0..101, keys: keys[1500]..=keys[1600] }]);
        let gap = [RangeInclusive::new(keys[1499] + 1, keys[1500] - 1)];
        assert_eq!(items(&two.segs, false, &gap), vec![Item { seg: 0, block_in_segment: 1, rows: 0..0, keys: gap[0].clone() }]);
        assert!(items(&two.segs, true, &gap).is_empty());
    }

    #[test]
    fn summarize_refuses_a_table_over_the_budget() {
        let cat = catalog(Config { compact_min: usize::MAX, dense_always: true, ..Config::default() });
        let st = store(&mut Rng(3), 0, &cat);
        let e = st.summarize(&[0, 1, 2], &Restrict::all(3), ReadAgg::Sum, &cat, &Budget::new(1000, 0)).unwrap_err();
        assert!(e.starts_with("メモリの予算を超える: 集計"), "{e}");
    }

    /// A Store with each key of `sizes` and no delta. Each run of 13 rows needs a Spill: the magnitudes are far apart.
    fn plain(sizes: &[u32], cat: &Catalog) -> Store {
        let mut cols = vec![Vec::new(); sizes.len()];
        let mut vals = Vec::new();
        let n = sizes.iter().product::<u32>();
        for mut i in 0..n {
            for (c, &s) in cols.iter_mut().zip(sizes).rev() {
                c.push(i % s);
                i /= s;
            }
            vals.push([1e200, 1.0, 1e-200, 1e100][vals.len() % 4]);
        }
        let slices: Vec<&[u32]> = cols.iter().map(|c| c.as_slice()).collect();
        let dims: Vec<DimId> = (0..sizes.len()).collect();
        Store::new(&dims, Some(0), Kind::Num, cat).unwrap().with_rows(&slices, &vals).unwrap()
    }

    fn over(r: Result<Groups>) -> bool {
        r.is_err_and(|e| e.starts_with("メモリの予算を超える: 集計"))
    }

    /// The Sort path counts the result Cube and the output columns (20 B a group here) with the budget.
    #[test]
    fn sort_counts_the_output_columns() {
        let cat = catalog(Config { compact_min: usize::MAX, par_min: usize::MAX, ..Config::default() });
        let st = plain(&SIZES, &cat);
        let rows = st.rows_hint();
        let run = |cells: usize| st.summarize(&[0, 1, 2], &Restrict::all(3), ReadAgg::Sum, &cat, &Budget::new(cells * CELL_BYTES, 0));
        assert_eq!(run(rows * 3).unwrap().vals.len(), rows);
        assert!(over(run(rows * 2 + rows / 8)), "the pairs fit, the Cube and the columns do not");
    }

    /// The remap and the members of a kept dimension (8 B a member) count with the budget.
    #[test]
    fn the_members_of_a_kept_dimension_count() {
        let dims = [1 << 20, 3].iter().enumerate().map(|(i, &size)| DimInfo { size, ordered: false, name: format!("d{i}") }).collect();
        let cat = Catalog { dims, maps: Vec::new(), cfg: Config { compact_min: usize::MAX, ..Config::default() } };
        let st = plain(&[100, 3], &cat);
        let run = |bytes: usize| st.summarize(&[0], &Restrict::all(2), ReadAgg::Sum, &cat, &Budget::new(bytes, 0));
        assert_eq!(run(9 << 20).unwrap().vals.len(), 100);
        assert!(over(run(1 << 20)));
    }

    #[test]
    fn the_key_ranges_and_the_items_count() {
        let dims = [1 << 20, 3].iter().enumerate().map(|(i, &size)| DimInfo { size, ordered: false, name: format!("d{i}") }).collect();
        let cat = Catalog { dims, maps: Vec::new(), cfg: Config { compact_min: usize::MAX, ..Config::default() } };
        let mut st = Store::new(&[0, 1], Some(0), Kind::Num, &cat).unwrap();
        st.write(&[5, 1], Some(1.0));
        let r = Restrict::all(2).with(0, Sel::new((0..100_000).collect(), 1 << 20));
        let run = |bytes: usize| st.summarize(&[1], &r, ReadAgg::Count, &cat, &Budget::new(bytes, 0));
        assert_eq!(run(usize::MAX).unwrap(), Groups { cols: vec![vec![1]], vals: vec![1.0] });
        assert!(over(run(4 << 20)));
    }

    /// The Spill of a run lives for a short time, so it does not use the budget again for each run.
    #[test]
    fn runs_do_not_use_up_the_budget() {
        let cat = catalog(Config { compact_min: usize::MAX, par_min: usize::MAX, dense_always: true, ..Config::default() });
        let st = plain(&SIZES, &cat);
        let g = st.summarize(&[1], &Restrict::all(3), ReadAgg::Sum, &cat, &Budget::new(300 * CELL_BYTES, 0)).unwrap();
        assert_eq!(got(g), want(&st, &[1], &Restrict::all(3), Agg::Sum, &cat));
    }

    /// Each chunk of CHUNK items goes to one thread, so there is no table for more threads than chunks.
    #[test]
    fn no_more_tables_than_chunks() {
        let cfg = Config { par_min: 0, ..Config::default() };
        let pool = rayon::ThreadPoolBuilder::new().num_threads(8).build().unwrap();
        let tables = |items: usize| pool.install(|| choose(Some(10), 16, items, 1 << 20, &cfg));
        assert_eq!(tables(100), Group::Dense { slots: 10, tables: 2 });
        assert_eq!(tables(1000), Group::Dense { slots: 10, tables: 8 });
    }
}

