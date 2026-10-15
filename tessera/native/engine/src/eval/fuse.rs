//! 要素ごとの演算の融合。四則、比較、AND / OR、NOT、IF、FILTER、ON、Coalesce、EXPAND、ISBLANK、IFBLANK、
//! 軸の値（DimRef）、SELECT、時点のずらし（Shift）、引き下ろし（ByLookup）をつないだ式を、演算ごとの途中結果
//! （Cube）を作らずに、結果のキーを 1 つずつ走査して評価する。
//!
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
//! Fusion applies only to a full evaluation with no range restriction, and only when each source
//! is stored data or a Cube in key order. Stored data with a delta is first merged into a Cube in
//! key order (one copy of its rows). Otherwise `fused` returns None and the engine evaluates one
//! operation at a time.
//!
//! メモリの予算（Budget）には、結果の配列を確保する前に、その大きさを数える。演算ごとに評価したときに
//! 必ず作る途中結果（mat）さえ予算に収まらないなら、融合せずに演算ごとの評価に任せる（その演算が、
//! 確保する前に予算を超えることを知らせる）。

#[allow(unused_imports)]
use crate::*;
use std::cell::OnceCell;

/// 読み出し元の列。キーの昇順に並び、番号で読める。
#[derive(Clone, Copy)]
enum Col<'a> {
    Store(&'a Store),
    Cells(&'a [(u64, f64)]),
}

impl<'a> Col<'a> {
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
            Col::Store(s) => s.base.keys.get(i),
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
            Col::Store(s) => s.base.keys.partition_point(|k| k < key),
            Col::Cells(c) => c.partition_point(|x| x.0 < key),
        }
    }

    /// For a `Col::Store` it decodes one packed block at a time.
    fn cursor(&self) -> Cursor<'a> {
        Cursor::new(match self {
            Col::Store(s) => Sorted::Packed(&s.base.keys),
            Col::Cells(c) => Sorted::Cells(c),
        })
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
            let mut rd = leaf.col.cursor();
            for i in 0..leaf.col.n() {
                let k = rd.key(i) as usize;
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
    bud: &'a Budget,
    merged: &'a [OnceCell<Cube>], // Per src index: the stored data merged with its delta, made on first use
    copied: usize,                // The cell capacity of `merged`. Later budget checks add it, because Budget::need does not keep it
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
            Src::Store(s) => {
                // The merge of the base and the delta is in key order. The copy counts against the budget.
                self.bud.need(self.copied + s.rows_hint(), "").ok()?;
                let c = self.merged[i].get_or_init(|| s.read(&Restrict::all(self.cat.dims.len())));
                self.copied += c.cells.capacity(); // the Vec of the merge can grow past its length
                Leaf { col: Col::Cells(&c.cells), pack: &s.pack, kind: s.kind }
            }
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

/// The slice holds one `Cursor` for each access (the last row and the decoded block of each `Aligned`
/// and `Search` access; a `Dense` access gets an empty placeholder).
type Ev<'s> = Box<dyn Fn(&mut [Cursor], u64) -> Option<f64> + Sync + 's>;

fn readers<'a>(access: &[Access<'a>]) -> Vec<Cursor<'a>> {
    access
        .iter()
        .map(|a| match a {
            Access::Aligned(col) | Access::Search { col, .. } => col.cursor(),
            Access::Dense { .. } => Cursor::new(Sorted::Cells(&[])),
        })
        .collect()
}

fn compile<'s>(f: &'s F, acc: &'s [Access]) -> Ev<'s> {
    let go = |x: &'s F| compile(x, acc);
    match f {
        F::Leaf(l) => leaf(*l, &acc[*l]),
        F::Const(v) => {
            let v = *v;
            Box::new(move |_, _| Some(v))
        }
        F::Dim { shift, mask } => {
            let (shift, mask) = (*shift, *mask);
            Box::new(move |_, key| Some(((key >> shift) & mask) as f64))
        }
        F::Bin(op, a, b) => match op {
            // One arm for each operator gives `scalar` a constant operator. One shared arm was 18% slower.
            Op::Mul => inner_join(a, b, acc, |x, y| scalar(Op::Mul, x, y)),
            Op::Div => inner_join(a, b, acc, |x, y| scalar(Op::Div, x, y)),
            Op::Eq => inner_join(a, b, acc, |x, y| scalar(Op::Eq, x, y)),
            Op::Ne => inner_join(a, b, acc, |x, y| scalar(Op::Ne, x, y)),
            Op::Lt => inner_join(a, b, acc, |x, y| scalar(Op::Lt, x, y)),
            Op::Le => inner_join(a, b, acc, |x, y| scalar(Op::Le, x, y)),
            Op::Gt => inner_join(a, b, acc, |x, y| scalar(Op::Gt, x, y)),
            Op::Ge => inner_join(a, b, acc, |x, y| scalar(Op::Ge, x, y)),
            Op::Add | Op::Sub => {
                let (op, a, b) = (*op, go(a), go(b));
                Box::new(move |p, key| {
                    let (x, y) = (a(p, key), b(p, key));
                    if x.is_none() && y.is_none() {
                        return None;
                    }
                    scalar(op, x.unwrap_or(0.0), y.unwrap_or(0.0))
                })
            }
            Op::And | Op::Or => {
                let (op, a, b) = (*op, go(a), go(b));
                Box::new(move |p, key| {
                    let (x, y) = (a(p, key), b(p, key));
                    if x.is_none() && y.is_none() {
                        return None;
                    }
                    kleene(op, x, y)
                })
            }
        },
        F::Not(c) => {
            let c = go(c);
            Box::new(move |p, key| c(p, key).map(|v| b2f(v == 0.0)))
        }
        F::If(c, t, e) => {
            let (c, t, e) = (go(c), go(t), e.as_deref().map(go));
            Box::new(move |p, key| if c(p, key)? != 0.0 { t(p, key) } else { e.as_ref()?(p, key) })
        }
        F::Filter(x, c) => {
            let (x, c) = (go(x), go(c));
            Box::new(move |p, key| {
                let v = x(p, key)?;
                (c(p, key)? != 0.0).then_some(v)
            })
        }
        F::On(x, o) => {
            let (x, o) = (go(x), go(o));
            Box::new(move |p, key| {
                let v = x(p, key)?;
                o(p, key).map(|_| v)
            })
        }
        F::Coalesce(a, b) => {
            let (a, b) = (go(a), go(b));
            Box::new(move |p, key| a(p, key).or_else(|| b(p, key)))
        }
        F::IsBlank(c) => {
            let c = go(c);
            Box::new(move |p, key| Some(b2f(c(p, key).is_none())))
        }
        F::IfBlank(c, v) => {
            let (c, v) = (go(c), *v);
            Box::new(move |p, key| Some(c(p, key).unwrap_or(v)))
        }
        F::Select { at, member, inner } => {
            let (at, member, inner) = (&**at, *member, go(inner));
            Box::new(move |p, key| inner(p, at.apply(key, member)))
        }
        F::Shift { shift, mask, n, size, inner } => {
            let (shift, mask, n, size, inner) = (*shift, *mask, *n, *size, go(inner));
            Box::new(move |p, key| {
                let s = ((key >> shift) & mask) as i64 - n;
                if !(0..size).contains(&s) {
                    return None;
                }
                inner(p, (key & !(mask << shift)) | ((s as u64) << shift))
            })
        }
        F::Lookup { at, src_shift, src_mask, fwd, inner } => {
            let (at, src_shift, src_mask, fwd, inner) = (&**at, *src_shift, *src_mask, *fwd, go(inner));
            Box::new(move |p, key| {
                let t = *fwd.get(((key >> src_shift) & src_mask) as usize)?;
                if t < 0 {
                    return None;
                }
                inner(p, at.apply(key, t as u32))
            })
        }
    }
}

fn leaf<'s>(l: usize, a: &'s Access) -> Ev<'s> {
    match a {
        Access::Aligned(col) => {
            let col = *col;
            Box::new(move |p, key| {
                let rd = &mut p[l];
                let i = rd.gallop(key);
                (i < col.n() && rd.key(i) == key).then(|| col.val(i))
            })
        }
        Access::Dense { ctx, proj, pack, vals, has } => Box::new(move |_, key| {
            let k = proj.apply(ctx, pack, key) as usize;
            has[k].then(|| vals[k])
        }),
        Access::Search { ctx, proj, pack, col } => {
            let col = *col;
            Box::new(move |p, key| {
                let k = proj.apply(ctx, pack, key);
                let rd = &mut p[l];
                let i = rd.seek(k);
                (i < col.n() && rd.key(i) == k).then(|| col.val(i))
            })
        }
    }
}

fn inner_join<'s>(a: &'s F, b: &'s F, acc: &'s [Access], g: impl Fn(f64, f64) -> Option<f64> + Sync + 's) -> Ev<'s> {
    match (a, b) {
        (_, F::Const(y)) => {
            let (a, y) = (compile(a, acc), *y);
            Box::new(move |p, key| g(a(p, key)?, y))
        }
        (F::Const(x), _) => {
            let (x, b) = (*x, compile(b, acc));
            Box::new(move |p, key| g(x, b(p, key)?))
        }
        _ => {
            let (a, b) = (compile(a, acc), compile(b, acc));
            Box::new(move |p, key| {
                let x = a(p, key)?;
                g(x, b(p, key)?)
            })
        }
    }
}

/// Part と All で走査してよい数: 演算ごとに評価したときに必ず作る途中結果の件数の、この倍まで。
const SPAN_RATIO: f64 = 4.0;

/// Evaluates `node` as one fused loop. Gives Ok(None) if the engine cannot fuse it.
/// Gives Err if the result does not fit in `bud`.
pub(crate) fn fused(node: &Node, cat: &Catalog, src: &[Src], r: &Restrict, bud: &Budget) -> Result<Option<Cube>> {
    if !r.is_all() || matches!(node, Node::Ref(_) | Node::Const(..) | Node::MemberConst(..)) {
        return Ok(None);
    }
    let merged: Vec<OnceCell<Cube>> = src.iter().map(|_| OnceCell::new()).collect();
    let mut fu = Fuse { cat, src, bud, merged: &merged, copied: 0, leaves: Vec::new(), ids: Vec::new() };
    let Some(info) = fu.info(node) else { return Ok(None) };
    if !fits(info.mat, fu.copied, bud) {
        return Ok(None);
    }
    fused_with(node, &mut fu, info, bud)
}

/// True if `mat` estimated cells and `copied` cells fit in `bud`. An estimate of `2^64` or more does not fit.
fn fits(mat: f64, copied: usize, bud: &Budget) -> bool {
    mat < usize::MAX as f64 && bud.need((mat as usize).saturating_add(copied), "").is_ok()
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
            bud.need(drivers.iter().map(|d| d.n()).sum::<usize>() + fu.copied, "式の結果")?;
            let cells = run_sorted(cfg, &drivers, &access, &compile(&f, &access));
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
    let mut cells = run_space(cfg, &space, &access, &compile(&f, &access), bud, fu.copied)?;
    if !cells.windows(2).all(|w| w[0].0 < w[1].0) {
        sort_cells(cfg, &mut cells);
    }
    Ok(Some(Cube { pack: out, kind: info.kind, cells }))
}

/// Scan the union of the keys of the drivers (same packing) in order, and get the value of f at each key.
/// The result is in key order.
fn run_sorted(cfg: &Config, drivers: &[Col], access: &[Access], f: &Ev) -> Vec<(u64, f64)> {
    // A unit of work: split at every cfg.chunk-th key of the largest driver, and find the row range of each driver
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

    // Write the result into one array with room for all rows of each range, then remove the gaps and truncate
    let mut cells = vec![(0u64, 0f64); total];
    let mut slices = Vec::with_capacity(ranges.len());
    let mut rest = &mut cells[..];
    for &n in &sizes {
        let (head, tail) = rest.split_at_mut(n);
        slices.push(head);
        rest = tail;
    }
    let work = |((lo, hi), slice): (&Span, &mut [(u64, f64)])| -> usize {
        let mut readers = readers(access);
        // Each Aligned access starts to read at the first key of the range
        let first = lo.iter().zip(hi).zip(drivers).filter(|((a, b), _)| a < b).map(|((a, _), d)| d.key(*a)).min();
        let Some(first) = first else { return 0 };
        for (l, a) in access.iter().enumerate() {
            if let Access::Aligned(col) = a {
                readers[l].pos = col.lower_bound(first);
            }
        }
        let mut at = lo.clone();
        let mut rds: Vec<Cursor> = drivers.iter().map(Col::cursor).collect();
        // The key at `at[j]` while `at[j] < hi[j]`. A packed key read is not free, so read it once.
        let mut next: Vec<u64> = rds.iter_mut().zip(&at).zip(hi).map(|((d, &a), &h)| if a < h { d.key(a) } else { 0 }).collect();
        let mut w = 0;
        while let Some(key) = (0..drivers.len()).filter(|&j| at[j] < hi[j]).map(|j| next[j]).min() {
            for (j, d) in rds.iter_mut().enumerate() {
                if at[j] < hi[j] && next[j] == key {
                    at[j] += 1;
                    if at[j] < hi[j] {
                        next[j] = d.key(at[j]);
                    }
                }
            }
            if let Some(v) = f(&mut readers, key) {
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

impl<'a> Space<'_, 'a> {
    fn combos(&self) -> usize {
        combos(self.fill)
    }

    fn len(&self) -> usize {
        self.base.as_ref().map_or(1, |(c, _, _)| c.n()).saturating_mul(self.combos())
    }

    fn cursor(&self) -> Cursor<'a> {
        self.base.as_ref().map_or(Col::Cells(&[]), |(c, _, _)| *c).cursor()
    }

    /// The key at `i`. `rd` is `self.cursor()`.
    #[inline]
    fn key(&self, i: usize, combos: usize, rd: &mut Cursor) -> u64 {
        let (row, mut c) = (i / combos, i % combos);
        let mut key = match &self.base {
            Some((_, proj, pack)) => proj.apply(pack, self.out, rd.key(row)),
            None => 0,
        };
        for (pos, ms) in self.fill.iter().rev() {
            key |= self.out.put(*pos, ms[c % ms.len()]);
            c /= ms.len();
        }
        key
    }
}

/// Scan the keys of space, and get the value of f at each key. It counts the values before it makes the
/// result array, so the array is only as large as the result (f runs two times at each key).
/// If the dimensions of the base source come first in the result key, the result is in key order.
fn run_space(cfg: &Config, space: &Space, access: &[Access], f: &Ev, bud: &Budget, copied: usize) -> Result<Vec<(u64, f64)>> {
    let (n, combos) = (space.len(), space.combos());
    if n == 0 || combos == 0 {
        return Ok(Vec::new());
    }
    let chunk = cfg.chunk.max(1);
    // parts, sizes and slices use 40 bytes for each part, which is less than 3 cells. Check them before they exist.
    let lists = n.div_ceil(chunk).saturating_mul(3).saturating_add(copied);
    bud.need(lists, "式の結果")?;
    let parts: Vec<(usize, usize)> = (0..n).step_by(chunk).map(|lo| (lo, (lo + chunk).min(n))).collect();
    let count = |&(lo, hi): &(usize, usize)| {
        let (mut readers, mut rd) = (readers(access), space.cursor());
        (lo..hi).filter(|&i| f(&mut readers, space.key(i, combos, &mut rd)).is_some()).count()
    };
    let sizes: Vec<usize> = if cfg.par(n) { parts.par_iter().map(count).collect() } else { parts.iter().map(count).collect() };
    bud.need(sizes.iter().sum::<usize>().saturating_add(lists), "式の結果")?;
    let mut cells = vec![(0u64, 0f64); sizes.iter().sum()];
    let mut slices = Vec::with_capacity(parts.len());
    let mut rest = &mut cells[..];
    for &m in &sizes {
        let (head, tail) = rest.split_at_mut(m);
        slices.push(head);
        rest = tail;
    }
    let write = |(&(lo, hi), slice): (&(usize, usize), &mut [(u64, f64)])| {
        let (mut readers, mut rd) = (readers(access), space.cursor());
        let mut w = 0;
        for i in lo..hi {
            let key = space.key(i, combos, &mut rd);
            if let Some(v) = f(&mut readers, key) {
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

#[cfg(test)]
mod tests {
    use super::fits;
    use crate::Budget;

    #[test]
    fn an_estimate_of_2_to_the_64_does_not_fit() {
        let bud = Budget::new(1 << 40, 0);
        assert!(!fits(2f64.powi(64), 1, &bud));
        assert!(!fits(2f64.powi(64), 0, &bud));
        assert!(fits(1000.0, 1, &bud));
    }
}
