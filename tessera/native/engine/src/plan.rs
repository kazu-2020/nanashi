//! 差分再計算の段取り。意味は Python の Model.recalc の差分の経路（evaluate.affected、
//! Model._scan_regions、Model._scan、Model._apply_delta）と同じで、テストで突き合わせる。
//!
//! 入力の変更範囲を計算計画の順に伝え、各 Metric の影響範囲だけを評価して書き戻す。書き戻すときに
//! 新旧の値を比べ、実際に値が変わったセルだけを下流への影響範囲にする。影響範囲はメンバー名ではなく
//! 番号の集合で持つので、1 回の変更で何百もの Metric を計算し直しても、Python との往復は 1 回で済む。

use crate::{eval_with, Agg, Budget, Catalog, Cube, CELL_BYTES, DimId, Kind, Node, Op, Restrict, Result, Sel, Src, Store};
use rayon::prelude::*;

use std::sync::Arc;

// ------------------------------------------------------------------ 影響範囲

/// 影響範囲。軸ごとのメンバー番号の集合の直積で、載っていない軸は全メンバー。
/// 空の Reg は Metric 全体。「影響なし」は Option の None で表す。
#[derive(Clone, Debug, PartialEq, Default)]
pub struct Reg {
    sels: Vec<(DimId, Vec<u32>)>, // 軸の番号の昇順。メンバーは昇順で重複なし
}

fn sorted(mut ms: Vec<u32>) -> Vec<u32> {
    ms.sort_unstable();
    ms.dedup();
    ms
}

impl Reg {
    pub fn new(sels: Vec<(DimId, Vec<u32>)>) -> Reg {
        let mut sels: Vec<(DimId, Vec<u32>)> = sels.into_iter().map(|(d, ms)| (d, sorted(ms))).collect();
        sels.sort_by_key(|s| s.0);
        Reg { sels }
    }

    pub fn into_parts(self) -> Vec<(DimId, Vec<u32>)> {
        self.sels
    }

    fn get(&self, d: DimId) -> Option<&[u32]> {
        self.sels.iter().find(|s| s.0 == d).map(|s| s.1.as_slice())
    }

    fn has(&self, d: DimId, m: u32) -> bool {
        self.get(d).is_none_or(|ms| ms.binary_search(&m).is_ok())
    }

    fn is_all(&self) -> bool {
        self.sels.is_empty()
    }

    fn without(&self, ds: &[DimId]) -> Reg {
        Reg { sels: self.sels.iter().filter(|s| !ds.contains(&s.0)).cloned().collect() }
    }

    /// 軸 d のメンバーを ms にした範囲。
    fn set(&self, d: DimId, ms: Vec<u32>) -> Reg {
        let mut out = self.without(&[d]);
        out.sels.push((d, sorted(ms)));
        out.sels.sort_by_key(|s| s.0);
        out
    }

    /// どれかの軸のメンバーが空なら、どのセルも含まないので None。
    fn nonempty(self) -> Option<Reg> {
        (!self.sels.iter().any(|s| s.1.is_empty())).then_some(self)
    }

    pub fn restrict(&self, cat: &Catalog) -> Restrict {
        let mut r = Restrict::all(cat.dims.len());
        for (d, ms) in &self.sels {
            r = r.with(*d, Sel::new(ms.clone(), cat.dims[*d].size));
        }
        r
    }
}

/// 2 つの影響範囲を囲む範囲（両方に載っている軸だけ、メンバーの和を取る）。
fn union(a: Option<Reg>, b: Option<Reg>) -> Option<Reg> {
    match (a, b) {
        (None, x) | (x, None) => x,
        (Some(a), Some(b)) => Some(Reg {
            sels: a.sels.iter().filter_map(|(d, ms)| b.get(*d).map(|ns| (*d, sorted([&ms[..], ns].concat())))).collect(),
        }),
    }
}

/// 影響範囲の計算に使う環境。regions は Metric の番号ごとの変更範囲、added は軸ごとの追加したメンバー。
pub struct Env<'a> {
    pub cat: &'a Catalog,
    pub regions: &'a [Option<Reg>],
    pub added: &'a [(DimId, Vec<u32>)], // 軸ごとの追加したメンバー（昇順）
    pub removed: Option<(DimId, u32)>,  // これから消すメンバー（前月参照の読み先が変わる時点を含める）
}

impl Env<'_> {
    /// dims のうちメンバーを追加した軸について、新しいメンバーの範囲を足す。
    fn grow(&self, mut r: Option<Reg>, dims: &[DimId]) -> Option<Reg> {
        for d in dims {
            if let Some((_, ms)) = self.added.iter().find(|(x, _)| x == d) {
                r = union(r, Some(Reg { sels: vec![(*d, ms.clone())] }));
            }
        }
        r
    }

    /// 式の結果のうち、値が変わりうる範囲。refs は式の Ref の番号 -> Metric の番号。
    pub fn affected(&self, node: &Node, refs: &[usize]) -> Option<Reg> {
        let af = |n: &Node| self.affected(n, refs);
        match node {
            Node::Ref(i) => self.regions[refs[*i]].clone(),
            Node::Const(..) | Node::MemberConst(..) => None,
            Node::By { .. } | Node::ByMetric { .. } => unreachable!("型を決めた式だけを使う"),
            Node::DimRef(d) => self.grow(None, &[*d]),
            Node::Bin(_, l, r, grow) => self.grow(union(af(l), af(r)), grow),
            Node::Filter(l, r) | Node::On(l, r) | Node::Coalesce(l, r) => union(af(l), af(r)),
            Node::If(c, t, e, grow) => {
                let mut r = union(af(c), af(t));
                if let Some(e) = e {
                    r = union(r, af(e));
                }
                self.grow(r, grow)
            }
            Node::Not(c) | Node::AsAxis { child: c, .. } => af(c),
            Node::IsBlank(c, grow) | Node::IfBlank(c, _, _, grow) => self.grow(af(c), grow),
            Node::Expand(c, dims) => self.grow(af(c), dims),
            Node::Remove { child, dim, .. } => af(child).map(|r| r.without(&[*dim])),
            Node::Select { child, dim, member, .. } => {
                let r = af(child)?;
                // 変更が選んだメンバーに届かなければ影響なし
                r.has(*dim, *member).then(|| r.without(&[*dim]))
            }
            Node::Shift { child, dim, n } => {
                let r = af(child).and_then(|r| match r.get(*dim) {
                    Some(ms) => {
                        let size = self.cat.dims[*dim].size as i64;
                        let moved = ms
                            .iter()
                            .filter_map(|&m| {
                                let t = m as i64 + n;
                                (0..size).contains(&t).then_some(t as u32)
                            })
                            .collect();
                        r.set(*dim, moved).nonempty()
                    }
                    None => Some(r),
                });
                let mut r = self.grow(r, &[*dim]); // 末尾に足した時点には、ずらした値が入りうる
                if let Some((rd, p)) = self.removed {
                    if rd == *dim {
                        // 消すメンバーを読み飛ばすようになる時点
                        let (size, p, n) = (self.cat.dims[*dim].size as i64, p as i64, *n);
                        let span: Vec<u32> = if n > 0 { p + 1..p + n + 1 } else { p + n..p }
                            .filter(|q| (0..size).contains(q))
                            .map(|q| q as u32)
                            .collect();
                        if !span.is_empty() {
                            r = union(r, Some(Reg::new(vec![(*dim, span)])));
                        }
                    }
                }
                r
            }
            Node::ByAgg { child, src, dst, map, .. } => {
                // 集約: 変わった社員の部署が変わる
                let r = af(child)?;
                let mp = &self.cat.maps[*map];
                let mut out = r.without(&[*src, *dst]);
                if let Some(ms) = r.get(*src) {
                    let ts = ms.iter().filter_map(|&m| mp.fwd.get(m as usize).and_then(|&t| (t >= 0).then_some(t as u32)));
                    out = out.set(*dst, ts.collect());
                }
                out.nonempty()
            }
            Node::ByLookup { child, src, dst, map } => {
                // 引き下ろし: 変わった部署に属する社員が変わる。新しい社員にも部署の値が配られる
                let r = af(child).and_then(|r| {
                    let mp = &self.cat.maps[*map];
                    let mut out = r.without(&[*src, *dst]);
                    if let Some(ts) = r.get(*dst) {
                        let ms = ts.iter().flat_map(|&t| mp.inv.get(t as usize).into_iter().flatten().copied());
                        out = out.set(*src, ms.collect());
                    }
                    out.nonempty()
                });
                self.grow(r, &[*src])
            }
        }
    }
}

/// scan に含まれる Metric の影響範囲。互いを参照し合うので、範囲が増えなくなるまで伝搬を繰り返す
/// （範囲は単調に広がるだけで有限なので必ず止まる）。regions の names の分をその場で広げる。
fn scan_regions(cat: &Catalog, plan: &Plan, regions: &mut [Option<Reg>], names: &[usize], added: &[(DimId, Vec<u32>)], removed: Option<(DimId, u32)>) {
    loop {
        let mut grown = false;
        for &n in names {
            let f = plan.metrics[n].formula.as_ref().expect("計算 Metric");
            let r = union(regions[n].clone(), Env { cat, regions, added, removed }.affected(&f.node, &f.refs));
            if r != regions[n] {
                regions[n] = r;
                grown = true;
            }
        }
        if !grown {
            break;
        }
    }
}

/// 入力の変更範囲と追加したメンバーを計画の順に伝え、影響を受ける全 Metric の範囲（changed を含む）。
/// Python の Model._propagate と同じ。分割軸の選択に使う。
pub fn propagate(cat: &Catalog, plan: &Plan, changed: Vec<(usize, Reg)>, added: &[(DimId, Vec<u32>)]) -> Vec<Option<Reg>> {
    let mut regions: Vec<Option<Reg>> = vec![None; plan.metrics.len()];
    for (m, r) in changed {
        regions[m] = Some(r);
    }
    let added: Vec<(DimId, Vec<u32>)> = added.iter().map(|(d, ms)| (*d, sorted(ms.clone()))).collect();
    for level in &plan.levels {
        for step in level {
            match step {
                Step::One(m) => {
                    if let Some(f) = &plan.metrics[*m].formula {
                        let r = Env { cat, regions: &regions, added: &added, removed: None }.affected(&f.node, &f.refs);
                        if r.is_some() {
                            regions[*m] = r;
                        }
                    }
                }
                Step::Scan(_, names) => scan_regions(cat, plan, &mut regions, names, &added, None),
            }
        }
    }
    regions
}

/// 入力を空にしたあと、軸 dim のメンバー member を消すと値が変わる範囲（計算 Metric -> 消すメンバーを
/// 除いた範囲）。Python の Model._removal_regions と同じ。
///
/// 計算 Metric がそのメンバーを指す値を持つのは、そのメンバーのセル自身（軸の値）か、それを前月参照や
/// 引き下ろしで運んだセルだけなので、消えるセルからの伝搬で足りる。全メンバーへ値を広げる演算が
/// そのメンバーに作っていたセルは、消すメンバーを「追加したメンバー」として伝えて拾う。
pub fn removal_regions(cat: &Catalog, plan: &Plan, stores: &[Arc<Store>], dim: DimId, member: u32) -> Vec<(usize, Reg)> {
    let n = plan.metrics.len();
    let point = Reg::new(vec![(dim, vec![member])]);
    let point_r = point.restrict(cat);
    let added = vec![(dim, vec![member])];
    let removed = Some((dim, member));
    let has_cells: Vec<bool> = (0..n)
        .map(|m| stores[m].metric_dims.contains(&dim) && !stores[m].read(&point_r).cells.is_empty())
        .collect();
    // 下流から見て変わる範囲（消えるセルと、計算し直す範囲）と、計算し直す範囲
    let mut changes: Vec<Option<Reg>> = vec![None; n];
    let mut todo: Vec<(usize, Reg)> = Vec::new();

    // 消すメンバーを除いた範囲。その軸のメンバーがそれだけなら、どのセルも残らない
    let surviving = |r: Option<Reg>| -> Option<Reg> {
        let r = r?;
        match r.get(dim) {
            None => Some(r),
            Some(ms) => {
                let rest: Vec<u32> = ms.iter().copied().filter(|&x| x != member).collect();
                if rest.is_empty() { None } else { Some(r.set(dim, rest)) }
            }
        }
    };
    let settle = |m: usize, r: Option<Reg>, changes: &mut Vec<Option<Reg>>, todo: &mut Vec<(usize, Reg)>| {
        let r = surviving(r);
        if let Some(r) = &r {
            todo.push((m, r.clone()));
        }
        let c = union(if has_cells[m] { Some(point.clone()) } else { None }, r);
        if c.is_some() {
            changes[m] = c;
        }
    };
    for level in &plan.levels {
        for step in level {
            match step {
                Step::One(m) => {
                    if let Some(f) = &plan.metrics[*m].formula {
                        let r = Env { cat, regions: &changes, added: &added, removed }.affected(&f.node, &f.refs);
                        settle(*m, r, &mut changes, &mut todo);
                    }
                }
                Step::Scan(_, names) => {
                    for &x in names {
                        if has_cells[x] {
                            changes[x] = Some(point.clone()); // scan の中の前月参照は、消えるセルからも伝わる
                        }
                    }
                    scan_regions(cat, plan, &mut changes, names, &added, removed);
                    for &x in names {
                        let r = changes[x].take();
                        settle(x, r, &mut changes, &mut todo);
                    }
                }
            }
        }
    }
    todo
}

// ------------------------------------------------------------------ 計算計画

/// Metric の式。refs は式の Ref の番号 -> 読み出し元の番号（Metric、または差分集計の作業データ）。
pub struct Formula {
    pub node: Arc<Node>,
    pub refs: Vec<usize>,
}

/// 差分集計の計画。inputs は [集計元, 対応表...]。d_count と d_value の refs は作業データの番号で、
/// inputs の i 番目の変更後が 2i、変更前が 2i + 1。
pub struct Delta {
    pub inputs: Vec<usize>,
    pub d_count: Formula,
    pub d_value: Option<Formula>, // None なら d_count が値の差分（COUNT の集計）
}

pub struct Metric {
    pub formula: Option<Formula>, // None なら入力
    pub count: Option<Formula>,   // 差分集計する SUM の、各グループの件数の式
    pub delta: Option<Delta>,
    pub source: bool, // 差分集計の集計元か対応表（変更前の値を取っておく）
}

// ------------------------------------------------------------------ 差分集計の判定と式

/// 差分集計の対象になる式か。意味は Python の delta.plan_for と同じ。
///
/// 対象は「1 つの Metric を集計していくだけ」の式。途中で SELECT で切り口を取っても、Metric を使った
/// BY の対応表（On(x, AsAxis(Ref))）と結合してもよい。一番内側が SUM か COUNT で、外側がすべて SUM なら
/// 結果は集計元について足し算で分解できる。返すのは (集計元の Ref の番号, 対応表の Ref の番号, 件数が要るか)。
/// SUM は「値のあるセルが 1 つもなければ空」なので、件数を裏で持って 0 と空を区別する（COUNT なら自身が件数）。
pub fn delta_pattern(node: &Node) -> Option<(usize, Vec<usize>, bool)> {
    let mut aggs: Vec<Agg> = Vec::new(); // 外側から内側の順
    let mut aux = Vec::new();
    let mut e = node;
    loop {
        match e {
            Node::ByAgg { child, agg, .. } | Node::Remove { child, agg, .. } => {
                aggs.push(*agg);
                e = child;
            }
            Node::On(child, other) => match &**other {
                Node::AsAxis { child: inner, .. } if matches!(**inner, Node::Ref(_)) => {
                    let Node::Ref(i) = **inner else { unreachable!() };
                    aux.push(i); // 対応表との結合は、集計元について線形
                    e = child;
                }
                _ => break,
            },
            Node::Select { child, .. } => e = child, // 切り口を取り出すだけで、分解を崩さない
            _ => break,
        }
    }
    let Node::Ref(source) = *e else { return None };
    let last = *aggs.last()?;
    if !matches!(last, Agg::Sum | Agg::Count) || aggs[..aggs.len() - 1].iter().any(|a| !matches!(a, Agg::Sum)) {
        return None;
    }
    Some((source, aux, matches!(last, Agg::Sum)))
}

fn has_agg(e: &Node) -> bool {
    match e {
        Node::ByAgg { .. } | Node::Remove { .. } => true,
        Node::Select { child, .. } | Node::On(child, _) => has_agg(child),
        _ => false,
    }
}

/// 一番内側の集計を COUNT に置き換えた式（外側の SUM と SELECT はそのまま）。
pub fn inner_count(e: &Node) -> Node {
    match e {
        Node::ByAgg { child, src, dst, map, .. } if !has_agg(child) => {
            Node::ByAgg { child: child.clone(), src: *src, dst: *dst, map: *map, agg: Agg::Count }
        }
        Node::Remove { child, dim, .. } if !has_agg(child) => Node::Remove { child: child.clone(), dim: *dim, agg: Agg::Count },
        Node::ByAgg { child, src, dst, map, agg } => {
            Node::ByAgg { child: Box::new(inner_count(child)), src: *src, dst: *dst, map: *map, agg: *agg }
        }
        Node::Remove { child, dim, agg } => Node::Remove { child: Box::new(inner_count(child)), dim: *dim, agg: *agg },
        Node::Select { child, dim, member, name } => {
            Node::Select { child: Box::new(inner_count(child)), dim: *dim, member: *member, name: name.clone() }
        }
        Node::On(child, other) => Node::On(Box::new(inner_count(child)), other.clone()),
        other => other.clone(),
    }
}

/// Ref の番号を offset だけずらした複製（同じ式の変更前と変更後を 1 つの式の中で区別するため）。
fn shift_refs(e: &Node, offset: usize) -> Node {
    let s = |n: &Node| Box::new(shift_refs(n, offset));
    match e {
        Node::Ref(i) => Node::Ref(i + offset),
        Node::Const(..) | Node::DimRef(_) | Node::MemberConst(..) => e.clone(),
        Node::Bin(op, l, r, grow) => Node::Bin(*op, s(l), s(r), grow.clone()),
        Node::Not(c) => Node::Not(s(c)),
        Node::If(c, t, x, grow) => Node::If(s(c), s(t), x.as_ref().map(|x| s(x)), grow.clone()),
        Node::Filter(l, r) => Node::Filter(s(l), s(r)),
        Node::On(l, r) => Node::On(s(l), s(r)),
        Node::Coalesce(l, r) => Node::Coalesce(s(l), s(r)),
        Node::Expand(c, dims) => Node::Expand(s(c), dims.clone()),
        Node::IsBlank(c, grow) => Node::IsBlank(s(c), grow.clone()),
        Node::IfBlank(c, v, b, grow) => Node::IfBlank(s(c), *v, *b, grow.clone()),
        Node::ByAgg { child, src, dst, map, agg } => Node::ByAgg { child: s(child), src: *src, dst: *dst, map: *map, agg: *agg },
        Node::ByLookup { child, src, dst, map } => Node::ByLookup { child: s(child), src: *src, dst: *dst, map: *map },
        Node::Remove { child, dim, agg } => Node::Remove { child: s(child), dim: *dim, agg: *agg },
        Node::Shift { child, dim, n } => Node::Shift { child: s(child), dim: *dim, n: *n },
        Node::Select { child, dim, member, name } => Node::Select { child: s(child), dim: *dim, member: *member, name: name.clone() },
        Node::AsAxis { child, dim } => Node::AsAxis { child: s(child), dim: *dim },
        Node::By { .. } | Node::ByMetric { .. } => unreachable!("型を決めた式だけを使う"),
    }
}

/// 差分集計の計画と、件数の式。意味は Python の Model.delta_exprs と同じ。
///
/// 差分の式は「新しい値での集計 - 古い値での集計」で、集計元と対応表の変更後を作業データの 2i 番、
/// 変更前を 2i + 1 番から読む。式の refs は、元の式の Ref の番号（変更後）と、それを式の Ref の数だけ
/// ずらした番号（変更前）を、作業データの番号に写す。
pub fn derive_delta(f: &Formula) -> Option<(Delta, Option<Formula>)> {
    let (source, aux, needs_count) = delta_pattern(&f.node)?;
    let mut input_refs = vec![source];
    input_refs.extend(aux.iter().copied().filter(|a| *a != source));
    let inputs: Vec<usize> = input_refs.iter().map(|&i| f.refs[i]).collect();
    let n = f.refs.len();
    let slot = |i: usize, old: bool| input_refs.iter().position(|&r| r == i).map(|j| 2 * j + old as usize).unwrap_or(0);
    let refs: Vec<usize> = (0..n).map(|i| slot(i, false)).chain((0..n).map(|i| slot(i, true))).collect();
    let diff = |node: &Node| Formula { node: Arc::new(Node::Bin(Op::Sub, Box::new(node.clone()), Box::new(shift_refs(node, n)), vec![])), refs: refs.clone() };
    let count_node = if needs_count { inner_count(&f.node) } else { (*f.node).clone() };
    let d_count = diff(&count_node);
    let d_value = needs_count.then(|| diff(&f.node));
    let count = needs_count.then(|| Formula { node: Arc::new(count_node), refs: f.refs.clone() });
    Some((Delta { inputs, d_count, d_value }, count))
}

pub enum Step {
    One(usize),
    Scan(DimId, Vec<usize>), // 時間軸に沿って 1 時点ずつ計算する Metric の組
}

/// 計算計画。levels は依存の段ごとの計算の段階で、同じ段の段階は互いに依存しない。
pub struct Plan {
    pub metrics: Vec<Metric>,
    pub levels: Vec<Vec<Step>>,
}

/// 段の中で計算し直す 1 つの Metric。
struct Task {
    m: usize,
    region: Reg,
    delta: bool,   // 差分集計で更新する
    weight: usize, // 計算し直す行数の見積もり（並列にするかの判断に使う）
}

/// 計算し終えて、書き戻しを待つ値。範囲が全体なら、新しい格納データの本体になるキー順のセルまで作っておく
/// （置き換える前の格納データを読むだけで作れるので、同じ段の Metric と並列に作れる）。格納データの形
/// （キーと値の 2 列）に移すのは書き戻すときで、移す間だけ値が 2 つ分になるのを、段の中で 1 つずつに抑える。
enum Write {
    Whole(Vec<(u64, f64)>, Option<Vec<Vec<u32>>>), // 新しい本体のセルと、値が変わったセルの範囲
    Part(Cube),                          // 範囲の中の新しい値
}

struct Done {
    m: usize,
    region: Reg,
    delta: bool,
    value: Write,
    count: Option<Write>, // 差分集計する SUM の、各グループの件数（Whole の範囲は使わない）
    old: Option<Src>,     // 書き戻す前の値（下流の差分集計の集計元なら）
}

/// 値が変わった範囲がセル全体のこの割合以上なら、全体が変わったものとして下流へ伝える
/// （この行数以上の Metric だけ。小さな Metric では範囲を細かく伝えても安い）。
pub const WIDEN_SHARE: f64 = 0.5;
/// 再計算した Metric の記録: (Metric の番号, 差分集計なら true, 範囲)。
pub type Log = Vec<(usize, bool, Reg)>;

// 差分集計の後半で使う式（読み出し元は、変更前の値、値の差分、新しい件数の順）
fn new_count() -> Node {
    Node::Bin(Op::Add, Box::new(Node::Ref(0)), Box::new(Node::Ref(1)), vec![])
}

fn alive(i: usize) -> Box<Node> {
    Box::new(Node::Bin(Op::Gt, Box::new(Node::Ref(i)), Box::new(Node::Const(0.0, Kind::Num)), vec![]))
}

fn new_value() -> Node {
    Node::Filter(Box::new(new_count()), alive(2))
}

fn kept_count() -> Node {
    Node::Filter(Box::new(Node::Ref(0)), alive(0))
}

struct Run<'a> {
    cat: &'a Catalog,
    plan: &'a Plan,
    stores: &'a mut [Arc<Store>],
    counts: &'a mut [Option<Arc<Store>>],
    added: &'a [(DimId, Vec<u32>)],
    forced: Vec<Option<Reg>>, // 定義を変えたので、影響範囲に関係なく計算し直す範囲
    full: bool,               // 全体の再計算（格納データが空で行数を見積もれなくても、段は並列に計算する）
    regions: Vec<Option<Reg>>,
    olds: Vec<Option<Src>>,
    log: Log,
}

/// 並列に計算する仕事を、メモリの予算 max を等分して収まる組（順は保つ）に分ける。1 つの仕事の見積もりは、
/// 計算し直す範囲の行数の見積もり × 1 セルのバイト数 × 途中結果の倍率（入力を読んだものと結果など）。
fn waves(tasks: &[Task], max: usize) -> Vec<Vec<&Task>> {
    const INTERMEDIATE: usize = 4;
    let need = |t: &Task| t.weight.saturating_mul(CELL_BYTES * INTERMEDIATE);
    let mut out: Vec<Vec<&Task>> = Vec::new();
    let mut most = 0;
    for t in tasks {
        let n = need(t);
        match out.last_mut() {
            Some(w) if (w.len() + 1).saturating_mul(most.max(n)) <= max => {
                w.push(t);
                most = most.max(n);
            }
            _ => {
                out.push(vec![t]);
                most = n;
            }
        }
    }
    out
}

/// 差分再計算。changed は入力の変更範囲、olds は差分集計の集計元になる入力の変更前の値、
/// forced は定義を変えたので必ず計算し直す計算 Metric の範囲。stores と counts（Metric の番号順）は
/// その場で書き換える。
#[allow(clippy::too_many_arguments)]
pub fn recalc(
    cat: &Catalog,
    plan: &Plan,
    stores: &mut [Arc<Store>],
    counts: &mut [Option<Arc<Store>>],
    changed: Vec<(usize, Reg)>,
    added: &[(DimId, Vec<u32>)],
    olds: Vec<(usize, Src)>,
    forced: Vec<(usize, Reg)>,
    full: bool,
) -> Result<Log> {
    let n = plan.metrics.len();
    let added: Vec<(DimId, Vec<u32>)> = added.iter().map(|(d, ms)| (*d, sorted(ms.clone()))).collect();
    let mut run = Run {
        cat,
        plan,
        stores,
        counts,
        added: &added,
        forced: vec![None; n],
        full,
        regions: vec![None; n],
        olds: vec![None; n],
        log: Vec::new(),
    };
    for (m, r) in forced {
        run.forced[m] = Some(r);
    }
    for (m, r) in changed {
        run.regions[m] = Some(r);
    }
    for (m, s) in olds {
        run.olds[m] = Some(s);
    }
    for level in &plan.levels {
        run.level(level)?;
    }
    Ok(run.log)
}

impl<'a> Run<'a> {
    /// 依存の段を 1 つ計算し直す。同じ段の Metric は互いを読まないので、範囲を決めてから並列に計算し、
    /// 順に書き戻す。scan は時点ごとに自分の格納データへ書き込むので、そのあとで順に計算する。
    fn level(&mut self, level: &[Step]) -> Result<()> {
        let mut tasks = Vec::new();
        let mut scans = Vec::new();
        for step in level {
            match step {
                Step::One(m) => tasks.extend(self.prepare(*m)),
                Step::Scan(dim, names) => scans.push((*dim, names)),
            }
        }
        // 小さな仕事ばかりなら、並列にする受け渡しの費用のほうが大きいので順に計算する
        let weight: usize = tasks.iter().map(|t| t.weight).sum();
        let run: &Self = self;
        let max = self.cat.cfg.max_bytes;
        let done: Vec<Result<Done>> = if tasks.len() > 1 && (self.full || weight >= self.cat.cfg.par_min) {
            // 並列に計算する仕事は、メモリの予算を等分して持つ。見積もりが等分した額に収まる仕事だけを
            // 一度に並べ、収まらない仕事は予算をすべて持って 1 つずつ計算する
            let mut done = Vec::with_capacity(tasks.len());
            for wave in waves(&tasks, max) {
                if wave.len() == 1 {
                    done.push(run.compute(wave[0], max));
                } else {
                    let share = max / wave.len();
                    done.extend(wave.par_iter().map(|t| run.compute(t, share)).collect::<Vec<_>>());
                }
            }
            done
        } else {
            tasks.iter().map(|t| run.compute(t, max)).collect()
        };
        for d in done {
            self.apply(d?)?;
        }
        for (dim, names) in scans {
            self.scan_step(dim, names)?;
        }
        Ok(())
    }

    fn env(&self) -> Env<'_> {
        Env { cat: self.cat, regions: &self.regions, added: self.added, removed: None }
    }

    fn formula(&self, m: usize) -> &'a Formula {
        let plan: &'a Plan = self.plan;
        plan.metrics[m].formula.as_ref().expect("計算 Metric")
    }

    fn eval(&self, f: &Formula, work: Option<&[Src]>, r: &Restrict, b: &Budget) -> Result<Cube> {
        let src: Vec<Src> = match work {
            Some(w) => f.refs.iter().map(|&i| w[i].clone()).collect(),
            None => f.refs.iter().map(|&i| Src::Store(self.stores[i].clone())).collect(),
        };
        eval_with(&f.node, self.cat, &src, r, b)
    }

    fn slice(&self, m: usize, r: &Reg) -> Src {
        Src::Store(Arc::new(self.stores[m].slice(&r.restrict(self.cat))))
    }

    /// 段の 1 つの Metric について、計算し直す範囲と方法を決める。計算し直さなくてよければ None。
    fn prepare(&self, m: usize) -> Option<Task> {
        let plan: &'a Plan = self.plan;
        let metric = &plan.metrics[m];
        let f = metric.formula.as_ref()?;
        let forced = self.forced[m].clone();
        let redefined = forced.is_some();
        let region = union(self.env().affected(&f.node, &f.refs), forced)?;
        let delta = match &metric.delta {
            Some(d) => !redefined && self.delta_applicable(d),
            None => false,
        };
        let size = |d: DimId| self.cat.dims[d].size.max(1) as f64;
        let share: f64 = region.sels.iter().map(|(d, ms)| ms.len() as f64 / size(*d)).product();
        let weight = (self.stores[m].rows_hint() as f64 * share) as usize;
        Some(Task { m, region, delta, weight })
    }

    /// 計算する（格納データは読むだけなので、同じ段の Metric を並列に計算できる）。limit はメモリの予算。
    fn compute(&self, t: &Task, limit: usize) -> Result<Done> {
        let plan: &'a Plan = self.plan;
        let metric = &plan.metrics[t.m];
        // 集計元なら、書き戻す前の値を下流の差分集計のために取っておく。全体を計算し直すなら、
        // 置き換える前の格納データをそのまま使う（複製しない）
        let old = metric.source.then(|| {
            if t.region.is_all() {
                Src::Store(self.stores[t.m].clone())
            } else {
                self.slice(t.m, &t.region)
            }
        });
        let (value, count) = match (&metric.delta, t.delta) {
            (Some(d), true) => self.compute_delta(t.m, d, &t.region, &Budget::new(limit))?,
            _ => {
                let r = t.region.restrict(self.cat);
                match &metric.count {
                    // 件数の式は値の式と同じ集計元を読むので、仕事が大きければ並列に評価する（予算に限りが
                    // あれば、値を持ったまま件数を評価する順の計算にして、値の分も予算に数える）
                    Some(c) if (self.full || t.weight >= self.cat.cfg.par_min) && limit == usize::MAX => {
                        let (value, count) = rayon::join(
                            || self.eval(self.formula(t.m), None, &r, &Budget::new(limit)),
                            || self.eval(c, None, &r, &Budget::new(limit)),
                        );
                        (value?, Some(count?))
                    }
                    Some(c) => {
                        let b = Budget::new(limit);
                        let value = self.eval(self.formula(t.m), None, &r, &b)?;
                        (value, Some(self.eval(c, None, &r, &b)?))
                    }
                    None => (self.eval(self.formula(t.m), None, &r, &Budget::new(limit))?, None),
                }
            }
        };
        let (value, count) = if t.region.is_all() {
            let (cells, sets) = if self.full {
                // 全体の再計算では下流も全体を計算し直すので、値が変わった範囲は求めない
                (self.stores[t.m].sorted_cells(value)?, None)
            } else {
                self.stores[t.m].replaced_all(value)?
            };
            let count = match count {
                Some(c) => Some(Write::Whole(self.counts[t.m].as_ref().expect("件数の格納").sorted_cells(c)?, None)),
                None => None,
            };
            (Write::Whole(cells, sets), count)
        } else {
            (Write::Part(value), count.map(Write::Part))
        };
        Ok(Done { m: t.m, region: t.region.clone(), delta: t.delta, value, count, old })
    }

    /// 計算した値を書き戻し、値が実際に変わったセルの範囲を下流への影響範囲にする。
    fn apply(&mut self, done: Done) -> Result<()> {
        let Done { m, region, delta, value, count, old } = done;
        match self.cat.cfg.fail_at {
            Some((f, false)) if f == m => return Err(format!("書き戻しの失敗（テスト用、Metric {m}）")),
            Some((f, true)) if f == m => panic!("書き戻しの panic（テスト用、Metric {m}）"),
            _ => {}
        }
        let r = region.restrict(self.cat);
        let sets = match value {
            Write::Whole(cells, sets) => {
                self.stores[m] = Arc::new(self.stores[m].with_sorted(cells));
                sets
            }
            Write::Part(value) => Arc::make_mut(&mut self.stores[m]).replace_diff(&r, &value)?,
        };
        match count {
            Some(Write::Whole(cells, _)) => {
                let counts = self.counts[m].as_ref().expect("件数の格納").with_sorted(cells);
                self.counts[m] = Some(Arc::new(counts));
            }
            Some(Write::Part(c)) => Arc::make_mut(self.counts[m].as_mut().expect("件数の格納")).replace(&r, &c)?,
            None => {}
        }
        if old.is_some() {
            self.olds[m] = old;
        }
        self.log.push((m, delta, region));
        if let Some(sets) = sets {
            self.regions[m] = Some(self.changed_region(m, sets));
        }
        Ok(())
    }

    /// 値が変わったセル（Metric の軸ごとのメンバー番号）を囲む範囲。全メンバーにわたる軸は範囲から外す
    /// （Python の Model._widened と同じ）。さらに WIDEN_MIN_ROWS 行以上の Metric では、範囲がセル全体の
    /// WIDEN_SHARE 以上を占めるなら全体にする。全体として扱えば、下流の書き戻しは並べ直すだけで済み、
    /// 差分集計より計算し直しを選べる（範囲を広げても結果は変わらない）。
    fn changed_region(&self, m: usize, sets: Vec<Vec<u32>>) -> Reg {
        let store = &self.stores[m];
        let dims = &store.metric_dims;
        let size = |d: DimId| self.cat.dims[d].size as usize;
        let share: f64 = dims.iter().zip(&sets).map(|(&d, ms)| ms.len() as f64 / size(d).max(1) as f64).product();
        if share >= WIDEN_SHARE && store.rows_hint() >= self.cat.cfg.widen_min_rows {
            return Reg::default();
        }
        Reg::new(dims.iter().copied().zip(sets).filter(|(d, ms)| ms.len() < size(*d)).collect())
    }

    /// 集計元と対応表の変更範囲を合わせた範囲。どれも変わっていなければ None。
    fn delta_range(&self, d: &Delta) -> Option<Reg> {
        d.inputs.iter().fold(None, |acc, &n| union(acc, self.regions[n].clone()))
    }

    fn delta_applicable(&self, d: &Delta) -> bool {
        let changed: Vec<usize> = d.inputs.iter().copied().filter(|&n| self.regions[n].is_some()).collect();
        // 範囲が Metric 全体に広がるなら、差分より計算し直すほうが速い
        !changed.is_empty()
            && changed.iter().all(|&n| self.olds[n].is_some())
            && self.delta_range(d).is_some_and(|r| !r.is_all())
    }

    /// 集計元（と対応表）の変更前後の差分を集計し、region 内の既存の値と件数に足し込んだ値と件数。
    fn compute_delta(&self, m: usize, d: &Delta, region: &Reg, b: &Budget) -> Result<(Cube, Option<Cube>)> {
        let cat = self.cat;
        let all = Restrict::all(cat.dims.len());
        let range = self.delta_range(d).expect("変更範囲").restrict(cat);
        let mut work = Vec::with_capacity(2 * d.inputs.len());
        for &n in &d.inputs {
            let new = self.stores[n].slice(&range);
            let old = match &self.regions[n] {
                // 変更前の値は、実際に変わった範囲の分だけ戻す
                Some(changed) => {
                    let rn = changed.restrict(cat);
                    let before = self.olds[n].as_ref().expect("変更前の値").read(&self.cat.cfg, &rn);
                    let mut old = new.clone();
                    old.replace(&rn, &before)?;
                    Arc::new(old)
                }
                None => Arc::new(new.clone()),
            };
            work.push(Src::Store(Arc::new(new)));
            work.push(Src::Store(old));
        }
        let plan: &'a Plan = self.plan;
        let metric = &plan.metrics[m];
        let r = region.restrict(cat);
        let old_value = Src::Store(Arc::new(self.stores[m].slice(&r)));
        let old_count = match (&metric.count, &self.counts[m]) {
            (Some(_), Some(c)) => Src::Store(Arc::new(c.slice(&r))),
            _ => old_value.clone(),
        };
        let d_count = self.eval(&d.d_count, Some(&work), &all, b)?;
        let d_value = match &d.d_value {
            Some(f) => self.eval(f, Some(&work), &all, b)?,
            None => d_count.clone(),
        };
        let count = Arc::new(eval_with(&new_count(), cat, &[old_count, Src::Cube(Arc::new(d_count))], &all, b)?);
        let value = eval_with(&new_value(), cat, &[old_value, Src::Cube(Arc::new(d_value)), Src::Cube(count.clone())], &all, b)?;
        // 件数が 0 になったグループは消す
        let kept = match metric.count {
            Some(_) => Some(eval_with(&kept_count(), cat, &[Src::Cube(count)], &all, b)?),
            None => None,
        };
        Ok((value, kept))
    }

    /// scan の Metric の影響範囲を、増えなくなるまで伝えてから、時間軸に沿って 1 時点ずつ計算する。
    fn scan_step(&mut self, dim: DimId, names: &[usize]) -> Result<()> {
        for &n in names {
            self.regions[n] = union(self.regions[n].take(), self.forced[n].clone());
        }
        scan_regions(self.cat, self.plan, &mut self.regions, names, self.added, None);
        let active: Vec<(usize, Reg)> = names.iter().filter_map(|&n| self.regions[n].clone().map(|r| (n, r))).collect();
        for (n, r) in &active {
            if self.plan.metrics[*n].source {
                let old = if r.is_all() { Src::Store(self.stores[*n].clone()) } else { self.slice(*n, r) };
                self.olds[*n] = Some(old);
            }
            if r.is_all() {
                // 全時点を書き直すので、空から書き込む（既存のセルを時点ごとに消して書き直すより速い）
                self.stores[*n] = Arc::new(self.stores[*n].empty_like());
            }
        }
        // 格納データにその場で 1 時点ずつ書き込む。次の時点の前月参照は、書き込んだばかりの時点を読む
        for t in 0..self.cat.dims[dim].size {
            for (n, r) in &active {
                if !r.has(dim, t) {
                    continue;
                }
                let sub = r.set(dim, vec![t]).restrict(self.cat);
                let value = self.eval(self.formula(*n), None, &sub, &Budget::new(self.cat.cfg.max_bytes))?;
                Arc::make_mut(&mut self.stores[*n]).replace(&sub, &value)?;
            }
        }
        for (n, r) in active {
            self.log.push((n, false, r));
        }
        Ok(())
    }
}
