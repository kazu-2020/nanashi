//! 要素ごとの演算の融合。四則、比較、AND / OR、NOT、IF、FILTER、ON、Coalesce、EXPAND、ISBLANK、IFBLANK、
//! 軸の値（DimRef）、SELECT、時点のずらし（Shift）、引き下ろし（ByLookup）をつないだ式を、演算ごとの途中結果
//! （Cube）を作らずに、結果のキーを 1 つずつ走査して評価する。
//!
//! 各キーでの値は、式の木をそのキーでたどって求める（Cursor::eval）。読み出し元はキーを写して引く。
//! SELECT、Shift、引き下ろしは、子をたどるキーを変える（例: SELECT は消える軸にメンバーを足したキーで子を引く）。
//! 値の求め方は、演算ごとの評価（mod.rs の node_value）と同じ意味になるようにしてある。INNER JOIN の演算
//! （* / 比較、FILTER、ON）は両方に値があるときだけ、FULL OUTER JOIN の演算（+ - AND OR、Coalesce）は片方に
//! 値があれば、空の側を 0（AND / OR は三値論理の空）として求める。軸の少ない側を全メンバーへ広げる演算
//! （EXPAND、ISBLANK など）は、走査するキーを写して引くことと同じになる。
//!
//! 走査するキー（値がありうるキー）は、式の木から決める（Cover）。
//!
//! - 式の軸をすべて持つ読み出し元の和（Exact）: その読み出し元をキー順に突き合わせて走査する。
//!   同じ詰め方の読み出し元はカーソルで読み、結果はキー順に並ぶ。例: `Volume * Price`
//! - 1 つの読み出し元 × 足りない軸の全メンバー（Part）、または全組み合わせ（All）: 例: 社員 × 月の在籍
//!   （`Month >= HireMonth AND ...`）。結果は件数を数えてから書き、最後に並べ替える
//!
//! Part と All は、走査する数が、演算ごとに評価したときに必ず作る途中結果（読み出し元の行数や、
//! 全組み合わせへ広げた結果）の 4 倍以下のときだけ使う。疎な結合（`X[P,S] * Y[S,M]`）を全組み合わせで
//! 走査しないためである。
//!
//! 使うのは、範囲の絞り込みがない全体の評価で、読み出し元がすべて差分のない格納データか、
//! キー順に並んだ Cube のときだけ。当てはまらなければ None を返し、演算ごとに評価する。
//!
//! メモリの予算（Budget）には、結果の配列を確保する前に、その大きさを数える。演算ごとに評価したときに
//! 必ず作る途中結果（mat）さえ予算に収まらないなら、融合せずに演算ごとの評価に任せる（その演算が、
//! 確保する前に予算を超えることを知らせる）。

#[allow(unused_imports)]
use crate::*;

/// 読み出し元の列。キーの昇順に並び、番号で読める。
#[derive(Clone, Copy)]
enum Col<'a> {
    Store(&'a Store),
    Cells(&'a [(u64, f64)]),
}

impl Col<'_> {
    #[inline]
    fn n(&self) -> usize {
        match self {
            Col::Store(s) => s.base.keys.len(),
            Col::Cells(c) => c.len(),
        }
    }

    #[inline]
    fn key(&self, i: usize) -> u64 {
        match self {
            Col::Store(s) => s.base.keys[i],
            Col::Cells(c) => c[i].0,
        }
    }

    #[inline]
    fn val(&self, i: usize) -> f64 {
        match self {
            Col::Store(s) => s.base.vals[i],
            Col::Cells(c) => c[i].1,
        }
    }

    /// key 以上の最初の行。
    fn lower_bound(&self, key: u64) -> usize {
        match self {
            Col::Store(s) => s.base.keys.partition_point(|&k| k < key),
            Col::Cells(c) => c.partition_point(|x| x.0 < key),
        }
    }

    /// key 以上の最初の行を、前回の位置 from の近くから探す（前後どちらへも指数探索）。
    /// 続けて引くキーが近ければ、二分探索より速い。
    #[inline]
    fn seek(&self, from: usize, key: u64) -> usize {
        if from == 0 || from > self.n() || self.key(from - 1) < key {
            return self.gallop(from.min(self.n()), key);
        }
        // key(from - 1) >= key: 後ろへ戻る
        let mut step = 1;
        let mut hi = from - 1; // key(hi) >= key
        let mut lo = hi.saturating_sub(1);
        while lo > 0 && self.key(lo) >= key {
            hi = lo;
            step *= 2;
            lo = lo.saturating_sub(step);
        }
        if self.key(lo) >= key {
            return lo; // lo == 0
        }
        // key(lo) < key <= key(hi)
        let (mut l, mut h) = (lo + 1, hi);
        while l < h {
            let mid = (l + h) / 2;
            if self.key(mid) < key {
                l = mid + 1;
            } else {
                h = mid;
            }
        }
        l
    }

    /// from 以降で key 以上の最初の行（指数探索。キー順に進むカーソル用）。
    #[inline]
    fn gallop(&self, from: usize, key: u64) -> usize {
        let n = self.n();
        if from >= n || self.key(from) >= key {
            return from;
        }
        let mut step = 1;
        let mut lo = from; // key(lo) < key
        let mut hi = from + 1;
        while hi < n && self.key(hi) < key {
            lo = hi;
            step *= 2;
            hi = (hi + step).min(n);
        }
        let (mut l, mut h) = (lo + 1, hi);
        while l < h {
            let mid = (l + h) / 2;
            if self.key(mid) < key {
                l = mid + 1;
            } else {
                h = mid;
            }
        }
        l
    }

}

/// 読み出し元（src の番号ごとに 1 つ）。
struct Leaf<'a> {
    col: Col<'a>,
    pack: &'a Packing,
    kind: Kind,
}

/// 式の値があるキーを覆う範囲（式の軸に対して）。
#[derive(Clone, PartialEq)]
enum Cover {
    Exact(Vec<usize>), // 式の軸をすべて持つ読み出し元（leaves の番号）のキーの和
    Part(usize),       // 1 つの読み出し元のキー × 足りない軸の全メンバー
    All,               // 全組み合わせ
}

/// 式の木の静的な性質。
struct Info {
    dims: Vec<DimId>,
    kind: Kind,
    cover: Cover,
    /// 演算ごとに評価したときに必ず作る途中結果の最大の件数（読み出し元の行数や、全組み合わせへ
    /// 広げた結果の件数）。Part と All で走査してよいかの目安にする
    mat: f64,
    /// 軸の全組み合わせに値がある（ISBLANK や軸の値、定数、それを広げたもの）
    full: bool,
}

/// 各キーでの値の求め方（式の木を写したもの）。
enum F<'a> {
    Leaf(usize), // accesses の番号
    Const(f64),
    Dim { shift: u32, mask: u64 }, // キーのその軸のメンバー番号
    Bin(Op, Box<F<'a>>, Box<F<'a>>),
    Not(Box<F<'a>>),
    If(Box<F<'a>>, Box<F<'a>>, Option<Box<F<'a>>>),
    Filter(Box<F<'a>>, Box<F<'a>>),
    On(Box<F<'a>>, Box<F<'a>>),
    Coalesce(Box<F<'a>>, Box<F<'a>>),
    IsBlank(Box<F<'a>>),
    IfBlank(Box<F<'a>>, f64),
    /// 子を、消えた軸に member を足したキーで引く
    Select { at: Box<KeyMap>, member: u32, inner: Box<F<'a>> },
    /// 子を、軸 (shift, mask) のメンバーを n だけ戻したキーで引く（範囲の外なら空）
    Shift { shift: u32, mask: u64, n: i64, size: i64, inner: Box<F<'a>> },
    /// 子を、src の軸のメンバーを対応表で dst のメンバーに替えたキーで引く
    Lookup { at: Box<KeyMap>, src_shift: u32, src_mask: u64, fwd: &'a [i64], inner: Box<F<'a>> },
}

/// 親のキーから子のキーへ: dim 以外の共通の軸を写し、子の dim の軸に値を置く。
///
/// 親のキーの詰め方（ctx）は、その式の軸より多くの軸を持ちうる（式の木の上の演算の軸）。
/// 親のキーが dim を持っていても、その値は使わずに置き換える。
struct KeyMap {
    from: Packing,
    to: Packing,
    proj: Proj,
    pos: usize,
}

impl KeyMap {
    /// to の軸は、from の軸から drop を除き、dim を最後に足したもの。
    fn new(from: &Packing, drop: &[DimId], dim: DimId, cat: &Catalog) -> Option<KeyMap> {
        let dims: Vec<DimId> = from.dims.iter().copied().filter(|d| *d != dim && !drop.contains(d)).chain([dim]).collect();
        let to = Packing::new(&dims, cat).ok()?;
        let mut proj = Proj::new(from, &to);
        let pos = to.pos(dim).unwrap();
        proj.pairs.retain(|&(_, j)| j != pos);
        Some(KeyMap { from: from.clone(), to, proj, pos })
    }

    #[inline]
    fn apply(&self, key: u64, m: u32) -> u64 {
        self.proj.apply(&self.from, &self.to, key) | self.to.put(self.pos, m)
    }
}

/// 読み出し元を、あるキーの詰め方（ctx）からどう読むか。
enum Access<'a> {
    /// 走査と同じ詰め方。キー順に進むカーソルで読む
    Aligned(Col<'a>),
    /// キーを写して、全組み合わせの配列で引く（全組み合わせが小さいとき）
    Dense { ctx: Packing, proj: Proj, pack: &'a Packing, vals: Vec<f64>, has: Vec<bool> },
    /// キーを写して、前回引いた位置の近くから探す
    Search { ctx: Packing, proj: Proj, pack: &'a Packing, col: Col<'a> },
}

/// 全組み合わせの配列にする読み出し元の、キーのビット幅の上限（配列は 2^20 要素、約 9 MB まで）。
const DENSE_BITS: u32 = 20;

fn bits(pack: &Packing) -> u32 {
    pack.masks.iter().map(|m| m.count_ones()).sum()
}

impl<'a> Access<'a> {
    fn new(leaf: &Leaf<'a>, ctx: &Packing, aligned: bool) -> Access<'a> {
        if aligned && leaf.pack == ctx {
            return Access::Aligned(leaf.col);
        }
        let proj = Proj::new(ctx, leaf.pack);
        let width = bits(leaf.pack);
        // 配列の大きさが行数に比べて大きすぎなければ、配列で引く
        if width <= DENSE_BITS && (1usize << width) <= leaf.col.n().saturating_mul(64).max(1 << 12) {
            let size = 1usize << width;
            let (mut vals, mut has) = (vec![0.0; size], vec![false; size]);
            for i in 0..leaf.col.n() {
                let k = leaf.col.key(i) as usize;
                vals[k] = leaf.col.val(i);
                has[k] = true;
            }
            return Access::Dense { ctx: ctx.clone(), proj, pack: leaf.pack, vals, has };
        }
        Access::Search { ctx: ctx.clone(), proj, pack: leaf.pack, col: leaf.col }
    }
}

struct Fuse<'a> {
    cat: &'a Catalog,
    src: &'a [Src],
    leaves: Vec<Leaf<'a>>,
    ids: Vec<usize>, // leaves の番号 -> src の番号
}

impl<'a> Fuse<'a> {
    fn leaf(&mut self, i: usize) -> Option<usize> {
        if let Some(p) = self.ids.iter().position(|&x| x == i) {
            return Some(p);
        }
        let leaf = match &self.src[i] {
            Src::Store(s) if s.base_rows().is_some() => Leaf { col: Col::Store(s), pack: &s.pack, kind: s.kind },
            Src::Cube(c) if c.cells.windows(2).all(|w| w[0].0 < w[1].0) => Leaf { col: Col::Cells(&c.cells), pack: &c.pack, kind: c.kind },
            _ => return None,
        };
        self.leaves.push(leaf);
        self.ids.push(i);
        Some(self.leaves.len() - 1)
    }

    fn rows(&self, l: usize) -> f64 {
        self.leaves[l].col.n() as f64
    }

    /// 軸の全組み合わせの数。
    fn size(&self, dims: &[DimId]) -> f64 {
        dims.iter().map(|&d| self.cat.dims[d].size as f64).product()
    }

    /// cover が覆うキーの数（dims は式の軸）。
    fn cost(&self, c: &Cover, dims: &[DimId]) -> f64 {
        match c {
            Cover::Exact(ls) => ls.iter().map(|&l| self.rows(l)).sum(),
            Cover::Part(l) => {
                let missing: Vec<DimId> = dims.iter().copied().filter(|d| !self.leaves[*l].pack.dims.contains(d)).collect();
                self.rows(*l) * self.size(&missing)
            }
            Cover::All => self.size(dims),
        }
    }

    /// 軸 from の式の cover を、軸 to（from を含む）の式の cover として読み直す。
    fn lift(&self, c: Cover, from: &[DimId], to: &[DimId]) -> Cover {
        match c {
            Cover::Exact(ls) if same_set(from, to) => Cover::Exact(ls),
            Cover::Exact(ls) if ls.len() == 1 => Cover::Part(ls[0]),
            Cover::Exact(_) => Cover::All,
            c => c,
        }
    }

    /// INNER JOIN: 結果はどちらの cover にも覆われるので、狭いほうを使う。
    fn inner(&self, a: Cover, b: Cover, dims: &[DimId]) -> Cover {
        if self.cost(&b, dims) < self.cost(&a, dims) {
            b
        } else {
            a
        }
    }

    /// FULL OUTER JOIN: 結果は両方の cover の和に覆われる。
    fn outer(&self, a: Cover, b: Cover) -> Cover {
        match (a, b) {
            (Cover::Exact(mut x), Cover::Exact(y)) => {
                for l in y {
                    if !x.contains(&l) {
                        x.push(l);
                    }
                }
                Cover::Exact(x)
            }
            (a, b) if a == b => a,
            _ => Cover::All,
        }
    }

    fn info(&mut self, node: &Node) -> Option<Info> {
        let all = |dims: Vec<DimId>, kind: Kind, mat: f64| Info { dims, kind, cover: Cover::All, mat, full: true };
        Some(match node {
            Node::Ref(i) => {
                let l = self.leaf(*i)?;
                let leaf = &self.leaves[l];
                Info { dims: leaf.pack.dims.clone(), kind: leaf.kind, cover: Cover::Exact(vec![l]), mat: self.rows(l), full: false }
            }
            Node::Const(_, kind) => all(vec![], *kind, 1.0),
            Node::MemberConst(..) => all(vec![], Kind::Num, 1.0),
            Node::DimRef(d) => all(vec![*d], Kind::Num, self.size(&[*d])),
            Node::Bin(op, l, r, _) => {
                let (a, b) = (self.info(l)?, self.info(r)?);
                let dims = merge(&a.dims, &b.dims);
                let (ca, cb) = (self.lift(a.cover.clone(), &a.dims, &dims), self.lift(b.cover.clone(), &b.dims, &dims));
                let wide = |x: &Info| x.full && same_set(&x.dims, &dims);
                let (kind, cover, full) = match op {
                    Op::Mul | Op::Div => (Kind::Num, self.inner(ca, cb, &dims), false),
                    Op::Eq | Op::Ne | Op::Lt | Op::Le | Op::Gt | Op::Ge => (Kind::Bool, self.inner(ca, cb, &dims), false),
                    Op::Add | Op::Sub => (Kind::Num, self.outer(ca, cb), wide(&a) || wide(&b)),
                    Op::And | Op::Or => (Kind::Bool, self.outer(ca, cb), a.full && b.full),
                };
                let mat = a.mat.max(b.mat).max(if full { self.size(&dims) } else { 0.0 });
                Info { dims, kind, cover, mat, full }
            }
            Node::Not(c) => self.info(c)?,
            Node::If(c, t, e, _) => {
                let (c, t) = (self.info(c)?, self.info(t)?);
                let e = match e {
                    Some(e) => Some(self.info(e)?),
                    None => None,
                };
                let mut dims = merge(&c.dims, &t.dims);
                if let Some(e) = &e {
                    dims = merge(&dims, &e.dims);
                }
                // 値は、条件が真で THEN に値があるセルか、条件が偽で ELSE に値があるセルにだけある
                let ct = self.lift(t.cover, &t.dims, &dims);
                let branches = match &e {
                    Some(e) => {
                        let ce = self.lift(e.cover.clone(), &e.dims, &dims);
                        self.outer(ct, ce)
                    }
                    None => ct,
                };
                let cc = self.lift(c.cover, &c.dims, &dims);
                let cover = self.inner(cc, branches, &dims);
                let mat = c.mat.max(t.mat).max(e.as_ref().map_or(0.0, |e| e.mat));
                Info { dims, kind: t.kind, cover, mat, full: false }
            }
            Node::Filter(x, c) | Node::On(x, c) => {
                let (x, c) = (self.info(x)?, self.info(c)?);
                let dims = merge(&x.dims, &c.dims);
                let (cx, cc) = (self.lift(x.cover, &x.dims, &dims), self.lift(c.cover, &c.dims, &dims));
                Info { cover: self.inner(cx, cc, &dims), dims, kind: x.kind, mat: x.mat.max(c.mat), full: false }
            }
            Node::Coalesce(first, second) => {
                let (a, b) = (self.info(first)?, self.info(second)?);
                if !same_set(&a.dims, &b.dims) {
                    return None;
                }
                let full = a.full || b.full;
                Info { cover: self.outer(a.cover, b.cover), dims: b.dims, kind: b.kind, mat: a.mat.max(b.mat), full }
            }
            Node::Expand(child, more) => {
                let c = self.info(child)?;
                let dims = merge(&c.dims, more);
                let cover = self.lift(c.cover, &c.dims, &dims);
                let mat = c.mat.max(if c.full { self.size(&dims) } else { 0.0 });
                Info { dims, kind: c.kind, cover, mat, full: c.full }
            }
            Node::IsBlank(child, _) => {
                let c = self.info(child)?;
                let mat = c.mat.max(self.size(&c.dims));
                all(c.dims, Kind::Bool, mat)
            }
            Node::IfBlank(child, ..) => {
                let c = self.info(child)?;
                let mat = c.mat.max(self.size(&c.dims));
                all(c.dims, c.kind, mat)
            }
            Node::Select { child, dim, .. } => {
                let c = self.info(child)?;
                let dims: Vec<DimId> = c.dims.iter().copied().filter(|d| d != dim).collect();
                Info { dims, kind: c.kind, cover: Cover::All, mat: c.mat, full: false }
            }
            Node::Shift { child, .. } => {
                let c = self.info(child)?;
                Info { dims: c.dims, kind: c.kind, cover: Cover::All, mat: c.mat, full: false }
            }
            Node::ByLookup { child, src: s, dst, .. } => {
                let c = self.info(child)?;
                if c.dims.contains(s) {
                    return None;
                }
                Info { dims: replace_dim(&c.dims, *dst, *s), kind: c.kind, cover: Cover::All, mat: c.mat, full: false }
            }
            _ => return None,
        })
    }

    /// node を、キーの詰め方 ctx（node の軸）でたどる F にする。aligned なら、ctx と同じ詰め方の
    /// 読み出し元をカーソルで読む（ctx がキー順に走査されるときだけ）。
    fn build(&self, node: &Node, ctx: &Packing, aligned: bool, acc: &mut Vec<Access<'a>>) -> Option<F<'a>> {
        let go = |n: &Node, acc: &mut Vec<Access<'a>>| self.build(n, ctx, aligned, acc).map(Box::new);
        Some(match node {
            Node::Ref(i) => {
                let l = self.ids.iter().position(|x| x == i)?;
                acc.push(Access::new(&self.leaves[l], ctx, aligned));
                F::Leaf(acc.len() - 1)
            }
            Node::Const(v, _) => F::Const(*v),
            Node::MemberConst(_, m) => F::Const(*m as f64),
            Node::DimRef(d) => {
                let p = ctx.pos(*d)?;
                F::Dim { shift: ctx.shifts[p], mask: ctx.masks[p] }
            }
            Node::Bin(op, l, r, _) => F::Bin(*op, go(l, acc)?, go(r, acc)?),
            Node::Not(c) => F::Not(go(c, acc)?),
            Node::If(c, t, e, _) => {
                let (c, t) = (go(c, acc)?, go(t, acc)?);
                let e = match e {
                    Some(e) => Some(go(e, acc)?),
                    None => None,
                };
                F::If(c, t, e)
            }
            Node::Filter(x, c) => F::Filter(go(x, acc)?, go(c, acc)?),
            Node::On(x, c) => F::On(go(x, acc)?, go(c, acc)?),
            Node::Coalesce(a, b) => F::Coalesce(go(a, acc)?, go(b, acc)?),
            Node::Expand(c, _) => return self.build(c, ctx, aligned, acc),
            Node::IsBlank(c, _) => F::IsBlank(go(c, acc)?),
            Node::IfBlank(c, v, ..) => F::IfBlank(go(c, acc)?, *v),
            Node::Select { child, dim, member, .. } => {
                let at = KeyMap::new(ctx, &[], *dim, self.cat)?;
                let inner = Box::new(self.build(child, &at.to, false, acc)?);
                F::Select { at: Box::new(at), member: *member, inner }
            }
            Node::Shift { child, dim, n } => {
                let p = ctx.pos(*dim)?;
                let inner = Box::new(self.build(child, ctx, false, acc)?);
                F::Shift { shift: ctx.shifts[p], mask: ctx.masks[p], n: *n, size: self.cat.dims[*dim].size as i64, inner }
            }
            Node::ByLookup { child, src: s, dst, map } => {
                let p = ctx.pos(*s)?;
                let at = KeyMap::new(ctx, &[*s], *dst, self.cat)?;
                let inner = Box::new(self.build(child, &at.to, false, acc)?);
                let fwd = &self.cat.maps[*map].fwd[..];
                F::Lookup { at: Box::new(at), src_shift: ctx.shifts[p], src_mask: ctx.masks[p], fwd, inner }
            }
            _ => return None,
        })
    }
}

/// 走査の状態（1 つの仕事の単位ごと）。
struct Cursor<'s, 'a> {
    access: &'s [Access<'a>],
    pos: Vec<usize>, // Aligned と Search の読み出し元ごとの、前回の行
}

impl Cursor<'_, '_> {
    #[inline]
    fn get(&mut self, l: usize, key: u64) -> Option<f64> {
        match &self.access[l] {
            Access::Aligned(col) => {
                let p = col.gallop(self.pos[l], key);
                self.pos[l] = p;
                (p < col.n() && col.key(p) == key).then(|| col.val(p))
            }
            Access::Dense { ctx, proj, pack, vals, has } => {
                let k = proj.apply(ctx, pack, key) as usize;
                has[k].then(|| vals[k])
            }
            Access::Search { ctx, proj, pack, col } => {
                let k = proj.apply(ctx, pack, key);
                let p = col.seek(self.pos[l], k);
                self.pos[l] = p;
                (p < col.n() && col.key(p) == k).then(|| col.val(p))
            }
        }
    }

    fn eval(&mut self, f: &F, key: u64) -> Option<f64> {
        match f {
            F::Leaf(l) => self.get(*l, key),
            F::Const(v) => Some(*v),
            F::Dim { shift, mask } => Some(((key >> shift) & mask) as f64),
            F::Bin(op, a, b) => match op {
                Op::Mul | Op::Div | Op::Eq | Op::Ne | Op::Lt | Op::Le | Op::Gt | Op::Ge => {
                    let x = self.eval(a, key)?;
                    let y = self.eval(b, key)?;
                    scalar(*op, x, y)
                }
                Op::Add | Op::Sub => {
                    let (x, y) = (self.eval(a, key), self.eval(b, key));
                    if x.is_none() && y.is_none() {
                        return None;
                    }
                    scalar(*op, x.unwrap_or(0.0), y.unwrap_or(0.0))
                }
                Op::And | Op::Or => {
                    let (x, y) = (self.eval(a, key), self.eval(b, key));
                    if x.is_none() && y.is_none() {
                        return None;
                    }
                    kleene(*op, x, y)
                }
            },
            F::Not(c) => self.eval(c, key).map(|v| b2f(v == 0.0)),
            F::If(c, t, e) => {
                if self.eval(c, key)? != 0.0 {
                    self.eval(t, key)
                } else {
                    self.eval(e.as_ref()?, key)
                }
            }
            F::Filter(x, c) => {
                let v = self.eval(x, key)?;
                (self.eval(c, key)? != 0.0).then_some(v)
            }
            F::On(x, o) => {
                let v = self.eval(x, key)?;
                self.eval(o, key).map(|_| v)
            }
            F::Coalesce(a, b) => self.eval(a, key).or_else(|| self.eval(b, key)),
            F::IsBlank(c) => Some(b2f(self.eval(c, key).is_none())),
            F::IfBlank(c, v) => Some(self.eval(c, key).unwrap_or(*v)),
            F::Select { at, member, inner } => self.eval(inner, at.apply(key, *member)),
            F::Shift { shift, mask, n, size, inner } => {
                let s = ((key >> shift) & mask) as i64 - n;
                if !(0..*size).contains(&s) {
                    return None;
                }
                self.eval(inner, (key & !(mask << shift)) | ((s as u64) << shift))
            }
            F::Lookup { at, src_shift, src_mask, fwd, inner } => {
                let m = ((key >> src_shift) & src_mask) as usize;
                let t = *fwd.get(m)?;
                if t < 0 {
                    return None;
                }
                self.eval(inner, at.apply(key, t as u32))
            }
        }
    }
}

/// Part と All で走査してよい数: 演算ごとに評価したときに必ず作る途中結果の件数の、この倍まで。
const SPAN_RATIO: f64 = 4.0;

/// node を融合して評価する。融合できなければ Ok(None)。結果が予算を超えるなら Err。
pub(crate) fn fused(node: &Node, cat: &Catalog, src: &[Src], r: &Restrict, bud: &Budget) -> Result<Option<Cube>> {
    if !r.is_all() || matches!(node, Node::Ref(_) | Node::Const(..) | Node::MemberConst(..)) {
        return Ok(None);
    }
    let mut fu = Fuse { cat, src, leaves: Vec::new(), ids: Vec::new() };
    let Some(info) = fu.info(node) else { return Ok(None) };
    if info.mat > usize::MAX as f64 || bud.need(info.mat as usize, "").is_err() {
        return Ok(None);
    }
    fused_with(node, &mut fu, info, bud)
}

fn fused_with(node: &Node, fu: &mut Fuse, info: Info, bud: &Budget) -> Result<Option<Cube>> {
    let cat = fu.cat;
    let cfg = &cat.cfg;
    // 式の軸をすべて持つ読み出し元の和を、キー順に走査する（同じ詰め方であること）
    if let Cover::Exact(ls) = &info.cover {
        let out = fu.leaves[ls[0]].pack;
        if same_set(&out.dims, &info.dims) && ls.iter().all(|&l| fu.leaves[l].pack == out) {
            let mut access = Vec::new();
            let Some(f) = fu.build(node, out, true, &mut access) else { return Ok(None) };
            let drivers: Vec<Col> = ls.iter().map(|&l| fu.leaves[l].col).collect();
            bud.need(drivers.iter().map(|d| d.n()).sum(), "式の結果")?;
            let cells = run_sorted(cfg, &drivers, &access, &f);
            return Ok(Some(Cube { pack: out.clone(), kind: info.kind, cells }));
        }
    }
    // 1 つの読み出し元 × 足りない軸の全メンバー、または全組み合わせを走査する
    let cover = match info.cover {
        Cover::Exact(ls) if ls.len() == 1 => Cover::Part(ls[0]),
        Cover::Exact(_) => Cover::All,
        c => c,
    };
    if fu.cost(&cover, &info.dims) > SPAN_RATIO * info.mat {
        return Ok(None);
    }
    // 走査は、結果のキーの昇順に進む（base の行の順 × fill の軸の組み合わせの順）。結果のキーの軸の順を、
    // 最も大きい読み出し元の軸の順に合わせると、その読み出し元を引くキーもほぼ昇順に進み、近くから探せる
    let order: Vec<DimId> = match &cover {
        Cover::Part(l) => {
            let head = &fu.leaves[*l].pack.dims;
            head.iter().copied().chain(info.dims.iter().copied().filter(|d| !head.contains(d))).collect()
        }
        _ => {
            let big = (0..fu.leaves.len()).filter(|&l| same_set(&fu.leaves[l].pack.dims, &info.dims)).max_by_key(|&l| fu.leaves[l].col.n());
            big.map_or(info.dims.clone(), |l| fu.leaves[l].pack.dims.clone())
        }
    };
    let Ok(out) = Packing::new(&order, cat) else { return Ok(None) };
    let mut access = Vec::new();
    let Some(f) = fu.build(node, &out, false, &mut access) else { return Ok(None) };
    let base = match cover {
        Cover::Part(l) => Some((fu.leaves[l].col, Proj::new(fu.leaves[l].pack, &out), fu.leaves[l].pack)),
        _ => None,
    };
    let fill: Vec<(usize, Vec<u32>)> = out
        .dims
        .iter()
        .enumerate()
        .filter(|(_, d)| base.as_ref().is_none_or(|(_, _, p)| !p.dims.contains(d)))
        .map(|(i, &d)| (i, (0..cat.dims[d].size).collect()))
        .collect();
    let space = Space { base: base.as_ref().map(|(c, p, pk)| (*c, p, *pk)), out: &out, fill: &fill };
    let mut cells = run_space(cfg, &space, &access, &f, bud)?;
    if !cells.windows(2).all(|w| w[0].0 < w[1].0) {
        sort_cells(cfg, &mut cells);
    }
    Ok(Some(Cube { pack: out, kind: info.kind, cells }))
}

/// 被覆（drivers、同じ詰め方）のキーの和を順に走査し、各キーで f の値を求める。結果はキー順に並ぶ。
fn run_sorted(cfg: &Config, drivers: &[Col], access: &[Access], f: &F) -> Vec<(u64, f64)> {
    // 仕事の単位: 最も大きい被覆の cfg.chunk 行おきのキーで区切り、各被覆のその区間の行の範囲を求める
    let chunk = cfg.chunk.max(1);
    let big = (0..drivers.len()).max_by_key(|&i| drivers[i].n()).unwrap();
    let mut bounds: Vec<u64> = (chunk..drivers[big].n()).step_by(chunk).map(|i| drivers[big].key(i)).collect();
    bounds.dedup();
    let starts = |key: Option<u64>| -> Vec<usize> { drivers.iter().map(|d| key.map_or(d.n(), |k| d.lower_bound(k))).collect() };
    let mut ranges: Vec<Span> = Vec::with_capacity(bounds.len() + 1);
    let mut lo = vec![0; drivers.len()];
    for &k in &bounds {
        let hi = starts(Some(k));
        ranges.push((lo, hi.clone()));
        lo = hi;
    }
    ranges.push((lo, starts(None)));
    let sizes: Vec<usize> = ranges.iter().map(|(lo, hi)| lo.iter().zip(hi).map(|(a, b)| b - a).sum()).collect();
    let total: usize = sizes.iter().sum();

    // 結果は、各区間の行数の和を上限に 1 つの配列へ書き、空のセルを詰めてから切り詰める
    let mut cells = vec![(0u64, 0f64); total];
    let mut slices = Vec::with_capacity(ranges.len());
    let mut rest = &mut cells[..];
    for &n in &sizes {
        let (head, tail) = rest.split_at_mut(n);
        slices.push(head);
        rest = tail;
    }
    let work = |((lo, hi), slice): (&Span, &mut [(u64, f64)])| -> usize {
        let mut cur = Cursor { access, pos: vec![0; access.len()] };
        // Aligned の読み出し元は、区間の先頭のキーから読み始める
        let first = lo.iter().zip(hi).zip(drivers).filter(|((a, b), _)| a < b).map(|((a, _), d)| d.key(*a)).min();
        let Some(first) = first else { return 0 };
        for (l, a) in access.iter().enumerate() {
            if let Access::Aligned(col) = a {
                cur.pos[l] = col.lower_bound(first);
            }
        }
        let mut at = lo.clone();
        let mut w = 0;
        loop {
            // 被覆の次のキー（和を取る）
            let mut key = u64::MAX;
            let mut any = false;
            for (j, d) in drivers.iter().enumerate() {
                if at[j] < hi[j] {
                    key = key.min(d.key(at[j]));
                    any = true;
                }
            }
            if !any {
                break;
            }
            for (j, d) in drivers.iter().enumerate() {
                if at[j] < hi[j] && d.key(at[j]) == key {
                    at[j] += 1;
                }
            }
            if let Some(v) = cur.eval(f, key) {
                slice[w] = (key, v);
                w += 1;
            }
        }
        w
    };
    let written: Vec<usize> = if cfg.par(total) {
        ranges.par_iter().zip(slices.into_par_iter()).map(work).collect()
    } else {
        ranges.iter().zip(slices).map(work).collect()
    };
    compact(&mut cells, &sizes, &written);
    cells
}

/// 1 つの仕事の単位: 被覆ごとの行の範囲の始まりと終わり。
type Span = (Vec<usize>, Vec<usize>);

/// 走査するキーの集合: 読み出し元 base の各キー（out へ写す）× fill の軸の全メンバー。base がなければ
/// fill の全組み合わせ。番号 i のキーは、base の i / fill の数 行目と、fill の i % fill の数 番目の組み合わせ。
struct Space<'s, 'a> {
    base: Option<(Col<'a>, &'s Proj, &'a Packing)>,
    out: &'s Packing,
    fill: &'s [(usize, Vec<u32>)],
}

impl Space<'_, '_> {
    fn combos(&self) -> usize {
        self.fill.iter().map(|(_, ms)| ms.len()).product()
    }

    fn len(&self) -> usize {
        self.base.as_ref().map_or(1, |(c, _, _)| c.n()) * self.combos()
    }

    #[inline]
    fn key(&self, i: usize, combos: usize) -> u64 {
        let (row, mut c) = (i / combos, i % combos);
        let mut key = match &self.base {
            Some((col, proj, pack)) => proj.apply(pack, self.out, col.key(row)),
            None => 0,
        };
        for (pos, ms) in self.fill.iter().rev() {
            key |= self.out.put(*pos, ms[c % ms.len()]);
            c /= ms.len();
        }
        key
    }
}

/// space のキーを走査し、各キーで f の値を求める。件数を数えてから結果の配列を確保するので、
/// 結果の分しか持たない（各キーで 2 回求める）。base の読み出し元の軸が結果のキーの先頭に並べば、
/// 結果はキー順に並ぶ。
fn run_space(cfg: &Config, space: &Space, access: &[Access], f: &F, bud: &Budget) -> Result<Vec<(u64, f64)>> {
    let (n, combos) = (space.len(), space.combos());
    if n == 0 || combos == 0 {
        return Ok(Vec::new());
    }
    let chunk = cfg.chunk.max(1);
    let parts: Vec<(usize, usize)> = (0..n).step_by(chunk).map(|lo| (lo, (lo + chunk).min(n))).collect();
    let count = |&(lo, hi): &(usize, usize)| {
        let mut cur = Cursor { access, pos: vec![0; access.len()] };
        (lo..hi).filter(|&i| cur.eval(f, space.key(i, combos)).is_some()).count()
    };
    let sizes: Vec<usize> = if cfg.par(n) { parts.par_iter().map(count).collect() } else { parts.iter().map(count).collect() };
    bud.need(sizes.iter().sum(), "式の結果")?;
    let mut cells = vec![(0u64, 0f64); sizes.iter().sum()];
    let mut slices = Vec::with_capacity(parts.len());
    let mut rest = &mut cells[..];
    for &m in &sizes {
        let (head, tail) = rest.split_at_mut(m);
        slices.push(head);
        rest = tail;
    }
    let write = |(&(lo, hi), slice): (&(usize, usize), &mut [(u64, f64)])| {
        let mut cur = Cursor { access, pos: vec![0; access.len()] };
        let mut w = 0;
        for i in lo..hi {
            let key = space.key(i, combos);
            if let Some(v) = cur.eval(f, key) {
                slice[w] = (key, v);
                w += 1;
            }
        }
    };
    if cfg.par(n) {
        parts.par_iter().zip(slices.into_par_iter()).for_each(write);
    } else {
        parts.iter().zip(slices).for_each(write);
    }
    Ok(cells)
}

/// 区間ごとに書いた先頭 written[i] 件（区間の大きさは sizes[i]）を前へ詰め、余りを切り詰める。
/// 空きが大きければ、確保した領域も縮める。
pub(crate) fn compact(cells: &mut Vec<(u64, f64)>, sizes: &[usize], written: &[usize]) {
    let (mut from, mut to) = (0, 0);
    for (&n, &w) in sizes.iter().zip(written) {
        if from != to {
            cells.copy_within(from..from + w, to);
        }
        from += n;
        to += w;
    }
    cells.truncate(to);
    if cells.len() < cells.capacity() / 4 * 3 {
        cells.shrink_to_fit();
    }
}

/// i = 0..n の各行を f で写し、None を捨てた列（件数が多ければ並列に）。結果は 1 つの配列へ直接書き、
/// 写す前に全体を集めて写し直すことはしない（途中で持つのは、n 件分の結果の配列だけ）。
pub(crate) fn collect_rows(cfg: &Config, n: usize, f: impl Fn(usize) -> Option<(u64, f64)> + Sync) -> Vec<(u64, f64)> {
    if !cfg.par(n) {
        return (0..n).filter_map(f).collect();
    }
    let chunk = cfg.chunk.max(1);
    let mut cells = vec![(0u64, 0f64); n];
    let sizes: Vec<usize> = (0..n).step_by(chunk).map(|lo| chunk.min(n - lo)).collect();
    let written: Vec<usize> = cells
        .par_chunks_mut(chunk)
        .enumerate()
        .map(|(c, slice)| {
            let mut w = 0;
            for i in c * chunk..c * chunk + slice.len() {
                if let Some(x) = f(i) {
                    slice[w] = x;
                    w += 1;
                }
            }
            w
        })
        .collect();
    compact(&mut cells, &sizes, &written);
    cells
}
