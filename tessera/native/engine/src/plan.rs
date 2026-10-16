//! 差分再計算の段取り。意味は Python の Model.recalc の差分の経路（evaluate.affected、
//! Model._scan_regions、Model._scan、Model._apply_delta）と同じで、テストで突き合わせる。
//!
//! 入力の変更範囲を計算計画の順に伝え、各 Metric の影響範囲だけを評価して書き戻す。書き戻すときに
//! 新旧の値を比べ、実際に値が変わったセルだけを下流への影響範囲にする。影響範囲はメンバー名ではなく
//! 番号の集合で持つので、1 回の変更で何百もの Metric を計算し直しても、Python との往復は 1 回で済む。

use crate::{eval_sums, eval_with, held, remove_run, tree, tree_scan, Agg, Budget, Catalog, Cube, CELL_BYTES, DimId, ExactSum, FxHashMap, Kind, Node, Op, Restrict, Result, Sel, Src, Store};
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

/// The plan of an incremental aggregation. inputs is [source, maps...]. The refs of the work formulas are work
/// data numbers: the new data of the i-th input is 2i, the old data is 2i + 1.
pub struct Delta {
    pub inputs: Vec<usize>,
    /// The dimensions that the aggregations under the outermost one remove. The delta range does not limit them,
    /// so each inner group with a changed row is read in full and rounds as in a full evaluation.
    pub widen: Vec<DimId>,
    pub method: Method,
}

/// How a Metric with incremental aggregation gets its values, counts and residuals.
pub enum Method {
    /// COUNT: d_count is the change of the value on the work data (the value is the count).
    Count { d_count: Formula },
    /// SUM: the formula on the new and on the old work data. eval_sums gives the value, the count and the residual
    /// from one pass, also in a compute without incremental aggregation. The count is the number of cells that go
    /// into the outermost aggregation.
    OnePass { work: [Formula; 2] },
}

pub struct Metric {
    pub formula: Option<Formula>, // None なら入力
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

/// Add to out the dimensions that the aggregations under the outermost one remove. A run of REMOVE SUM is one
/// aggregation (see remove_run).
fn inner_dims(e: &Node, outer: bool, out: &mut Vec<DimId>) {
    match e {
        Node::Remove { child, dim, agg } if outer => inner_dims(remove_run(*dim, *agg, child).1, false, out),
        Node::ByAgg { child, src: d, .. } | Node::Remove { child, dim: d, .. } => {
            if !outer {
                out.push(*d);
            }
            inner_dims(child, false, out);
        }
        Node::Select { child, .. } | Node::On(child, _) => inner_dims(child, outer, out),
        _ => {}
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

/// The plan of the incremental aggregation of f, or None if delta_pattern does not accept f.
///
/// The work formulas read the new data of the source and the maps from work data 2i, and the old data from 2i + 1.
/// Their refs map each Ref of f (new data), and the same Ref shifted by the number of refs (old data), to work
/// data numbers.
pub fn derive_delta(f: &Formula) -> Option<Delta> {
    let (source, aux, needs_count) = delta_pattern(&f.node)?;
    let mut input_refs = vec![source];
    input_refs.extend(aux.iter().copied().filter(|a| *a != source));
    let inputs: Vec<usize> = input_refs.iter().map(|&i| f.refs[i]).collect();
    let n = f.refs.len();
    let slot = |i: usize, old: bool| input_refs.iter().position(|&r| r == i).map(|j| 2 * j + old as usize).unwrap_or(0);
    let refs: Vec<usize> = (0..n).map(|i| slot(i, false)).chain((0..n).map(|i| slot(i, true))).collect();
    let work = |node: Node| Formula { node: Arc::new(node), refs: refs.clone() };
    let (new, old) = ((*f.node).clone(), shift_refs(&f.node, n));
    let mut widen = Vec::new();
    let method = if needs_count {
        inner_dims(&f.node, true, &mut widen);
        Method::OnePass { work: [work(new), work(old)] }
    } else {
        Method::Count { d_count: work(Node::Bin(Op::Sub, Box::new(new), Box::new(old), vec![])) }
    };
    Some(Delta { inputs, widen, method })
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

impl Write {
    fn bytes(&self) -> usize {
        match self {
            Write::Whole(cells, _) => cells.capacity(),
            Write::Part(c) => c.cells.capacity(),
        }
        .saturating_mul(CELL_BYTES)
    }
}

/// The cells that the write of whole results needs at its peak, with the results. The write puts the cells in the
/// order of the store (a copy if the packing is different) and then makes the new columns (about 1 cell for each
/// row). With `diff`, Store::replaced_all also merges the old and the new cells: 1.5 cells for each row of both,
/// and a copy of the old cells if the store has a delta.
fn whole_write(store: &Store, value: &Cube, sides: &[&Cube], diff: bool) -> usize {
    let (n, old) = (value.cells.len(), store.rows_hint());
    let merge = if diff { (old + n) * 3 / 2 + if store.base_rows().is_none() { old } else { 0 } } else { 0 };
    let peak = sides.iter().map(|c| c.cells.len()).fold(n.max(merge), usize::max);
    sides.iter().map(|c| c.cells.capacity()).sum::<usize>() + value.cells.capacity() + peak
}

struct Done {
    m: usize,
    region: Reg,
    delta: bool,
    value: Write,
    count: Option<Write>, // 差分集計する SUM の、各グループの件数（Whole の範囲は使わない）
    low: Option<Write>,   // the residual of each group of an incremental SUM (cells that are not 0)
    old: Option<Old>,     // the value before the write (if it is the source of a downstream incremental aggregation)
}

enum Old {
    Held(Arc<Store>),
    Slice(Arc<Store>),
}

/// The value before the write. For the whole region, keep the store itself (do not copy it).
fn old_of(store: &Arc<Store>, r: &Reg, cat: &Catalog) -> Old {
    if r.is_all() { Old::Held(store.clone()) } else { Old::Slice(Arc::new(store.slice(&r.restrict(cat)))) }
}

impl Old {
    /// The bytes of the copy. Held is the store itself, which held counts already.
    fn copied(&self) -> usize {
        match self {
            Old::Held(_) => 0,
            Old::Slice(s) => s.memory().bytes(),
        }
    }
}

impl From<Old> for Src {
    fn from(old: Old) -> Src {
        match old {
            Old::Held(s) | Old::Slice(s) => Src::Store(s),
        }
    }
}

impl Done {
    fn bytes(&self) -> usize {
        let writes: usize = [Some(&self.value), self.count.as_ref(), self.low.as_ref()].into_iter().flatten().map(Write::bytes).sum();
        writes.saturating_add(self.old.as_ref().map_or(0, Old::copied))
    }
}

/// 値が変わった範囲がセル全体のこの割合以上なら、全体が変わったものとして下流へ伝える
/// （この行数以上の Metric だけ。小さな Metric では範囲を細かく伝えても安い）。
pub const WIDEN_SHARE: f64 = 0.5;
/// 再計算した Metric の記録: (Metric の番号, 差分集計なら true, 範囲)。
pub type Log = Vec<(usize, bool, Reg)>;

/// The change of the count: the new count minus the old count (a missing cell is 0).
fn sub() -> Node {
    Node::Bin(Op::Sub, Box::new(Node::Ref(0)), Box::new(Node::Ref(1)), vec![])
}

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
    lows: &'a mut [Option<Arc<Store>>],
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

/// The limit of each task in a wave: held plus an equal part of the room. A task adds its copies to held, not to the
/// limit. Never more than max, so held data over max is an error.
fn share(max: usize, held: usize, n: usize) -> usize {
    held.saturating_add(max.saturating_sub(held) / n).min(max)
}

/// 差分再計算。changed は入力の変更範囲、olds は差分集計の集計元になる入力の変更前の値、
/// forced は定義を変えたので必ず計算し直す計算 Metric の範囲。stores と counts（Metric の番号順）は
/// その場で書き換える。lows (the residual stores, in Metric number order) also change in place.
#[allow(clippy::too_many_arguments)]
pub fn recalc(
    cat: &Catalog,
    plan: &Plan,
    stores: &mut [Arc<Store>],
    counts: &mut [Option<Arc<Store>>],
    lows: &mut [Option<Arc<Store>>],
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
        lows,
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
    /// Calculate one level of the dependencies again. The Metrics of one level do not read each other.
    /// Thus decide the regions, calculate them in parallel, and then write them back in sequence.
    /// A scan writes to its own stored data at each time point, so it calculates after them in sequence.
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
        let held = if tasks.is_empty() { 0 } else { self.held() };
        let mut unapplied = 0;
        let mut done: Vec<Result<Done>> = Vec::with_capacity(tasks.len());
        let parallel = tasks.len() > 1 && (self.full || weight >= self.cat.cfg.par_min);
        let mut rest = &tasks[..];
        while !rest.is_empty() {
            let held = held.saturating_add(unapplied);
            // Group each wave again, because the earlier waves leave less room
            let wave = if parallel { waves(rest, max.saturating_sub(held)).remove(0) } else { vec![&rest[0]] };
            rest = &rest[wave.len()..];
            let limit = share(max, held, wave.len());
            let out: Vec<Result<Done>> = if wave.len() == 1 {
                vec![run.compute(wave[0], limit, held)]
            } else {
                wave.par_iter().map(|t| run.compute(t, limit, held)).collect()
            };
            unapplied += out.iter().flatten().map(Done::bytes).sum::<usize>();
            done.extend(out);
        }
        for d in done {
            self.apply(d?)?;
        }
        for (dim, names) in scans {
            self.scan_step(dim, names)?;
        }
        Ok(())
    }

    fn held(&self) -> usize {
        if self.cat.cfg.max_bytes == usize::MAX {
            return 0;
        }
        let stores = self.stores.iter().chain(self.counts.iter().flatten()).chain(self.lows.iter().flatten());
        let src: Vec<Src> = stores.map(|s| Src::Store(s.clone())).chain(self.olds.iter().flatten().cloned()).collect();
        held(&src)
    }

    fn env(&self) -> Env<'_> {
        Env { cat: self.cat, regions: &self.regions, added: self.added, removed: None }
    }

    fn formula(&self, m: usize) -> &'a Formula {
        let plan: &'a Plan = self.plan;
        plan.metrics[m].formula.as_ref().expect("計算 Metric")
    }

    /// The sources of f: the work data, or the stored data if work is None.
    fn srcs(&self, f: &Formula, work: Option<&[Src]>) -> Vec<Src> {
        match work {
            Some(w) => f.refs.iter().map(|&i| w[i].clone()).collect(),
            None => f.refs.iter().map(|&i| Src::Store(self.stores[i].clone())).collect(),
        }
    }

    fn eval(&self, f: &Formula, work: Option<&[Src]>, r: &Restrict, b: &Budget) -> Result<Cube> {
        eval_with(&f.node, self.cat, &self.srcs(f, work), r, b)
    }

    /// The value, the count and the residual of a SUM (Method::OnePass).
    fn sums(&self, f: &Formula, work: Option<&[Src]>, r: &Restrict, b: &Budget) -> Result<[Cube; 3]> {
        eval_sums(&f.node, self.cat, &self.srcs(f, work), r, b)
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

    /// Calculate the task. It only reads the stored data, so the Metrics of one level can calculate in parallel.
    fn compute(&self, t: &Task, limit: usize, held: usize) -> Result<Done> {
        let plan: &'a Plan = self.plan;
        let metric = &plan.metrics[t.m];
        let old = metric.source.then(|| old_of(&self.stores[t.m], &t.region, self.cat));
        let held = held.saturating_add(old.as_ref().map_or(0, Old::copied));
        let delta = match (&metric.delta, t.delta) {
            (Some(d), true) => self.compute_delta(t.m, d, &t.region, limit, held)?,
            _ => None,
        };
        let delta_used = delta.is_some();
        let (value, count, low) = match delta {
            Some(done) => done,
            None => {
                let (r, b) = (t.region.restrict(self.cat), Budget::new(limit, held));
                match metric.delta.as_ref().map(|d| &d.method) {
                    Some(Method::OnePass { .. }) => {
                        let [value, count, low] = self.sums(self.formula(t.m), None, &r, &b)?;
                        (value, Some(count), Some(low))
                    }
                    _ => (self.eval(self.formula(t.m), None, &r, &b)?, None, None),
                }
            }
        };
        if t.region.is_all() {
            let sides: Vec<&Cube> = count.iter().chain(&low).collect();
            Budget::new(limit, held).need(whole_write(&self.stores[t.m], &value, &sides, !self.full), "結果の書き込み")?;
        }
        let (value, count, low) = if t.region.is_all() {
            let (cells, sets) = if self.full {
                // 全体の再計算では下流も全体を計算し直すので、値が変わった範囲は求めない
                (self.stores[t.m].sorted_cells(value)?, None)
            } else {
                self.stores[t.m].replaced_all(value)?
            };
            let whole = |side: &[Option<Arc<Store>>], c: Option<Cube>| {
                c.map(|c| side[t.m].as_ref().expect("件数か残差の格納").sorted_cells(c).map(|cells| Write::Whole(cells, None))).transpose()
            };
            (Write::Whole(cells, sets), whole(&*self.counts, count)?, whole(&*self.lows, low)?)
        } else {
            (Write::Part(value), count.map(Write::Part), low.map(Write::Part))
        };
        Ok(Done { m: t.m, region: t.region.clone(), delta: delta_used, value, count, low, old })
    }

    /// Write back the calculated values. The region of the cells whose values changed goes downstream.
    fn apply(&mut self, done: Done) -> Result<()> {
        let Done { m, region, delta, value, count, low, old } = done;
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
        for (side, w) in [(&mut *self.counts, count), (&mut *self.lows, low)] {
            match w {
                Some(Write::Whole(cells, _)) => side[m] = Some(Arc::new(side[m].as_ref().expect("件数か残差の格納").with_sorted(cells))),
                Some(Write::Part(c)) => Arc::make_mut(side[m].as_mut().expect("件数か残差の格納")).replace(&r, &c)?,
                None => {}
            }
        }
        if let Some(old) = old {
            self.olds[m] = Some(old.into());
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

    /// The union of the changed ranges of the source and the maps, without the widen dimensions. None if
    /// nothing changed.
    fn delta_range(&self, d: &Delta) -> Option<Reg> {
        d.inputs.iter().fold(None, |acc, &n| union(acc, self.regions[n].clone())).map(|r| r.without(&d.widen))
    }

    fn delta_applicable(&self, d: &Delta) -> bool {
        let changed: Vec<usize> = d.inputs.iter().copied().filter(|&n| self.regions[n].is_some()).collect();
        // 範囲が Metric 全体に広がるなら、差分より計算し直すほうが速い
        !changed.is_empty()
            && changed.iter().all(|&n| self.olds[n].is_some())
            && self.delta_range(d).is_some_and(|r| !r.is_all())
    }

    /// Aggregate the change of the source (and the maps) and add it to the values and counts in region. Returns
    /// (values, counts, residuals). A SUM is exact: the stored value, the stored residual and the changes go into
    /// one ExactSum for each group. Thus the result has the same bits as a full evaluation. If a residual is not
    /// one f64 (NaN), returns None, and the caller calculates the region again.
    #[allow(clippy::type_complexity)]
    fn compute_delta(&self, m: usize, d: &Delta, region: &Reg, limit: usize, held: usize) -> Result<Option<(Cube, Option<Cube>, Option<Cube>)>> {
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
        // The slices in work stay until the end, so the budget counts them
        let b = &Budget::new(limit, held.saturating_add(crate::held(&work)));
        let r = region.restrict(cat);
        let store = &self.stores[m];
        let cube = |c: Cube| Src::Cube(Arc::new(c));
        let [new, old] = match &d.method {
            Method::Count { d_count } => {
                // COUNT: the counts are exact integers, and the value is the count
                let old_value = Src::Store(Arc::new(store.slice(&r)));
                let d_count = Arc::new(self.eval(d_count, Some(&work), &all, b)?);
                let count = eval_with(&new_count(), cat, &[old_value.clone(), Src::Cube(d_count.clone())], &all, b)?;
                let value = eval_with(&new_value(), cat, &[old_value, Src::Cube(d_count), cube(count)], &all, b)?;
                return Ok(Some((value, None, None)));
            }
            Method::OnePass { work } => work,
        };
        let [v_new, c_new, l_new] = self.sums(new, Some(&work), &all, b)?;
        let [v_old, c_old, l_old] = self.sums(old, Some(&work), &all, b)?;
        let d_count = eval_with(&sub(), cat, &[cube(c_new), cube(c_old)], &all, b)?;
        let old_count = Src::Store(Arc::new(self.counts[m].as_ref().expect("件数の格納").slice(&r)));
        let count = eval_with(&new_count(), cat, &[old_count, cube(d_count)], &all, b)?;
        // Drop the groups with a count of 0
        let kept = eval_with(&kept_count(), cat, &[cube(count)], &all, b)?.into_repacked(&cat.cfg, &store.pack);
        // A redefinition makes a new residual store, which can have another key packing. Thus repack each part
        let low = self.lows[m].as_ref().expect("残差の格納").read(&r);
        let parts = [(store.read(&r), 1.0), (low, 1.0), (v_new, 1.0), (l_new, 1.0), (v_old, -1.0), (l_old, -1.0)];
        // A map entry (a key and an ExactSum) is about 3 cells. The values and the residuals are 2 cells a group
        let rows: usize = parts.iter().map(|(c, _)| c.cells.len()).sum();
        b.need(rows.saturating_mul(3).saturating_add(kept.cells.len() * 2), "差分集計の合算")?;
        let mut exact: FxHashMap<u64, ExactSum> = FxHashMap::default();
        for (c, sign) in parts {
            for &(k, v) in &c.into_repacked(&cat.cfg, &store.pack).cells {
                exact.entry(k).or_default().add(sign * v);
            }
        }
        let (mut values, mut lows) = (Vec::with_capacity(kept.cells.len()), Vec::new());
        let zero = ExactSum::default();
        for &(k, _) in &kept.cells {
            let s = exact.get(&k).unwrap_or(&zero);
            let low = s.residual();
            if low.is_nan() {
                return Ok(None);
            }
            values.push((k, s.finish()));
            if low != 0.0 {
                lows.push((k, low));
            }
        }
        values.sort_unstable_by_key(|c| c.0);
        lows.sort_unstable_by_key(|c| c.0);
        let num = |cells| Cube { pack: store.pack.clone(), kind: Kind::Num, cells };
        Ok(Some((num(values), Some(kept), Some(num(lows)))))
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
                self.olds[*n] = Some(old_of(&self.stores[*n], r, self.cat).into());
            }
            if r.is_all() {
                // 全時点を書き直すので、空から書き込む（既存のセルを時点ごとに消して書き直すより速い）
                self.stores[*n] = Arc::new(self.stores[*n].empty_like());
            }
        }
        if active.is_empty() {
            return Ok(());
        }
        let held = self.held();
        // If only one Metric of the scan is active, the other Metrics do not change, thus the operands can read their stored data
        if let [(n, r)] = &active[..] {
            if self.tree_scan(dim, *n, r, held)? {
                self.log.push((*n, false, r.clone()));
                return Ok(());
            }
        }
        // The active stores grow in each period, so count their current bytes again before each evaluation
        let grown = |stores: &[Arc<Store>]| active.iter().map(|(n, _)| stores[*n].memory().bytes()).sum::<usize>();
        let others = held.saturating_sub(grown(self.stores));
        // 格納データにその場で 1 時点ずつ書き込む。次の時点の前月参照は、書き込んだばかりの時点を読む
        for t in 0..self.cat.dims[dim].size {
            for (n, r) in &active {
                if !r.has(dim, t) {
                    continue;
                }
                let sub = r.set(dim, vec![t]).restrict(self.cat);
                let held = others.saturating_add(grown(self.stores));
                let value = self.eval(self.formula(*n), None, &sub, &Budget::new(self.cat.cfg.max_bytes, held))?;
                Arc::make_mut(&mut self.stores[*n]).replace(&sub, &value)?;
            }
        }
        for (n, r) in active {
            self.log.push((n, false, r));
        }
        Ok(())
    }

    /// Calculate all periods of the range r for the scan Metric n in one pass, and write them back.
    /// The formula must be a tree of +, -, *, /, IF and COALESCE over PREVIOUS (see `eval/scan.rs`).
    /// Return false if the pass cannot calculate the Metric. Then the per-period path calculates it.
    fn tree_scan(&mut self, dim: DimId, n: usize, r: &Reg, held: usize) -> Result<bool> {
        let f = self.formula(n);
        let Some(shape) = tree(&f.node, dim, &|i| f.refs[i] == n) else { return Ok(false) };
        let ts: Vec<u32> = (0..self.cat.dims[dim].size).filter(|&t| r.has(dim, t)).collect();
        // The periods that PREVIOUS reads outside ts: it reads the stored values there
        let mut gaps: Vec<u32> = ts.iter().flat_map(|&t| shape.lags.iter().filter_map(move |&k| t.checked_sub(k))).filter(|&s| !r.has(dim, s)).collect();
        gaps.sort_unstable();
        gaps.dedup();
        let store = &self.stores[n];
        let before = if gaps.is_empty() {
            Cube { pack: store.pack.clone(), kind: store.kind, cells: Vec::new() }
        } else {
            store.read(&r.set(dim, gaps).restrict(self.cat))
        };
        let src: Vec<Src> = f.refs.iter().map(|&i| Src::Store(self.stores[i].clone())).collect();
        let rr = r.restrict(self.cat);
        let b = Budget::new(self.cat.cfg.max_bytes, held);
        // An error here also occurs in the per-period path, except a budget error. That path uses less memory
        let Ok(value) = tree_scan(&shape, &before, &store.pack, dim, &ts, self.cat, &src, &rr, &b) else { return Ok(false) };
        Arc::make_mut(&mut self.stores[n]).replace(&rr, &value)?;
        Ok(true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tasks_of_a_wave_split_the_room_after_the_held_data() {
        let limit = share(1000, 400, 2);
        let b = Budget::new(limit, 400);
        assert_eq!(limit, 700);
        assert!(b.need(300 / CELL_BYTES, "x").is_ok());
        assert!(b.need(300 / CELL_BYTES + 1, "x").is_err());
        assert_eq!(share(100, 400, 2), 100);
    }

    #[test]
    fn the_old_value_of_a_partial_region_counts_but_of_the_whole_region_does_not() {
        let dims = vec![crate::DimInfo { size: 10, ordered: false, name: "d0".into() }];
        let cat = Catalog { dims, maps: Vec::new(), cfg: crate::Config::default() };
        let store = Arc::new(Store::new(&[0], None, Kind::Num, &cat).unwrap().with_rows(&[&[1, 2]], &[1.0, 2.0]).unwrap());
        let done = |region: Reg| {
            let old = old_of(&store, &region, &cat);
            Done { m: 0, region, delta: false, value: Write::Whole(Vec::new(), None), count: None, low: None, old: Some(old) }
        };
        assert_eq!(done(Reg::new(vec![])).bytes(), 0);
        let part = Reg::new(vec![(0, vec![1])]);
        let bytes = store.slice(&part.restrict(&cat)).memory().bytes();
        assert!(bytes > 0);
        assert_eq!(done(part).bytes(), bytes);
    }

    #[test]
    fn each_wave_fits_in_the_room_that_the_earlier_waves_leave() {
        // The rows of a stored result set the estimate of its task. Metric 1 runs alone. Metrics 2 and 3 fit
        // together at the start of the level, but not after metric 1 keeps 16,000 bytes for apply. The write of a
        // copy of 1,000 rows needs 32,000 bytes (the result and the new columns)
        let dims = vec![crate::DimInfo { size: 1000, ordered: false, name: "d0".into() }];
        let start = Catalog { dims: dims.clone(), maps: Vec::new(), cfg: crate::Config::default() };
        let store = |n: u32| {
            let keys: Vec<u32> = (0..n).collect();
            Arc::new(Store::new(&[0], None, Kind::Num, &start).unwrap().with_rows(&[&keys], &vec![1.0; n as usize]).unwrap())
        };
        let mut stores = vec![store(1000), store(1000), store(300), store(250), store(1)];
        let input = || Metric { formula: None, delta: None, source: false };
        let copy = |i| Metric { formula: Some(Formula { node: Arc::new(Node::Ref(0)), refs: vec![i] }), delta: None, source: false };
        let plan = Plan { metrics: vec![input(), copy(0), copy(0), copy(4), input()], levels: vec![vec![Step::One(1), Step::One(2), Step::One(3)]] };
        let src: Vec<Src> = stores.iter().map(|s| Src::Store(s.clone())).collect();
        let held = held(&src);
        let cat = Catalog { dims, maps: Vec::new(), cfg: crate::Config { max_bytes: held + 48_000, ..crate::Config::default() } };
        let all = || Reg::new(vec![]);
        let changed = vec![(0, all()), (4, all())];
        recalc(&cat, &plan, &mut stores, &mut vec![None; 5], &mut vec![None; 5], changed, &[], vec![], vec![], true).unwrap();
        assert_eq!(stores[2].rows_hint(), 1000);
    }

    #[test]
    fn stored_data_over_the_budget_is_an_error_also_for_an_empty_result() {
        let dims = vec![crate::DimInfo { size: 10, ordered: false, name: "d0".into() }];
        let start = Catalog { dims: dims.clone(), maps: Vec::new(), cfg: crate::Config::default() };
        let store = |n: u32| {
            let keys: Vec<u32> = (0..n).collect();
            Arc::new(Store::new(&[0], None, Kind::Num, &start).unwrap().with_rows(&[&keys], &vec![1.0; n as usize]).unwrap())
        };
        let mut stores = vec![store(0), store(0), store(10)];
        let input = || Metric { formula: None, delta: None, source: false };
        let copy = Metric { formula: Some(Formula { node: Arc::new(Node::Ref(0)), refs: vec![0] }), delta: None, source: false };
        let plan = Plan { metrics: vec![input(), copy, input()], levels: vec![vec![Step::One(1)]] };
        let src: Vec<Src> = stores.iter().map(|s| Src::Store(s.clone())).collect();
        let cat = Catalog { dims, maps: Vec::new(), cfg: crate::Config { max_bytes: held(&src) - 1, ..crate::Config::default() } };
        let changed = vec![(0, Reg::new(vec![]))];
        assert!(recalc(&cat, &plan, &mut stores, &mut vec![None; 3], &mut vec![None; 3], changed, &[], vec![], vec![], true).is_err());
    }
}
