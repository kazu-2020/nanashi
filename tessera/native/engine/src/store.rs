//! Store（Metric の格納）。

#[allow(unused_imports)]
use crate::*;

/// Metric の格納。分割軸（index）を先頭（最上位ビット）に詰めた整数キーで持つ。
///
/// 本体はキー順に並んだ配列で、全体の再計算の結果は並べ替えるだけで格納できる。
/// 小さな書き換えは差分の木（delta）に入れ、読むときに本体と突き合わせる。
/// 差分が大きくなったら本体にまとめ直す。分割軸で絞った範囲は、本体の二分探索と
/// 差分の範囲検索で、その範囲の行数だけ読めばよい。
///
/// 本体は作ったら変えず、Arc で版どうし（複製したモデルどうし）で共有する。差分は書き換えても
/// 古い版を壊さない永続的な木で持つ。そのため Store の複製は本体の配列も差分も写さず O(1) で済み、
/// 公開済みの版を読み手が持っていても、書き込みの費用は変わらない。
#[derive(Clone, Debug)]
pub struct Store {
    pub metric_dims: Vec<DimId>, // Metric として宣言した軸の順
    pub pack: Packing,           // 分割軸が先頭
    pub kind: Kind,
    pub(crate) base: Arc<Base>,
    pub(crate) delta: OrdMap<u64, Option<f64>>, // 差分（Some = 値を置く、None = 消す）
    pub(crate) cfg: Config,                      // 作ったときの Catalog の調整値
}

/// 全体を置き換えるための、キー順のセルと、値が変わったセルを囲む範囲（宣言した軸の順の、軸ごとの
/// メンバー番号。何も変わらなければ None）。
pub type Replaced = (Vec<(u64, f64)>, Option<Vec<Vec<u32>>>);

/// Store が確保しているメモリの内訳（Store::memory）。
pub struct Mem {
    pub rows: usize,       // 本体の行数
    pub base: usize,       // 本体のキーと値（バイト）
    pub delta_rows: usize, // 差分の件数
    pub delta: usize,      // 差分（バイト。木の節の分を含まない）
    pub index: usize,      // 分割軸以外の軸の索引（バイト）
}

/// The base of a Store. Nothing changes it after it is made.
///
/// The keys and values are immutable columns with no gaps (`Box<[T]>`, with no extra capacity).
/// The packed u64 keys and f64 values have the same layout as the data of Arrow UInt64 / Float64 arrays.
/// They contain no pointers. Thus a memory-mapped file or shared memory can keep the same layout
/// (step 2 of "Plan" in docs/out-of-core.md). The index (postings) stays in memory.
#[derive(Debug)]
pub(crate) struct Base {
    pub(crate) keys: Box<[u64]>, // 昇順・重複なし
    pub(crate) vals: Box<[f64]>,
    pub(crate) postings: Vec<OnceLock<Arc<Postings>>>, // 分割軸以外の軸の索引（詰め方の位置ごと。必要になったら作る）
}

impl Base {
    pub(crate) fn empty(n_dims: usize) -> Arc<Base> {
        Arc::new(Base { keys: Box::new([]), vals: Box::new([]), postings: fresh_postings(n_dims) })
    }
}

/// 分割軸以外の軸の索引。本体の行番号を、その軸のメンバーごとにまとめたもの（転置索引）。
#[derive(Debug)]
pub(crate) struct Postings {
    pub(crate) offsets: Vec<u32>, // メンバー m の行は rows[offsets[m]..offsets[m + 1]]
    pub(crate) rows: Vec<u32>,
}

impl Postings {
    pub(crate) fn of(&self, m: u32) -> &[u32] {
        let m = m as usize;
        if m + 1 >= self.offsets.len() {
            return &[];
        }
        &self.rows[self.offsets[m] as usize..self.offsets[m + 1] as usize]
    }
}

pub(crate) fn fresh_postings(n: usize) -> Vec<OnceLock<Arc<Postings>>> {
    (0..n).map(|_| OnceLock::new()).collect()
}

impl Store {
    pub fn new(metric_dims: &[DimId], index: Option<DimId>, kind: Kind, cat: &Catalog) -> Result<Store> {
        let mut order: Vec<DimId> = Vec::with_capacity(metric_dims.len());
        if let Some(i) = index {
            if !metric_dims.contains(&i) {
                return Err("分割軸が Metric の軸にない".into());
            }
            order.push(i);
        }
        order.extend(metric_dims.iter().copied().filter(|d| Some(*d) != index));
        Ok(Store {
            metric_dims: metric_dims.to_vec(),
            pack: Packing::new(&order, cat)?,
            kind,
            base: Base::empty(order.len()),
            delta: OrdMap::new(),
            cfg: cat.cfg,
        })
    }

    pub fn index_dim(&self) -> Option<DimId> {
        self.pack.dims.first().copied()
    }

    /// [lo, hi) のセルを、本体と差分を突き合わせながらキー順に渡す。
    pub(crate) fn merged(&self, lo: u64, hi: Option<u64>, mut f: impl FnMut(u64, f64)) {
        let start = self.base.keys.partition_point(|&k| k < lo);
        let end = hi.map_or(self.base.keys.len(), |h| self.base.keys.partition_point(|&k| k < h));
        let upper = hi.map_or(Bound::Unbounded, Bound::Excluded);
        let mut delta = self.delta.range((Bound::Included(lo), upper)).peekable();
        let mut i = start;
        loop {
            let b = (i < end).then(|| self.base.keys[i]);
            match (b, delta.peek().map(|(&k, &v)| (k, v))) {
                (None, None) => break,
                (Some(bk), Some((dk, dv))) if dk <= bk => {
                    delta.next();
                    if dk == bk {
                        i += 1;
                    }
                    if let Some(v) = dv {
                        f(dk, v);
                    }
                }
                (Some(bk), _) => {
                    f(bk, self.base.vals[i]);
                    i += 1;
                }
                (None, Some((dk, dv))) => {
                    delta.next();
                    if let Some(v) = dv {
                        f(dk, v);
                    }
                }
            }
        }
    }

    pub(crate) fn for_each_in(&self, r: &Restrict, mut f: impl FnMut(u64, f64)) {
        let cs = checks(&self.pack, r);
        if let Some(&d0) = self.pack.dims.first() {
            if let Some(sel) = r.get(d0) {
                let shift = self.pack.shifts[0];
                for &m in &sel.members {
                    let lo = (m as u64) << shift;
                    self.merged(lo, lo.checked_add(1u64 << shift), |k, v| {
                        if pass(&self.pack, k, &cs) {
                            f(k, v)
                        }
                    });
                }
                return;
            }
        }
        if let Some((sel, post)) = self.best_postings(r) {
            // 索引でその軸のメンバーの行だけを集める。差分の B 木にある変更は別に突き合わせる
            let mut cells = Vec::new();
            for &m in &sel.members {
                for &row in post.of(m) {
                    let k = self.base.keys[row as usize];
                    if !self.delta.contains_key(&k) && pass(&self.pack, k, &cs) {
                        cells.push((k, self.base.vals[row as usize]));
                    }
                }
            }
            for (&k, &v) in &self.delta {
                if let Some(v) = v {
                    if pass(&self.pack, k, &cs) {
                        cells.push((k, v));
                    }
                }
            }
            cells.sort_unstable_by_key(|c| c.0);
            for (k, v) in cells {
                f(k, v);
            }
            return;
        }
        self.merged(0, None, |k, v| {
            if pass(&self.pack, k, &cs) {
                f(k, v)
            }
        });
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn len(&self) -> usize {
        if self.delta.is_empty() {
            return self.base.keys.len();
        }
        let mut n = 0;
        self.merged(0, None, |_, _| n += 1);
        n
    }

    pub fn read(&self, r: &Restrict) -> Cube {
        if r.is_all() && self.delta.is_empty() {
            let cells = if self.cfg.par(self.base.keys.len()) {
                self.base.keys.par_iter().copied().zip(self.base.vals.par_iter().copied()).collect()
            } else {
                self.base.keys.iter().copied().zip(self.base.vals.iter().copied()).collect()
            };
            return Cube { pack: self.pack.clone(), kind: self.kind, cells };
        }
        let mut cells = Vec::new();
        self.for_each_in(r, |k, v| cells.push((k, v)));
        Cube { pack: self.pack.clone(), kind: self.kind, cells }
    }

    /// 差分がなければ本体の行数（本体の行を番号で直接読める）。差分があれば None。
    pub fn base_rows(&self) -> Option<usize> {
        self.delta.is_empty().then(|| self.base.keys.len())
    }

    /// 本体の i 行目（base_rows が Some のときだけ使う）。
    #[inline]
    pub fn base_cell(&self, i: usize) -> (u64, f64) {
        (self.base.keys[i], self.base.vals[i])
    }

    /// 全セルを、本体と差分を突き合わせながらキー順に渡す。
    pub fn for_each(&self, f: impl FnMut(u64, f64)) {
        self.merged(0, None, f);
    }

    /// 格納データが確保しているメモリ（バイト）。本体はキーと値の列（余分な容量はない）、索引は作ったものだけを
    /// 数える。差分の木は 1 件の大きさ×件数で、木の節の分を含まない（下限）。本体を版どうしで共有していても、
    /// この Store の分として数える。
    pub fn memory(&self) -> Mem {
        let b = &*self.base;
        let index = b.postings.iter().filter_map(|p| p.get()).map(|p| (p.offsets.capacity() + p.rows.capacity()) * 4).sum();
        Mem {
            rows: b.keys.len(),
            base: (b.keys.len() + b.vals.len()) * 8,
            delta_rows: self.delta.len(),
            delta: self.delta.len() * std::mem::size_of::<(u64, Option<f64>)>(),
            index,
        }
    }

    /// 行数のおおよその値（本体と差分の件数の和。差分の上書きや削除を数え直さない）。
    pub fn rows_hint(&self) -> usize {
        self.base.keys.len() + self.delta.len()
    }

    /// 同じ軸と分割軸の空の Store。
    pub fn empty_like(&self) -> Store {
        Store {
            metric_dims: self.metric_dims.clone(),
            pack: self.pack.clone(),
            kind: self.kind,
            base: Base::empty(self.pack.dims.len()),
            delta: OrdMap::new(),
            cfg: self.cfg,
        }
    }

    /// 並んだセルを本体にする（差分は捨てる）。unzip は件数ちょうどの容量で作るので、列にするときに写さない。
    pub(crate) fn set_sorted(&mut self, cells: Vec<(u64, f64)>) {
        let (keys, vals): (Vec<u64>, Vec<f64>) =
            if self.cfg.par(cells.len()) { cells.into_par_iter().unzip() } else { cells.into_iter().unzip() };
        self.base = Arc::new(Base {
            keys: keys.into_boxed_slice(),
            vals: vals.into_boxed_slice(),
            postings: fresh_postings(self.pack.dims.len()),
        });
        self.delta = OrdMap::new();
    }

    /// 詰め方の位置 pos の軸の索引。初めて使うときに計数ソートで作る（本体の行数に比例）。
    pub(crate) fn postings(&self, pos: usize) -> &Postings {
        let base = &*self.base;
        base.postings[pos].get_or_init(|| {
            let size = (self.pack.masks[pos] + 1) as usize;
            let mut offsets = vec![0u32; size + 1];
            for &k in &self.base.keys {
                offsets[self.pack.get(k, pos) as usize + 1] += 1;
            }
            for i in 1..offsets.len() {
                offsets[i] += offsets[i - 1];
            }
            let mut fill = offsets.clone();
            let mut rows = vec![0u32; self.base.keys.len()];
            for (row, &k) in self.base.keys.iter().enumerate() {
                let m = self.pack.get(k, pos) as usize;
                rows[fill[m] as usize] = row as u32;
                fill[m] += 1;
            }
            Arc::new(Postings { offsets, rows })
        })
    }

    /// 分割軸以外の軸で絞られていて、索引を使うと対象が十分減るなら、その軸の位置と索引を返す。
    pub(crate) fn best_postings<'a>(&'a self, r: &'a Restrict) -> Option<(&'a Arc<Sel>, &'a Postings)> {
        let min_rows = self.cfg.postings_min_rows;
        let n = self.base.keys.len();
        if n == 0 || n < min_rows {
            return None;
        }
        let mut best: Option<(usize, &Arc<Sel>, &Postings)> = None;
        for (pos, &d) in self.pack.dims.iter().enumerate().skip(1) {
            let Some(sel) = r.get(d) else { continue };
            if self.pack.masks[pos] >= (1 << 22) {
                continue; // メンバー数が大きすぎる軸には索引を作らない
            }
            let post = self.postings(pos);
            let cand: usize = sel.members.iter().map(|&m| post.of(m).len()).sum();
            if best.is_none_or(|(c, _, _)| cand < c) {
                best = Some((cand, sel, post));
            }
        }
        let (cand, sel, post) = best?;
        (min_rows == 0 || cand * 4 < n).then_some((sel, post))
    }

    /// r の範囲の行だけを持つ Store（同じ軸と分割軸）。
    pub fn slice(&self, r: &Restrict) -> Store {
        let mut out = self.empty_like();
        out.set_sorted(self.read(r).cells);
        out
    }

    /// 差分を本体にまとめ直す。
    pub(crate) fn compact(&mut self) {
        let mut cells = Vec::with_capacity(self.base.keys.len() + self.delta.len());
        self.merged(0, None, |k, v| cells.push((k, v)));
        self.set_sorted(cells);
    }

    pub(crate) fn after_delta(&mut self) {
        if self.delta.len() > self.cfg.compact_min.max(self.base.keys.len() / 8) {
            self.compact();
        }
    }

    /// new を、この Store の詰め方でキー順に並べたセルにする（全体を置き換える前の準備）。new を受け取るので、
    /// 詰め方が同じならセルを写さずに並べ替える。置き換えは with_sorted で行う。
    pub fn sorted_cells(&self, new: Cube) -> Result<Vec<(u64, f64)>> {
        if !same_set(new.dims(), &self.metric_dims) {
            return Err("書き戻す結果の軸が Metric の軸と一致しない".into());
        }
        Ok(sorted(&self.cfg, new.into_repacked(&self.cfg, &self.pack).cells))
    }

    /// 同じ軸と分割軸で、本体をキー順のセル cells にした Store（差分はない）。
    pub fn with_sorted(&self, cells: Vec<(u64, f64)>) -> Store {
        let mut out = self.empty_like();
        out.set_sorted(cells);
        out
    }

    pub fn replace(&mut self, r: &Restrict, new: &Cube) -> Result<()> {
        if !same_set(new.dims(), &self.metric_dims) {
            return Err("書き戻す結果の軸が Metric の軸と一致しない".into());
        }
        let mut new = new.repack(&self.cfg, &self.pack).cells;
        if r.is_all() {
            sort_cells(&self.cfg, &mut new);
            self.set_sorted(new);
            return Ok(());
        }
        let mut old = Vec::new();
        self.for_each_in(r, |k, _| old.push(k));
        sort_cells(&self.cfg, &mut new);
        self.write_back(r, &old, new);
        Ok(())
    }

    /// r の範囲の古いセル（old のキー）を消し、new（キー順）を置く。差分の木に入れると本体に
    /// まとめ直すことになる量なら、差分の木を通さずに、新しい本体を直接作る（差分の木への
    /// 挿入は 1 件ずつなので、大量に入れてからまとめ直すより速い）。
    pub(crate) fn write_back(&mut self, r: &Restrict, old: &[u64], new: Vec<(u64, f64)>) {
        let limit = self.cfg.compact_min.max(self.base.keys.len() / 8);
        if self.delta.len() + old.len() + new.len() > limit {
            let cs = checks(&self.pack, r);
            let mut kept = Vec::with_capacity(self.base.keys.len() + self.delta.len());
            self.merged(0, None, |k, v| {
                if !pass(&self.pack, k, &cs) {
                    kept.push((k, v))
                }
            });
            self.set_sorted(merge_sorted(&kept[..], &new[..], true, |a, b| b.or(a)));
            return;
        }
        for &k in old {
            self.delta.insert(k, None);
        }
        for (k, v) in new {
            self.delta.insert(k, Some(v));
        }
    }

    /// replace と同じだが、値が実際に変わったセルの範囲（宣言した軸の順の、軸ごとのメンバー番号）を
    /// 返す。何も変わらなければ None。下流に伝える影響範囲を、値の変化で絞るために使う。
    pub fn replace_diff(&mut self, r: &Restrict, new: &Cube) -> Result<Option<Vec<Vec<u32>>>> {
        if !same_set(new.dims(), &self.metric_dims) {
            return Err("書き戻す結果の軸が Metric の軸と一致しない".into());
        }
        // 変更前はキー順に読める。変更後も並べて、1 回の走査で突き合わせる
        let mut old = Vec::new();
        self.for_each_in(r, |k, v| old.push((k, v)));
        let new = Cube { pack: self.pack.clone(), kind: new.kind, cells: sorted(&self.cfg, new.repack(&self.cfg, &self.pack).cells) };
        let changed = changed_keys(&old[..], &new.cells);
        // 書き戻し。変更前のキーは上で集めたので、消すためにもう一度走査しない
        if r.is_all() {
            self.set_sorted(new.cells);
        } else {
            let old: Vec<u64> = old.iter().map(|c| c.0).collect();
            self.write_back(r, &old, new.cells);
        }
        Ok(self.region_of(&changed))
    }

    /// 全体を new に置き換えるための、キー順のセル（sorted_cells と同じ）と、値が変わったセルの範囲
    /// （replace_diff と同じ形）。自分は変えないので、置き換える前の Store を複製せずに取っておける。
    pub fn replaced_all(&self, new: Cube) -> Result<Replaced> {
        let cells = self.sorted_cells(new)?;
        let changed = if self.base_rows().is_some() {
            changed_keys(self, &cells) // 差分がなければ、本体を写さずに突き合わせる
        } else {
            let mut old = Vec::with_capacity(self.base.keys.len() + self.delta.len());
            self.merged(0, None, |k, v| old.push((k, v)));
            changed_keys(&old[..], &cells)
        };
        let sets = self.region_of(&changed);
        Ok((cells, sets))
    }

    /// keys を囲む範囲（宣言した軸の順の、軸ごとのメンバー番号）。keys が空なら None。
    pub(crate) fn region_of(&self, keys: &[u64]) -> Option<Vec<Vec<u32>>> {
        if keys.is_empty() {
            return None;
        }
        let sets = self
            .metric_dims
            .iter()
            .map(|d| {
                let p = self.pack.pos(*d).unwrap();
                let mut ms: Vec<u32> = keys.iter().map(|&k| self.pack.get(k, p)).collect();
                ms.sort_unstable();
                ms.dedup();
                ms
            })
            .collect();
        Some(sets)
    }

    /// 同じ格納データ（複製しただけで、どちらにも書き込んでいない）か。
    pub fn same_as(&self, other: &Store) -> bool {
        Arc::ptr_eq(&self.base, &other.base) && self.delta.ptr_eq(&other.delta)
    }

    /// self（変更前）と other（変更後）で値が違うセルの (キー, 変更前, 変更後)。空のセルは None。
    /// キーの詰め方が違えば比べられないので None を返す（呼び出し側が別の方法で比べる）。
    /// 本体を共有していれば差分の木どうしだけを比べ、木の共有している部分も飛ばす。
    #[allow(clippy::type_complexity)]
    pub fn diff_cells(&self, other: &Store) -> Option<Vec<(u64, Option<f64>, Option<f64>)>> {
        if self.pack != other.pack {
            return None;
        }
        let mut out = Vec::new();
        if Arc::ptr_eq(&self.base, &other.base) {
            for item in self.delta.diff(&other.delta) {
                let k = *match item {
                    DiffItem::Add(k, _) | DiffItem::Remove(k, _) | DiffItem::Update { new: (k, _), .. } => k,
                };
                let (a, b) = (self.value_at(k), other.value_at(k));
                if a != b {
                    out.push((k, a, b));
                }
            }
            return Some(out);
        }
        let (mut a, mut b) = (Vec::new(), Vec::new());
        self.merged(0, None, |k, v| a.push((k, v)));
        other.merged(0, None, |k, v| b.push((k, v)));
        let (mut i, mut j) = (0, 0);
        while i < a.len() || j < b.len() {
            let ka = a.get(i).map(|c| c.0);
            let kb = b.get(j).map(|c| c.0);
            match (ka, kb) {
                (Some(x), Some(y)) if x == y => {
                    if a[i].1 != b[j].1 {
                        out.push((x, Some(a[i].1), Some(b[j].1)));
                    }
                    i += 1;
                    j += 1;
                }
                (Some(x), Some(y)) if x < y => {
                    out.push((x, Some(a[i].1), None));
                    i += 1;
                }
                (Some(x), None) => {
                    out.push((x, Some(a[i].1), None));
                    i += 1;
                }
                (_, Some(y)) => {
                    out.push((y, None, Some(b[j].1)));
                    j += 1;
                }
                (None, None) => break,
            }
        }
        Some(out)
    }

    /// キー k のセルの値（空なら None）。
    pub(crate) fn value_at(&self, k: u64) -> Option<f64> {
        match self.delta.get(&k) {
            Some(v) => *v,
            None => self.base.keys.binary_search(&k).ok().map(|i| self.base.vals[i]),
        }
    }

    /// 1 セルの値（宣言した軸の順のメンバー番号）。空なら None。二分探索と差分の木の検索で済む。
    pub fn get(&self, key: &[u32]) -> Option<f64> {
        self.value_at(self.encode(key))
    }

    /// r の範囲の行を、宣言した軸の順に各軸のメンバーの並び順で並べ、offset 件目から limit 件だけ返す
    /// （軸ごとのメンバー番号の列、値の列、範囲の全行数）。ranks は宣言した軸ごとの「番号 -> 並び順の
    /// 位置」の表で、None の軸は番号の順に並ぶ。どの軸も番号の順で、分割軸が宣言の先頭なら並べ直さずに済む。
    pub fn rows_in(&self, r: &Restrict, ranks: &[Option<Vec<u32>>], offset: usize, limit: Option<usize>) -> (Vec<Vec<u32>>, Vec<f64>, usize) {
        let mut cells = Vec::new();
        self.for_each_in(r, |k, v| cells.push((k, v)));
        let pos: Vec<usize> = self.metric_dims.iter().map(|d| self.pack.pos(*d).unwrap()).collect();
        if pos.windows(2).any(|w| w[0] > w[1]) || ranks.iter().any(Option::is_some) {
            let rank = |i: usize, m: u32| ranks.get(i).and_then(Option::as_ref).map_or(m, |t| t[m as usize]);
            cells.sort_by_cached_key(|c| pos.iter().enumerate().map(|(i, &p)| rank(i, self.pack.get(c.0, p))).collect::<Vec<u32>>());
        }
        let total = cells.len();
        let start = offset.min(total);
        let end = limit.map_or(total, |l| start.saturating_add(l).min(total));
        let page = &cells[start..end];
        let mut cols = vec![Vec::with_capacity(page.len()); pos.len()];
        let mut values = Vec::with_capacity(page.len());
        for &(k, v) in page {
            for (c, &p) in cols.iter_mut().zip(&pos) {
                c.push(self.pack.get(k, p));
            }
            values.push(v);
        }
        (cols, values, total)
    }

    /// キーを、宣言した軸の順のメンバー番号に直す。
    pub fn decode(&self, k: u64) -> Vec<u32> {
        self.metric_dims.iter().map(|d| self.pack.get(k, self.pack.pos(*d).unwrap())).collect()
    }

    /// 値が v のセルを消す（メンバー型の Metric で、消すメンバーを指す値を空にする）。
    pub fn drop_value(&mut self, v: f64) {
        let mut cells = Vec::with_capacity(self.base.keys.len() + self.delta.len());
        self.merged(0, None, |k, x| {
            if x != v {
                cells.push((k, x))
            }
        });
        self.set_sorted(cells);
    }

    /// 値が v のセルを囲む範囲。なければ None（メンバー型の Metric で、消すメンバーを指すセルを探す）。
    pub fn region_of_value(&self, v: f64) -> Option<Vec<Vec<u32>>> {
        let mut keys = Vec::new();
        self.merged(0, None, |k, x| {
            if x == v {
                keys.push(k)
            }
        });
        self.region_of(&keys)
    }

    pub(crate) fn encode(&self, key: &[u32]) -> u64 {
        debug_assert_eq!(key.len(), self.metric_dims.len(), "キーの長さが軸の数と合わない");
        let mut out = 0;
        for (d, &m) in self.metric_dims.iter().zip(key) {
            out |= self.pack.put(self.pack.pos(*d).unwrap(), m);
        }
        out
    }

    pub fn write(&mut self, key: &[u32], value: Option<f64>) {
        let k = self.encode(key);
        self.delta.insert(k, value);
        self.after_delta();
    }

    /// まとめて書き込む。cols は宣言した軸の順のメンバー番号の列、values の None は消す。
    /// 同じセルが複数あれば後のものが勝つ。差分に入れると本体にまとめ直すことになる量なら
    /// （write_back と同じ基準）、差分に 1 件ずつ入れずに本体を作り直す。
    pub fn write_many(&mut self, cols: &[&[u32]], values: &[Option<f64>]) {
        let limit = self.cfg.compact_min.max(self.base.keys.len() / 8);
        if self.delta.len() + values.len() <= limit {
            let mut key = vec![0; cols.len()];
            for (i, v) in values.iter().enumerate() {
                for (k, c) in key.iter_mut().zip(cols) {
                    *k = c[i];
                }
                self.write(&key, *v);
            }
            return;
        }
        let pos: Vec<usize> = self.metric_dims.iter().map(|d| self.pack.pos(*d).unwrap()).collect();
        let pack = &self.pack;
        let mut cells: Vec<(u64, Option<f64>)> = (0..values.len())
            .into_par_iter()
            .map(|i| {
                let mut k = 0;
                for (c, &p) in cols.iter().zip(&pos) {
                    k |= pack.put(p, c[i]);
                }
                (k, values[i])
            })
            .collect();
        cells.par_sort_by_key(|c| c.0); // 安定なので、同じキーは元の順に並ぶ
        let mut updates: Vec<(u64, Option<f64>)> = Vec::with_capacity(cells.len());
        for c in cells {
            match updates.last_mut() {
                Some(last) if last.0 == c.0 => *last = c,
                _ => updates.push(c),
            }
        }
        let mut out = Vec::with_capacity(self.base.keys.len() + self.delta.len() + updates.len());
        let mut i = 0;
        self.merged(0, None, |k, v| {
            while i < updates.len() && updates[i].0 < k {
                if let Some(x) = updates[i].1 {
                    out.push((updates[i].0, x));
                }
                i += 1;
            }
            if i < updates.len() && updates[i].0 == k {
                if let Some(x) = updates[i].1 {
                    out.push((k, x));
                }
                i += 1;
            } else {
                out.push((k, v));
            }
        });
        out.extend(updates[i..].iter().filter_map(|&(k, v)| v.map(|x| (k, x))));
        self.set_sorted(out);
    }

    /// 宣言した軸の順のメンバー番号の列と、値の列。
    pub fn rows(&self) -> (Vec<Vec<u32>>, Vec<f64>) {
        let pos: Vec<usize> = self.metric_dims.iter().map(|d| self.pack.pos(*d).unwrap()).collect();
        let mut cols = vec![Vec::new(); pos.len()];
        let mut values = Vec::new();
        self.merged(0, None, |k, v| {
            for (c, &p) in cols.iter_mut().zip(&pos) {
                c.push(self.pack.get(k, p));
            }
            values.push(v);
        });
        (cols, values)
    }

    pub fn with_rows(mut self, cols: &[&[u32]], values: &[f64]) -> Result<Store> {
        if cols.len() != self.metric_dims.len() {
            return Err("列の数が軸の数と合わない".into());
        }
        let pos: Vec<usize> = self.metric_dims.iter().map(|d| self.pack.pos(*d).unwrap()).collect();
        let pack = &self.pack;
        let mut cells: Vec<(u64, f64)> = (0..values.len())
            .into_par_iter()
            .map(|i| {
                let mut k = 0;
                for (c, &p) in cols.iter().zip(&pos) {
                    k |= pack.put(p, c[i]);
                }
                (k, values[i])
            })
            .collect();
        cells.par_sort_unstable_by_key(|c| c.0);
        self.set_sorted(cells);
        Ok(self)
    }

    pub fn as_cube(&self) -> Cube {
        self.read(&Restrict::all(0))
    }

    /// 軸 dim のメンバー m のセルを消し、後ろのメンバーの番号を 1 つずつ詰める。values なら値も
    /// dim のメンバー番号として扱い、m を指す値は消し、後ろの番号を詰める。
    ///
    /// 番号の詰め方は残るメンバーの順序を保つので、キーの順序も保たれ、並べ直さずに済む。
    pub fn remove_member(&mut self, dim: DimId, m: u32, values: bool) {
        let pos = self.pack.pos(dim);
        let pack = &self.pack;
        let target = m as f64;
        let mut cells = Vec::with_capacity(self.base.keys.len() + self.delta.len());
        self.merged(0, None, |mut k, mut v| {
            if let Some(p) = pos {
                let c = pack.get(k, p);
                if c == m {
                    return;
                }
                if c > m {
                    k = pack.clear(k, p) | pack.put(p, c - 1);
                }
            }
            if values {
                if v == target {
                    return;
                }
                if v > target {
                    v -= 1.0;
                }
            }
            cells.push((k, v));
        });
        debug_assert!(cells.windows(2).all(|w| w[0].0 < w[1].0));
        self.set_sorted(cells);
    }

    /// 同じ軸・分割軸で、今のメンバー数に合わせて詰め直した Store。
    pub fn repacked(&self, cat: &Catalog) -> Result<Store> {
        let mut out = Store::new(&self.metric_dims, self.index_dim(), self.kind, cat)?;
        out.replace(&Restrict::all(cat.dims.len()), &self.as_cube())?;
        Ok(out)
    }
}

pub(crate) fn same_set(a: &[DimId], b: &[DimId]) -> bool {
    a.len() == b.len() && a.iter().all(|d| b.contains(d))
}

/// 並んだ 2 つのセル列で、値が違うか片側にしかないキー。
fn changed_keys<A: Cells + ?Sized>(old: &A, new: &[(u64, f64)]) -> Vec<u64> {
    merge_sorted(old, new, true, |a, b| (a != b).then_some(0.0)).into_iter().map(|(k, _)| k).collect()
}
