//! Store（Metric の格納）。

#[allow(unused_imports)]
use crate::*;
use crate::packed::BLOCK;
use std::ops::Range;

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
    pub(crate) delta: OrdMap<u64, Slot>, // The delta (Some sets a value, None removes the cell)
    pub(crate) cfg: Config,               // 作ったときの Catalog の調整値
}

/// A value in the delta tree. The tree diff compares values with `==`, so equality is by bits:
/// a change from 0.0 to -0.0 is then a change.
#[derive(Clone, Copy, Debug)]
pub(crate) struct Slot(pub(crate) Option<f64>);

impl PartialEq for Slot {
    fn eq(&self, other: &Self) -> bool {
        self.0.map(f64::to_bits) == other.0.map(f64::to_bits)
    }
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

impl Mem {
    pub fn bytes(&self) -> usize {
        self.base + self.delta + self.index
    }
}

/// The base of a Store. Nothing changes it after it is made.
///
/// The keys are packed in frame-of-reference blocks (`PackedKeys`). The values are an immutable column
/// with no gaps (`Box<[f64]>`), with the same layout as the data of an Arrow Float64 array.
/// The index (postings) stays in memory.
#[derive(Debug)]
pub(crate) struct Base {
    pub(crate) keys: PackedKeys, // ascending, no duplicates
    pub(crate) vals: Box<[f64]>,
    pub(crate) postings: Vec<OnceLock<Arc<Postings>>>, // 分割軸以外の軸の索引（詰め方の位置ごと。必要になったら作る）
}

impl Base {
    pub(crate) fn empty(n_dims: usize) -> Arc<Base> {
        Arc::new(Base { keys: PackedKeys::new(&[], false), vals: Box::new([]), postings: fresh_postings(n_dims) })
    }

    /// The keys are decoded one block at a time (`PackedKeys::range`).
    pub(crate) fn range(&self, r: Range<usize>) -> impl Iterator<Item = (u64, f64)> + '_ {
        self.keys.range(r.clone()).zip(self.vals[r].iter().copied())
    }
}

/// The index of a dimension other than the partition dimension: the base rows of each member (an inverted index).
#[derive(Debug)]
pub(crate) struct Postings {
    pub(crate) offsets: Vec<u32>, // the entries of member m are entries[offsets[m]..offsets[m + 1]]
    pub(crate) entries: PackedKeys, // `m * n + row` for each base row, sorted
    pub(crate) n: u64,              // the number of base rows
}

impl Postings {
    /// The positions in `entries` of member `m`.
    pub(crate) fn span(&self, m: u32) -> Range<usize> {
        let m = m as usize;
        if m + 1 >= self.offsets.len() {
            return 0..0;
        }
        self.offsets[m] as usize..self.offsets[m + 1] as usize
    }

    /// The base rows of member `m`, in ascending order.
    pub(crate) fn of(&self, m: u32) -> impl Iterator<Item = u32> + '_ {
        let first = m as u64 * self.n;
        self.entries.range(self.span(m)).map(move |k| (k - first) as u32)
    }
}

pub(crate) fn fresh_postings(n: usize) -> Vec<OnceLock<Arc<Postings>>> {
    (0..n).map(|_| OnceLock::new()).collect()
}

impl Store {
    pub fn new(metric_dims: &[DimId], index: Option<DimId>, kind: Kind, cat: &Catalog) -> Result<Store> {
        let mut order = metric_dims.to_vec();
        if let Some(i) = index {
            let Some(p) = order.iter().position(|&d| d == i) else {
                return Err("分割軸が Metric の軸にない".into());
            };
            order[..=p].rotate_right(1); // move only the first copy of a repeated dimension
        }
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

    /// Gives the cells in [lo, hi) in key order, merged from the base and the delta.
    pub(crate) fn merged(&self, lo: u64, hi: Option<u64>, mut f: impl FnMut(u64, f64)) {
        let start = self.base.keys.partition_point(|k| k < lo);
        let end = hi.map_or(self.base.keys.len(), |h| self.base.keys.partition_point(|k| k < h));
        let upper = hi.map_or(Bound::Unbounded, Bound::Excluded);
        let mut delta = self.delta.range((Bound::Included(lo), upper)).peekable();
        let mut base = self.base.range(start..end).peekable();
        while let Some(&(bk, bv)) = base.peek() {
            match delta.peek() {
                Some(&(&dk, &Slot(dv))) if dk <= bk => {
                    delta.next();
                    if dk == bk {
                        base.next();
                    }
                    if let Some(v) = dv {
                        f(dk, v);
                    }
                }
                _ => {
                    f(bk, bv);
                    base.next();
                }
            }
        }
        for (&dk, &Slot(dv)) in delta {
            if let Some(v) = dv {
                f(dk, v);
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
            // Collect only the rows of the selected members with the index. Match the changes in the delta tree separately
            let mut cells = Vec::new();
            for &m in &sel.members {
                for row in post.of(m) {
                    let k = self.base.keys.get(row as usize);
                    if !self.delta.contains_key(&k) && pass(&self.pack, k, &cs) {
                        cells.push((k, self.base.vals[row as usize]));
                    }
                }
            }
            for (&k, &Slot(v)) in &self.delta {
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
            // Each block is decoded once, in parallel on a large base.
            let mut cells = vec![(0u64, 0f64); self.base.keys.len()];
            let fill = |(b, out): (usize, &mut [(u64, f64)])| {
                let rows = self.base.range(b * BLOCK..b * BLOCK + out.len());
                for (c, row) in out.iter_mut().zip(rows) {
                    *c = row;
                }
            };
            if self.cfg.par(cells.len()) {
                cells.par_chunks_mut(BLOCK).enumerate().for_each(fill);
            } else {
                cells.chunks_mut(BLOCK).enumerate().for_each(fill);
            }
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

    /// The rows `r` of stored data without a delta, in order (the base rows are the cells). It decodes the
    /// keys one block at a time, so it is much faster than `PackedKeys::get` for each row.
    pub(crate) fn base_range(&self, r: Range<usize>) -> impl Iterator<Item = (u64, f64)> + '_ {
        assert!(self.base_rows().is_some(), "差分のある格納データは番号で読めない");
        self.base.range(r)
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
        let index = b.postings.iter().filter_map(|p| p.get()).map(|p| p.offsets.capacity() * 4 + p.entries.bytes()).sum();
        Mem {
            rows: b.keys.len(),
            base: b.keys.bytes() + b.vals.len() * 8,
            delta_rows: self.delta.len(),
            delta: self.delta.len() * std::mem::size_of::<(u64, Option<f64>)>(),
            index,
        }
    }

    /// 行数のおおよその値（本体と差分の件数の和。差分の上書きや削除を数え直さない）。
    pub fn rows_hint(&self) -> usize {
        self.base.keys.len() + self.delta.len()
    }

    fn slots(&self) -> Vec<usize> {
        (0..self.metric_dims.len()).map(|j| self.pack.slot_of(&self.metric_dims, j).unwrap()).collect()
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

    /// Make the sorted cells the base and drop the delta.
    /// Use the buffer of `cells` for the keys, so that only one new column is made.
    pub(crate) fn set_sorted(&mut self, cells: Vec<(u64, f64)>) {
        let vals: Vec<f64> =
            if self.cfg.par(cells.len()) { cells.par_iter().map(|c| c.1).collect() } else { cells.iter().map(|c| c.1).collect() };
        let keys = keys_in_place(cells);
        self.set_columns(PackedKeys::new(&keys, self.cfg.par(keys.len())), vals);
    }

    /// Make the packed sorted keys and the values the base and drop the delta.
    fn set_columns(&mut self, keys: PackedKeys, vals: Vec<f64>) {
        self.base = Arc::new(Base {
            keys,
            vals: vals.into_boxed_slice(),
            postings: fresh_postings(self.pack.dims.len()),
        });
        self.delta = OrdMap::new();
    }

    /// The index of the dimension at packing position pos. The first use makes it with a counting sort (linear in the base rows).
    /// The build is sequential: a rayon wait inside `get_or_init` can run a task that waits on the same cell.
    pub(crate) fn postings(&self, pos: usize) -> &Postings {
        let base = &*self.base;
        base.postings[pos].get_or_init(|| {
            let size = (self.pack.masks[pos] + 1) as usize;
            let mut offsets = vec![0u32; size + 1];
            for k in self.base.keys.iter() {
                offsets[self.pack.get(k, pos) as usize + 1] += 1;
            }
            for i in 1..offsets.len() {
                offsets[i] += offsets[i - 1];
            }
            let mut fill = offsets.clone();
            let n = self.base.keys.len() as u64; // best_postings makes sure that n fits in u32
            // Hold the rows as u32 and pack the u64 entries one at a time. Then the build needs 4 B for each row, not 8 B.
            let mut rows = vec![0u32; n as usize];
            for (row, k) in self.base.keys.iter().enumerate() {
                let m = self.pack.get(k, pos) as usize;
                rows[fill[m] as usize] = row as u32;
                fill[m] += 1;
            }
            drop(fill);
            let entries = || {
                offsets.windows(2).enumerate().flat_map(|(m, w)| rows[w[0] as usize..w[1] as usize].iter().map(move |&row| m as u64 * n + row as u64))
            };
            let mut packed = PackedKeys::builder_with_bytes(n as usize, packed::packed_bytes(entries()));
            entries().for_each(|e| packed.push(e));
            Arc::new(Postings { offsets, entries: packed.finish(), n })
        })
    }

    /// If r filters a dimension other than the partition dimension and its index makes the rows much fewer,
    /// return the selection and the index of that dimension.
    pub(crate) fn best_postings<'a>(&'a self, r: &'a Restrict) -> Option<(&'a Arc<Sel>, &'a Postings)> {
        let min_rows = self.cfg.postings_min_rows;
        let n = self.base.keys.len();
        if n == 0 || n < min_rows || n > u32::MAX as usize {
            return None;
        }
        let mut best: Option<(usize, &Arc<Sel>, &Postings)> = None;
        for (pos, &d) in self.pack.dims.iter().enumerate().skip(1) {
            let Some(sel) = r.get(d) else { continue };
            if self.pack.masks[pos] >= (1 << 22) {
                continue; // no index for a dimension with too many members
            }
            let post = self.postings(pos);
            let cand: usize = sel.members.iter().map(|&m| post.span(m).len()).sum();
            if best.is_none_or(|(c, _, _)| cand < c) {
                best = Some((cand, sel, post));
            }
        }
        let (cand, sel, post) = best?;
        (min_rows == 0 || cand * 4 < n).then_some((sel, post))
    }

    /// A Store with only the rows in r (the same dimensions and partition dimension).
    pub fn slice(&self, r: &Restrict) -> Store {
        // A Store with a delta must compact here: aggregation streams only a Store without a delta.
        if r.is_all() && self.delta.is_empty() {
            return self.clone();
        }
        let mut out = self.empty_like();
        out.set_sorted(self.read(r).cells);
        out
    }

    /// The most rows of the delta. With more rows, the delta goes into a new base.
    fn delta_limit(&self) -> usize {
        self.cfg.compact_min.max(self.base.keys.len() / 8)
    }

    /// The most cells that `replace_diff` (if `diff`) or `replace` needs at its peak to write `n` new cells into a
    /// region with at most `old` cells. A list of keys or cells can grow to 2 times its length.
    pub(crate) fn part_write_cells(&self, old: usize, n: usize, diff: bool) -> usize {
        // replace_diff keeps the old cells, their keys and the changed keys. replace keeps only the old keys
        let lists = if diff { old * 2 + old / 2 + (old + n) } else { old };
        // The new rows of the delta tree (a probe measured 57 bytes, about 4 cells, for each row), or new base
        // columns (about 1 cell for each row). shortcut: a delta tree that another store shares is copied when it
        // changes, and this does not count the copy. Count it if a probe finds such a copy over the budget.
        let (rows, limit) = (self.delta.len().saturating_add(old).saturating_add(n), self.delta_limit());
        let tail = if rows <= limit { (old + n) * 4 } else { self.rows_hint().saturating_add(n).max(limit * 4) };
        lists.saturating_add(n).saturating_add(tail)
    }

    /// Merge the delta into a new base.
    pub(crate) fn compact(&mut self) {
        let n = self.base.keys.len() + self.delta.len();
        let (mut keys, mut vals) = (PackedKeys::builder(n, &self.base.keys), Vec::with_capacity(n));
        self.merged(0, None, |k, v| {
            keys.push(k);
            vals.push(v);
        });
        self.set_columns(keys.finish(), vals);
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

    /// Remove the old cells in r (the keys in old) and put new (in key order). If the delta tree would then
    /// need a compaction, make a new base directly. The delta tree inserts one cell at a time, so this is faster.
    pub(crate) fn write_back(&mut self, r: &Restrict, old: &[u64], new: Vec<(u64, f64)>) {
        let limit = self.delta_limit();
        if self.delta.len() + old.len() + new.len() > limit {
            // Write directly into the new packed base columns. An intermediate cell array or a plain key
            // array adds to the peak memory. A new cell replaces a kept cell with the same key.
            let cs = checks(&self.pack, r);
            let n = (self.base.keys.len() + self.delta.len() + new.len()).saturating_sub(old.len());
            let (mut keys, mut vals) = (PackedKeys::builder(n, &self.base.keys), Vec::with_capacity(n));
            let mut push = |(k, v): (u64, f64)| {
                keys.push(k);
                vals.push(v);
            };
            let mut new = new.into_iter().peekable();
            self.merged(0, None, |k, v| {
                while let Some(c) = new.next_if(|c| c.0 < k) {
                    push(c);
                }
                if !pass(&self.pack, k, &cs) && new.peek().is_none_or(|c| c.0 != k) {
                    push((k, v));
                }
            });
            new.for_each(push);
            self.set_columns(keys.finish(), vals);
            return;
        }
        for &k in old {
            self.delta.insert(k, Slot(None));
        }
        for (k, v) in new {
            self.delta.insert(k, Slot(Some(v)));
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
            .slots()
            .into_iter()
            .map(|p| {
                let mut ms: Vec<u32> = keys.iter().map(|&k| self.pack.get(k, p)).collect();
                ms.sort_unstable();
                ms.dedup();
                ms.shrink_to_fit(); // the sets wait in plan::Done until the write, so do not keep room for all keys
                ms
            })
            .collect();
        Some(sets)
    }

    /// 同じ格納データ（複製しただけで、どちらにも書き込んでいない）か。
    pub fn same_as(&self, other: &Store) -> bool {
        Arc::ptr_eq(&self.base, &other.base) && self.delta.ptr_eq(&other.delta)
    }

    /// The cells with different bits in self (before) and other (after), as (key, before, after). A blank cell is None.
    /// If the key packings are different, it returns None (the caller compares in a different way).
    /// If the two share the base, it compares only the delta trees and skips their shared parts.
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
                if a.map(f64::to_bits) != b.map(f64::to_bits) {
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
                    if a[i].1.to_bits() != b[j].1.to_bits() {
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
            Some(v) => v.0,
            None => self.base.keys.find(k).map(|i| self.base.vals[i]),
        }
    }

    /// 1 セルの値（宣言した軸の順のメンバー番号）。空なら None。二分探索と差分の木の検索で済む。
    pub fn get(&self, key: &[u32]) -> Option<f64> {
        self.value_at(self.encode(key))
    }

    /// Give at most limit rows in r, from row offset. The rows sort by the declared dimensions, and in each dimension
    /// by the member order. ranks gives the position of each member number for each declared dimension. A dimension
    /// with None sorts by member number. The result has the member number columns in the declared dimension order,
    /// the values, and the number of rows in r.
    pub fn rows_in(&self, r: &Restrict, ranks: &[Option<Vec<u32>>], offset: usize, limit: Option<usize>) -> (Vec<Vec<u32>>, Vec<f64>, usize) {
        let pos = self.slots();
        let want = limit.map_or(usize::MAX, |l| offset.saturating_add(l));
        let (mut cells, mut total) = (Vec::new(), 0);
        if pos.windows(2).any(|w| w[0] > w[1]) || ranks.iter().any(Option::is_some) {
            let rank = |i: usize, m: u32| ranks.get(i).and_then(Option::as_ref).map_or(m, |t| t[m as usize]);
            let order = |a: &(u64, f64), b: &(u64, f64)| {
                pos.iter().enumerate().map(|(i, &p)| rank(i, self.pack.get(a.0, p)).cmp(&rank(i, self.pack.get(b.0, p)))).find(|o| o.is_ne()).unwrap_or(std::cmp::Ordering::Equal)
            };
            // Keep only the first `want` rows, so that a short page does not copy and sort the whole range.
            self.for_each_in(r, |k, v| {
                total += 1;
                if want > 0 {
                    cells.push((k, v));
                    if cells.len() == want.saturating_mul(2) {
                        cells.select_nth_unstable_by(want - 1, order);
                        cells.truncate(want);
                    }
                }
            });
            cells.sort_unstable_by(order);
            cells.truncate(want);
            cells.drain(..offset.min(cells.len()));
        } else {
            self.for_each_in(r, |k, v| {
                if (offset..want).contains(&total) {
                    cells.push((k, v));
                }
                total += 1;
            });
        }
        let mut cols = vec![Vec::with_capacity(cells.len()); pos.len()];
        let mut values = Vec::with_capacity(cells.len());
        for (k, v) in cells {
            for (c, &p) in cols.iter_mut().zip(&pos) {
                c.push(self.pack.get(k, p));
            }
            values.push(v);
        }
        (cols, values, total)
    }

    /// キーを、宣言した軸の順のメンバー番号に直す。
    pub fn decode(&self, k: u64) -> Vec<u32> {
        self.slots().into_iter().map(|p| self.pack.get(k, p)).collect()
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
        for (j, &m) in key.iter().enumerate() {
            out |= self.pack.put(self.pack.slot_of(&self.metric_dims, j).unwrap(), m);
        }
        out
    }

    pub fn write(&mut self, key: &[u32], value: Option<f64>) {
        let k = self.encode(key);
        self.delta.insert(k, Slot(value));
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
        let pos = self.slots();
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
        let pos = self.slots();
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
        let pos = self.slots();
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
        if cells.windows(2).any(|w| w[0].0 == w[1].0) {
            return Err("同じセルが 2 回ある".into());
        }
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
        let pack = &self.pack;
        let pos: Vec<usize> = (0..pack.dims.len()).filter(|&p| pack.dims[p] == dim).collect();
        let target = m as f64;
        let mut cells = Vec::with_capacity(self.base.keys.len() + self.delta.len());
        self.merged(0, None, |mut k, mut v| {
            for &p in &pos {
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

/// The standard library collects the keys into the buffer of `cells` (in-place iteration).
/// It does not promise this, so a test checks it.
fn keys_in_place(cells: Vec<(u64, f64)>) -> Vec<u64> {
    cells.into_iter().map(|c| c.0).collect()
}

/// The keys that are in one sorted cell list only, or that have different bits (-0.0 is not 0.0).
fn changed_keys<A: Cells + ?Sized>(old: &A, new: &[(u64, f64)]) -> Vec<u64> {
    merge_sorted(old, new, true, |a, b| (a.map(f64::to_bits) != b.map(f64::to_bits)).then_some(0.0)).into_iter().map(|(k, _)| k).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys_reuse_the_buffer_of_the_cells() {
        let cells: Vec<(u64, f64)> = (0..1000).map(|i| (i, i as f64)).collect();
        let ptr = cells.as_ptr().cast::<u64>();
        let keys = keys_in_place(cells);
        assert_eq!(keys.as_ptr(), ptr);
        assert_eq!(keys, (0..1000).collect::<Vec<u64>>());
    }

    #[test]
    fn postings_give_the_rows_of_each_member() {
        let dims = (0..2).map(|i| DimInfo { size: 64, ordered: false, name: format!("d{i}") }).collect();
        let cat = Catalog { dims, maps: Vec::new(), cfg: Config::default() };
        let mut s = Store::new(&[0, 1], Some(0), Kind::Num, &cat).unwrap();
        // 3 blocks of rows, so the packed entries cross block boundaries inside and between members.
        let cells: Vec<(u64, f64)> = (0..64u64 * 50).map(|k| (k * 7 % 4096, k as f64)).filter(|c| c.0 % 3 != 0).collect();
        s.set_sorted(sorted(&cat.cfg, cells));
        let keys: Vec<u64> = s.base.keys.iter().collect();
        for m in 0..64 {
            let want: Vec<u32> = keys.iter().enumerate().filter(|(_, &k)| s.pack.get(k, 1) == m).map(|(row, _)| row as u32).collect();
            assert_eq!(s.postings(1).of(m).collect::<Vec<u32>>(), want, "m={m}");
        }
    }

    #[test]
    fn a_repeated_dimension_keeps_both_members() {
        let dims = (0..2).map(|i| DimInfo { size: 4, ordered: false, name: format!("d{i}") }).collect();
        let cat = Catalog { dims, maps: Vec::new(), cfg: Config::default() };
        for index in [None, Some(0)] {
            let mut s = Store::new(&[0, 1, 0], index, Kind::Num, &cat).unwrap();
            s.write(&[1, 2, 3], Some(5.0));
            s.write(&[3, 2, 1], Some(6.0));
            assert_eq!(s.get(&[1, 2, 3]), Some(5.0));
            assert_eq!(s.rows(), (vec![vec![1, 3], vec![2, 2], vec![3, 1]], vec![5.0, 6.0]));
            let s = s.repacked(&cat).unwrap();
            assert_eq!(s.rows(), (vec![vec![1, 3], vec![2, 2], vec![3, 1]], vec![5.0, 6.0]));
        }
    }
}
