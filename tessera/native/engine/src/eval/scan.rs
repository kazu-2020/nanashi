//! A scan whose formula is a tree of +, -, *, /, IF and COALESCE over PREVIOUS(dim, n), for example
//! `IF(IsActual, Actual, PREVIOUS(Month) * 0.9 + PREVIOUS(Month, 2) * 0.1 + Margin)`.
//! The engine evaluates each operand one time for all periods, then runs the tree along the time dimension.
//! Each cell gets the same operations in the same order as one evaluation for each period, thus the values agree bit for bit.

#[allow(unused_imports)]
use crate::*;

/// One node of the formula tree for one cell.
enum Step {
    /// The value of the Metric n periods before (n >= 1).
    Prev(u32),
    /// An index into the operands.
    Operand(usize),
    Bin(Op, Box<Step>, Box<Step>),
    /// The condition is an operand (a lookup).
    If(usize, Box<Step>, Option<Box<Step>>),
    Coalesce(Box<Step>, Box<Step>),
}

/// How the tree reads an operand. This follows `eval_with`.
#[derive(Clone, Copy)]
enum Use {
    /// + and - and the branches of IF: the operand is expanded to the dimensions of the Metric and adds keys.
    Union,
    /// COALESCE: the same as Union, but `eval_with` does not expand the operand, thus it needs all dimensions of the Metric.
    Coalesce,
    /// * and / and the condition of IF: an intersection through a projection. It adds no keys.
    Lookup,
}

/// The formula of a scan Metric as a tree over its operands.
pub(crate) struct Tree<'n> {
    root: Step,
    operands: Vec<(&'n Node, Use)>,
    /// The n of each PREVIOUS(dim, n), ascending, without duplicates.
    pub(crate) lags: Vec<u32>,
}

/// The tree of the formula, or None if the formula has a different shape.
/// `me(i)` is true if the Ref number i reads the Metric itself.
pub(crate) fn tree<'n>(node: &'n Node, dim: DimId, me: &dyn Fn(usize) -> bool) -> Option<Tree<'n>> {
    if plain(node, me) {
        return None;
    }
    let mut t = Tree { root: Step::Prev(0), operands: Vec::new(), lags: Vec::new() };
    t.root = step(node, dim, me, &mut t)?;
    t.lags.sort_unstable();
    t.lags.dedup();
    Some(t)
}

fn step<'n>(node: &'n Node, dim: DimId, me: &dyn Fn(usize) -> bool, t: &mut Tree<'n>) -> Option<Step> {
    let side = |n: &'n Node, u: Use, t: &mut Tree<'n>| -> Option<Box<Step>> {
        if plain(n, me) {
            t.operands.push((n, u));
            Some(Box::new(Step::Operand(t.operands.len() - 1)))
        } else {
            step(n, dim, me, t).map(Box::new)
        }
    };
    match node {
        Node::Shift { child, dim: d, n } if *d == dim && *n >= 1 && matches!(**child, Node::Ref(i) if me(i)) => {
            let n = u32::try_from(*n).ok()?;
            t.lags.push(n);
            Some(Step::Prev(n))
        }
        Node::Bin(op @ (Op::Add | Op::Sub | Op::Mul | Op::Div), l, r, _) => {
            let u = if matches!(op, Op::Mul | Op::Div) { Use::Lookup } else { Use::Union };
            Some(Step::Bin(*op, side(l, u, t)?, side(r, u, t)?))
        }
        Node::Coalesce(l, r) => Some(Step::Coalesce(side(l, Use::Coalesce, t)?, side(r, Use::Coalesce, t)?)),
        Node::If(c, then, else_, _) if plain(c, me) => {
            t.operands.push((c, Use::Lookup));
            let c = t.operands.len() - 1;
            let then = side(then, Use::Union, t)?;
            let else_ = match else_ {
                Some(e) => Some(side(e, Use::Union, t)?),
                None => None,
            };
            Some(Step::If(c, then, else_))
        }
        _ => None,
    }
}

/// True if the expression does not read the Metric itself and has no aggregation.
fn plain(node: &Node, me: &dyn Fn(usize) -> bool) -> bool {
    let p = |n: &Node| plain(n, me);
    match node {
        Node::Ref(i) => !me(*i),
        Node::Const(..) | Node::DimRef(_) | Node::MemberConst(..) => true,
        Node::Bin(_, l, r, _) | Node::Filter(l, r) | Node::On(l, r) | Node::Coalesce(l, r) => p(l) && p(r),
        Node::If(c, t, e, _) => p(c) && p(t) && e.as_deref().is_none_or(p),
        Node::Not(c) | Node::Expand(c, _) | Node::IsBlank(c, _) | Node::IfBlank(c, ..) => p(c),
        Node::ByLookup { child, .. } | Node::Shift { child, .. } | Node::Select { child, .. } | Node::AsAxis { child, .. } => p(child),
        // shortcut: a parallel aggregation over all periods can add in a different order than over one period.
        // If xsum makes aggregations exact, permit them here.
        Node::ByAgg { .. } | Node::Remove { .. } | Node::By { .. } | Node::ByMetric { .. } => false,
    }
}

/// An operand, evaluated for all periods.
enum Operand {
    /// The cells of each period (by the key without the time dimension), in the dimensions of the Metric.
    ByPeriod(Vec<FxHashMap<u64, f64>>),
    /// The cells by their own key, and the projection from the key of the Metric.
    Lookup(FxHashMap<u64, f64>, Packing, Proj),
}

/// The inputs of the tree for one period t.
struct At<'a> {
    t: u32,
    operands: &'a [Operand],
    pack: &'a Packing,
    p: usize,
    /// The results of the Metric at t - n (index n), empty if t - n < 0.
    prev: &'a [Option<&'a FxHashMap<u64, f64>>],
}

impl At<'_> {
    fn operand(&self, i: usize, k: u64) -> Option<f64> {
        match &self.operands[i] {
            Operand::ByPeriod(cells) => cells[self.t as usize].get(&k).copied(),
            Operand::Lookup(cells, cp, proj) => cells.get(&proj.apply(self.pack, cp, k | self.pack.put(self.p, self.t))).copied(),
        }
    }

    /// The value of the cell k, with the semantics of `eval_with`.
    fn value(&self, s: &Step, k: u64) -> Option<f64> {
        match s {
            Step::Prev(n) => self.prev[*n as usize]?.get(&k).copied(),
            Step::Operand(i) => self.operand(*i, k),
            // A union: a blank side counts as 0
            Step::Bin(op @ (Op::Add | Op::Sub), l, r) => match (self.value(l, k), self.value(r, k)) {
                (None, None) => None,
                (a, b) => scalar(*op, a.unwrap_or(0.0), b.unwrap_or(0.0)),
            },
            // An intersection: a division by 0 is blank
            Step::Bin(op, l, r) => scalar(*op, self.value(l, k)?, self.value(r, k)?),
            // A blank condition goes to neither branch
            Step::If(c, then, else_) => {
                if self.operand(*c, k)? != 0.0 {
                    self.value(then, k)
                } else {
                    self.value(else_.as_deref()?, k)
                }
            }
            Step::Coalesce(l, r) => self.value(l, k).or_else(|| self.value(r, k)),
        }
    }
}

/// Calculate the scan for the periods `ts` (ascending) in the range r (r limits the time dimension to ts).
/// `before` has the values of the Metric at each period t - n (n in `tree.lags`) that is not in ts. The result is in the packing `pack`.
#[allow(clippy::too_many_arguments)]
pub(crate) fn tree_scan(tree: &Tree, before: &Cube, pack: &Packing, dim: DimId, ts: &[u32], cat: &Catalog, src: &[Src], r: &Restrict, b: &Budget) -> Result<Cube> {
    let cfg = &cat.cfg;
    let p = pack.pos(dim).expect("scan の軸");
    let size = cat.dims[dim].size as usize;
    let by_period = |cells: &[(u64, f64)]| {
        let mut out: Vec<FxHashMap<u64, f64>> = vec![FxHashMap::default(); size];
        for &(k, v) in cells {
            out[pack.get(k, p) as usize].insert(pack.clear(k, p), v);
        }
        out
    };
    let mut operands = Vec::with_capacity(tree.operands.len());
    // eval_with already counts each operand. The budget must also count the expanded operands, `before` and the kept results
    let mut held = before.cells.len();
    for &(node, u) in &tree.operands {
        let c = eval_with(node, cat, src, r, b)?;
        let fits = match u {
            Use::Coalesce => same_set(c.dims(), &pack.dims),
            Use::Union | Use::Lookup => c.dims().iter().all(|d| pack.dims.contains(d)),
        };
        if !fits {
            return Err("scan の式の軸が Metric の軸と一致しない".into());
        }
        operands.push(match u {
            Use::Lookup => {
                let proj = Proj::new(pack, &c.pack);
                Operand::Lookup(c.cells.into_iter().collect(), c.pack, proj)
            }
            Use::Union | Use::Coalesce => {
                let e = expand(&c, &pack.dims, cat, r, b)?.into_repacked(cfg, pack);
                held += e.cells.len();
                b.need(held, "scan の加数")?;
                Operand::ByPeriod(by_period(&e.cells))
            }
        });
    }
    let before = by_period(&before.repack(cfg, pack).cells);
    let max_lag = *tree.lags.last().expect("a tree has PREVIOUS") as usize;
    // The results of the last max_lag periods in ts. PREVIOUS reads them, and they leave when no later period can read them
    let mut done: Vec<FxHashMap<u64, f64>> = vec![FxHashMap::default(); size];
    let mut kept = 0;
    let mut out = Vec::new();
    let mut seen = FxHashSet::default();
    for (i, &t) in ts.iter().enumerate() {
        let in_ts = |s: u32| ts[..i].binary_search(&s).is_ok();
        let prev: Vec<Option<&FxHashMap<u64, f64>>> = (0..=max_lag as u32)
            .map(|n| t.checked_sub(n).filter(|_| n > 0).map(|s| if in_ts(s) { &done[s as usize] } else { &before[s as usize] }))
            .collect();
        let at = At { t, operands: &operands, pack, p, prev: &prev };
        // A cell can have a value only if an operand that adds keys or a PREVIOUS has a value there
        let mut keys: Vec<&FxHashMap<u64, f64>> = tree.lags.iter().filter_map(|&n| prev[n as usize]).collect();
        keys.extend(operands.iter().filter_map(|o| match o {
            Operand::ByPeriod(cells) => Some(&cells[t as usize]),
            Operand::Lookup(..) => None,
        }));
        let mut cur = FxHashMap::default();
        seen.clear();
        for &k in keys.iter().flat_map(|m| m.keys()) {
            if seen.insert(k) {
                if let Some(v) = at.value(&tree.root, k) {
                    cur.insert(k, v);
                }
            }
        }
        b.need(held + kept + out.len() + cur.len(), "scan の結果")?;
        out.extend(cur.iter().map(|(&k, &v)| (k | pack.put(p, t), v)));
        kept += cur.len();
        done[t as usize] = cur;
        if let Some(old) = (t as usize).checked_sub(max_lag) {
            kept -= std::mem::take(&mut done[old]).len();
        }
    }
    Ok(Cube { pack: pack.clone(), kind: Kind::Num, cells: out })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn prev(n: i64) -> Box<Node> {
        Box::new(Node::Shift { child: Box::new(Node::Ref(0)), dim: 0, n })
    }

    fn bin(op: Op, l: Box<Node>, r: Node) -> Node {
        Node::Bin(op, l, Box::new(r), vec![])
    }

    #[test]
    fn tree_finds_only_the_supported_shapes() {
        let me = |i: usize| i == 0;
        let ok = |n: &Node| tree(n, 0, &me).is_some();
        let affine = bin(Op::Add, Box::new(bin(Op::Mul, prev(1), Node::Const(0.9, Kind::Num))), Node::Ref(1));
        assert_eq!(tree(&affine, 0, &me).map(|t| t.operands.len()), Some(2));
        assert!(ok(&bin(Op::Add, prev(2), Node::Ref(1))), "PREVIOUS(dim, 2)");
        let sum = bin(Op::Add, Box::new(bin(Op::Add, prev(1), *prev(2))), *prev(3));
        assert_eq!(tree(&sum, 0, &me).map(|t| t.lags), Some(vec![1, 2, 3]));
        let turnover = Node::If(Box::new(Node::Ref(1)), Box::new(Node::Ref(2)), Some(Box::new(bin(Op::Mul, Box::new(Node::Ref(3)), sum))), vec![]);
        assert!(ok(&turnover), "IF at the root with a plain condition");
        let agg = Node::Remove { child: Box::new(Node::Ref(1)), dim: 1, agg: Agg::Sum };
        assert!(!ok(&bin(Op::Add, prev(1), agg)), "an aggregation in an operand");
        assert!(!ok(&bin(Op::Add, prev(1), Node::Not(prev(1)))), "PREVIOUS under NOT");
        assert!(!ok(&bin(Op::Add, prev(0), Node::Ref(1))), "PREVIOUS(dim, 0)");
        let other = Node::Shift { child: Box::new(Node::Ref(0)), dim: 1, n: 1 };
        assert!(!ok(&bin(Op::Add, Box::new(other), Node::Ref(1))), "PREVIOUS of another dimension");
        let cond = Node::If(Box::new(bin(Op::Gt, prev(1), Node::Const(0.0, Kind::Num))), Box::new(Node::Ref(1)), None, vec![]);
        assert!(!ok(&cond), "PREVIOUS in the condition of IF");
    }
}
