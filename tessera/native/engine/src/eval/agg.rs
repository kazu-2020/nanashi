//! 集計（行を集めて並べ替える集計と、集計先が少ないときの読みながらの集計）。

#[allow(unused_imports)]
use crate::*;
use std::ops::Range;

use super::exact::ExactSum;

#[derive(Clone)]
pub(crate) struct Acc {
    pub(crate) sum: ExactSum,
    pub(crate) cnt: u64,
    pub(crate) min: i64, // min_key
    pub(crate) max: i64, // max_key
    pub(crate) first: f64,
}

/// A key whose integer order is the order of MIN: a NaN is the least (it gives NaN, as for SUM), and -0.0 is less
/// than 0.0. Thus the result does not depend on the order of the values (f64::min ignores a NaN and can give
/// either zero).
fn min_key(v: f64) -> i64 {
    if v.is_nan() { i64::MIN } else { order(v.to_bits() as i64) }
}

/// A key whose integer order is the order of MAX: a NaN is the greatest, and 0.0 is greater than -0.0.
fn max_key(v: f64) -> i64 {
    if v.is_nan() { i64::MAX } else { order(v.to_bits() as i64) }
}

/// Maps the bits of a value to a key in the order of the values (the same map as f64::total_cmp). The map is its
/// own inverse.
fn order(b: i64) -> i64 {
    b ^ (((b >> 63) as u64) >> 1) as i64
}

/// The value of a key of min_key or max_key.
fn from_key(k: i64) -> f64 {
    if k == i64::MIN || k == i64::MAX { f64::NAN } else { f64::from_bits(order(k) as u64) }
}

impl Acc {
    pub(crate) fn new() -> Acc {
        Acc { sum: ExactSum::default(), cnt: 0, min: i64::MAX, max: i64::MIN, first: 0.0 }
    }
    pub(crate) fn add(&mut self, v: f64) {
        if self.cnt == 0 {
            self.first = v;
        }
        self.sum.add(v);
        self.cnt += 1;
        self.min = self.min.min(min_key(v));
        self.max = self.max.max(max_key(v));
    }
    pub(crate) fn finish(&self, agg: Agg) -> f64 {
        match agg {
            Agg::Sum => self.sum.finish(),
            Agg::Avg => self.sum.finish() / self.cnt as f64,
            Agg::Low => self.sum.residual(),
            Agg::Min => from_key(self.min),
            Agg::Max => from_key(self.max),
            Agg::Count => self.cnt as f64,
            Agg::First => self.first,
        }
    }
}

/// Sort the (group key, value) pairs and aggregate each run of the same key. Gives one Cube for each of aggs,
/// in key order.
pub(crate) fn group(cfg: &Config, mut pairs: Vec<(u64, f64)>, pack: Packing, aggs: &[Agg]) -> Vec<Cube> {
    sort_cells(cfg, &mut pairs);
    let mut cells = vec![Vec::new(); aggs.len()];
    let mut i = 0;
    while i < pairs.len() {
        let mut acc = Acc::new();
        let k = pairs[i].0;
        while i < pairs.len() && pairs[i].0 == k {
            acc.add(pairs[i].1);
            i += 1;
        }
        push_group(&mut cells, aggs, k, |a| acc.finish(a));
    }
    cubes(pack, cells)
}

/// Push the result of each of aggs for group k. A Low (residual) of 0 is not a cell: a missing residual means 0,
/// and the residual Cube of a one-pass SUM then stays small.
fn push_group(cells: &mut [Vec<(u64, f64)>], aggs: &[Agg], k: u64, finish: impl Fn(Agg) -> f64) {
    for (c, &a) in cells.iter_mut().zip(aggs) {
        let v = finish(a);
        if !(a == Agg::Low && v == 0.0) {
            c.push((k, v));
        }
    }
}

/// One Cube of numbers with pack for each cell list.
fn cubes(pack: Packing, cells: Vec<Vec<(u64, f64)>>) -> Vec<Cube> {
    cells.into_iter().map(|cells| Cube { pack: pack.clone(), kind: Kind::Num, cells }).collect()
}

impl Acc {
    pub(crate) fn merge(&mut self, o: &Acc) {
        if o.cnt == 0 {
            return;
        }
        if self.cnt == 0 {
            *self = o.clone();
            return;
        }
        self.sum.merge(&o.sum);
        self.cnt += o.cnt;
        self.min = self.min.min(o.min);
        self.max = self.max.max(o.max);
    }
}

/// 集計元のセルの並び。格納データや読み出し元の Cube は写さずに、行の番号で読む。
pub(crate) enum Rows {
    Store(Arc<Store>), // 差分のない格納データの本体
    Shared(Arc<Cube>), // 読み出し元の Cube（scan の途中など）
    Owned(Cube),       // 式を評価した結果
}

impl Rows {
    pub(crate) fn pack(&self) -> &Packing {
        match self {
            Rows::Store(s) => &s.pack,
            Rows::Shared(c) => &c.pack,
            Rows::Owned(c) => &c.pack,
        }
    }

    pub(crate) fn len(&self) -> usize {
        match self {
            Rows::Store(s) => s.base_rows().unwrap_or(0),
            Rows::Shared(c) => c.cells.len(),
            Rows::Owned(c) => c.cells.len(),
        }
    }

    /// For a Store it reads the keys block by block (see `Store::base_range`).
    pub(crate) fn for_each_in(&self, r: Range<usize>, mut f: impl FnMut(u64, f64)) {
        match self {
            Rows::Store(s) => s.base_range(r).for_each(|(k, v)| f(k, v)),
            Rows::Shared(c) => c.cells[r].iter().for_each(|&(k, v)| f(k, v)),
            Rows::Owned(c) => c.cells[r].iter().for_each(|&(k, v)| f(k, v)),
        }
    }

    /// Maps each row with `f` and drops the None results. On many rows the parts run in parallel and
    /// write directly into one array, so only that array is held while it is made.
    pub(crate) fn collect(&self, cfg: &Config, f: impl Fn(u64, f64) -> Option<(u64, f64)> + Sync) -> Vec<(u64, f64)> {
        let (n, chunk) = (self.len(), cfg.chunk.max(1));
        if !cfg.par(n) {
            let mut cells = Vec::new();
            self.for_each_in(0..n, |k, v| cells.extend(f(k, v)));
            return cells;
        }
        let mut cells = vec![(0u64, 0f64); n];
        let fill = |(c, part): (usize, &mut [(u64, f64)])| {
            let mut w = 0;
            self.for_each_in(c * chunk..c * chunk + part.len(), |k, v| {
                if let Some(x) = f(k, v) {
                    part[w] = x;
                    w += 1;
                }
            });
            w
        };
        let sizes: Vec<usize> = (0..n).step_by(chunk).map(|lo| chunk.min(n - lo)).collect();
        let written: Vec<usize> = cells.par_chunks_mut(chunk).enumerate().map(fill).collect();
        compact(&mut cells, &sizes, &written);
        cells
    }

    pub(crate) fn into_cube(self) -> Cube {
        match self {
            Rows::Store(s) => s.as_cube(),
            Rows::Shared(c) => (*c).clone(),
            Rows::Owned(c) => c,
        }
    }
}

/// node を r の範囲で評価したセル。範囲の絞り込みがない Ref は、格納データや読み出し元を写さずに渡す。
pub(crate) fn rows_of(node: &Node, cat: &Catalog, src: &[Src], r: &Restrict, b: &Budget) -> Result<Rows> {
    if let Node::Ref(i) = node {
        if r.is_all() {
            match &src[*i] {
                Src::Store(s) if s.base_rows().is_some() => return Ok(Rows::Store(s.clone())),
                Src::Cube(c) => return Ok(Rows::Shared(c.clone())),
                _ => {}
            }
        }
    }
    Ok(Rows::Owned(eval_with(node, cat, src, r, b)?))
}

/// dims の、r の範囲での全組み合わせの数（集計先の数の上限）。
pub(crate) fn combos_in(dims: &[DimId], cat: &Catalog, r: &Restrict) -> f64 {
    dims.iter().fold(1.0, |n, &d| n * r.get(d).map_or(cat.dims[d].size as usize, |s| s.members.len()) as f64)
}

/// If the number of groups is this or less, read the source and add each value to its group in a hash map, with
/// no copy. Each thread keeps an intermediate value (about 64 B) for each group, and the threads merge them at
/// the end. Thus the limit is 1/(16 x thread count) of the source rows. With more groups, the merge time and
/// memory come near the cost to copy the source to (group, value) pairs and sort them. First is not used here,
/// because the order of the rows has a meaning for it.
pub(crate) fn few_groups(cfg: &Config, aggs: &[Agg], groups: f64, rows: usize) -> bool {
    if aggs.contains(&Agg::First) {
        return false;
    }
    cfg.stream_always || groups * (rayon::current_num_threads() * 16) as f64 <= rows as f64
}

/// Reads `rows` and adds each value to the group that `key` gives. Many rows are read in parallel.
pub(crate) fn fold_groups(cfg: &Config, rows: &Rows, key: impl Fn(u64) -> Option<u64> + Sync) -> FxHashMap<u64, Acc> {
    use crate::packed::BLOCK; // one unit of work is one block of packed keys
    let n = rows.len();
    let add = |mut g: FxHashMap<u64, Acc>, b: usize| {
        rows.for_each_in(b * BLOCK..((b + 1) * BLOCK).min(n), |k, v| {
            if let Some(o) = key(k) {
                g.entry(o).or_insert_with(Acc::new).add(v);
            }
        });
        g
    };
    let blocks = n.div_ceil(BLOCK);
    if !cfg.par(n) {
        return (0..blocks).fold(FxHashMap::default(), add);
    }
    let min_len = (blocks / rayon::current_num_threads()).max(1); // one table of intermediate values per thread
    (0..blocks).into_par_iter().with_min_len(min_len).fold(FxHashMap::default, add).reduce(FxHashMap::default, |mut a, b| {
        for (k, x) in &b {
            a.entry(*k).or_insert_with(Acc::new).merge(x);
        }
        a
    })
}

/// Finish each group into one cell for each of aggs, then sort the cells (16 B each, not the 64 B groups).
pub(crate) fn finish_groups(groups: FxHashMap<u64, Acc>, pack: Packing, aggs: &[Agg]) -> Vec<Cube> {
    let mut cells: Vec<Vec<(u64, f64)>> = aggs.iter().map(|&a| Vec::with_capacity(if a == Agg::Low { 0 } else { groups.len() })).collect();
    for (k, x) in groups {
        push_group(&mut cells, aggs, k, |a| x.finish(a));
    }
    for c in &mut cells {
        c.sort_unstable_by_key(|c| c.0);
    }
    cubes(pack, cells)
}

/// A run of nested REMOVE SUM nodes (the parser makes `REMOVE SUM: A, B` into nested nodes) is one aggregation
/// with one rounding. Returns the removed dimensions (outer first) and the child under the run. The top node can
/// be Low (the residual formula). Any other node is a run of 1.
pub(crate) fn remove_run(dim: DimId, agg: Agg, child: &Node) -> (Vec<DimId>, &Node) {
    let mut dims = vec![dim];
    let mut c = child;
    if matches!(agg, Agg::Sum | Agg::Low) {
        while let Node::Remove { child, dim, agg: Agg::Sum } = c {
            dims.push(*dim);
            c = child;
        }
    }
    (dims, c)
}

/// Aggregate rows without the dimensions removed (REMOVE). r is the range of the result (to estimate the number
/// of groups).
pub(crate) fn remove_rows(rows: Rows, removed: &[DimId], aggs: &[Agg], cat: &Catalog, r: &Restrict) -> Result<Vec<Cube>> {
    let dims: Vec<DimId> = rows.pack().dims.iter().copied().filter(|d| !removed.contains(d)).collect();
    let out = Packing::new(&dims, cat)?;
    let proj = Proj::new(rows.pack(), &out);
    Ok(aggregate_rows(rows, out, aggs, cat, r, move |xp, out, k| Some(proj.apply(xp, out, k))))
}

/// key gives the group key (packed as out) from (row packing, out, row key). r is the range of the result.
/// The array path is 2 to 3 times faster than the hash map (docs/performance.md), but it ignores r. Thus if r
/// narrows the groups, or with stream_always, the hash map goes first.
pub(crate) fn aggregate_rows(rows: Rows, out: Packing, aggs: &[Agg], cat: &Catalog, r: &Restrict, key: impl Fn(&Packing, &Packing, u64) -> Option<u64> + Sync) -> Vec<Cube> {
    let cfg = &cat.cfg;
    let xp = rows.pack();
    let k = |x| key(xp, &out, x);
    let groups = combos_in(&out.dims, cat, r);
    let dense_first = dense_first(cat, &out.dims, r);
    if dense_first {
        if let Some(c) = dense_groups(cfg, &rows, &out, cat, aggs, k) {
            return c;
        }
    }
    if few_groups(cfg, aggs, groups, rows.len()) {
        return finish_groups(fold_groups(cfg, &rows, k), out, aggs);
    }
    if !dense_first {
        if let Some(c) = dense_groups(cfg, &rows, &out, cat, aggs, k) {
            return c;
        }
    }
    // Do not copy the source: make the (group, value) pairs directly in one array, then sort them
    let pairs = rows.collect(cfg, |x, v| k(x).map(|o| (o, v)));
    drop(rows);
    group(cfg, pairs, out, aggs)
}

/// True if aggregate_rows tries the array before the hash map: r does not narrow the groups and stream_always
/// is off.
fn dense_first(cat: &Catalog, dims: &[DimId], r: &Restrict) -> bool {
    !cat.cfg.stream_always && combos_in(dims, cat, r) == combos_in(dims, cat, &Restrict::all(0))
}

/// 対応表（メンバー型の Metric、例: 社員・月 -> 部署）を、その軸の全組み合わせの配列にしたもの。
/// 表の大きさが Metric の件数の 4 倍（少なくとも 2^20）か 2^27 を超えるなら作らない。
pub(crate) struct Table {
    pub(crate) cells: Vec<u32>,              // 組み合わせの番号 -> 行き先のメンバー（なければ u32::MAX）
    pub(crate) strides: Vec<(DimId, usize)>, // 軸と、組み合わせの番号での桁の重み
}

impl Table {
    pub(crate) fn of(v: &Store, target: DimId, cat: &Catalog) -> Option<Table> {
        let dense = combos_in(&v.pack.dims, cat, &Restrict::all(0));
        let rows = v.base_rows().unwrap_or_else(|| v.rows_hint()) as f64;
        if dense > (4.0 * rows).max((1u64 << 20) as f64) || dense > (1u64 << 27) as f64 {
            return None;
        }
        let mut strides = Vec::with_capacity(v.pack.dims.len());
        let mut w = 1usize;
        for &d in v.pack.dims.iter().rev() {
            strides.push((d, w));
            w *= cat.dims[d].size as usize;
        }
        let mut cells = vec![u32::MAX; dense as usize];
        let size = cat.dims[target].size as f64;
        v.for_each(|k, val| {
            if val >= 0.0 && val < size {
                let i: usize = strides.iter().enumerate().map(|(j, &(_, s))| v.pack.get(k, v.pack.dims.len() - 1 - j) as usize * s).sum();
                cells[i] = val as u32;
            }
        });
        Some(Table { cells, strides })
    }
}

/// Aggregate `x[BY agg: D.V]` (the type check makes it Remove(On(x, AsAxis(V, T)), D)) with no join result. For
/// each cell of x, look up its target member of T in the mapping table and add the value to that group.
/// removed are the dimensions of the REMOVE run. If T is one of them, the result has no T.
/// Use it only for a full evaluation with no range limit and a mapping table in stored data. Else returns None.
pub(crate) fn remove_by_table(x: &Rows, removed: &[DimId], target: DimId, v: &Store, aggs: &[Agg], cat: &Catalog, r: &Restrict) -> Result<Option<Vec<Cube>>> {
    let cfg = &cat.cfg;
    let xp = x.pack();
    if removed.iter().any(|d| *d != target && xp.pos(*d).is_none()) || !v.pack.dims.iter().all(|d| xp.pos(*d).is_some()) {
        return Ok(None);
    }
    let keep = (!removed.contains(&target)).then_some(target);
    let dims: Vec<DimId> = xp.dims.iter().copied().filter(|d| !removed.contains(d)).chain(keep).collect();
    if !few_groups(cfg, aggs, combos_in(&dims, cat, r), x.len()) {
        return Ok(None);
    }
    let Some(table) = Table::of(v, target, cat) else { return Ok(None) };
    let out = Packing::new(&dims, cat)?;
    let (proj, pt) = (Proj::new(xp, &out), out.pos(target));
    let at: Vec<(usize, usize)> = table.strides.iter().map(|&(d, s)| (xp.pos(d).unwrap(), s)).collect();
    let groups = fold_groups(cfg, x, |k| {
        let i: usize = at.iter().map(|&(p, s)| xp.get(k, p) as usize * s).sum();
        let t = table.cells[i];
        (t != u32::MAX).then(|| proj.apply(xp, &out, k) | pt.map_or(0, |p| out.put(p, t)))
    });
    Ok(Some(finish_groups(groups, out, aggs)))
}

/// 集計先の全組み合わせの数がこれ以下なら、全組み合わせの配列へ足し込む経路を考える。
const DENSE_MAX: usize = 1 << 26;

/// Add the rows into an array of all the group combinations (all members of each dimension of out). If the
/// arrays are larger than the (group, value) pairs (16 B a row) of the sort path, returns None. For MIN, MAX or
/// COUNT alone, the limit is half of the pairs. key gives the group key (packed as out) of a row key. Gives one
/// Cube for each of aggs (Min, Max and First only alone), in key order.
pub(crate) fn dense_groups(cfg: &Config, rows: &Rows, out: &Packing, cat: &Catalog, aggs: &[Agg], key: impl Fn(u64) -> Option<u64> + Sync) -> Option<Vec<Cube>> {
    let d = Dense { cfg, rows, out, cat, aggs, key };
    match aggs {
        [Agg::First] => None,
        [Agg::Min] => d.fold(i64::MAX, |x, v| *x = (*x).min(min_key(v)), |x, o| *x = (*x).min(*o), |x, _, _| from_key(*x), 8),
        [Agg::Max] => d.fold(i64::MIN, |x, v| *x = (*x).max(max_key(v)), |x, o| *x = (*x).max(*o), |x, _, _| from_key(*x), 8),
        [Agg::Count] => d.fold((), |_, _| {}, |_, _| {}, |_, n, _| n as f64, 8),
        _ => d.fold(
            ExactSum::default(),
            |s, v| s.add(v),
            |s, o| s.merge(o),
            |s, n, agg| match agg {
                Agg::Sum => s.finish(),
                Agg::Avg => s.finish() / n as f64,
                Agg::Low => s.residual(),
                Agg::Count => n as f64,
                Agg::Min | Agg::Max | Agg::First => unreachable!("only alone"),
            },
            16,
        ),
    }
}

struct Dense<'a, K> {
    cfg: &'a Config,
    rows: &'a Rows,
    out: &'a Packing,
    cat: &'a Catalog,
    aggs: &'a [Agg],
    key: K,
}

/// Slots in one bucket of the partitioned fill. 2^14 was the fastest of 2^14, 2^15 and 2^16.
const BUCKET: usize = 1 << 14;

/// The buffers of one part in one round of the partitioned fill.
#[derive(Clone, Default)]
struct Round {
    idx: Vec<u32>,            // the slot of each row (u32::MAX if the row has no group)
    start: Vec<usize>,        // the start of each bucket in pairs
    pairs: Vec<(u32, f64)>,   // the (slot, value) pairs in bucket order
}

/// True if the sampled slot indices touch more bytes of the array than scatter_bytes, in 4 KiB pages. Then
/// each row misses the cache, and the partitioned fill is faster.
fn scattered(sample: impl Iterator<Item = usize>, tuple_bytes: usize, scatter_bytes: usize) -> bool {
    let pages: FxHashSet<usize> = sample.map(|s| s * tuple_bytes / 4096).collect();
    pages.len() * 4096 > scatter_bytes
}

impl<K: Fn(u64) -> Option<u64> + Sync> Dense<'_, K> {
    /// Each slot holds a T and a count (u32). If the arrays are larger than row_bytes for each row, returns None.
    ///
    /// A local access gives one array to each thread, and the arrays merge in order at the end. A scattered
    /// access (see `scattered`) fills one array in rounds. In each round, each thread puts its rows into buckets
    /// of BUCKET slots. Then each bucket adds its rows into its part of the array, which stays in the cache.
    fn fold<T: Clone + Send + Sync>(
        &self,
        init: T,
        add: impl Fn(&mut T, f64) + Sync,
        merge: impl Fn(&mut T, &T),
        finish: impl Fn(&T, u32, Agg) -> f64,
        row_bytes: usize,
    ) -> Option<Vec<Cube>> {
        let (cfg, rows, out) = (self.cfg, self.rows, self.out);
        let n = rows.len();
        let sizes: Vec<usize> = out.dims.iter().map(|&d| self.cat.dims[d].size.max(1) as usize).collect();
        let slots = sizes.iter().try_fold(1usize, |a, &s| a.checked_mul(s).filter(|&x| x <= DENSE_MAX))?;
        let parts = if cfg.par(n) { rayon::current_num_threads().max(1) } else { 1 };
        // group key -> array index (the first dimension is the highest digit, so the index order is the key order)
        let index = |k: u64| -> usize { (0..sizes.len()).fold(0, |i, j| i * sizes[j] + out.get(k, j) as usize) };
        let slot = |k: u64| (self.key)(k).map(index);
        let tuple_bytes = std::mem::size_of::<(T, u32)>();
        let partitioned = slots * tuple_bytes > cfg.scatter_bytes && {
            let mut sample = Vec::new();
            rows.for_each_in(0..n.min(1 << 16), |k, _| sample.extend(slot(k)));
            scattered(sample.into_iter(), tuple_bytes, cfg.scatter_bytes)
        };
        // The partitioned fill has one array, and the buffers of a round have about the memory of 1.25 arrays
        let arrays = if partitioned { 3 } else { parts };
        if !cfg.dense_always && slots.saturating_mul(arrays).saturating_mul(tuple_bytes) > n.saturating_mul(row_bytes) {
            return None;
        }
        let g: Vec<(T, u32)> = if partitioned {
            let mut g = vec![(init, 0u32); slots];
            let buckets = slots.div_ceil(BUCKET);
            // A round has one 16 B pair and one 4 B slot index for each row, in buffers of a fixed size
            let round = (slots * tuple_bytes / 16).max(1);
            let mut bufs = vec![Round::default(); parts];
            let scatter = |lo: usize, hi: usize, Round { idx, start, pairs }: &mut Round| {
                idx.clear();
                rows.for_each_in(lo..hi, |k, _| idx.push(slot(k).map_or(u32::MAX, |s| s as u32)));
                start.clear();
                start.resize(buckets + 1, 0);
                for &s in idx.iter().filter(|&&s| s != u32::MAX) {
                    start[s as usize / BUCKET + 1] += 1;
                }
                for b in 0..buckets {
                    start[b + 1] += start[b];
                }
                pairs.clear();
                pairs.resize(start[buckets], (0, 0.0));
                let mut at = start.clone();
                let mut i = 0;
                rows.for_each_in(lo..hi, |_, v| {
                    let s = idx[i];
                    i += 1;
                    if s != u32::MAX {
                        let b = s as usize / BUCKET;
                        pairs[at[b]] = (s, v);
                        at[b] += 1;
                    }
                });
            };
            for lo in (0..n).step_by(round) {
                let hi = (lo + round).min(n);
                let step = (hi - lo).div_ceil(parts);
                if parts > 1 {
                    bufs.par_iter_mut().enumerate().for_each(|(p, b)| scatter((lo + p * step).min(hi), (lo + (p + 1) * step).min(hi), b));
                } else {
                    scatter(lo, hi, &mut bufs[0]);
                }
                let gather = |(bi, chunk): (usize, &mut [(T, u32)])| {
                    for Round { start, pairs, .. } in &bufs {
                        for &(s, v) in &pairs[start[bi]..start[bi + 1]] {
                            let e = &mut chunk[s as usize - bi * BUCKET];
                            add(&mut e.0, v);
                            e.1 += 1;
                        }
                    }
                };
                if parts > 1 {
                    g.par_chunks_mut(BUCKET).enumerate().for_each(gather);
                } else {
                    g.chunks_mut(BUCKET).enumerate().for_each(gather);
                }
            }
            g
        } else {
            let fill = |lo: usize, hi: usize| {
                let mut g = vec![(init.clone(), 0u32); slots];
                rows.for_each_in(lo..hi, |k, v| {
                    if let Some(s) = slot(k) {
                        add(&mut g[s].0, v);
                        g[s].1 += 1;
                    }
                });
                g
            };
            let step = n.div_ceil(parts).max(1);
            let mut acc: Vec<Vec<(T, u32)>> = if parts > 1 {
                (0..parts).into_par_iter().map(|p| fill((p * step).min(n), ((p + 1) * step).min(n))).collect()
            } else {
                vec![fill(0, n)]
            };
            let mut g = acc.remove(0);
            for g2 in acc {
                for (e, e2) in g.iter_mut().zip(&g2) {
                    if e2.1 > 0 {
                        merge(&mut e.0, &e2.0);
                        e.1 += e2.1;
                    }
                }
            }
            g
        };
        let mut cells = vec![Vec::new(); self.aggs.len()];
        for (s, (x, cnt)) in g.iter().enumerate() {
            if *cnt == 0 {
                continue;
            }
            let (mut rest, mut k) = (s, 0u64);
            for j in (0..sizes.len()).rev() {
                k |= out.put(j, (rest % sizes[j]) as u32);
                rest /= sizes[j];
            }
            push_group(&mut cells, self.aggs, k, |a| finish(x, *cnt, a));
        }
        Some(cubes(out.clone(), cells))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dense_first_only_for_the_full_range_without_stream_always() {
        let dims = (0..2).map(|i| DimInfo { size: 12, ordered: false, name: format!("d{i}") }).collect();
        let mut cat = Catalog { dims, maps: Vec::new(), cfg: Config::default() };
        let all = Restrict::all(2);
        assert!(dense_first(&cat, &[0], &all));
        assert!(dense_first(&cat, &[0], &all.with(1, Sel::new(vec![3], 12))), "a range on a removed dimension");
        assert!(!dense_first(&cat, &[0], &all.with(0, Sel::new(vec![3], 12))), "a narrowed group dimension");
        cat.cfg.stream_always = true;
        assert!(!dense_first(&cat, &[0], &all));
    }

    #[test]
    fn scattered_counts_distinct_pages() {
        // 32 B a slot: 128 slots fill one 4 KiB page
        assert!(!scattered([].into_iter(), 32, 0), "no rows, no pages");
        assert!(!scattered([0, 127, 5].into_iter(), 32, 4096), "1 page is not more than 4096 B");
        assert!(scattered([0, 128].into_iter(), 32, 4096), "2 pages are 8192 B");
        assert!(!scattered([0, 128].into_iter(), 32, 8192));
        assert!(scattered([0, 1, 2, 300].into_iter(), 32, 4096), "pages 0 and 2");
        assert!(scattered([0, 1, 2, 300].into_iter(), 32, 0), "scatter_bytes=0 partitions always");
    }

    /// The partitioned fill, the fill with one array for each thread, and the sort path give the same bits.
    #[test]
    fn dense_paths_give_the_same_bits() {
        let dims = [100u32, 100, 5].iter().enumerate().map(|(i, &size)| DimInfo { size, ordered: false, name: format!("d{i}") }).collect();
        let mut cat = Catalog { dims, maps: Vec::new(), cfg: Config { dense_always: true, ..Config::default() } };
        let out = Packing::new(&[0, 1, 2], &cat).unwrap();
        // An LCG gives scattered slot indices with duplicates. The ±1e16 values make the sum depend on the order of the adds
        let mut x = 12345u64;
        let cells: Vec<(u64, f64)> = (0..200_000)
            .map(|i| {
                x = x.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
                let s = (x >> 33) as usize % 50_000;
                let k = out.put(0, (s / 500) as u32) | out.put(1, (s / 5 % 100) as u32) | out.put(2, (s % 5) as u32);
                let v = if i % 97 == 0 { 1e16 } else if i % 89 == 0 { -1e16 } else { (i % 1000) as f64 * 0.1 };
                (k, v)
            })
            .collect();
        let key = |k: u64| (!k.is_multiple_of(7)).then_some(k);
        let rows = Rows::Owned(Cube { pack: out.clone(), kind: Kind::Num, cells: cells.clone() });
        let bits = |c: Vec<Cube>| -> Vec<Vec<(u64, u64)>> { c.iter().map(|c| c.cells.iter().map(|&(k, v)| (k, v.to_bits())).collect()).collect() };
        for aggs in [vec![Agg::Sum, Agg::Low, Agg::Count, Agg::Avg], vec![Agg::Min], vec![Agg::Max], vec![Agg::Count]] {
            let pairs = cells.iter().filter_map(|&(k, v)| key(k).map(|o| (o, v))).collect();
            let want = bits(group(&cat.cfg, pairs, out.clone(), &aggs));
            assert!(want[0].len() > 40_000, "most slots are used");
            for (par_min, scatter_bytes) in [(0, 0), (0, usize::MAX), (usize::MAX, 0), (usize::MAX, usize::MAX)] {
                cat.cfg.par_min = par_min;
                cat.cfg.scatter_bytes = scatter_bytes;
                let got = bits(dense_groups(&cat.cfg, &rows, &out, &cat, &aggs, key).unwrap());
                assert_eq!(got, want, "{aggs:?} par_min={par_min} scatter_bytes={scatter_bytes}");
            }
        }
    }
}
