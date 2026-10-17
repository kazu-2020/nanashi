//! Store（Metric の格納）。

#[allow(unused_imports)]
use crate::*;
use crate::packed::BLOCK;
use std::iter::Peekable;
use std::ops::Range;

/// Metric の格納。分割軸（index）を先頭（最上位ビット）に詰めた整数キーで持つ。
///
/// The base is sorted in key order, so one sort stores the result of a full recalculation. A small change
/// goes into the delta tree, and a read merges the delta with the base. When the delta becomes large, the
/// engine merges it into a new base. A binary search in the base and a range search in the delta read
/// only the rows of a range on the partition dimension.
///
/// The base is a list of immutable segments (`Base`). Versions (clones of the model) share the segments by
/// Arc. The delta is a persistent tree, so a write does not break an old version. A clone of a Store copies
/// one Arc for each segment and no cells. So a reader that keeps a published version does not change the
/// cost of a write.
#[derive(Clone, Debug)]
pub struct Store {
    pub metric_dims: Vec<DimId>, // the dimensions in the declared order of the Metric
    pub pack: Packing,           // the partition dimension first
    pub kind: Kind,
    pub(crate) base: Base,
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

/// One immutable run of the base in key order. Nothing changes a segment after it is made.
///
/// The keys are packed in frame-of-reference blocks (`PackedKeys`). The values are an immutable column
/// with no gaps (`Box<[f64]>`), with the same layout as the data of an Arrow Float64 array.
/// The index (postings) of the segment stays in memory. Its rows are local to the segment.
#[derive(Debug)]
pub(crate) struct Segment {
    pub(crate) keys: PackedKeys, // ascending, no duplicates, never empty
    pub(crate) vals: Box<[f64]>,
    first: u64,
    last: u64,
    postings: Vec<OnceLock<Arc<Postings>>>, // one for each packing position, made when it is necessary
}

impl Segment {
    fn new(keys: PackedKeys, vals: Box<[f64]>, n_dims: usize) -> Arc<Segment> {
        debug_assert!(keys.len() > 0 && keys.len() == vals.len());
        let (first, last) = (keys.get(0), keys.get(keys.len() - 1));
        Arc::new(Segment { keys, vals, first, last, postings: fresh_postings(n_dims) })
    }

    pub(crate) fn len(&self) -> usize {
        self.keys.len()
    }

    fn bytes(&self) -> usize {
        self.keys.bytes() + self.vals.len() * 8
    }

    /// The keys are decoded one block at a time (`PackedKeys::range`).
    pub(crate) fn range(&self, r: Range<usize>) -> BaseRange<'_> {
        self.keys.range(r.clone()).zip(self.vals[r].iter().copied())
    }

    /// The index of the dimension at packing position `pos`. The first use makes it with a counting sort (linear in
    /// the rows). The build is sequential: a rayon wait inside `get_or_init` can run a task that waits on the same cell.
    pub(crate) fn postings(&self, pack: &Packing, pos: usize) -> &Postings {
        self.postings[pos].get_or_init(|| {
            let size = (pack.masks[pos] + 1) as usize;
            let mut count = vec![0u32; size + 1];
            for k in self.keys.iter() {
                count[pack.get(k, pos) as usize + 1] += 1;
            }
            for i in 1..count.len() {
                count[i] += count[i - 1];
            }
            let mut fill = count.clone();
            let n = self.len() as u64; // best_postings makes sure that n fits in u32
            // Hold the rows as u32 and pack the u64 entries one at a time. Then the build needs 4 B for each row, not 8 B.
            let mut rows = vec![0u32; n as usize];
            for (row, k) in self.keys.iter().enumerate() {
                let m = pack.get(k, pos) as usize;
                rows[fill[m] as usize] = row as u32;
                fill[m] += 1;
            }
            drop(fill);
            let members: Box<[u32]> = (0..size as u32).filter(|&m| count[m as usize + 1] > count[m as usize]).collect();
            let entries = || {
                members.iter().enumerate().flat_map(|(j, &m)| rows[count[m as usize] as usize..count[m as usize + 1] as usize].iter().map(move |&row| j as u64 * n + row as u64))
            };
            let mut packed = PackedKeys::builder_with_bytes(n as usize, packed::packed_bytes(entries()));
            entries().for_each(|e| packed.push(e));
            let offsets = members.iter().map(|&m| count[m as usize]).chain([n as u32]).collect();
            Arc::new(Postings { members, offsets, entries: packed.finish(), n })
        })
    }
}

/// The base of a Store: segments in key order, and the global row at which each segment starts. Readers
/// address the base by the global row. A clone copies one Arc for each segment. Only `Base::new` makes a
/// Base with segments, so `starts` and `rows` always agree with `segs`.
#[derive(Clone, Debug, Default)]
pub(crate) struct Base {
    pub(crate) segs: Vec<Arc<Segment>>,
    starts: Vec<usize>, // starts[i] = the rows before segment i
    rows: usize,        // a field, because the fused scan reads it for each row
}

impl Base {
    fn new(segs: Vec<Arc<Segment>>) -> Base {
        let mut starts = Vec::with_capacity(segs.len());
        let mut rows = 0;
        for s in &segs {
            starts.push(rows);
            rows += s.len();
        }
        Base { segs, starts, rows }
    }

    #[inline]
    pub(crate) fn rows(&self) -> usize {
        self.rows
    }

    pub(crate) fn bytes(&self) -> usize {
        self.segs.iter().map(|s| s.bytes()).sum()
    }

    /// The segment of global row `i` and the row inside it. `i` must be below `rows()`.
    #[inline]
    pub(crate) fn locate(&self, i: usize) -> (usize, usize) {
        let s = self.starts.partition_point(|&start| start <= i) - 1;
        (s, i - self.starts[s])
    }

    #[inline]
    pub(crate) fn key(&self, i: usize) -> u64 {
        let (s, j) = self.locate(i);
        self.segs[s].keys.get(j)
    }

    /// The first global row whose key is at least `key`.
    pub(crate) fn lower_bound(&self, key: u64) -> usize {
        let s = self.segs.partition_point(|seg| seg.last < key);
        if s == self.segs.len() {
            return self.rows();
        }
        self.starts[s] + self.segs[s].keys.partition_point(|k| k < key)
    }

    /// The value of the row with `key`, or None if the base has no such row.
    fn value(&self, key: u64) -> Option<f64> {
        let seg = self.segs.get(self.segs.partition_point(|seg| seg.last < key))?;
        seg.keys.find(key).map(|j| seg.vals[j])
    }

    /// The rows in `r` (global rows), decoded one block at a time, segment after segment.
    pub(crate) fn range(&self, r: Range<usize>) -> Rows<'_> {
        debug_assert!(r.end <= self.rows());
        let mut out = Rows { base: self, seg: 0, cur: None, end: r.end };
        if r.start < r.end {
            let (s, j) = self.locate(r.start);
            let seg = &self.segs[s];
            out.seg = s;
            out.cur = Some(seg.range(j..(r.end - self.starts[s]).min(seg.len())));
        }
        out
    }

    /// The rows of each segment with a key in `[lo, hi)`, segment by segment.
    fn segments_in(&self, lo: u64, hi: Option<u64>) -> impl Iterator<Item = BaseRange<'_>> {
        let first = self.segs.partition_point(|s| s.last < lo);
        self.segs[first..].iter().take_while(move |s| hi.is_none_or(|h| s.first < h)).map(move |s| {
            let a = if s.first < lo { s.keys.partition_point(|k| k < lo) } else { 0 };
            let b = match hi {
                Some(h) if s.last >= h => s.keys.partition_point(|k| k < h),
                _ => s.len(),
            };
            s.range(a..b)
        })
    }

    /// True if the two bases are the same segments (shared by Arc, not compared by cells).
    fn same_as(&self, other: &Base) -> bool {
        self.segs.len() == other.segs.len() && self.segs.iter().zip(&other.segs).all(|(a, b)| Arc::ptr_eq(a, b))
    }

    /// A base of the given segments, with the key as the value of each row (for the tests of the readers).
    #[cfg(test)]
    pub(crate) fn from_parts(parts: &[&[u64]]) -> Base {
        Base::new(parts.iter().map(|p| Segment::new(PackedKeys::new(p, false), p.iter().map(|&k| k as f64).collect(), 0)).collect())
    }
}

type BaseRange<'a> = std::iter::Zip<packed::Keys<'a>, std::iter::Copied<std::slice::Iter<'a, f64>>>;

/// The iterator of `Base::range`: the rows of one segment, then the next segment, up to global row `end`.
pub(crate) struct Rows<'a> {
    base: &'a Base,
    seg: usize,
    cur: Option<BaseRange<'a>>,
    end: usize,
}

impl Iterator for Rows<'_> {
    type Item = (u64, f64);

    #[inline]
    fn next(&mut self) -> Option<(u64, f64)> {
        loop {
            if let Some(c) = self.cur.as_mut()?.next() {
                return Some(c);
            }
            self.cur = self.next_segment();
        }
    }

    /// Folds each segment with its own loop, so `for_each` has no segment check for each row.
    fn fold<B, F: FnMut(B, (u64, f64)) -> B>(mut self, mut acc: B, mut f: F) -> B {
        while let Some(cur) = self.cur.take() {
            acc = cur.fold(acc, &mut f);
            self.cur = self.next_segment();
        }
        acc
    }
}

impl<'a> Rows<'a> {
    /// The rows of the segment after `seg` up to `end`, or None at the end. `seg` becomes that segment.
    fn next_segment(&mut self) -> Option<BaseRange<'a>> {
        self.seg += 1;
        let &start = self.base.starts.get(self.seg)?;
        let seg = &self.base.segs[self.seg];
        (start < self.end).then(|| seg.range(0..(self.end - start).min(seg.len())))
    }
}

/// Builds a Base in key order. It cuts a segment at the first partition-member boundary after `seg_rows`
/// rows, so a segment holds whole partition members. `like` gives the key density for the reservation.
pub(crate) struct BaseBuilder<'a> {
    pack: &'a Packing,
    seg_rows: usize,
    density: (usize, usize), // (bytes, rows) of the packed keys of a base like the new one
    hint: usize,             // the rows still expected
    keys: packed::Builder,
    vals: Vec<f64>,
    last: u64,
    done: Vec<Arc<Segment>>,
}

impl<'a> BaseBuilder<'a> {
    pub(crate) fn new(pack: &'a Packing, cfg: &Config, hint: usize, like: &Base) -> BaseBuilder<'a> {
        let density = (like.segs.iter().map(|s| s.keys.bytes()).sum(), like.rows());
        let seg_rows = cfg.seg_rows.max(1);
        let cap = BaseBuilder::cap(seg_rows, hint);
        BaseBuilder { pack, seg_rows, density, hint, keys: PackedKeys::builder(cap, density), vals: Vec::with_capacity(cap), last: 0, done: Vec::new() }
    }

    /// The reservation of one segment: `seg_rows` and room for the rest of the partition member at the cut,
    /// so the columns do not grow by doubling (a copy of the segment) for a usual member.
    fn cap(seg_rows: usize, hint: usize) -> usize {
        seg_rows.saturating_add(seg_rows / 16).min(hint)
    }

    #[inline(always)]
    pub(crate) fn push(&mut self, k: u64, v: f64) {
        if self.vals.len() >= self.seg_rows && self.pack.get(k, 0) != self.pack.get(self.last, 0) {
            self.cut();
        }
        self.keys.push(k);
        self.vals.push(v);
        self.last = k;
    }

    #[cold]
    fn cut(&mut self) {
        self.hint = self.hint.saturating_sub(self.vals.len());
        let cap = BaseBuilder::cap(self.seg_rows, self.hint);
        // The next segment packs like this one. In a full recalculation the old base is empty. Then 2 B for each
        // key is too small, and the key bytes grow by doubling (a copy).
        self.density = self.keys.density();
        let keys = std::mem::replace(&mut self.keys, PackedKeys::builder(cap, self.density)).finish();
        let vals = std::mem::replace(&mut self.vals, Vec::with_capacity(cap));
        self.done.push(Segment::new(keys, vals.into_boxed_slice(), self.pack.dims.len()));
    }

    pub(crate) fn finish(mut self) -> Base {
        if !self.vals.is_empty() {
            let n_dims = self.pack.dims.len();
            self.done.push(Segment::new(self.keys.finish(), self.vals.into_boxed_slice(), n_dims));
        }
        Base::new(self.done)
    }
}

/// The iterator of `Store::merged_iter`: the base rows and the delta in one key order (as `merged`). A delta entry
/// replaces the base row with the same key, and a removal (`Slot(None)`) gives nothing.
pub(crate) struct Merged<'a> {
    base: Peekable<Rows<'a>>,
    delta: Peekable<DeltaRange<'a>>,
}

type DeltaRange<'a> = imbl::ordmap::RangedIter<'a, u64, Slot, imbl::shared_ptr::DefaultSharedPtr>;

impl Iterator for Merged<'_> {
    type Item = (u64, f64);

    fn next(&mut self) -> Option<(u64, f64)> {
        loop {
            let Some(&(&dk, &Slot(dv))) = self.delta.peek() else { return self.base.next() };
            match self.base.peek() {
                Some(&(bk, bv)) if bk < dk => {
                    self.base.next();
                    return Some((bk, bv));
                }
                Some(&(bk, _)) if bk == dk => {
                    self.base.next();
                }
                _ => {}
            }
            self.delta.next();
            if let Some(v) = dv {
                return Some((dk, v));
            }
        }
    }
}

/// The new base of a whole-region write while it fills in key order (`Store::sink`). With a diff, it finds
/// the changed keys against the old store while the new cells arrive, as `replaced_all` does.
pub(crate) struct StoreSink<'a> {
    old: &'a Store,
    out: BaseBuilder<'a>,
    last: Option<u64>,
    diff: Option<Diff<'a>>,
}

/// The old cells (the base and the delta merged) and the keys whose value changed so far.
struct Diff<'a> {
    old: Peekable<Merged<'a>>,
    changed: Vec<u64>,
}

impl Diff<'_> {
    /// The new cell `(k, v)` arrived. Old keys below `k` are removed, and `k` is added or changed.
    fn step(&mut self, k: u64, v: f64) {
        while let Some(&(ok, ov)) = self.old.peek() {
            if ok > k {
                break;
            }
            self.old.next();
            if ok == k {
                if ov.to_bits() != v.to_bits() {
                    self.changed.push(k);
                }
                return;
            }
            self.changed.push(ok);
        }
        self.changed.push(k);
    }
}

/// The finished new base of a whole-region write, and the region of the cells whose value changed
/// (None without a diff, or if nothing changed).
#[derive(Debug)]
pub struct Columns {
    pub(crate) base: Base,
    pub(crate) changed: Option<Vec<Vec<u32>>>,
}

impl Columns {
    pub fn bytes(&self) -> usize {
        self.base.bytes()
    }
}

impl Sink for StoreSink<'_> {
    fn part(&mut self, cells: Vec<(u64, f64)>) -> Result<()> {
        for (k, v) in cells {
            if self.last.is_some_and(|last| last >= k) {
                return Err("内部エラー: 融合した結果がキー順でない".into());
            }
            self.last = Some(k);
            self.out.push(k, v);
            if let Some(d) = &mut self.diff {
                d.step(k, v);
            }
        }
        Ok(())
    }
}

impl StoreSink<'_> {
    pub(crate) fn finish(self) -> Columns {
        let changed = self.diff.map(|mut d| {
            d.changed.extend(d.old.map(|(k, _)| k));
            self.old.region_of(&d.changed)
        });
        Columns { base: self.out.finish(), changed: changed.flatten() }
    }
}

/// The index of a dimension other than the partition dimension, for one segment: the rows of each member
/// (an inverted index). Only the members in the segment have an entry, so the index does not grow with the
/// number of segments.
#[derive(Debug)]
pub(crate) struct Postings {
    members: Box<[u32]>, // the members in the segment, ascending
    offsets: Box<[u32]>, // the entries of members[j] are entries[offsets[j]..offsets[j + 1]]
    entries: PackedKeys, // `j * n + row` for each row of the segment, sorted
    n: u64,              // the rows of the segment
}

impl Postings {
    /// The index of `m` in `members` and its positions in `entries`.
    fn at(&self, m: u32) -> (usize, Range<usize>) {
        match self.members.binary_search(&m) {
            Ok(j) => (j, self.offsets[j] as usize..self.offsets[j + 1] as usize),
            Err(_) => (0, 0..0),
        }
    }

    /// The number of rows of member `m`.
    pub(crate) fn count(&self, m: u32) -> usize {
        self.at(m).1.len()
    }

    /// The rows of member `m` in the segment, in ascending order.
    pub(crate) fn of(&self, m: u32) -> impl Iterator<Item = u32> + '_ {
        let (j, span) = self.at(m);
        let first = j as u64 * self.n;
        self.entries.range(span).map(move |k| (k - first) as u32)
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
            base: Base::default(),
            delta: OrdMap::new(),
            cfg: cat.cfg,
        })
    }

    pub fn index_dim(&self) -> Option<DimId> {
        self.pack.dims.first().copied()
    }

    /// Gives the cells in [lo, hi) in key order, merged from the base and the delta.
    pub(crate) fn merged(&self, lo: u64, hi: Option<u64>, mut f: impl FnMut(u64, f64)) {
        let upper = hi.map_or(Bound::Unbounded, Bound::Excluded);
        let mut delta = self.delta.range((Bound::Included(lo), upper)).peekable();
        for rows in self.base.segments_in(lo, hi) {
            merge_rows(rows, &mut delta, &mut f);
        }
        drain_delta(delta, &mut f);
    }

    /// The cells in key order, merged from the base and the delta, as an iterator (for `StoreSink`).
    fn merged_iter(&self) -> Merged<'_> {
        Merged { base: self.base.range(0..self.base.rows()).peekable(), delta: self.delta.range((Bound::Included(0u64), Bound::Unbounded)).peekable() }
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
        if let Some((sel, pos)) = self.best_postings(r) {
            // Collect only the rows of the selected members with the index. Match the changes in the delta tree separately
            let mut cells = Vec::new();
            for seg in &self.base.segs {
                let post = seg.postings(&self.pack, pos);
                for &m in &sel.members {
                    for row in post.of(m) {
                        let k = seg.keys.get(row as usize);
                        if !self.delta.contains_key(&k) && pass(&self.pack, k, &cs) {
                            cells.push((k, seg.vals[row as usize]));
                        }
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
            return self.base.rows();
        }
        let mut n = 0;
        self.merged(0, None, |_, _| n += 1);
        n
    }

    pub fn read(&self, r: &Restrict) -> Cube {
        if r.is_all() && self.delta.is_empty() {
            // Each block is decoded once, in parallel on a large base.
            let mut cells = vec![(0u64, 0f64); self.base.rows()];
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
        self.delta.is_empty().then(|| self.base.rows())
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

    /// The memory of the stored data (bytes). The base is its key and value columns (no spare capacity). The index
    /// counts only the parts that exist. The delta is the size of one entry x the entries, without the tree nodes
    /// (a lower bound). A base that versions share counts for each Store.
    pub fn memory(&self) -> Mem {
        let built = self.base.segs.iter().flat_map(|s| s.postings.iter().filter_map(|p| p.get()));
        let index = built.map(|p| (p.members.len() + p.offsets.len()) * 4 + p.entries.bytes()).sum();
        Mem {
            rows: self.base.rows(),
            base: self.base.bytes(),
            delta_rows: self.delta.len(),
            delta: self.delta.len() * std::mem::size_of::<(u64, Option<f64>)>(),
            index,
        }
    }

    /// 行数のおおよその値（本体と差分の件数の和。差分の上書きや削除を数え直さない）。
    pub fn rows_hint(&self) -> usize {
        self.base.rows() + self.delta.len()
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
            base: Base::default(),
            delta: OrdMap::new(),
            cfg: self.cfg,
        }
    }

    /// Make the sorted cells the base and drop the delta.
    /// Use the buffer of `cells` for the keys, so that only one new column is made.
    pub(crate) fn set_sorted(&mut self, cells: Vec<(u64, f64)>) {
        let par = self.cfg.par(cells.len());
        // The segment cuts are the ones of `BaseBuilder`. A cut is at the first member boundary at least `seg_rows`
        // rows after the last cut. So the search starts there and does not read every row.
        let seg_rows = self.cfg.seg_rows.max(1);
        let mut cuts = vec![0];
        let mut j = seg_rows;
        while j < cells.len() {
            if self.pack.get(cells[j - 1].0, 0) != self.pack.get(cells[j].0, 0) {
                cuts.push(j);
                j = j.saturating_add(seg_rows);
            } else {
                j += 1;
            }
        }
        cuts.push(cells.len());
        cuts.dedup();
        // Allocate the value columns on the calling thread. A parallel collect also reserves its Vec there.
        // If the rayon workers allocate the columns, their malloc arenas keep the freed columns of old segments.
        // Then RSS grows by about 20 % on retail xlarge.
        let column = |c: &[(u64, f64)]| -> Box<[f64]> {
            if par { c.par_iter().map(|c| c.1).collect::<Vec<f64>>().into_boxed_slice() } else { c.iter().map(|c| c.1).collect() }
        };
        let vals: Vec<Box<[f64]>> = cuts.windows(2).map(|w| column(&cells[w[0]..w[1]])).collect();
        let keys = keys_in_place(cells);
        let n_dims = self.pack.dims.len();
        let make = |(w, vals): (&[usize], Box<[f64]>)| Segment::new(PackedKeys::new(&keys[w[0]..w[1]], par), vals, n_dims);
        self.set_base(Base::new(cuts.windows(2).zip(vals).map(make).collect()));
    }

    /// A sink for at most `n` cells in key order that replace the whole stored data. With `diff`, the
    /// sink also finds the region of the cells whose value changed (as `replaced_all`).
    pub(crate) fn sink(&self, n: usize, diff: bool) -> StoreSink<'_> {
        let diff = diff.then(|| Diff { old: self.merged_iter().peekable(), changed: Vec::new() });
        StoreSink { old: self, out: BaseBuilder::new(&self.pack, &self.cfg, n, &self.base), last: None, diff }
    }

    /// The Store with the finished sink as its base and no delta, and the region of the changed cells.
    pub fn with_columns(&self, c: Columns) -> (Store, Option<Vec<Vec<u32>>>) {
        let mut out = self.empty_like();
        out.set_base(c.base);
        (out, c.changed)
    }

    /// Make `base` the base and drop the delta.
    fn set_base(&mut self, base: Base) {
        self.base = base;
        self.delta = OrdMap::new();
    }

    /// If r filters a dimension other than the partition dimension and its index makes the rows much fewer,
    /// return the selection and the packing position of that dimension.
    pub(crate) fn best_postings<'a>(&'a self, r: &'a Restrict) -> Option<(&'a Arc<Sel>, usize)> {
        let min_rows = self.cfg.postings_min_rows;
        let n = self.base.rows();
        if n == 0 || n < min_rows || self.base.segs.iter().any(|s| s.len() > u32::MAX as usize) {
            return None;
        }
        let mut best: Option<(usize, &Arc<Sel>, usize)> = None;
        for (pos, &d) in self.pack.dims.iter().enumerate().skip(1) {
            let Some(sel) = r.get(d) else { continue };
            if self.pack.masks[pos] >= (1 << 22) {
                continue; // no index for a dimension with too many members
            }
            let cand: usize = self.base.segs.iter().map(|s| {
                let p = s.postings(&self.pack, pos);
                sel.members.iter().map(|&m| p.count(m)).sum::<usize>()
            }).sum();
            if best.is_none_or(|(c, _, _)| cand < c) {
                best = Some((cand, sel, pos));
            }
        }
        let (cand, sel, pos) = best?;
        (min_rows == 0 || cand * 4 < n).then_some((sel, pos))
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

    /// The bytes that the segments of a base of `rows` rows add. Each segment has 256 B for its struct, a partly
    /// filled block of keys and 16 spare key bytes. It also has 16 B for the index cell of each dimension.
    pub(crate) fn seg_bytes(&self, rows: usize) -> usize {
        (rows / self.cfg.seg_rows.max(1) + 1).saturating_mul(256 + self.pack.dims.len() * 16)
    }

    /// The most rows of the delta. With more rows, the delta goes into a new base.
    fn delta_limit(&self) -> usize {
        self.cfg.compact_min.max(self.base.rows() / 8)
    }

    /// The cells that `replace_diff` (if `diff`) or `replace` needs to write `n` new cells into a region of `size`
    /// keys: (the most at the peak, the cells that the store keeps after the write). If `shared`, another version of
    /// the store shares the base and the delta tree. `filtered(d)` tells if the region filters dimension d.
    /// Each term below is one allocation of the write, in bytes. A list that grows by push can be 2 times its length.
    pub(crate) fn part_write_cells(&self, size: usize, n: usize, diff: bool, shared: bool, filtered: impl Fn(DimId) -> bool) -> (usize, usize) {
        let old = size.min(self.rows_hint());
        let (index, index_kept) = self.postings_bytes(filtered);
        // for_each_in: the old cells (replace_diff) or keys (replace), and the cells that it collects with an index
        let olds = old.saturating_mul(if diff { 32 } else { 16 });
        let found = if index > 0 { old.saturating_mul(32) } else { 0 };
        let repacked = n.saturating_mul(CELL_BYTES);
        // replace_diff: the changed keys (a cell for each old and new cell), the old keys, then region_of's lists
        let changed = if diff { (old + n).saturating_mul(CELL_BYTES) } else { 0 };
        let old_keys = if diff { old.saturating_mul(8) } else { 0 };
        let axes = self.metric_dims.len();
        let sets = if diff { (old + n).saturating_mul(axes * 4).saturating_add(axes * 24) } else { 0 };
        // write_back into the delta tree: a probe measured 57 B for each row and 416 B for a small tree. A shared tree
        // also copies the nodes on the path to each key: about 3 nodes of 16 rows, less than 32 rows
        let keys = (old + n).min(size);
        let copy = if shared { self.delta.len().min(keys.saturating_mul(32).saturating_add(64)) } else { 0 };
        let tree = (keys + copy).saturating_mul(4 * CELL_BYTES).saturating_add(1024);
        let (built, scratch, grown) = if self.delta.len().saturating_add(old).saturating_add(n) <= self.delta_limit() {
            (tree, 0, tree)
        } else {
            // write_back into new base columns: keys (at most 8 B each, PackedKeys::builder), block headers, 8 B
            // values, and the block buffer of the builder. The old base goes if no other version has it.
            // The region can have fewer old cells than `old`. Then the write goes into a tree of at most `limit` rows
            let rows = self.rows_hint().saturating_add(n);
            let base = rows.saturating_mul(16).saturating_add(rows.div_ceil(BLOCK) * 16).saturating_add(self.seg_bytes(rows));
            let tree = tree.min(self.delta_limit().saturating_mul(4 * CELL_BYTES).saturating_add(1024));
            let freed = if shared || self.base.segs.iter().any(|s| Arc::strong_count(s) > 1) { 0 } else { self.memory().base };
            (base.max(tree), BLOCK * 8, base.saturating_sub(freed).max(tree))
        };
        // Small parts: the packing of the new cells, the checks of the region, the axes of region_of
        let small = 256 + axes * 64;
        let lists = [found + olds + repacked, olds + repacked + changed + old_keys + built + scratch, olds + changed + built + sets];
        // replace repacks the result before for_each_in builds the index, so the result lives during the build
        let building = if diff || index == 0 { index } else { index.saturating_add(repacked) };
        let peak = building.max(index_kept.saturating_add(lists.into_iter().max().unwrap_or(0)).saturating_add(small));
        let kept = index_kept.saturating_add(grown).saturating_add(sets);
        (peak.div_ceil(CELL_BYTES), kept.div_ceil(CELL_BYTES))
    }

    /// The bytes of the indexes that `for_each_in` builds for a region that filters the dimensions in `filtered`
    /// (see `best_postings` and `postings`): (the most at the peak, the bytes that the store keeps).
    fn postings_bytes(&self, filtered: impl Fn(DimId) -> bool) -> (usize, usize) {
        let n = self.base.rows();
        if n == 0 || n < self.cfg.postings_min_rows || self.base.segs.iter().any(|s| s.len() > u32::MAX as usize) || self.pack.dims.first().is_some_and(|&d| filtered(d)) {
            return (0, 0);
        }
        let (mut peak, mut kept) = (0usize, 0usize);
        for (pos, &d) in self.pack.dims.iter().enumerate().skip(1) {
            if !filtered(d) || self.pack.masks[pos] >= (1 << 22) {
                continue;
            }
            // Each segment has its own index
            for seg in self.base.segs.iter().filter(|s| s.postings[pos].get().is_none()) {
                let rows = seg.len();
                // A member and an offset (u32 each) for each member. A segment has at most `rows` members. Then the
                // packed entries (at most 8 B each), the block headers and the struct
                let lists = (rows.min(self.pack.masks[pos] as usize + 1) + 1) * 8;
                let entries = rows * 8 + rows.div_ceil(BLOCK) * 16 + 16 + 128;
                // The build also holds the count and fill arrays (u32 for each dimension member), the rows (u32)
                // and the block buffer of the builder
                let dense = (self.pack.masks[pos] as usize + 2) * 4;
                peak = peak.max(kept + dense * 2 + lists + entries + rows * 4 + BLOCK * 8);
                kept += lists + entries;
            }
        }
        (peak, kept)
    }

    /// Merge the delta into a new base.
    pub(crate) fn compact(&mut self) {
        let mut out = BaseBuilder::new(&self.pack, &self.cfg, self.rows_hint(), &self.base);
        let (base, delta) = (std::mem::take(&mut self.base), std::mem::take(&mut self.delta));
        consume_merged(base, &delta, |k, v| out.push(k, v));
        self.set_base(out.finish());
    }

    pub(crate) fn after_delta(&mut self) {
        if self.delta.len() > self.cfg.compact_min.max(self.base.rows() / 8) {
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
            let n = (self.rows_hint() + new.len()).saturating_sub(old.len());
            let mut out = BaseBuilder::new(&self.pack, &self.cfg, n, &self.base);
            let (base, delta) = (std::mem::take(&mut self.base), std::mem::take(&mut self.delta));
            let mut new = new.into_iter().peekable();
            consume_merged(base, &delta, |k, v| {
                while let Some((nk, nv)) = new.next_if(|c| c.0 < k) {
                    out.push(nk, nv);
                }
                if !pass(&self.pack, k, &cs) && new.peek().is_none_or(|c| c.0 != k) {
                    out.push(k, v);
                }
            });
            new.for_each(|(k, v)| out.push(k, v));
            self.set_base(out.finish());
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
            let mut old = Vec::with_capacity(self.rows_hint());
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

    /// True if the two are the same stored data (one is a clone of the other, and nothing wrote to either).
    pub fn same_as(&self, other: &Store) -> bool {
        self.base.same_as(&other.base) && self.delta.ptr_eq(&other.delta)
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
        if self.base.same_as(&other.base) {
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
            None => self.base.value(k),
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
        let mut cells = Vec::with_capacity(self.rows_hint());
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
        let limit = self.cfg.compact_min.max(self.base.rows() / 8);
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
        let mut out = Vec::with_capacity(self.rows_hint() + updates.len());
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
        let mut cells = Vec::with_capacity(self.rows_hint());
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

/// Gives the base rows of one segment merged with the delta entries up to its last key, in key order. The loop
/// is the same as `Merged`, written out because this is the hot path of every read with a delta.
#[inline]
fn merge_rows(rows: BaseRange<'_>, delta: &mut Peekable<DeltaRange<'_>>, f: &mut impl FnMut(u64, f64)) {
    let mut base = rows.peekable();
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
}

/// Gives the delta entries that are left after the last base row.
fn drain_delta(delta: Peekable<DeltaRange<'_>>, f: &mut impl FnMut(u64, f64)) {
    for (&dk, &Slot(dv)) in delta {
        if let Some(v) = dv {
            f(dk, v);
        }
    }
}

/// Gives all cells of `base` merged with `delta` as `Store::merged(0, None, f)`, and takes the base. It drops each
/// segment after it reads its rows. A rewrite holds the unread old segments, the finished new segments and one
/// segment in build: about one base and one segment. If a version shares the old segments, nothing frees.
fn consume_merged(base: Base, delta: &OrdMap<u64, Slot>, mut f: impl FnMut(u64, f64)) {
    let mut delta = delta.range((Bound::Included(0u64), Bound::Unbounded)).peekable();
    for seg in base.segs {
        merge_rows(seg.range(0..seg.len()), &mut delta, &mut f);
    }
    drain_delta(delta, &mut f);
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
        let cat = two_dims_with(64, Config { seg_rows: 1000, postings_min_rows: 0, ..Config::default() });
        let mut s = Store::new(&[0, 1], Some(0), Kind::Num, &cat).unwrap();
        let cells: Vec<(u64, f64)> = (0..64u64 * 50).map(|k| (k * 7 % 4096, k as f64)).filter(|c| c.0 % 3 != 0).collect();
        s.set_sorted(sorted(&cat.cfg, cells));
        assert_eq!(s.base.segs.len(), 3);
        for seg in &s.base.segs {
            let keys: Vec<u64> = seg.keys.iter().collect();
            for m in 0..64 {
                let want: Vec<u32> = keys.iter().enumerate().filter(|(_, &k)| s.pack.get(k, 1) == m).map(|(row, _)| row as u32).collect();
                assert_eq!(seg.postings(&s.pack, 1).of(m).collect::<Vec<u32>>(), want, "m={m}");
            }
        }
        let r = Restrict::all(2).with(1, Sel::new(vec![3, 17, 40], 64));
        assert!(s.best_postings(&r).is_some());
        let want: Vec<(u64, f64)> = s.as_cube().cells.into_iter().filter(|c| [3, 17, 40].contains(&s.pack.get(c.0, 1))).collect();
        assert_eq!(s.read(&r).cells, want);
    }

    fn two_dims(size: u32) -> Catalog {
        two_dims_with(size, Config::default())
    }

    fn two_dims_with(size: u32, cfg: Config) -> Catalog {
        let dims = (0..2).map(|i| DimInfo { size, ordered: false, name: format!("d{i}") }).collect();
        Catalog { dims, maps: Vec::new(), cfg }
    }

    /// The cells of `s` as a map (the expected contents of a Store after writes).
    fn map_of(s: &Store) -> std::collections::BTreeMap<u64, f64> {
        s.as_cube().cells.into_iter().collect()
    }

    #[test]
    fn a_segment_holds_whole_partition_members() {
        let cat = two_dims_with(1024, Config { seg_rows: 2048, ..Config::default() });
        let mut s = Store::new(&[0, 1], Some(0), Kind::Num, &cat).unwrap();
        let mut cells: Vec<(u64, f64)> = (0..1024u64).flat_map(|m| (0..10u64).map(move |j| ((m << 10) | (j * 100), m as f64))).collect();
        cells.extend((0..1024u64).filter(|j| j % 100 != 0).map(|j| ((100 << 10) | j, 100.0)));
        let cells = sorted(&cat.cfg, cells);
        s.set_sorted(cells.clone());
        assert!(s.base.segs.len() >= 4, "{}", s.base.segs.len());
        for w in s.base.segs.windows(2) {
            assert_ne!(s.pack.get(w[0].last, 0), s.pack.get(w[1].first, 0), "a member spans two segments");
            assert!(w[0].len() >= 2048);
        }
        assert_eq!(s.as_cube().cells, cells);
        let empty = s.empty_like();
        let mut sink = empty.sink(cells.len(), false);
        sink.part(cells.clone()).unwrap();
        let (got, _) = s.with_columns(sink.finish());
        assert_eq!(got.base.starts, s.base.starts);
        s.set_sorted((0..1024u64).flat_map(|m| (0..8u64).map(move |j| ((m << 10) | j, 1.0))).collect());
        assert_eq!(s.base.starts[..3], [0, 2048, 4096]);
    }

    #[test]
    fn a_compaction_merges_the_delta_across_segments() {
        let cat = two_dims_with(1024, Config { seg_rows: 2048, compact_min: 16, ..Config::default() });
        let mut s = Store::new(&[0, 1], Some(0), Kind::Num, &cat).unwrap();
        s.set_sorted(random_cells(10_000, 7));
        assert_eq!(s.base.segs.len(), 5);
        let mut want = map_of(&s);
        let (gaps, seg1, seg3) = (s.base.segs[2].last + 1..s.base.segs[3].first, s.base.segs[1].first, s.base.segs[3].last);
        let keys: Vec<u64> = [0, u64::MAX >> 44, gaps.start, gaps.end - 1, seg1, seg1 + 1, seg3, seg3 - 1].into_iter().chain((1..1300).map(|i| seg1 + i * 7)).collect();
        for (i, &k) in keys.iter().enumerate() {
            let v = (i % 3 != 0).then_some(i as f64);
            s.write(&s.decode(k), v);
            match v {
                Some(v) => want.insert(k, v),
                None => want.remove(&k),
            };
        }
        assert!(s.base.rows() > 10_500, "the compaction merged the delta into the base ({} rows)", s.base.rows());
        assert!(s.base.segs.len() >= 5);
        assert_eq!(map_of(&s), want);
        assert_eq!(s.len(), want.len());
    }

    /// At most `n` distinct keys below `2^20` in ascending order, with a value for each.
    fn random_cells(n: usize, seed: u64) -> Vec<(u64, f64)> {
        let mut x = seed;
        let mut keys: Vec<u64> = (0..n)
            .map(|_| {
                x = x.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
                x >> 44
            })
            .collect();
        keys.sort_unstable();
        keys.dedup();
        keys.into_iter().map(|k| (k, (k % 97) as f64)).collect()
    }

    #[test]
    fn a_sink_builds_the_same_base_as_set_sorted() {
        let cat = two_dims_with(1024, Config { seg_rows: 2048, ..Config::default() });
        let cells = random_cells(10_000, 7);
        let mut want = Store::new(&[0, 1], Some(0), Kind::Num, &cat).unwrap();
        want.set_sorted(cells.clone());
        assert_eq!(want.base.segs.len(), 5);
        // Also with more room than cells (the Exact cover reserves the rows of the drivers)
        let empty = want.empty_like();
        for n in [cells.len(), cells.len() + 1234] {
            let mut sink = empty.sink(n, false);
            for part in cells.chunks(700) {
                sink.part(part.to_vec()).unwrap();
            }
            let (got, changed) = want.with_columns(sink.finish());
            assert!(changed.is_none());
            assert_eq!(got.base.starts, want.base.starts);
            for (g, w) in got.base.segs.iter().zip(&want.base.segs) {
                assert_eq!(g.keys.bytes(), w.keys.bytes());
                assert_eq!(g.keys.iter().collect::<Vec<u64>>(), w.keys.iter().collect::<Vec<u64>>());
                assert_eq!(g.vals, w.vals);
            }
        }
    }

    #[test]
    fn a_sink_finds_the_same_changed_region_as_replaced_all() {
        let cat = two_dims(64);
        let mut old = Store::new(&[0, 1], Some(0), Kind::Num, &cat).unwrap();
        old.set_sorted(random_cells(300, 3));
        old.write(&[5, 5], Some(1.0));
        old.write(&[6, 6], Some(0.0));
        old.write(&[7, 7], None);
        let mut new: Vec<(u64, f64)> = Vec::new();
        old.merged(0, None, |k, v| new.push((k, v)));
        assert!(new.len() > 200);
        new[10].1 += 1.0;
        let zero = old.encode(&[6, 6]);
        new.iter_mut().find(|c| c.0 == zero).unwrap().1 = -0.0;
        new.remove(30);
        let added = (0..1u64 << 12).find(|k| new.binary_search_by_key(k, |c| c.0).is_err()).unwrap();
        new.push((added, 9.0));
        new.sort_unstable_by_key(|c| c.0);
        let cube = Cube { pack: old.pack.clone(), kind: Kind::Num, cells: new.clone() };
        let want = old.replaced_all(cube).unwrap().1;
        assert!(want.as_ref().is_some_and(|sets| sets.iter().all(|ms| ms.len() >= 2)));
        let mut sink = old.sink(new.len(), true);
        for part in new.chunks(37) {
            sink.part(part.to_vec()).unwrap();
        }
        let (got, changed) = old.with_columns(sink.finish());
        assert_eq!(changed, want);
        assert_eq!(got.as_cube().cells, new);
        let same = Cube { pack: old.pack.clone(), kind: Kind::Num, cells: got.as_cube().cells };
        assert_eq!(got.replaced_all(same).unwrap().1, None);
        let mut sink = got.sink(new.len(), true);
        sink.part(new.clone()).unwrap();
        assert_eq!(got.with_columns(sink.finish()).1, None);
    }

    #[test]
    fn a_sink_refuses_a_descent() {
        let cat = two_dims(64);
        let s = Store::new(&[0, 1], Some(0), Kind::Num, &cat).unwrap();
        let mut sink = s.sink(2, false);
        sink.part(vec![(5, 1.0)]).unwrap();
        assert!(sink.part(vec![(4, 1.0)]).unwrap_err().contains("キー順"));
        let mut sink = s.sink(2, false);
        assert!(sink.part(vec![(5, 1.0), (5, 2.0)]).is_err());
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
