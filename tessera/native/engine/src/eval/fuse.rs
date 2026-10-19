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
            Col::Store(s) => s.base.rows(),
            Col::Cells(c) => c.len(),
        }
    }

    #[inline]
    fn key(&self, i: usize) -> u64 {
        match self {
            Col::Store(s) => s.base.key(i),
            Col::Cells(c) => c[i].0,
        }
    }

    /// key 以上の最初の行。
    fn lower_bound(&self, key: u64) -> usize {
        match self {
            Col::Store(s) => s.base.lower_bound(key),
            Col::Cells(c) => c.partition_point(|x| x.0 < key),
        }
    }

    /// For a `Col::Store` it decodes one packed block at a time.
    fn cursor(&self) -> Cursor<'a> {
        Cursor::new(match self {
            Col::Store(s) => Sorted::Base(&s.base),
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
    /// A Dense access uses at most `room` cells of the budget. If it needs more, it is a Search access.
    fn new(leaf: &Leaf<'a>, ctx: &Packing, aligned: bool, room: usize) -> Access<'a> {
        if aligned && leaf.pack == ctx {
            return Access::Aligned(leaf.col);
        }
        let proj = Proj::new(ctx, leaf.pack);
        let width = bits(leaf.pack);
        if width <= DENSE_BITS {
            // If the array is not too large for the row count, look up the keys in an array
            let size = 1usize << width;
            if size <= leaf.col.n().saturating_mul(64).max(1 << 12) && dense_cells(size) <= room {
                let (mut vals, mut has) = (vec![0.0; size], vec![false; size]);
                let mut rd = leaf.col.cursor();
                for i in 0..leaf.col.n() {
                    let k = rd.key(i) as usize;
                    vals[k] = rd.val(i);
                    has[k] = true;
                }
                return Access::Dense { ctx: ctx.clone(), proj, pack: leaf.pack, vals, has };
            }
        }
        Access::Search { ctx: ctx.clone(), proj, pack: leaf.pack, col: leaf.col }
    }

    /// The budget cells of the lookup arrays of a Dense access. Other accesses have no lookup arrays.
    fn cells(&self) -> usize {
        match self {
            Access::Dense { vals, .. } => dense_cells(vals.len()),
            _ => 0,
        }
    }
}

/// The budget cells of the 2 lookup arrays (an f64 and a bool for each key) of a Dense access with `size` keys.
fn dense_cells(size: usize) -> usize {
    (size * 9).div_ceil(CELL_BYTES)
}

struct Fuse<'a> {
    cat: &'a Catalog,
    src: &'a [Src],
    bud: &'a Budget,
    merged: &'a [OnceCell<Cube>], // Per src index: the stored data merged with its delta, made on first use
    copied: usize,                // The cell capacity of `merged`. Later budget checks add it, because Budget::need does not keep it
    reserve: usize,               // The cells that the scan needs after `build`. A Dense access leaves room for them
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
                let taken = self.copied.saturating_add(self.reserve).saturating_add(acc.iter().map(Access::cells).sum());
                acc.push(Access::new(&self.leaves[l], ctx, aligned, self.bud.room().saturating_sub(taken)));
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
                (i < col.n() && rd.key(i) == key).then(|| rd.val(i))
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
                (i < col.n() && rd.key(i) == k).then(|| rd.val(i))
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

/// Where a fused scan puts its cells: one part at a time, parts in key order, cells of a part in key order.
pub(crate) trait Sink: Send {
    fn part(&mut self, cells: Vec<(u64, f64)>) -> Result<()>;

    /// A sink that is one array gives it with room for `sizes[i]` cells of part i. Then all parts write
    /// in parallel, each at its own offset, and `compact` removes the gaps. `StoreSink` gives None.
    fn presized(&mut self, sizes: &[usize]) -> Option<&mut Vec<(u64, f64)>> {
        let _ = sizes;
        None
    }

    /// After `run_space`: an array sink sorts its cells if they are not in key order. `StoreSink` refuses
    /// a key out of order as it arrives, so it does nothing here.
    fn sort_if_unsorted(&mut self, cfg: &Config) {
        let _ = cfg;
    }
}

impl Sink for Vec<(u64, f64)> {
    fn part(&mut self, cells: Vec<(u64, f64)>) -> Result<()> {
        self.extend(cells);
        Ok(())
    }

    fn presized(&mut self, sizes: &[usize]) -> Option<&mut Vec<(u64, f64)>> {
        *self = vec![(0u64, 0f64); sizes.iter().sum()];
        Some(self)
    }

    fn sort_if_unsorted(&mut self, cfg: &Config) {
        if !self.windows(2).all(|w| w[0].0 < w[1].0) {
            sort_cells(cfg, self);
        }
    }
}

/// Evaluates `node` as one fused loop. Gives Ok(None) if the engine cannot fuse it.
/// Gives Err if the result does not fit in `bud`.
pub(crate) fn fused(node: &Node, cat: &Catalog, src: &[Src], r: &Restrict, bud: &Budget) -> Result<Option<Cube>> {
    let make = |_: &Packing, need: Need| bud.need(need.rows.saturating_add(need.extra), "式の結果").map(|_| Vec::new());
    let Some((pack, kind, cells)) = fused_into(node, cat, src, r, bud, None, make)? else { return Ok(None) };
    Ok(Some(Cube { pack, kind, cells }))
}

/// What the sink of a fused scan must hold, in cells (16 B).
#[derive(Clone, Copy)]
pub(crate) struct Need {
    /// The upper bound of the rows of the result: the rows of the drivers, or the counted rows of the space.
    pub(crate) rows: usize,
    /// The copied sources and the lists of the parts, which live with the result.
    pub(crate) extra: usize,
    /// The part buffers in flight, if the sink takes the parts one by one (not `presized`).
    pub(crate) parts: usize,
}

/// The store that a fused result can go into (`eval_columns`). With `diff`, its write finds the changed keys.
pub(crate) struct Target<'s> {
    pub(crate) store: &'s Store,
    pub(crate) diff: bool,
}

impl Target<'_> {
    /// The cells that the columns of the new base, the part buffers and the extra cells of `need` use.
    pub(crate) fn cells(&self, need: Need) -> usize {
        self.store.sink_bytes(need.rows, self.diff).div_ceil(CELL_BYTES).saturating_add(need.extra).saturating_add(need.parts)
    }
}

/// The fused scan into the sink `make(pack, need)`, where `pack` is the packing of the result. `make` checks
/// the budget. Gives the packing and the kind of the result with the sink. If no source decides the order of
/// the result dimensions, they take the order of the `target` store when it has the same dimensions.
pub(crate) fn fused_into<S: Sink>(node: &Node, cat: &Catalog, src: &[Src], r: &Restrict, bud: &Budget, target: Option<&Target>, make: impl FnOnce(&Packing, Need) -> Result<S>) -> Result<Option<(Packing, Kind, S)>> {
    if !r.is_all() || matches!(node, Node::Ref(_) | Node::Const(..) | Node::MemberConst(..)) {
        return Ok(None);
    }
    let merged: Vec<OnceCell<Cube>> = src.iter().map(|_| OnceCell::new()).collect();
    let mut fu = Fuse { cat, src, bud, merged: &merged, copied: 0, reserve: 0, leaves: Vec::new(), ids: Vec::new() };
    let Some(info) = fu.info(node) else { return Ok(None) };
    if !fits(info.mat, fu.copied, bud) {
        return Ok(None);
    }
    fused_with(node, &mut fu, info, bud, target, make)
}

/// True if `mat` estimated cells and `copied` cells fit in `bud`. An estimate of `2^64` or more does not fit.
fn fits(mat: f64, copied: usize, bud: &Budget) -> bool {
    mat < usize::MAX as f64 && bud.need((mat as usize).saturating_add(copied), "").is_ok()
}

fn fused_with<S: Sink>(node: &Node, fu: &mut Fuse, info: Info, bud: &Budget, target: Option<&Target>, make: impl FnOnce(&Packing, Need) -> Result<S>) -> Result<Option<(Packing, Kind, S)>> {
    let cat = fu.cat;
    let cfg = &cat.cfg;
    let prefer: &[DimId] = target.map_or(&[], |t| &t.store.pack.dims);
    // The cells of the sink of `rows` rows, which a Dense access leaves room for: the columns of the target
    // store with the part buffers (at most all rows), or an array
    let sink = |pack: &Packing, rows: usize| match target {
        Some(t) if t.store.pack == *pack => t.cells(Need { rows, extra: 0, parts: rows }),
        _ => rows,
    };
    // Scan the union of the sources that have all dimensions of the formula, in key order (same packing)
    if let Cover::Exact(ls) = &info.cover {
        let out = fu.leaves[ls[0]].pack;
        if same_set(&out.dims, &info.dims) && ls.iter().all(|&l| fu.leaves[l].pack == out) {
            let drivers: Vec<Col> = ls.iter().map(|&l| fu.leaves[l].col).collect();
            fu.reserve = sink(out, drivers.iter().map(|d| d.n()).sum());
            let mut access = Vec::new();
            let Some(f) = fu.build(node, out, true, &mut access) else { return Ok(None) };
            let copied = fu.copied + access.iter().map(Access::cells).sum::<usize>();
            let sink = run_sorted(cfg, &drivers, &access, &compile(&f, &access), bud, copied, |need| make(out, need))?;
            return Ok(Some((out.clone(), info.kind, sink)));
        }
    }
    // Scan one source x all members of the missing dimensions, or all combinations
    let cover = match info.cover {
        Cover::Exact(ls) if ls.len() == 1 => Cover::Part(ls[0]),
        Cover::Exact(_) => Cover::All,
        c => c,
    };
    if fu.cost(&cover, &info.dims) > SPAN_RATIO * info.mat {
        return Ok(None);
    }
    // The scan goes in ascending order of the result keys (the rows of base x the combinations of fill).
    // If the dimensions of the result key follow the order of the largest source, the lookup keys of that
    // source also go up, and each lookup can start near the last one
    let order: Vec<DimId> = match &cover {
        // The fill dimensions can take any order. Take the order of `prefer` if it starts with the head
        Cover::Part(l) => {
            let head = &fu.leaves[*l].pack.dims;
            if same_set(prefer, &info.dims) && prefer.starts_with(head) {
                prefer.to_vec()
            } else {
                head.iter().copied().chain(info.dims.iter().copied().filter(|d| !head.contains(d))).collect()
            }
        }
        _ => {
            let big = (0..fu.leaves.len()).filter(|&l| same_set(&fu.leaves[l].pack.dims, &info.dims)).max_by_key(|&l| fu.leaves[l].col.n());
            let free = if same_set(prefer, &info.dims) { prefer } else { &info.dims };
            big.map_or(free.to_vec(), |l| fu.leaves[l].pack.dims.clone())
        }
    };
    let Ok(out) = Packing::new(&order, cat) else { return Ok(None) };
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
    fu.reserve = sink(&out, space.len()).saturating_add(list_cells(space.len(), cfg));
    let mut access = Vec::new();
    let Some(f) = fu.build(node, &out, false, &mut access) else { return Ok(None) };
    let copied = fu.copied + access.iter().map(Access::cells).sum::<usize>();
    let mut sink = run_space(cfg, &space, &access, &compile(&f, &access), bud, copied, |need| make(&out, need))?;
    sink.sort_if_unsorted(cfg);
    Ok(Some((out, info.kind, sink)))
}

/// Runs `work` on the parts, part i with room for `sizes[i]` cells, and gives the cells to `sink`. A sink
/// that is one array (`presized`) takes all parts in parallel at their offsets. Another sink takes the parts
/// in their order. In parallel, the parts go in batches of 2 parts for each thread. At most 2 batches of
/// results are in flight. A part of `run_space` has at most `chunk` cells (2 x 4 threads x 64 K cells x 16 B
/// = 16 MB). A part of `run_sorted` holds the rows of all drivers in its range. The largest driver splits the
/// ranges at every `chunk`-th key, so a smaller driver with many rows in one range makes a part above `chunk`
/// cells.
fn stream<T: Sync>(par: bool, parts: &[T], sizes: &[usize], work: impl Fn(&T, &mut [(u64, f64)]) -> usize + Sync, sink: &mut impl Sink) -> Result<()> {
    if let Some(cells) = sink.presized(sizes) {
        let mut slices = Vec::with_capacity(parts.len());
        let mut rest = &mut cells[..];
        for &n in sizes {
            let (head, tail) = rest.split_at_mut(n);
            slices.push(head);
            rest = tail;
        }
        let written: Vec<usize> = if par {
            parts.par_iter().zip(slices).map(|(p, s)| work(p, s)).collect()
        } else {
            parts.iter().zip(slices).map(|(p, s)| work(p, s)).collect()
        };
        compact(cells, sizes, &written);
        return Ok(());
    }
    let one = |(p, &n): (&T, &usize)| {
        let mut out = vec![(0u64, 0f64); n];
        let w = work(p, &mut out);
        out.truncate(w);
        out
    };
    if !par {
        return parts.iter().zip(sizes).try_for_each(|x| sink.part(one(x)));
    }
    let run = |ps: &[T], ns: &[usize]| -> Vec<Vec<(u64, f64)>> { ps.par_iter().zip(ns).map(one).collect() };
    let mut batches = parts.chunks(batch()).zip(sizes.chunks(batch()));
    let Some((ps, ns)) = batches.next() else { return Ok(()) };
    let mut ready = run(ps, ns);
    for (ps, ns) in batches {
        // The sink takes the parts on this thread, not on a rayon worker. A worker allocates the next segment columns
        // in its own malloc arena from new pages: 25,000 more page faults for Revenue on retail xlarge.
        let mut computed = Vec::new();
        let given = rayon::in_place_scope(|s| {
            s.spawn(|_| computed = run(ps, ns));
            ready.into_iter().try_for_each(|c| sink.part(c))
        });
        given?;
        ready = computed;
    }
    ready.into_iter().try_for_each(|c| sink.part(c))
}

fn batch() -> usize {
    2 * rayon::current_num_threads().max(1)
}

/// The most cells that `stream` holds in part buffers at one time if the sink is not presized: the largest
/// part, or in parallel the largest 2 batches in a row.
fn in_flight(par: bool, sizes: &[usize]) -> usize {
    if !par {
        return sizes.iter().copied().max().unwrap_or(0);
    }
    let batches: Vec<usize> = sizes.chunks(batch()).map(|b| b.iter().sum()).collect();
    batches.windows(2).map(|w| w[0] + w[1]).chain(batches.first().copied()).max().unwrap_or(0)
}

/// Scan the union of the keys of the drivers (same packing) in order, and get the value of f at each key.
/// The result goes in key order into the sink `make`. The rows of the result are at most the rows of the
/// drivers. `copied` is the cells of the copied sources.
fn run_sorted<S: Sink>(cfg: &Config, drivers: &[Col], access: &[Access], f: &Ev, bud: &Budget, copied: usize, make: impl FnOnce(Need) -> Result<S>) -> Result<S> {
    // A unit of work: split at every cfg.chunk-th key of the largest driver, and find the row range of each driver
    let chunk = cfg.chunk.max(1);
    let big = (0..drivers.len()).max_by_key(|&i| drivers[i].n()).unwrap();
    // Check the lists of the ranges before they exist. A range has its bound and size, and 2 lists of a row for each driver
    let range = 2 * size_of::<Vec<usize>>() + 2 * size_of::<usize>() + 2 * drivers.len() * size_of::<usize>();
    let lists = (drivers[big].n() / chunk + 1).saturating_mul(range).div_ceil(CELL_BYTES).saturating_add(copied);
    bud.need(lists, "式の結果")?;
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

    let work = |(lo, hi): &Span, out: &mut [(u64, f64)]| -> usize {
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
                out[w] = (key, v);
                w += 1;
            }
        }
        w
    };
    let par = cfg.par(total);
    let mut sink = make(Need { rows: total, extra: lists, parts: in_flight(par, &sizes) })?;
    stream(par, &ranges, &sizes, work, &mut sink)?;
    Ok(sink)
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

/// The cells of the lists of the parts (parts, sizes and slices) that `run_space` makes for a space of `n` keys.
/// The lists use 40 bytes for each part, which is less than 3 cells.
fn list_cells(n: usize, cfg: &Config) -> usize {
    n.div_ceil(cfg.chunk.max(1)).saturating_mul(3)
}

/// Scan the keys of space, and get the value of f at each key. It counts the values before `make` makes the
/// sink. Thus the sink is only as large as the result, and f runs two times at each key.
/// If the dimensions of the base source come first in the result key, the result is in key order.
fn run_space<S: Sink>(cfg: &Config, space: &Space, access: &[Access], f: &Ev, bud: &Budget, copied: usize, make: impl FnOnce(Need) -> Result<S>) -> Result<S> {
    let (n, combos) = (space.len(), space.combos());
    if n == 0 || combos == 0 {
        return make(Need { rows: 0, extra: 0, parts: 0 });
    }
    let chunk = cfg.chunk.max(1);
    // Check the lists before they exist
    let lists = list_cells(n, cfg).saturating_add(copied);
    bud.need(lists, "式の結果")?;
    let parts: Vec<(usize, usize)> = (0..n).step_by(chunk).map(|lo| (lo, (lo + chunk).min(n))).collect();
    let count = |&(lo, hi): &(usize, usize)| {
        let (mut readers, mut rd) = (readers(access), space.cursor());
        (lo..hi).filter(|&i| f(&mut readers, space.key(i, combos, &mut rd)).is_some()).count()
    };
    let sizes: Vec<usize> = if cfg.par(n) { parts.par_iter().map(count).collect() } else { parts.iter().map(count).collect() };
    let total: usize = sizes.iter().sum();
    let write = |&(lo, hi): &(usize, usize), out: &mut [(u64, f64)]| -> usize {
        let (mut readers, mut rd) = (readers(access), space.cursor());
        let mut w = 0;
        for i in lo..hi {
            let key = space.key(i, combos, &mut rd);
            if let Some(v) = f(&mut readers, key) {
                out[w] = (key, v);
                w += 1;
            }
        }
        w
    };
    let par = cfg.par(n);
    let mut sink = make(Need { rows: total, extra: lists, parts: in_flight(par, &sizes) })?;
    stream(par, &parts, &sizes, write, &mut sink)?;
    Ok(sink)
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
    use super::{batch, fits, in_flight};
    use crate::*;

    /// A model on d0 (10 members) x d1 (20 members): Volume on both (dense) and Start on d0 with 3 members.
    /// With 3 members, the scan of Start x d1 stays below `SPAN_RATIO` x the 20 values of `d1`.
    fn part_cover(cfg: Config) -> (Catalog, Vec<Src>) {
        let dims = [10, 20].iter().enumerate().map(|(i, &size)| DimInfo { size, ordered: false, name: format!("d{i}") }).collect();
        let cat = Catalog { dims, maps: Vec::new(), cfg };
        let (d0, d1): (Vec<u32>, Vec<u32>) = (0..200u32).map(|i| (i / 20, i % 20)).unzip();
        let vals: Vec<f64> = (0..200).map(|i| i as f64 + 1.0).collect();
        let volume = Store::new(&[0, 1], Some(0), Kind::Num, &cat).unwrap().with_rows(&[&d0, &d1], &vals).unwrap();
        let start = Store::new(&[0], None, Kind::Num, &cat).unwrap().with_rows(&[&[1, 4, 7]], &[2.0, 0.0, 19.0]).unwrap();
        (cat, vec![Src::Store(Arc::new(volume)), Src::Store(Arc::new(start))])
    }

    /// `d1 >= Start`: a Part cover (Start x all of d1, `run_space`). With Volume in front, Volume drives the
    /// scan (an Exact cover, `run_sorted`) and 140 of its 200 rows give no cell.
    fn ge_start(volume: bool) -> Node {
        let cond = Node::Bin(Op::Ge, Box::new(Node::DimRef(1)), Box::new(Node::Ref(1)), vec![]);
        if volume { Node::Bin(Op::Mul, Box::new(Node::Ref(0)), Box::new(cond), vec![]) } else { cond }
    }

    /// The cells of `node` by `eval` and `with_sorted`, in the packing of `store`.
    fn cells_of(store: &Store, cat: &Catalog, src: &[Src], node: &Node) -> Vec<(u64, f64)> {
        store.with_sorted(store.sorted_cells(eval(node, cat, src, &Restrict::all(2)).unwrap()).unwrap()).as_cube().cells
    }

    #[test]
    fn streamed_parts_arrive_in_key_order() {
        let (cat, src) = part_cover(Config { par_min: 0, chunk: 1, ..Config::default() });
        let (plain_cat, plain_src) = part_cover(Config { fuse: false, ..Config::default() });
        let store = Store::new(&[0, 1], Some(0), Kind::Num, &cat).unwrap();
        for node in [ge_start(true), ge_start(false)] {
            let want = cells_of(&store, &cat, &src, &node);
            assert_eq!(want.len(), 60, "Start x d1 keys (a false comparison gives 0, not blank)");
            assert_eq!(cells_of(&store, &plain_cat, &plain_src, &node), want);
            let Some(Whole::Columns(c)) = eval_columns(&node, &cat, &src, &Budget::new(usize::MAX, 0), &store, false).unwrap() else { panic!("streams") };
            assert_eq!(store.with_columns(c).0.as_cube().cells, want);
        }
    }

    #[test]
    fn a_formula_in_another_packing_gives_a_cube() {
        let (cat, src) = part_cover(Config { par_min: 0, chunk: 1, ..Config::default() });
        let store = Store::new(&[0, 1], Some(1), Kind::Num, &cat).unwrap();
        for node in [ge_start(true), ge_start(false)] {
            let Some(Whole::Cube(c)) = eval_columns(&node, &cat, &src, &Budget::new(usize::MAX, 0), &store, false).unwrap() else { panic!("a cube") };
            assert_eq!(store.with_sorted(store.sorted_cells(c).unwrap()).as_cube().cells, cells_of(&store, &cat, &src, &node));
        }
    }

    /// Volume and Price on d0 (50) x d1 (400), with their segments cut after 2,048 and 3,000 rows, and Start on d0.
    fn segmented(cfg: Config) -> (Catalog, Vec<Src>) {
        let dims = [50, 400].iter().enumerate().map(|(i, &size)| DimInfo { size, ordered: false, name: format!("d{i}") }).collect();
        let cat = Catalog { dims, maps: Vec::new(), cfg };
        let cut = |seg_rows| Catalog { cfg: Config { seg_rows, ..cfg }, ..cat.clone() };
        let grid = |skip: u32| -> (Vec<u32>, Vec<u32>, Vec<f64>) {
            let (d0, d1): (Vec<u32>, Vec<u32>) = (0..20_000u32).filter(|i| i % skip != 0).map(|i| (i / 400, i % 400)).unzip();
            let vals = d0.iter().zip(&d1).map(|(a, b)| (a * 400 + b) as f64 + 1.0).collect();
            (d0, d1, vals)
        };
        let (d0, d1, vals) = grid(7);
        let volume = Store::new(&[0, 1], Some(0), Kind::Num, &cut(2048)).unwrap().with_rows(&[&d0, &d1], &vals).unwrap();
        let (d0, d1, vals) = grid(5);
        let price = Store::new(&[0, 1], Some(0), Kind::Num, &cut(3000)).unwrap().with_rows(&[&d0, &d1], &vals).unwrap();
        assert!(volume.base.segs.len() > 5 && price.base.segs.len() > 4 && volume.base.segs.len() != price.base.segs.len());
        let members: Vec<u32> = (0..50).collect();
        let starts: Vec<f64> = (0..50).map(|m| (m * 37 % 400) as f64).collect();
        let start = Store::new(&[0], None, Kind::Num, &cut(2048)).unwrap().with_rows(&[&members], &starts).unwrap();
        (cat, vec![Src::Store(Arc::new(volume)), Src::Store(Arc::new(price)), Src::Store(Arc::new(start))])
    }

    #[test]
    fn a_fused_scan_reads_across_segments() {
        let (cat, src) = segmented(Config { par_min: 0, ..Config::default() });
        let (plain_cat, plain_src) = segmented(Config { fuse: false, ..Config::default() });
        let store = Store::new(&[0, 1], Some(0), Kind::Num, &cat).unwrap();
        let ge_start = Node::Bin(Op::Ge, Box::new(Node::DimRef(1)), Box::new(Node::Ref(2)), vec![]);
        let nodes = [
            Node::Bin(Op::Mul, Box::new(Node::Ref(0)), Box::new(Node::Ref(1)), vec![]),          // an Exact cover with 2 drivers
            Node::Bin(Op::Mul, Box::new(Node::Ref(0)), Box::new(ge_start.clone()), vec![]),      // Volume drives, Start is searched
            Node::Bin(Op::Add, Box::new(Node::Ref(0)), Box::new(Node::Ref(1)), vec![]),          // the union of the rows
            ge_start,                                                                             // a Part cover: Start x d1
        ];
        let mut streamed = 0;
        for node in nodes {
            let want = cells_of(&store, &plain_cat, &plain_src, &node);
            assert!(want.len() > 10_000);
            assert_eq!(cells_of(&store, &cat, &src, &node), want);
            if let Some(Whole::Columns(c)) = eval_columns(&node, &cat, &src, &Budget::new(usize::MAX, 0), &store, false).unwrap() {
                assert_eq!(store.with_columns(c).0.as_cube().cells, want);
                streamed += 1;
            }
        }
        assert!(streamed >= 3, "{streamed} of 4 formulas streamed into the segments");
    }

    #[test]
    fn a_part_cover_fills_its_dimensions_in_the_order_of_the_store() {
        // Month is d0 and Version is d1, but the store keeps (Product, Version, Month). Price on 1 Product is the
        // head. The scan of Price x Version x Month (36 keys) stays below `SPAN_RATIO` x the 12 Months
        let dims = [("Month", 12), ("Version", 3), ("Product", 40)].iter().map(|&(name, size)| DimInfo { size, ordered: false, name: name.into() }).collect();
        let cat = Catalog { dims, maps: Vec::new(), cfg: Config::default() };
        let price = Store::new(&[2], None, Kind::Num, &cat).unwrap().with_rows(&[&[30]], &[2.0]).unwrap();
        let src = [Src::Store(Arc::new(price))];
        // Price * (Month + Version)
        let plus = Node::Bin(Op::Add, Box::new(Node::DimRef(0)), Box::new(Node::DimRef(1)), vec![]);
        let node = Node::Bin(Op::Mul, Box::new(Node::Ref(0)), Box::new(plus), vec![]);
        let store = Store::new(&[2, 1, 0], Some(2), Kind::Bool, &cat).unwrap();
        let want = store.with_sorted(store.sorted_cells(eval(&node, &cat, &src, &Restrict::all(3)).unwrap()).unwrap()).as_cube().cells;
        assert_eq!(want.len(), 3 * 12);
        let Some(Whole::Columns(c)) = eval_columns(&node, &cat, &src, &Budget::new(usize::MAX, 0), &store, false).unwrap() else { panic!("streams") };
        assert_eq!(store.with_columns(c).0.as_cube().cells, want);
    }

    #[test]
    fn in_flight_is_the_largest_part_or_2_batches_in_a_row() {
        assert_eq!(in_flight(false, &[3, 9, 4]), 9);
        assert_eq!(in_flight(false, &[]), 0);
        assert_eq!(in_flight(true, &[7]), 7);
        let b = batch();
        let mut sizes = vec![1; 3 * b];
        sizes[2 * b] = 5; // the batches are b, b and b + 4 cells
        assert_eq!(in_flight(true, &sizes), 2 * b + 4);
    }

    #[test]
    fn an_estimate_of_2_to_the_64_does_not_fit() {
        let bud = Budget::new(1 << 40, 0);
        assert!(!fits(2f64.powi(64), 1, &bud));
        assert!(!fits(2f64.powi(64), 0, &bud));
        assert!(fits(1000.0, 1, &bud));
    }
}
