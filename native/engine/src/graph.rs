//! 計算計画。Metric 単位の依存グラフから、強連結成分をトポロジカル順に並べ、依存の段に分ける。
//! 意味と文言は Python の参照実装（Model._make_step、_levels、_tarjan、evaluate.collect_refs）と同じで、
//! テスト（tests/test_expr_coverage.py）で突き合わせる。
//!
//! 循環は「全員が同じ順序付き軸を持ち、循環が必ず正のずらし（PREVIOUS）を通る」場合だけ許し、
//! その軸に沿った scan として 1 時点ずつ計算する。それ以外の循環はエラーにする。

use crate::{Arg, Catalog, DimId, Diag, Node};
use crate::plan::Formula;

/// Metric -> 参照先 Metric。lags は参照経路上での各軸方向のずらし量の合計、
/// broken は経路上で集約・付け替えされた軸（ずらし量を信用できない）。
#[derive(Clone, Debug)]
struct Edge {
    target: usize,
    lags: Vec<(DimId, i64)>,
    broken: Vec<DimId>,
}

impl Edge {
    fn lag(&self, d: DimId) -> i64 {
        self.lags.iter().find(|(x, _)| *x == d).map_or(0, |(_, n)| *n)
    }
}

fn add_lag(lags: &[(DimId, i64)], d: DimId, n: i64) -> Vec<(DimId, i64)> {
    let mut out = lags.to_vec();
    match out.iter_mut().find(|(x, _)| *x == d) {
        Some(e) => e.1 += n,
        None => out.push((d, n)),
    }
    out
}

fn with(broken: &[DimId], ds: &[DimId]) -> Vec<DimId> {
    let mut out = broken.to_vec();
    for &d in ds {
        if !out.contains(&d) {
            out.push(d);
        }
    }
    out
}

/// 式が読む Metric を、経路上のずらしと壊れた軸つきで集める（Ref の出現ごとに 1 本）。
fn collect_refs(node: &Node, refs: &[usize], lags: &[(DimId, i64)], broken: &[DimId], out: &mut Vec<Edge>) {
    let go = |n: &Node, out: &mut Vec<Edge>| collect_refs(n, refs, lags, broken, out);
    match node {
        Node::Ref(i) => out.push(Edge { target: refs[*i], lags: lags.to_vec(), broken: broken.to_vec() }),
        Node::Const(..) | Node::DimRef(_) | Node::MemberConst(..) => {}
        Node::Bin(_, l, r, _) | Node::Filter(l, r) | Node::On(l, r) | Node::Coalesce(l, r) => {
            go(l, out);
            go(r, out);
        }
        Node::If(c, t, e, _) => {
            go(c, out);
            go(t, out);
            if let Some(e) = e {
                go(e, out);
            }
        }
        Node::Not(c) | Node::IsBlank(c, _) | Node::IfBlank(c, _, _, _) | Node::Expand(c, _) | Node::AsAxis { child: c, .. } => go(c, out),
        Node::Shift { child, dim, n } => collect_refs(child, refs, &add_lag(lags, *dim, *n), broken, out),
        Node::Remove { child, dim, .. } | Node::Select { child, dim, .. } => {
            collect_refs(child, refs, lags, &with(broken, &[*dim]), out)
        }
        Node::ByAgg { child, src, dst, .. } | Node::ByLookup { child, src, dst, .. } => {
            collect_refs(child, refs, lags, &with(broken, &[*src, *dst]), out)
        }
        Node::By { .. } | Node::ByMetric { .. } => unreachable!("型を決めた式だけを使う"),
    }
}

/// 強連結成分を「依存先が先」の順で返す（Tarjan）。頂点は 0 から順に訪ねる。
fn tarjan(adj: &[Vec<usize>]) -> Vec<Vec<usize>> {
    struct State<'a> {
        adj: &'a [Vec<usize>],
        index: Vec<Option<usize>>,
        low: Vec<usize>,
        stack: Vec<usize>,
        on_stack: Vec<bool>,
        next: usize,
        out: Vec<Vec<usize>>,
    }
    fn visit(s: &mut State<'_>, v: usize) {
        s.index[v] = Some(s.next);
        s.low[v] = s.next;
        s.next += 1;
        s.stack.push(v);
        s.on_stack[v] = true;
        for &w in s.adj[v].iter() {
            if s.index[w].is_none() {
                visit(s, w);
                s.low[v] = s.low[v].min(s.low[w]);
            } else if s.on_stack[w] {
                s.low[v] = s.low[v].min(s.index[w].unwrap());
            }
        }
        if s.low[v] == s.index[v].unwrap() {
            let mut comp = Vec::new();
            loop {
                let w = s.stack.pop().unwrap();
                s.on_stack[w] = false;
                comp.push(w);
                if w == v {
                    break;
                }
            }
            s.out.push(comp);
        }
    }
    let n = adj.len();
    let mut s = State { adj, index: vec![None; n], low: vec![0; n], stack: Vec::new(), on_stack: vec![false; n], next: 0, out: Vec::new() };
    for v in 0..n {
        if s.index[v].is_none() {
            visit(&mut s, v);
        }
    }
    s.out
}

/// 名前順の Metric の名前（Python の参照実装の sorted(members)）。
fn sorted_names(members: &[usize], names: &[String]) -> Arg {
    let mut ns: Vec<&String> = members.iter().map(|&m| &names[m]).collect();
    ns.sort();
    Arg::List(ns.into_iter().map(Arg::from).collect())
}

/// 強連結成分 1 つを計算の段階にする。循環がなければ 1 Metric の段階、あれば scan の段階。
fn make_step(cat: &Catalog, scc: &[usize], edges: &[Vec<Edge>], names: &[String], dims: &[Vec<DimId>]) -> Result<(Vec<usize>, Option<DimId>), Diag> {
    let internal: Vec<(usize, &Edge)> = scc.iter().flat_map(|&src| edges[src].iter().filter(|e| scc.contains(&e.target)).map(move |e| (src, e))).collect();
    if internal.is_empty() {
        return Ok((vec![scc[0]], None));
    }
    let mut lag_dims: Vec<DimId> = Vec::new();
    for (_, e) in &internal {
        for &(d, n) in &e.lags {
            if n >= 1 && !lag_dims.contains(&d) {
                lag_dims.push(d);
            }
        }
    }
    if lag_dims.len() != 1 {
        return Err(Diag::new("cycle_no_lag").arg("members", sorted_names(scc, names)));
    }
    let dim = lag_dims[0];
    let mut same_time: Vec<Vec<usize>> = vec![Vec::new(); scc.len()]; // scc 内の位置 -> 同じ時点で読む相手（位置）
    let pos = |m: usize| scc.iter().position(|&x| x == m).unwrap();
    let dim_name = &cat.dims[dim].name;
    for (src, e) in &internal {
        if !dims[*src].contains(&dim) {
            return Err(Diag::new("cycle_scan_dim").arg("src", &names[*src]).arg("dim", dim_name));
        }
        if e.broken.contains(&dim) {
            return Err(Diag::new("cycle_broken").arg("src", &names[*src]).arg("target", &names[e.target]).arg("dim", dim_name));
        }
        if e.lag(dim) < 0 {
            return Err(Diag::new("cycle_future").arg("src", &names[*src]).arg("target", &names[e.target]));
        }
        if e.lag(dim) == 0 {
            let (s, t) = (pos(*src), pos(e.target));
            if !same_time[s].contains(&t) {
                same_time[s].push(t);
            }
        }
    }
    // 同じ時点どうしの依存（ずらし 0）は非循環でなければならない
    let order = tarjan(&same_time);
    if order.iter().any(|c| c.len() > 1 || same_time[c[0]].contains(&c[0])) {
        return Err(Diag::new("cycle_same_time").arg("members", sorted_names(scc, names)));
    }
    Ok((order.iter().map(|c| scc[c[0]]).collect(), Some(dim)))
}

/// 依存グラフの辺（Python に返す形）: Metric ごとの (参照先, 軸ごとのずらし, 壊れた軸)。
pub type Edges = Vec<Vec<(usize, Vec<(DimId, i64)>, Vec<DimId>)>>;

/// 計算計画を作る。formulas は Metric の番号順の式（入力は None）、names と dims はその名前と軸。
/// 返すのは、依存先が先の順の段階 (Metric の番号の列, scan の軸) と、互いに依存しない段階を段ごとに
/// まとめた段階の番号の列と、依存グラフの辺。
#[allow(clippy::type_complexity)]
pub fn plan(cat: &Catalog, formulas: &[Option<Formula>], names: &[String], dims: &[Vec<DimId>]) -> Result<(Vec<(Vec<usize>, Option<DimId>)>, Vec<Vec<usize>>, Edges), Diag> {
    let n = formulas.len();
    let mut edges: Vec<Vec<Edge>> = vec![Vec::new(); n];
    for (m, f) in formulas.iter().enumerate() {
        if let Some(f) = f {
            collect_refs(&f.node, &f.refs, &[], &[], &mut edges[m]);
        }
    }
    let adj: Vec<Vec<usize>> = edges.iter().map(|es| es.iter().map(|e| e.target).collect()).collect();
    let mut steps = Vec::new();
    for scc in tarjan(&adj) {
        steps.push(make_step(cat, &scc, &edges, names, dims)?);
    }
    // 依存の段。plan は依存先が先の順なので、参照先の段はすでに決まっている
    let mut level_of = vec![0usize; n];
    let mut levels: Vec<Vec<usize>> = Vec::new();
    for (i, (members, _)) in steps.iter().enumerate() {
        let mut level = 0;
        for &m in members {
            for e in &edges[m] {
                if !members.contains(&e.target) {
                    level = level.max(level_of[e.target] + 1);
                }
            }
        }
        for &m in members {
            level_of[m] = level;
        }
        while levels.len() <= level {
            levels.push(Vec::new());
        }
        levels[level].push(i);
    }
    let out_edges = edges.into_iter().map(|es| es.into_iter().map(|e| (e.target, e.lags, e.broken)).collect()).collect();
    Ok((steps, levels, out_edges))
}
