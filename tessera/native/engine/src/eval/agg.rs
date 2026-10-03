//! 集計（行を集めて並べ替える集計と、集計先が少ないときの読みながらの集計）。

#[allow(unused_imports)]
use crate::*;

#[derive(Clone, Copy)]
pub(crate) struct Acc {
    pub(crate) sum: f64,
    pub(crate) cnt: u64,
    pub(crate) min: f64,
    pub(crate) max: f64,
    pub(crate) first: f64,
}

impl Acc {
    pub(crate) fn new() -> Acc {
        Acc { sum: 0.0, cnt: 0, min: f64::INFINITY, max: f64::NEG_INFINITY, first: 0.0 }
    }
    pub(crate) fn add(&mut self, v: f64) {
        if self.cnt == 0 {
            self.first = v;
        }
        self.sum += v;
        self.cnt += 1;
        self.min = self.min.min(v);
        self.max = self.max.max(v);
    }
    pub(crate) fn finish(&self, agg: Agg) -> f64 {
        match agg {
            Agg::Sum => self.sum,
            Agg::Avg => self.sum / self.cnt as f64,
            Agg::Min => self.min,
            Agg::Max => self.max,
            Agg::Count => self.cnt as f64,
            Agg::First => self.first,
        }
    }
}

/// (集計先のキー, 値) を並べ替え、同じキーが続く区間ごとに集計する。結果はキー順に並ぶ。
pub(crate) fn group(cfg: &Config, mut pairs: Vec<(u64, f64)>, pack: Packing, agg: Agg) -> Cube {
    sort_cells(cfg, &mut pairs);
    let mut cells = Vec::new();
    let mut i = 0;
    while i < pairs.len() {
        let mut acc = Acc::new();
        let k = pairs[i].0;
        while i < pairs.len() && pairs[i].0 == k {
            acc.add(pairs[i].1);
            i += 1;
        }
        cells.push((k, acc.finish(agg)));
    }
    Cube { pack, kind: Kind::Num, cells }
}

impl Acc {
    pub(crate) fn merge(&mut self, o: &Acc) {
        if o.cnt == 0 {
            return;
        }
        if self.cnt == 0 {
            *self = *o;
            return;
        }
        self.sum += o.sum;
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

/// 集計先がこの数以下なら、集計元を写さずに読みながら足し込む。集計先ごとの途中の値（約 50 B）を
/// スレッドごとに持ち、最後にスレッドの間で合算するので、集計元の件数の 1/(16 × スレッド数) を
/// 上限にする（それより多ければ、合算の手間とメモリが、集計元を (集計先, 値) に写して並べ替える
/// 費用に近づく）。First は並びに意味があるので対象にしない。
pub(crate) fn few_groups(cfg: &Config, agg: Agg, groups: f64, rows: usize) -> bool {
    if matches!(agg, Agg::First) {
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

pub(crate) fn finish_groups(groups: FxHashMap<u64, Acc>, pack: Packing, agg: Agg) -> Cube {
    let mut cells: Vec<(u64, f64)> = groups.into_iter().map(|(k, a)| (k, a.finish(agg))).collect();
    cells.sort_unstable_by_key(|c| c.0);
    Cube { pack, kind: Kind::Num, cells }
}

/// rows から dim を外して集計する（REMOVE）。r は結果の範囲（集計先の数の見積もりに使う）。
pub(crate) fn remove_rows(rows: Rows, dim: DimId, agg: Agg, cat: &Catalog, r: &Restrict) -> Result<Cube> {
    let cfg = &cat.cfg;
    let dims: Vec<DimId> = rows.pack().dims.iter().copied().filter(|d| *d != dim).collect();
    let out = Packing::new(&dims, cat)?;
    if few_groups(cfg, agg, combos_in(&dims, cat, r), rows.len()) {
        let proj = Proj::new(rows.pack(), &out);
        let groups = fold_groups(cfg, &rows, |k| Some(proj.apply(rows.pack(), &out, k)));
        return Ok(finish_groups(groups, out, agg));
    }
    let xp = rows.pack();
    let proj = Proj::new(xp, &out);
    if let Some(c) = dense_groups(cfg, &rows, &out, cat, agg, |k| Some(proj.apply(xp, &out, k))) {
        return Ok(c);
    }
    // 集計元を写さずに、(集計先, 値) の組を 1 つの配列へ直接作って並べ替える
    let pairs = collect_rows(cfg, rows.len(), |i| {
        let (k, v) = rows.get(i);
        Some((proj.apply(xp, &out, k), v))
    });
    drop(rows);
    Ok(group(cfg, pairs, out, agg))
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

/// `x[BY agg: D.V]`（型検査が Remove(On(x, AsAxis(V, T)), D) に書き換えたもの）を、結合の結果を
/// 作らずに集計する。x の各セルについて、対応表から行き先 T のメンバーを引いて足し込む。
/// 範囲の絞り込みがない全体の評価で、対応表が格納データのときだけ使い、使えなければ None。
pub(crate) fn remove_by_table(x: &Rows, dim: DimId, target: DimId, v: &Store, agg: Agg, cat: &Catalog, r: &Restrict) -> Result<Option<Cube>> {
    let cfg = &cat.cfg;
    let xp = x.pack();
    if xp.pos(dim).is_none() || !v.pack.dims.iter().all(|d| xp.pos(*d).is_some()) {
        return Ok(None);
    }
    let dims: Vec<DimId> = xp.dims.iter().copied().filter(|d| *d != dim).chain([target]).collect();
    if !few_groups(cfg, agg, combos_in(&dims, cat, r), x.len()) {
        return Ok(None);
    }
    let Some(table) = Table::of(v, target, cat) else { return Ok(None) };
    let out = Packing::new(&dims, cat)?;
    let (proj, pt) = (Proj::new(xp, &out), out.pos(target).unwrap());
    let at: Vec<(usize, usize)> = table.strides.iter().map(|&(d, s)| (xp.pos(d).unwrap(), s)).collect();
    let groups = fold_groups(cfg, x, |k| {
        let i: usize = at.iter().map(|&(p, s)| xp.get(k, p) as usize * s).sum();
        let t = table.cells[i];
        (t != u32::MAX).then(|| proj.apply(xp, &out, k) | out.put(pt, t))
    });
    Ok(Some(finish_groups(groups, out, agg)))
}

/// 集計先の全組み合わせの数がこれ以下なら、全組み合わせの配列へ足し込む経路を考える。
const DENSE_MAX: usize = 1 << 26;

/// 集計先の全組み合わせ（out の各軸の全メンバー）の配列へ足し込む。仕事は行をスレッド数に区切り、
/// 区切りごとに 1 つの配列（1 組み合わせ 12 B）を持って最後に順に合わせる（スレッド数が同じなら、結果は実行ごとに同じ）。
/// 配列の合計が、(集計先, 値) の組を集めて並べ替える経路の半分（1 行 8 B）を超えるなら None。
/// key は行のキーから集計先のキー（out の詰め方）を返す。結果はキー順に並ぶ。
pub(crate) fn dense_groups(cfg: &Config, rows: &Rows, out: &Packing, cat: &Catalog, agg: Agg, key: impl Fn(u64) -> Option<u64> + Sync) -> Option<Cube> {
    if matches!(agg, Agg::First) {
        return None;
    }
    let n = rows.len();
    let sizes: Vec<usize> = out.dims.iter().map(|&d| cat.dims[d].size.max(1) as usize).collect();
    let slots = sizes.iter().try_fold(1usize, |a, &s| a.checked_mul(s).filter(|&x| x <= DENSE_MAX))?;
    let parts = if cfg.par(n) { rayon::current_num_threads().max(1) } else { 1 };
    if !cfg.dense_always && slots.saturating_mul(parts).saturating_mul(12) > n.saturating_mul(8) {
        return None;
    }
    // 集計先のキー -> 配列の番号（先頭の軸が最上位の桁なので、番号の順がキーの順になる）
    let index = |k: u64| -> usize { (0..sizes.len()).fold(0, |i, j| i * sizes[j] + out.get(k, j) as usize) };
    let init = match agg {
        Agg::Min => f64::INFINITY,
        Agg::Max => f64::NEG_INFINITY,
        _ => 0.0,
    };
    let add = |x: &mut f64, v: f64| match agg {
        Agg::Min => *x = x.min(v),
        Agg::Max => *x = x.max(v),
        Agg::Sum | Agg::Avg => *x += v,
        Agg::Count | Agg::First => {}
    };
    let fill = |lo: usize, hi: usize| {
        let (mut vals, mut cnt) = (vec![init; slots], vec![0u32; slots]);
        for i in lo..hi {
            let (k, v) = rows.get(i);
            if let Some(o) = key(k) {
                let s = index(o);
                add(&mut vals[s], v);
                cnt[s] += 1;
            }
        }
        (vals, cnt)
    };
    let step = n.div_ceil(parts).max(1);
    let mut acc: Vec<(Vec<f64>, Vec<u32>)> = if parts > 1 {
        (0..parts).into_par_iter().map(|p| fill((p * step).min(n), ((p + 1) * step).min(n))).collect()
    } else {
        vec![fill(0, n)]
    };
    let (mut vals, mut cnt) = acc.remove(0);
    for (v2, c2) in acc {
        for s in 0..slots {
            if c2[s] > 0 {
                add(&mut vals[s], v2[s]);
                cnt[s] += c2[s];
            }
        }
    }
    let mut cells = Vec::new();
    for s in 0..slots {
        if cnt[s] == 0 {
            continue;
        }
        let v = match agg {
            Agg::Avg => vals[s] / cnt[s] as f64,
            Agg::Count => cnt[s] as f64,
            _ => vals[s],
        };
        let (mut rest, mut k) = (s, 0u64);
        for j in (0..sizes.len()).rev() {
            k |= out.put(j, (rest % sizes[j]) as u32);
            rest /= sizes[j];
        }
        cells.push((k, v));
    }
    Some(Cube { pack: out.clone(), kind: Kind::Num, cells })
}
