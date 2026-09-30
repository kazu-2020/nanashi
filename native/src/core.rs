//! 疎な Cube の格納と、式の評価。意味は Python の参照実装（evaluate.py）と同じ。
//!
//! キーは各軸のメンバー番号をビット単位で詰めた u64。値は f64 で、真偽値は 1.0 / 0.0。
//! 空のセルはキーがないことで表す。

use rayon::prelude::*;
use rustc_hash::{FxHashMap, FxHashSet};
use imbl::ordmap::DiffItem;
use imbl::OrdMap;
use std::ops::Bound;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, OnceLock};

/// 差分がこの件数（と本体の 1/8）を超えたら本体にまとめ直す。テストでは小さくして、まとめ直しを頻繁に起こす。
pub static COMPACT_MIN: AtomicUsize = AtomicUsize::new(4096);

/// これより少ない件数は並列にしない。rayon を呼ぶだけでスレッドプールへの受け渡し（数マイクロ秒）が
/// かかり、差分再計算のように小さな演算を何百回も繰り返すときに積み上がるため。
pub static PAR_MIN: AtomicUsize = AtomicUsize::new(16_384);

#[inline]
fn par(n: usize) -> bool {
    n >= PAR_MIN.load(Ordering::Relaxed)
}

fn sort_cells(v: &mut [(u64, f64)]) {
    if par(v.len()) {
        v.par_sort_unstable_by_key(|c| c.0);
    } else {
        v.sort_unstable_by_key(|c| c.0);
    }
}

/// 各セルを f で写し、None を捨てる（件数が多ければ並列に）。
fn map_cells<F>(cells: &[(u64, f64)], f: F) -> Vec<(u64, f64)>
where
    F: Fn(u64, f64) -> Option<(u64, f64)> + Sync + Send,
{
    if par(cells.len()) {
        cells.par_iter().filter_map(|&(k, v)| f(k, v)).collect()
    } else {
        cells.iter().filter_map(|&(k, v)| f(k, v)).collect()
    }
}

/// 各セルを複数のセルへ写す（件数が多ければ並列に）。
fn flat_map_cells<F, I>(cells: &[(u64, f64)], f: F) -> Vec<(u64, f64)>
where
    F: Fn(u64, f64) -> I + Sync + Send,
    I: Iterator<Item = (u64, f64)>,
{
    if par(cells.len()) {
        cells.par_iter().flat_map_iter(|&(k, v)| f(k, v)).collect()
    } else {
        cells.iter().flat_map(|&(k, v)| f(k, v)).collect()
    }
}

pub type DimId = usize;
pub type Result<T> = std::result::Result<T, String>;

#[derive(Clone, Debug)]
pub struct DimInfo {
    pub size: u32,
    pub ordered: bool,
    pub name: String, // 型検査の文言に使う
}

/// 軸のプロパティ（例: Employee.Department）。src のメンバー -> dst のメンバー。
#[derive(Clone, Debug)]
pub struct Mapping {
    pub fwd: Vec<i64>,      // src メンバー -> dst メンバー（なければ -1）
    pub inv: Vec<Vec<u32>>, // dst メンバー -> src メンバーの一覧
}

/// 軸と対応表。複製したモデルどうしで Arc で共有し、変えるときに写す（対応表は Arc なので、
/// 軸を 1 つ変えても対応表の中身までは写さない）。
#[derive(Clone, Default, Debug)]
pub struct Catalog {
    pub dims: Vec<DimInfo>,
    pub maps: Vec<Arc<Mapping>>,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    Num,
    Bool,
}

// ------------------------------------------------------------------ キーの詰め方

/// 軸の並びと、各軸のビット位置。先頭の軸が最上位ビットに来る。
#[derive(Clone, Debug, PartialEq)]
pub struct Packing {
    pub dims: Vec<DimId>,
    shifts: Vec<u32>,
    masks: Vec<u64>,
}

fn bits_for(size: u32) -> u32 {
    if size <= 1 {
        1
    } else {
        32 - (size - 1).leading_zeros()
    }
}

impl Packing {
    /// 各軸に、今のメンバー数の 2 倍まで入るビット幅（1 ビットの余裕）を取る。メンバーを追加しても
    /// その範囲なら詰め直さずに済む。余裕を足すと 64 ビットに収まらないときは、余裕なしで詰める。
    pub fn new(dims: &[DimId], cat: &Catalog) -> Result<Packing> {
        let exact: Vec<u32> = dims.iter().map(|&d| bits_for(cat.dims[d].size)).collect();
        let roomy: Vec<u32> = exact.iter().map(|b| b + 1).collect();
        let bits = if roomy.iter().sum::<u32>() <= 64 { roomy } else { exact };
        let total: u32 = bits.iter().sum();
        if total > 64 {
            return Err(format!("軸の組み合わせが 64 ビットに収まらない（{total} ビット）"));
        }
        let mut shifts = vec![0; dims.len()];
        let mut acc = 0;
        for i in (0..dims.len()).rev() {
            shifts[i] = acc;
            acc += bits[i];
        }
        let masks = bits.iter().map(|&b| if b == 64 { u64::MAX } else { (1u64 << b) - 1 }).collect();
        Ok(Packing { dims: dims.to_vec(), shifts, masks })
    }

    #[inline]
    pub fn get(&self, key: u64, i: usize) -> u32 {
        ((key >> self.shifts[i]) & self.masks[i]) as u32
    }

    #[inline]
    pub fn put(&self, i: usize, m: u32) -> u64 {
        (m as u64) << self.shifts[i]
    }

    #[inline]
    pub fn clear(&self, key: u64, i: usize) -> u64 {
        key & !(self.masks[i] << self.shifts[i])
    }

    pub fn pos(&self, d: DimId) -> Option<usize> {
        self.dims.iter().position(|&x| x == d)
    }

    /// 今のメンバー数のメンバー番号が、すべてのビット幅に収まるか。
    pub fn fits(&self, cat: &Catalog) -> bool {
        self.dims.iter().zip(&self.masks).all(|(&d, &mask)| (cat.dims[d].size as u64).saturating_sub(1) <= mask)
    }
}

/// src の詰め方のキーから、dst の詰め方のキーへ（両方にある軸だけを移す）。
struct Proj {
    pairs: Vec<(usize, usize)>,
}

impl Proj {
    fn new(src: &Packing, dst: &Packing) -> Proj {
        let pairs = dst.dims.iter().enumerate().filter_map(|(j, &d)| src.pos(d).map(|i| (i, j))).collect();
        Proj { pairs }
    }

    #[inline]
    fn apply(&self, src: &Packing, dst: &Packing, key: u64) -> u64 {
        let mut out = 0;
        for &(i, j) in &self.pairs {
            out |= dst.put(j, src.get(key, i));
        }
        out
    }
}

// ------------------------------------------------------------------ 範囲（restrict）

/// 1 つの軸で対象にするメンバーの集合。所属の検査はビット集合で行う（メンバー数の 1/8 バイト）。
#[derive(Debug)]
pub struct Sel {
    pub members: Vec<u32>, // 昇順
    mask: Vec<u64>,
}

impl Sel {
    pub fn new(mut members: Vec<u32>, size: u32) -> Sel {
        members.sort_unstable();
        members.dedup();
        let mut mask = vec![0u64; (size as usize).div_ceil(64)];
        for &m in &members {
            if (m as usize) < size as usize {
                mask[(m >> 6) as usize] |= 1u64 << (m & 63);
            }
        }
        Sel { members, mask }
    }

    #[inline]
    pub fn has(&self, m: u32) -> bool {
        self.mask.get((m >> 6) as usize).is_some_and(|w| (w >> (m & 63)) & 1 == 1)
    }
}

/// 軸ごとの対象メンバー。None の軸は全メンバー。
#[derive(Clone, Debug)]
pub struct Restrict {
    sels: Vec<Option<Arc<Sel>>>,
}

impl Restrict {
    pub fn all(n_dims: usize) -> Restrict {
        Restrict { sels: vec![None; n_dims] }
    }

    pub fn get(&self, d: DimId) -> Option<&Arc<Sel>> {
        self.sels.get(d).and_then(|s| s.as_ref())
    }

    pub fn with(&self, d: DimId, sel: Sel) -> Restrict {
        let mut r = self.clone();
        r.sels[d] = Some(Arc::new(sel));
        r
    }

    pub fn without(&self, ds: &[DimId]) -> Restrict {
        let mut r = self.clone();
        for &d in ds {
            r.sels[d] = None;
        }
        r
    }

    pub fn is_all(&self) -> bool {
        self.sels.iter().all(|s| s.is_none())
    }
}

fn members(cat: &Catalog, d: DimId, r: &Restrict) -> Vec<u32> {
    match r.get(d) {
        Some(sel) => sel.members.clone(),
        None => (0..cat.dims[d].size).collect(),
    }
}

fn checks(pack: &Packing, r: &Restrict) -> Vec<(usize, Arc<Sel>)> {
    pack.dims.iter().enumerate().filter_map(|(i, &d)| r.get(d).map(|s| (i, s.clone()))).collect()
}

#[inline]
fn pass(pack: &Packing, key: u64, checks: &[(usize, Arc<Sel>)]) -> bool {
    checks.iter().all(|(i, s)| s.has(pack.get(key, *i)))
}

// ------------------------------------------------------------------ Cube（評価の途中結果）

#[derive(Clone, Debug)]
pub struct Cube {
    pub pack: Packing,
    pub kind: Kind,
    pub cells: Vec<(u64, f64)>,
}

impl Cube {
    pub fn dims(&self) -> &[DimId] {
        &self.pack.dims
    }

    pub fn filter(&self, r: &Restrict) -> Cube {
        let cs = checks(&self.pack, r);
        if cs.is_empty() {
            return self.clone();
        }
        let cells = map_cells(&self.cells, |k, v| pass(&self.pack, k, &cs).then_some((k, v)));
        Cube { pack: self.pack.clone(), kind: self.kind, cells }
    }

    pub fn repack(&self, pack: &Packing) -> Cube {
        if &self.pack == pack {
            return self.clone();
        }
        let proj = Proj::new(&self.pack, pack);
        let cells = map_cells(&self.cells, |k, v| Some((proj.apply(&self.pack, pack, k), v)));
        Cube { pack: pack.clone(), kind: self.kind, cells }
    }
}

// ------------------------------------------------------------------ Store（格納）

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
    base: Arc<Base>,
    delta: OrdMap<u64, Option<f64>>, // 差分（Some = 値を置く、None = 消す）
}

/// Store の本体。作ったら変えない。
#[derive(Debug)]
struct Base {
    keys: Vec<u64>, // 昇順・重複なし
    vals: Vec<f64>,
    postings: Vec<OnceLock<Arc<Postings>>>, // 分割軸以外の軸の索引（詰め方の位置ごと。必要になったら作る）
}

impl Base {
    fn empty(n_dims: usize) -> Arc<Base> {
        Arc::new(Base { keys: Vec::new(), vals: Vec::new(), postings: fresh_postings(n_dims) })
    }
}

/// 分割軸以外の軸の索引。本体の行番号を、その軸のメンバーごとにまとめたもの（転置索引）。
#[derive(Debug)]
struct Postings {
    offsets: Vec<u32>, // メンバー m の行は rows[offsets[m]..offsets[m + 1]]
    rows: Vec<u32>,
}

impl Postings {
    fn of(&self, m: u32) -> &[u32] {
        let m = m as usize;
        if m + 1 >= self.offsets.len() {
            return &[];
        }
        &self.rows[self.offsets[m] as usize..self.offsets[m + 1] as usize]
    }
}

/// 本体がこの行数以上なら、分割軸以外の軸で絞った読み出しに索引を使う。0 なら常に使う（テスト用）。
pub static POSTINGS_MIN_ROWS: AtomicUsize = AtomicUsize::new(1024);

fn fresh_postings(n: usize) -> Vec<OnceLock<Arc<Postings>>> {
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
        })
    }

    pub fn index_dim(&self) -> Option<DimId> {
        self.pack.dims.first().copied()
    }

    /// [lo, hi) のセルを、本体と差分を突き合わせながらキー順に渡す。
    fn merged(&self, lo: u64, hi: Option<u64>, mut f: impl FnMut(u64, f64)) {
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

    fn for_each_in(&self, r: &Restrict, mut f: impl FnMut(u64, f64)) {
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
            let cells = if par(self.base.keys.len()) {
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

    /// 行数のおおよその値（本体と差分の件数の和。差分の上書きや削除を数え直さない）。
    pub fn rows_hint(&self) -> usize {
        self.base.keys.len() + self.delta.len()
    }

    /// 同じ軸と分割軸の空の Store。
    pub fn emptied(&self) -> Store {
        self.empty_like()
    }

    fn empty_like(&self) -> Store {
        Store {
            metric_dims: self.metric_dims.clone(),
            pack: self.pack.clone(),
            kind: self.kind,
            base: Base::empty(self.pack.dims.len()),
            delta: OrdMap::new(),
        }
    }

    /// 並んだセルを本体にする（差分は捨てる）。
    fn set_sorted(&mut self, cells: Vec<(u64, f64)>) {
        let (keys, vals) = if par(cells.len()) { cells.into_par_iter().unzip() } else { cells.into_iter().unzip() };
        self.base = Arc::new(Base { keys, vals, postings: fresh_postings(self.pack.dims.len()) });
        self.delta = OrdMap::new();
    }

    /// 詰め方の位置 pos の軸の索引。初めて使うときに計数ソートで作る（本体の行数に比例）。
    fn postings(&self, pos: usize) -> &Postings {
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
    fn best_postings<'a>(&'a self, r: &'a Restrict) -> Option<(&'a Arc<Sel>, &'a Postings)> {
        let min_rows = POSTINGS_MIN_ROWS.load(Ordering::Relaxed);
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
    fn compact(&mut self) {
        let mut cells = Vec::with_capacity(self.base.keys.len() + self.delta.len());
        self.merged(0, None, |k, v| cells.push((k, v)));
        self.set_sorted(cells);
    }

    fn after_delta(&mut self) {
        if self.delta.len() > COMPACT_MIN.load(Ordering::Relaxed).max(self.base.keys.len() / 8) {
            self.compact();
        }
    }

    pub fn replace(&mut self, r: &Restrict, new: &Cube) -> Result<()> {
        if !same_set(new.dims(), &self.metric_dims) {
            return Err("書き戻す結果の軸が Metric の軸と一致しない".into());
        }
        let mut new = new.repack(&self.pack).cells;
        if r.is_all() {
            sort_cells(&mut new);
            self.set_sorted(new);
            return Ok(());
        }
        let mut old = Vec::new();
        self.for_each_in(r, |k, _| old.push(k));
        sort_cells(&mut new);
        self.write_back(r, &old, new);
        Ok(())
    }

    /// r の範囲の古いセル（old のキー）を消し、new（キー順）を置く。差分の木に入れると本体に
    /// まとめ直すことになる量なら、差分の木を通さずに、新しい本体を直接作る（差分の木への
    /// 挿入は 1 件ずつなので、大量に入れてからまとめ直すより速い）。
    fn write_back(&mut self, r: &Restrict, old: &[u64], new: Vec<(u64, f64)>) {
        let limit = COMPACT_MIN.load(Ordering::Relaxed).max(self.base.keys.len() / 8);
        if self.delta.len() + old.len() + new.len() > limit {
            let cs = checks(&self.pack, r);
            let mut kept = Vec::with_capacity(self.base.keys.len() + self.delta.len());
            self.merged(0, None, |k, v| {
                if !pass(&self.pack, k, &cs) {
                    kept.push((k, v))
                }
            });
            self.set_sorted(merge_sorted(&kept, &new, true, |a, b| b.or(a)));
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
        let new = Cube { pack: self.pack.clone(), kind: new.kind, cells: sorted(new.repack(&self.pack).cells) };
        let changed: Vec<u64> = merge_sorted(&old, &new.cells, true, |a, b| (a != b).then_some(0.0))
            .into_iter()
            .map(|(k, _)| k)
            .collect();
        // 書き戻し。変更前のキーは上で集めたので、消すためにもう一度走査しない
        if r.is_all() {
            self.set_sorted(new.cells);
        } else {
            let old: Vec<u64> = old.iter().map(|c| c.0).collect();
            self.write_back(r, &old, new.cells);
        }
        Ok(self.region_of(&changed))
    }

    /// 全体を new に置き換えた新しい Store と、値が変わったセルの範囲（replace_diff と同じ形）。
    /// 自分は変えないので、置き換える前の Store を複製せずに取っておける。
    pub fn replaced_all(&self, new: &Cube) -> Result<(Store, Option<Vec<Vec<u32>>>)> {
        if !same_set(new.dims(), &self.metric_dims) {
            return Err("書き戻す結果の軸が Metric の軸と一致しない".into());
        }
        let mut old = Vec::with_capacity(self.base.keys.len() + self.delta.len());
        self.merged(0, None, |k, v| old.push((k, v)));
        let cells = sorted(new.repack(&self.pack).cells);
        let changed: Vec<u64> =
            merge_sorted(&old, &cells, true, |a, b| (a != b).then_some(0.0)).into_iter().map(|(k, _)| k).collect();
        let mut out = self.empty_like();
        out.set_sorted(cells);
        let sets = self.region_of(&changed);
        Ok((out, sets))
    }

    /// keys を囲む範囲（宣言した軸の順の、軸ごとのメンバー番号）。keys が空なら None。
    fn region_of(&self, keys: &[u64]) -> Option<Vec<Vec<u32>>> {
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
    fn value_at(&self, k: u64) -> Option<f64> {
        match self.delta.get(&k) {
            Some(v) => *v,
            None => self.base.keys.binary_search(&k).ok().map(|i| self.base.vals[i]),
        }
    }

    /// 1 セルの値（宣言した軸の順のメンバー番号）。空なら None。二分探索と差分の木の検索で済む。
    pub fn get(&self, key: &[u32]) -> Option<f64> {
        self.value_at(self.encode(key))
    }

    /// r の範囲の行を、宣言した軸の順のメンバー順に並べ、offset 件目から limit 件だけ返す
    /// （軸ごとのメンバー番号の列、値の列、範囲の全行数）。分割軸が宣言の先頭なら並べ直さずに済む。
    pub fn rows_in(&self, r: &Restrict, offset: usize, limit: Option<usize>) -> (Vec<Vec<u32>>, Vec<f64>, usize) {
        let mut cells = Vec::new();
        self.for_each_in(r, |k, v| cells.push((k, v)));
        let pos: Vec<usize> = self.metric_dims.iter().map(|d| self.pack.pos(*d).unwrap()).collect();
        if pos.windows(2).any(|w| w[0] > w[1]) {
            cells.sort_by_cached_key(|c| pos.iter().map(|&p| self.pack.get(c.0, p)).collect::<Vec<u32>>());
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

    fn encode(&self, key: &[u32]) -> u64 {
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

fn same_set(a: &[DimId], b: &[DimId]) -> bool {
    a.len() == b.len() && a.iter().all(|d| b.contains(d))
}

// ------------------------------------------------------------------ 式

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Op {
    Add,
    Sub,
    Mul,
    Div,
    Eq,
    Ne,
    Lt,
    Le,
    Gt,
    Ge,
    And,
    Or,
}

#[derive(Clone, Copy, Debug)]
pub enum Agg {
    Sum,
    Avg,
    Min,
    Max,
    Count,
    First, // 値が 1 つしかないグループ用（引き下ろしの内部で使う）
}

/// 式の構文木。Bin、If、IsBlank、IfBlank の Vec<DimId> は、軸にメンバーを追加したときに
/// 新しいメンバーへ値が広がる軸（影響範囲の計算に使う。評価には使わない）で、型検査（check.rs）が埋める。
/// By は型検査が式の軸を見て ByAgg か ByLookup に決めるので、評価には現れない。
#[derive(Debug)]
pub enum Node {
    Ref(usize), // 評価時に渡す読み出し元の番号
    DimRef(DimId), // 軸そのもの。値は各セルのメンバー番号
    Const(f64, Kind),
    MemberConst(DimId, u32), // 軸のメンバーの定数（Month."Mar"）。値はメンバー番号
    Bin(Op, Box<Node>, Box<Node>, Vec<DimId>),
    Not(Box<Node>),
    If(Box<Node>, Box<Node>, Option<Box<Node>>, Vec<DimId>),
    Filter(Box<Node>, Box<Node>),
    On(Box<Node>, Box<Node>),
    Expand(Box<Node>, Vec<DimId>),
    IsBlank(Box<Node>, Vec<DimId>),
    IfBlank(Box<Node>, f64, bool, Vec<DimId>), // 既定値と、それが真偽値か
    By { child: Box<Node>, src: DimId, dst: DimId, map: usize, agg: Option<Agg>, dim: String, prop: String },
    ByAgg { child: Box<Node>, src: DimId, dst: DimId, map: usize, agg: Agg },
    ByLookup { child: Box<Node>, src: DimId, dst: DimId, map: usize },
    Remove { child: Box<Node>, dim: DimId, agg: Agg },
    Shift { child: Box<Node>, dim: DimId, n: i64 },
    Select { child: Box<Node>, dim: DimId, member: u32, name: String },
    AsAxis { child: Box<Node>, dim: DimId },
    Coalesce(Box<Node>, Box<Node>), // 左に値があればそれ、なければ右
}

/// 式が読む Metric（格納データ、または評価の途中結果）。
#[derive(Clone)]
pub enum Src {
    Store(Arc<Store>),
    Cube(Arc<Cube>),
}

impl Src {
    pub fn read(&self, r: &Restrict) -> Cube {
        match self {
            Src::Store(s) => s.read(r),
            Src::Cube(c) => c.filter(r),
        }
    }
}

// ------------------------------------------------------------------ 評価

#[inline]
fn b2f(x: bool) -> f64 {
    if x {
        1.0
    } else {
        0.0
    }
}

fn scalar(op: Op, a: f64, b: f64) -> Option<f64> {
    match op {
        Op::Add => Some(a + b),
        Op::Sub => Some(a - b),
        Op::Mul => Some(a * b),
        Op::Div => (b != 0.0).then(|| a / b), // 0 除算は空
        Op::Eq => Some(b2f(a == b)),
        Op::Ne => Some(b2f(a != b)),
        Op::Lt => Some(b2f(a < b)),
        Op::Le => Some(b2f(a <= b)),
        Op::Gt => Some(b2f(a > b)),
        Op::Ge => Some(b2f(a >= b)),
        Op::And | Op::Or => unreachable!(),
    }
}

/// 三値論理。None は空。
fn kleene(op: Op, a: Option<f64>, b: Option<f64>) -> Option<f64> {
    let (a, b) = (a.map(|x| x != 0.0), b.map(|x| x != 0.0));
    match op {
        Op::And => {
            if a == Some(false) || b == Some(false) {
                Some(0.0)
            } else if a.is_none() || b.is_none() {
                None
            } else {
                Some(1.0)
            }
        }
        Op::Or => {
            if a == Some(true) || b == Some(true) {
                Some(1.0)
            } else if a.is_none() || b.is_none() {
                None
            } else {
                Some(0.0)
            }
        }
        _ => unreachable!(),
    }
}

fn merge(a: &[DimId], b: &[DimId]) -> Vec<DimId> {
    let mut out = a.to_vec();
    out.extend(b.iter().copied().filter(|d| !a.contains(d)));
    out
}

/// 並んだ 2 つのセル列を突き合わせる（sort-merge）。outer なら片側だけのキーも f に None を渡して残す。
fn merge_sorted(
    a: &[(u64, f64)],
    b: &[(u64, f64)],
    outer: bool,
    f: impl Fn(Option<f64>, Option<f64>) -> Option<f64>,
) -> Vec<(u64, f64)> {
    let mut out = Vec::with_capacity(if outer { a.len() + b.len() } else { a.len().min(b.len()) });
    let mut push = |k: u64, x: Option<f64>| {
        if let Some(x) = x {
            out.push((k, x));
        }
    };
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        let (x, y) = (a[i].0, b[j].0);
        if x == y {
            push(x, f(Some(a[i].1), Some(b[j].1)));
            i += 1;
            j += 1;
        } else if x < y {
            if outer {
                push(x, f(Some(a[i].1), None));
            }
            i += 1;
        } else {
            if outer {
                push(y, f(None, Some(b[j].1)));
            }
            j += 1;
        }
    }
    if outer {
        for &(k, v) in &a[i..] {
            push(k, f(Some(v), None));
        }
        for &(k, v) in &b[j..] {
            push(k, f(None, Some(v)));
        }
    }
    out
}

fn sorted(mut cells: Vec<(u64, f64)>) -> Vec<(u64, f64)> {
    sort_cells(&mut cells);
    cells
}

/// INNER JOIN。両側に値があるセルだけ結果を持つ。f が None を返したセルは空。
fn intersect(a: &Cube, b: &Cube, kind: Kind, cat: &Catalog, f: impl Fn(f64, f64) -> Option<f64> + Sync) -> Result<Cube> {
    // 片側が定数（軸なし）なら、結合せずに 1 回の走査で済む
    if b.dims().is_empty() {
        let Some(&(_, bv)) = b.cells.first() else { return Ok(Cube { pack: a.pack.clone(), kind, cells: Vec::new() }) };
        let cells = map_cells(&a.cells, |k, v| f(v, bv).map(|x| (k, x)));
        return Ok(Cube { pack: a.pack.clone(), kind, cells });
    }
    if a.dims().is_empty() {
        let Some(&(_, av)) = a.cells.first() else { return Ok(Cube { pack: b.pack.clone(), kind, cells: Vec::new() }) };
        let cells = map_cells(&b.cells, |k, v| f(av, v).map(|x| (k, x)));
        return Ok(Cube { pack: b.pack.clone(), kind, cells });
    }
    // 同じ軸どうしなら、並べて突き合わせる
    if same_set(a.dims(), b.dims()) {
        let (x, y) = (sorted(a.cells.clone()), sorted(b.repack(&a.pack).cells));
        let cells = merge_sorted(&x, &y, false, |p, q| f(p.unwrap(), q.unwrap()));
        return Ok(Cube { pack: a.pack.clone(), kind, cells });
    }

    let shared: Vec<DimId> = a.dims().iter().copied().filter(|d| b.dims().contains(d)).collect();
    let extra: Vec<DimId> = b.dims().iter().copied().filter(|d| !a.dims().contains(d)).collect();
    let out = Packing::new(&[a.dims(), &extra[..]].concat(), cat)?;
    let sk = Packing::new(&shared, cat)?;
    let (a_sk, b_sk) = (Proj::new(&a.pack, &sk), Proj::new(&b.pack, &sk));
    let (a_out, b_out) = (Proj::new(&a.pack, &out), Proj::new(&b.pack, &out));

    // b を共有する軸のキーで並べ、キーごとの区間を引けるようにする
    let entry = |&(k, v): &(u64, f64)| (b_sk.apply(&b.pack, &sk, k), b_out.apply(&b.pack, &out, k), v);
    let entries: Vec<(u64, u64, f64)> = if par(b.cells.len()) {
        let mut e: Vec<_> = b.cells.par_iter().map(entry).collect();
        e.par_sort_unstable_by_key(|e| e.0);
        e
    } else {
        let mut e: Vec<_> = b.cells.iter().map(entry).collect();
        e.sort_unstable_by_key(|e| e.0);
        e
    };
    let mut index: FxHashMap<u64, (usize, usize)> = FxHashMap::default();
    let mut i = 0;
    while i < entries.len() {
        let mut j = i;
        while j < entries.len() && entries[j].0 == entries[i].0 {
            j += 1;
        }
        index.insert(entries[i].0, (i, j));
        i = j;
    }

    let (index, entries, f) = (&index, &entries, &f);
    let cells = flat_map_cells(&a.cells, |k, v| {
        let range = index.get(&a_sk.apply(&a.pack, &sk, k)).copied().unwrap_or((0, 0));
        let base = a_out.apply(&a.pack, &out, k);
        entries[range.0..range.1].iter().filter_map(move |&(_, bk, bv)| f(v, bv).map(|x| (base | bk, x)))
    });
    Ok(Cube { pack: out, kind, cells })
}

/// 足りない軸の全メンバー（restrict の範囲）へ値を複製する。
fn expand(c: &Cube, dims: &[DimId], cat: &Catalog, r: &Restrict) -> Result<Cube> {
    let out = Packing::new(dims, cat)?;
    if out == c.pack {
        return Ok(c.clone());
    }
    let proj = Proj::new(&c.pack, &out);
    let missing: Vec<(usize, Vec<u32>)> = dims
        .iter()
        .enumerate()
        .filter(|(_, d)| !c.dims().contains(d))
        .map(|(i, &d)| (i, members(cat, d, r)))
        .collect();
    let combos = product(&out, &missing);
    let combos = &combos;
    let cells = flat_map_cells(&c.cells, |k, v| {
        let base = proj.apply(&c.pack, &out, k);
        combos.iter().map(move |&extra| (base | extra, v))
    });
    Ok(Cube { pack: out, kind: c.kind, cells })
}

/// 与えた軸（位置, メンバー一覧）の全組み合わせを、pack の詰め方のキーの部分として列挙する。
fn product(pack: &Packing, spans: &[(usize, Vec<u32>)]) -> Vec<u64> {
    let mut out = vec![0u64];
    for (pos, ms) in spans {
        let mut next = Vec::with_capacity(out.len() * ms.len());
        for &base in &out {
            for &m in ms {
                next.push(base | pack.put(*pos, m));
            }
        }
        out = next;
    }
    out
}

/// FULL OUTER JOIN（両側とも同じ詰め方）。片側だけのセルは f に None を渡す。
fn union(a: Cube, b: Cube, kind: Kind, f: impl Fn(Option<f64>, Option<f64>) -> Option<f64>) -> Cube {
    let (x, y) = (sorted(a.cells), sorted(b.cells));
    Cube { pack: a.pack, kind, cells: merge_sorted(&x, &y, true, f) }
}

#[derive(Clone, Copy)]
struct Acc {
    sum: f64,
    cnt: u64,
    min: f64,
    max: f64,
    first: f64,
}

impl Acc {
    fn new() -> Acc {
        Acc { sum: 0.0, cnt: 0, min: f64::INFINITY, max: f64::NEG_INFINITY, first: 0.0 }
    }
    fn add(&mut self, v: f64) {
        if self.cnt == 0 {
            self.first = v;
        }
        self.sum += v;
        self.cnt += 1;
        self.min = self.min.min(v);
        self.max = self.max.max(v);
    }
    fn finish(&self, agg: Agg) -> f64 {
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
fn group(mut pairs: Vec<(u64, f64)>, pack: Packing, agg: Agg) -> Cube {
    sort_cells(&mut pairs);
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

/// 片側がこれ以下の件数なら、もう片側の読み出しをその側に現れるメンバーに絞る（準結合）。
pub static SEMI_MAX: AtomicUsize = AtomicUsize::new(4096);

/// c に現れるメンバーだけに各軸を絞った範囲。c が大きければ None（絞っても得をしない）。
///
/// INNER JOIN の結果は c に値があるセルにしか残らないので、もう片側をこの範囲で評価しても
/// 結果は変わらない。もう片側が持たない軸の絞り込みは、その軸を外す演算が捨てるので害がない。
fn semi(r: &Restrict, c: &Cube, cat: &Catalog) -> Option<Restrict> {
    if c.dims().is_empty() || c.cells.len() > SEMI_MAX.load(Ordering::Relaxed) {
        return None;
    }
    let mut out = r.clone();
    for (i, &d) in c.dims().iter().enumerate() {
        let ms = c.cells.iter().map(|&(k, _)| c.pack.get(k, i)).collect();
        out = out.with(d, Sel::new(ms, cat.dims[d].size));
    }
    Some(out)
}

fn replace_dim(dims: &[DimId], old: DimId, new: DimId) -> Vec<DimId> {
    dims.iter().map(|&d| if d == old { new } else { d }).collect()
}

fn dense(c: &Cube, cat: &Catalog, r: &Restrict) -> Vec<u64> {
    let spans: Vec<(usize, Vec<u32>)> = c.dims().iter().enumerate().map(|(i, &d)| (i, members(cat, d, r))).collect();
    product(&c.pack, &spans)
}

pub fn eval(node: &Node, cat: &Catalog, src: &[Src], r: &Restrict) -> Result<Cube> {
    let ev = |n: &Node| eval(n, cat, src, r);
    match node {
        Node::Ref(i) => Ok(src[*i].read(r)),

        Node::DimRef(d) => {
            let pack = Packing::new(&[*d], cat)?;
            let cells = members(cat, *d, r).into_iter().map(|m| (pack.put(0, m), m as f64)).collect();
            Ok(Cube { pack, kind: Kind::Num, cells })
        }

        Node::Const(v, kind) => Ok(Cube { pack: Packing::new(&[], cat)?, kind: *kind, cells: vec![(0, *v)] }),
        Node::MemberConst(_, m) => Ok(Cube { pack: Packing::new(&[], cat)?, kind: Kind::Num, cells: vec![(0, *m as f64)] }),
        Node::By { .. } => Err("型を決めていない式は評価できない".into()),

        Node::Bin(op, l, rt, _) => {
            let a = ev(l)?;
            let b = match op {
                // INNER JOIN の演算は、左が小さければ右を左のメンバーに絞って評価する
                Op::Mul | Op::Div | Op::Eq | Op::Ne | Op::Lt | Op::Le | Op::Gt | Op::Ge => match semi(r, &a, cat) {
                    Some(r2) => eval(rt, cat, src, &r2)?,
                    None => ev(rt)?,
                },
                _ => ev(rt)?,
            };
            match op {
                Op::Mul | Op::Div => intersect(&a, &b, Kind::Num, cat, |x, y| scalar(*op, x, y)),
                Op::Eq | Op::Ne | Op::Lt | Op::Le | Op::Gt | Op::Ge => {
                    intersect(&a, &b, Kind::Bool, cat, |x, y| scalar(*op, x, y))
                }
                Op::Add | Op::Sub => {
                    let dims = merge(a.dims(), b.dims());
                    let (a, b) = (expand(&a, &dims, cat, r)?, expand(&b, &dims, cat, r)?);
                    let op = *op;
                    Ok(union(a, b, Kind::Num, move |x, y| scalar(op, x.unwrap_or(0.0), y.unwrap_or(0.0))))
                }
                Op::And | Op::Or => {
                    let dims = merge(a.dims(), b.dims());
                    let (a, b) = (expand(&a, &dims, cat, r)?, expand(&b, &dims, cat, r)?);
                    let op = *op;
                    Ok(union(a, b, Kind::Bool, move |x, y| kleene(op, x, y)))
                }
            }
        }

        Node::Not(c) => {
            let mut c = ev(c)?;
            for cell in &mut c.cells {
                cell.1 = b2f(cell.1 == 0.0);
            }
            Ok(c)
        }

        Node::If(cond, then, else_, _) => {
            // TRUE のセルは THEN と、FALSE のセルは ELSE と INNER JOIN する。空の条件はどちらにも入らない
            let c = ev(cond)?;
            let pick = |want: bool| Cube {
                pack: c.pack.clone(),
                kind: Kind::Bool,
                cells: map_cells(&c.cells, |k, v| ((v != 0.0) == want).then_some((k, v))),
            };
            // 各分岐は、条件がその値になるセルのメンバーに絞って評価する
            let branch = |n: &Node, part: &Cube| match semi(r, part, cat) {
                Some(r2) => eval(n, cat, src, &r2),
                None => eval(n, cat, src, r),
            };
            let (yes, no) = (pick(true), pick(false));
            let t = branch(then, &yes)?;
            let kind = t.kind;
            let mut parts = vec![intersect(&yes, &t, kind, cat, |_, y| Some(y))?];
            if let Some(e) = else_ {
                parts.push(intersect(&no, &branch(e, &no)?, kind, cat, |_, y| Some(y))?);
            }
            let dims = parts.iter().fold(Vec::new(), |acc, p| merge(&acc, p.dims()));
            let mut out = Cube { pack: Packing::new(&dims, cat)?, kind, cells: Vec::new() };
            for p in &parts {
                out.cells.extend(expand(p, &dims, cat, r)?.cells);
            }
            Ok(out)
        }

        Node::Filter(child, cond) => {
            let x = ev(child)?;
            let c = match semi(r, &x, cat) {
                Some(r2) => eval(cond, cat, src, &r2)?,
                None => ev(cond)?,
            };
            let keep = Cube { pack: c.pack.clone(), kind: Kind::Bool, cells: map_cells(&c.cells, |k, v| (v != 0.0).then_some((k, v))) };
            intersect(&x, &keep, x.kind, cat, |a, _| Some(a))
        }

        Node::Coalesce(first, second) => {
            let s = ev(second)?;
            let f = ev(first)?.repack(&s.pack);
            let kind = s.kind;
            Ok(union(f, s, kind, |a, b| a.or(b)))
        }

        Node::On(child, other) => {
            let x = ev(child)?;
            let o = match semi(r, &x, cat) {
                Some(r2) => eval(other, cat, src, &r2)?,
                None => ev(other)?,
            };
            intersect(&x, &o, x.kind, cat, |a, _| Some(a))
        }

        Node::Expand(child, dims) => {
            let c = ev(child)?;
            expand(&c, &[c.dims(), &dims[..]].concat(), cat, r)
        }

        Node::IsBlank(child, _) => {
            let c = ev(child)?;
            let present: FxHashSet<u64> = c.cells.iter().map(|c| c.0).collect();
            let cells = dense(&c, cat, r).into_iter().map(|k| (k, b2f(!present.contains(&k)))).collect();
            Ok(Cube { pack: c.pack, kind: Kind::Bool, cells })
        }

        Node::IfBlank(child, value, _, _) => {
            let c = ev(child)?;
            let present: FxHashMap<u64, f64> = c.cells.iter().copied().collect();
            let cells = dense(&c, cat, r).into_iter().map(|k| (k, *present.get(&k).unwrap_or(value))).collect();
            Ok(Cube { pack: c.pack, kind: c.kind, cells })
        }

        Node::ByAgg { child, src: s, dst, map, agg } => {
            // 集約: src のメンバーを dst のメンバーへ寄せて集計する
            let mp = &cat.maps[*map];
            let mut sub = r.without(&[*s, *dst]);
            if let Some(sel) = r.get(*dst) {
                let ms = (0..cat.dims[*s].size).filter(|&m| mp.fwd[m as usize] >= 0 && sel.has(mp.fwd[m as usize] as u32)).collect();
                sub = sub.with(*s, Sel::new(ms, cat.dims[*s].size));
            }
            let c = eval(child, cat, src, &sub)?;
            let out = Packing::new(&replace_dim(c.dims(), *s, *dst), cat)?;
            let (ps, pd) = (c.pack.pos(*s).unwrap(), out.pos(*dst).unwrap());
            let rest = Proj::new(&c.pack, &out);
            let pairs = map_cells(&c.cells, |k, v| {
                let t = mp.fwd[c.pack.get(k, ps) as usize];
                (t >= 0).then(|| (rest.apply(&c.pack, &out, k) | out.put(pd, t as u32), v))
            });
            Ok(group(pairs, out, *agg))
        }

        Node::ByLookup { child, src: s, dst, map } => {
            // 引き下ろし: dst の値を、そこへ対応する src の各メンバーへ配る
            let mp = &cat.maps[*map];
            let mut sub = r.without(&[*s, *dst]);
            let only = r.get(*s).cloned();
            if let Some(sel) = &only {
                let ts = sel.members.iter().filter(|&&m| mp.fwd[m as usize] >= 0).map(|&m| mp.fwd[m as usize] as u32).collect();
                sub = sub.with(*dst, Sel::new(ts, cat.dims[*dst].size));
            }
            let c = eval(child, cat, src, &sub)?;
            let out = Packing::new(&replace_dim(c.dims(), *dst, *s), cat)?;
            let (pt, ps) = (c.pack.pos(*dst).unwrap(), out.pos(*s).unwrap());
            let rest = Proj::new(&c.pack, &out);
            let mut cells = Vec::new();
            for &(k, v) in &c.cells {
                let base = rest.apply(&c.pack, &out, k);
                for &m in &mp.inv[c.pack.get(k, pt) as usize] {
                    if only.as_ref().is_none_or(|sel| sel.has(m)) {
                        cells.push((base | out.put(ps, m), v));
                    }
                }
            }
            Ok(Cube { pack: out, kind: c.kind, cells })
        }

        Node::Remove { child, dim, agg } => {
            let c = eval(child, cat, src, &r.without(&[*dim]))?;
            let dims: Vec<DimId> = c.dims().iter().copied().filter(|d| d != dim).collect();
            let out = Packing::new(&dims, cat)?;
            let proj = Proj::new(&c.pack, &out);
            let pairs = map_cells(&c.cells, |k, v| Some((proj.apply(&c.pack, &out, k), v)));
            Ok(group(pairs, out, *agg))
        }

        Node::AsAxis { child, dim } => {
            // メンバー番号を値に持つ Cube を、そのメンバーを dim の座標に持つ表（値は 1）にする
            let c = eval(child, cat, src, &r.without(&[*dim]))?;
            let out = Packing::new(&[c.dims(), &[*dim]].concat(), cat)?;
            let proj = Proj::new(&c.pack, &out);
            let (p, size) = (out.pos(*dim).unwrap(), cat.dims[*dim].size as f64);
            let cells = map_cells(&c.cells, |k, v| {
                (v >= 0.0 && v < size).then(|| (proj.apply(&c.pack, &out, k) | out.put(p, v as u32), 1.0))
            });
            Ok(Cube { pack: out, kind: Kind::Num, cells }.filter(r))
        }

        Node::Select { child, dim, member, .. } => {
            // member の切り口を取り出し、dim を外す
            let c = eval(child, cat, src, &r.with(*dim, Sel::new(vec![*member], cat.dims[*dim].size)))?;
            let p = c.pack.pos(*dim).unwrap();
            let dims: Vec<DimId> = c.dims().iter().copied().filter(|d| d != dim).collect();
            let out = Packing::new(&dims, cat)?;
            let proj = Proj::new(&c.pack, &out);
            let cells = map_cells(&c.cells, |k, v| (c.pack.get(k, p) == *member).then(|| (proj.apply(&c.pack, &out, k), v)));
            Ok(Cube { pack: out, kind: c.kind, cells })
        }

        Node::Shift { child, dim, n } => {
            let size = cat.dims[*dim].size as i64;
            let sub = match r.get(*dim) {
                Some(sel) => {
                    let ms = sel.members.iter().filter_map(|&t| {
                        let p = t as i64 - n;
                        (0..size).contains(&p).then_some(p as u32)
                    });
                    r.with(*dim, Sel::new(ms.collect(), size as u32))
                }
                None => r.clone(),
            };
            let c = eval(child, cat, src, &sub)?;
            let p = c.pack.pos(*dim).unwrap();
            let cells = map_cells(&c.cells, |k, v| {
                let t = c.pack.get(k, p) as i64 + n;
                (0..size).contains(&t).then(|| (c.pack.clear(k, p) | c.pack.put(p, t as u32), v))
            });
            Ok(Cube { pack: c.pack, kind: c.kind, cells }.filter(r))
        }
    }
}
