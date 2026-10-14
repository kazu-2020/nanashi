//! 集計（行を集めて並べ替える集計と、集計先が少ないときの読みながらの集計）。

#[allow(unused_imports)]
use crate::*;

use super::exact::ExactSum;

#[derive(Clone)]
pub(crate) struct Acc {
    pub(crate) sum: ExactSum,
    pub(crate) cnt: u64,
    pub(crate) min: f64,
    pub(crate) max: f64,
    pub(crate) first: f64,
}

impl Acc {
    pub(crate) fn new() -> Acc {
        Acc { sum: ExactSum::default(), cnt: 0, min: f64::INFINITY, max: f64::NEG_INFINITY, first: 0.0 }
    }
    pub(crate) fn add(&mut self, v: f64) {
        if self.cnt == 0 {
            self.first = v;
        }
        self.sum.add(v);
        self.cnt += 1;
        self.min = self.min.min(v);
        self.max = self.max.max(v);
    }
    pub(crate) fn finish(&self, agg: Agg) -> f64 {
        match agg {
            Agg::Sum => self.sum.finish(),
            Agg::Avg => self.sum.finish() / self.cnt as f64,
            Agg::Low => self.sum.residual(),
            Agg::Min => self.min,
            Agg::Max => self.max,
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

    #[inline]
    pub(crate) fn get(&self, i: usize) -> (u64, f64) {
        match self {
            Rows::Store(s) => s.base_cell(i),
            Rows::Shared(c) => c.cells[i],
            Rows::Owned(c) => c.cells[i],
        }
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

/// rows を読みながら、key が返す集計先ごとに値を足し込む（件数が多ければ並列に）。
pub(crate) fn fold_groups(cfg: &Config, rows: &Rows, key: impl Fn(u64) -> Option<u64> + Sync) -> FxHashMap<u64, Acc> {
    let add = |mut g: FxHashMap<u64, Acc>, i: usize| {
        let (k, v) = rows.get(i);
        if let Some(o) = key(k) {
            g.entry(o).or_insert_with(Acc::new).add(v);
        }
        g
    };
    let n = rows.len();
    if !cfg.par(n) {
        return (0..n).fold(FxHashMap::default(), add);
    }
    let min_len = (n / rayon::current_num_threads()).max(1); // スレッドごとに 1 つの途中の値の表
    (0..n).into_par_iter().with_min_len(min_len).fold(FxHashMap::default, add).reduce(FxHashMap::default, |mut a, b| {
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
    Ok(aggregate_rows(rows, out, aggs, cat, r, None, move |xp, out, k| Some(proj.apply(xp, out, k))))
}

/// key gives the group key (packed as out) from (row packing, out, row key). digit: see dense_groups. r is the
/// range of the result.
/// The array path is 2 to 3 times faster than the hash map (docs/performance.md), but it ignores r. Thus if r
/// narrows the groups, or with stream_always, the hash map goes first.
pub(crate) fn aggregate_rows(rows: Rows, out: Packing, aggs: &[Agg], cat: &Catalog, r: &Restrict, digit: Option<&[i64]>, key: impl Fn(&Packing, &Packing, u64) -> Option<u64> + Sync) -> Vec<Cube> {
    let cfg = &cat.cfg;
    let xp = rows.pack();
    let k = |x| key(xp, &out, x);
    let groups = combos_in(&out.dims, cat, r);
    let dense_first = dense_first(cat, &out.dims, r);
    if dense_first {
        if let Some(c) = dense_groups(cfg, &rows, &out, cat, aggs, digit, k) {
            return c;
        }
    }
    if few_groups(cfg, aggs, groups, rows.len()) {
        return finish_groups(fold_groups(cfg, &rows, k), out, aggs);
    }
    if !dense_first {
        if let Some(c) = dense_groups(cfg, &rows, &out, cat, aggs, digit, k) {
            return c;
        }
    }
    // Do not copy the source: make the (group, value) pairs directly in one array, then sort them
    let pairs = collect_rows(cfg, rows.len(), |i| {
        let (x, v) = rows.get(i);
        k(x).map(|o| (o, v))
    });
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

/// Add the rows into an array of all the group combinations (all members of each dimension of out). key gives
/// the group key (packed as out) of a row key. digit gives, for each member of the first dimension of the rows,
/// its member of the first dimension of out (or -1 for no group). Without digit, if out keeps the first
/// dimension of the rows, each member is its own digit. With digit, if the rows are in the order of the first
/// dimension, each thread aggregates whole groups and there is no merge (docs/engine.md). Returns None if the
/// arrays are larger than the (group, value) pairs (16 B a row) of the sort path, or half of the pairs for MIN,
/// MAX or COUNT alone. Gives one Cube for each of aggs (Min, Max and First only alone), in key order.
pub(crate) fn dense_groups(cfg: &Config, rows: &Rows, out: &Packing, cat: &Catalog, aggs: &[Agg], digit: Option<&[i64]>, key: impl Fn(u64) -> Option<u64> + Sync) -> Option<Vec<Cube>> {
    let d = Dense { cfg, rows, out, cat, aggs, digit, key };
    match aggs {
        [Agg::First] => None,
        [Agg::Min] => d.fold(f64::INFINITY, |x, v| *x = x.min(v), |x, o| *x = x.min(*o), |x, _, _| *x, 8),
        [Agg::Max] => d.fold(f64::NEG_INFINITY, |x, v| *x = x.max(v), |x, o| *x = x.max(*o), |x, _, _| *x, 8),
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
    digit: Option<&'a [i64]>,
    key: K,
}

/// The rows of one member of the first dimension of the rows: (first row, end, first digit of the slot index).
type Run = (usize, usize, usize);

impl<K: Fn(u64) -> Option<u64> + Sync> Dense<'_, K> {
    /// The runs of the members with a group, in the order of the group digit. None if the rows are not in the
    /// order of their first dimension (an If appends the THEN cells, then the ELSE cells).
    fn runs(&self) -> Option<Vec<Run>> {
        let (rows, xp, n) = (self.rows, self.rows.pack(), self.rows.len());
        let identity: Vec<i64>;
        let digit = match self.digit {
            Some(d) => d,
            None if self.out.dims.first().is_some_and(|d| xp.dims.first() == Some(d)) => {
                identity = (0..self.cat.dims[self.out.dims[0]].size as i64).collect();
                &identity
            }
            None => return None,
        };
        let member = |i: usize| xp.get(rows.get(i).0, 0);
        let in_order = |i: usize| member(i - 1) <= member(i);
        // The base of stored data is in key order
        let ordered = matches!(rows, Rows::Store(_)) || if self.cfg.par(n) { (1..n).into_par_iter().all(in_order) } else { (1..n).all(in_order) };
        if !ordered {
            return None;
        }
        let bound = |m: u32| {
            let (mut lo, mut hi) = (0, n);
            while lo < hi {
                let mid = lo + (hi - lo) / 2;
                if xp.get(rows.get(mid).0, 0) < m {
                    lo = mid + 1
                } else {
                    hi = mid
                }
            }
            lo
        };
        let starts: Vec<usize> = (0..=digit.len() as u32).map(bound).collect();
        // A digit outside out (a bad mapping) must not reach past the slots
        let size = self.cat.dims[self.out.dims[0]].size as i64;
        let mut members: Vec<usize> = (0..digit.len()).filter(|&m| (0..size).contains(&digit[m]) && starts[m] < starts[m + 1]).collect();
        members.sort_by_key(|&m| digit[m]);
        Some(members.iter().map(|&m| (starts[m], starts[m + 1], digit[m] as usize)).collect())
    }

    /// If the arrays are larger than the rows (row_bytes a row), returns None.
    fn fold<T: Clone + Send + Sync>(
        &self,
        init: T,
        add: impl Fn(&mut T, f64) + Sync,
        merge: impl Fn(&mut T, &T),
        finish: impl Fn(&T, u32, Agg) -> f64 + Sync,
        row_bytes: usize,
    ) -> Option<Vec<Cube>> {
        let slot_bytes = std::mem::size_of::<(T, u32)>();
        let (cfg, rows, out) = (self.cfg, self.rows, self.out);
        let n = rows.len();
        let sizes: Vec<usize> = out.dims.iter().map(|&d| self.cat.dims[d].size.max(1) as usize).collect();
        let slots = sizes.iter().try_fold(1usize, |a, &s| a.checked_mul(s).filter(|&x| x <= DENSE_MAX))?;
        let parts = if cfg.par(n) { rayon::current_num_threads().max(1) } else { 1 };
        let arrays = if self.digit.is_some() { slots } else { slots.saturating_mul(parts) };
        if !cfg.dense_always && arrays.saturating_mul(slot_bytes) > n.saturating_mul(row_bytes) {
            return None;
        }
        let runs = self.runs();
        // group key -> array index (the first dimension is the highest digit, so the index order is the key order)
        let index = |k: u64| -> usize { (0..sizes.len()).fold(0, |i, j| i * sizes[j] + out.get(k, j) as usize) };
        // base is the slot index of vals[0]
        let fill = |base: usize, len: usize, ranges: &[(usize, usize)]| {
            let mut vals = vec![(init.clone(), 0u32); len];
            for &(lo, hi) in ranges {
                for i in lo..hi {
                    let (k, v) = rows.get(i);
                    if let Some(o) = (self.key)(k) {
                        let s = &mut vals[index(o) - base];
                        add(&mut s.0, v);
                        s.1 += 1;
                    }
                }
            }
            vals
        };
        let emit = |base: usize, vals: &[(T, u32)]| {
            let mut cells = vec![Vec::new(); self.aggs.len()];
            for (s, (val, cnt)) in vals.iter().enumerate() {
                if *cnt == 0 {
                    continue;
                }
                let (mut rest, mut k) = (base + s, 0u64);
                for j in (0..sizes.len()).rev() {
                    k |= out.put(j, (rest % sizes[j]) as u32);
                    rest /= sizes[j];
                }
                push_group(&mut cells, self.aggs, k, |a| finish(val, *cnt, a));
            }
            cells
        };
        let parts_cells: Vec<Vec<Vec<(u64, f64)>>> = match runs {
            Some(runs) => {
                let block = slots / sizes[0];
                let groups = split_runs(&runs, parts);
                let part = |rs: &[Run]| {
                    let (lo, hi) = (rs[0].2 * block, (rs.last().unwrap().2 + 1) * block);
                    let ranges: Vec<(usize, usize)> = rs.iter().map(|&(lo, hi, _)| (lo, hi)).collect();
                    emit(lo, &fill(lo, hi - lo, &ranges))
                };
                if parts > 1 {
                    groups.par_iter().map(|rs| part(rs)).collect()
                } else {
                    groups.iter().map(|rs| part(rs)).collect()
                }
            }
            None => {
                let step = n.div_ceil(parts).max(1);
                let mut acc: Vec<Vec<(T, u32)>> = if parts > 1 {
                    (0..parts).into_par_iter().map(|p| fill(0, slots, &[((p * step).min(n), ((p + 1) * step).min(n))])).collect()
                } else {
                    vec![fill(0, slots, &[(0, n)])]
                };
                let mut vals = acc.remove(0);
                for v2 in acc {
                    for (a, b) in vals.iter_mut().zip(&v2) {
                        if b.1 > 0 {
                            merge(&mut a.0, &b.0);
                            a.1 += b.1;
                        }
                    }
                }
                vec![emit(0, &vals)]
            }
        };
        let mut cells = vec![Vec::new(); self.aggs.len()];
        for pc in parts_cells {
            for (c, p) in cells.iter_mut().zip(pc) {
                c.extend(p);
            }
        }
        Some(cubes(out.clone(), cells))
    }
}

/// Divide the runs (in digit order) into up to parts groups with about the same number of rows. A group ends
/// only where the digit changes, so the groups have separate slot ranges.
fn split_runs(runs: &[Run], parts: usize) -> Vec<&[Run]> {
    let total: usize = runs.iter().map(|r| r.1 - r.0).sum();
    let (mut groups, mut begin, mut acc) = (Vec::new(), 0, 0);
    for (i, r) in runs.iter().enumerate() {
        acc += r.1 - r.0;
        let last = i + 1 == runs.len();
        if last || (acc * parts >= total * (groups.len() + 1) && runs[i + 1].2 != r.2) {
            groups.push(&runs[begin..=i]);
            begin = i + 1;
        }
    }
    groups
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn split_runs_ends_a_group_only_where_the_digit_changes() {
        // 2 members of digit 0 with 10 rows, then 1 row each of digits 1, 2, 3
        let runs = [(0, 5, 0), (5, 10, 0), (10, 11, 1), (11, 12, 2), (12, 13, 3)];
        let groups: Vec<Vec<Run>> = split_runs(&runs, 4).into_iter().map(|g| g.to_vec()).collect();
        assert_eq!(groups, [vec![(0, 5, 0), (5, 10, 0)], vec![(10, 11, 1)], vec![(11, 12, 2)], vec![(12, 13, 3)]]);
        assert_eq!(split_runs(&runs, 1), [&runs[..]]);
        assert_eq!(split_runs(&runs[..2], 4), [&runs[..2]], "one digit is one group");
        assert!(split_runs(&[], 4).is_empty());
    }

    #[test]
    fn dense_groups_reads_runs_only_from_rows_in_key_order() {
        // Without fuse, If appends the THEN cells (d0 = 3), then the ELSE cells (d0 = 0, 1, 2)
        let dims = (0..2).map(|i| DimInfo { size: 4, ordered: false, name: format!("d{i}") }).collect();
        let mut cat = Catalog { dims, maps: Vec::new(), cfg: Config::default() };
        cat.cfg.dense_always = true;
        cat.cfg.fuse = false;
        let pack = Packing::new(&[0, 1], &cat).unwrap();
        let cells = (0..4).map(|m| (pack.put(0, m) | pack.put(1, 0), [1.0, 2.0, 3.0, 30.0][m as usize])).collect();
        let src = vec![Src::Cube(Arc::new(Cube { pack, kind: Kind::Num, cells }))];
        let cond = Node::Bin(Op::Gt, Box::new(Node::Ref(0)), Box::new(Node::Const(5.0, Kind::Num)), vec![]);
        let branch = Node::If(Box::new(cond), Box::new(Node::Ref(0)), Some(Box::new(Node::Ref(0))), vec![]);
        let node = Node::Remove { child: Box::new(branch), dim: 1, agg: Agg::Sum };
        let got = eval_with(&node, &cat, &src, &Restrict::all(2), &Budget::new(1 << 30)).unwrap();
        let out = Packing::new(&[0], &cat).unwrap();
        assert_eq!(got.cells, [(out.put(0, 0), 1.0), (out.put(0, 1), 2.0), (out.put(0, 2), 3.0), (out.put(0, 3), 30.0)]);
    }

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
}
