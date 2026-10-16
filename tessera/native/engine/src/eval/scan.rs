//! A scan whose formula is a chain of +, -, *, / and COALESCE around PREVIOUS, for example `PREVIOUS(Month) * 0.9 + Margin`.
//! The engine evaluates each operand one time for all periods, then runs the chain along the time dimension.
//! Each cell gets the same operations in the same order as one evaluation for each period, thus the values agree bit for bit.

#[allow(unused_imports)]
use crate::*;

/// One operation on the path from PREVIOUS to the root: `acc op operand`, or `operand op acc` if `acc_left` is false.
/// `op` None is COALESCE (an override of an overridable Metric).
pub(crate) struct Link<'n> {
    op: Option<Op>,
    acc_left: bool,
    operand: &'n Node,
}

/// The links from `PREVIOUS(dim)` of the Metric to the root, or None if the formula has a different shape.
/// `me(i)` is true if the Ref number i reads the Metric itself.
pub(crate) fn chain<'n>(node: &'n Node, dim: DimId, me: &dyn Fn(usize) -> bool) -> Option<Vec<Link<'n>>> {
    match node {
        Node::Shift { child, dim: d, n: 1 } if *d == dim && matches!(**child, Node::Ref(i) if me(i)) => Some(Vec::new()),
        Node::Bin(Op::Add | Op::Sub | Op::Mul | Op::Div, l, r, _) | Node::Coalesce(l, r) => {
            let op = match node {
                Node::Bin(op, ..) => Some(*op),
                _ => None,
            };
            let (spine, operand, acc_left) = if plain(r, me) {
                (l, r, true)
            } else if plain(l, me) {
                (r, l, false)
            } else {
                return None;
            };
            let mut links = chain(spine, dim, me)?;
            links.push(Link { op, acc_left, operand });
            Some(links)
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

/// An operand of a link, evaluated for all periods.
enum Operand {
    /// For +, - and COALESCE: the cells of each period (by the key without the time dimension), in the dimensions of the Metric.
    ByPeriod(Vec<FxHashMap<u64, f64>>),
    /// For * and /: the cells by their own key, and the projection from the key of the Metric.
    Lookup(FxHashMap<u64, f64>, Packing, Proj),
}

/// A union of two values: + and - count a blank side as 0, COALESCE takes the left value if it is not blank.
fn outer(op: Option<Op>, l: Option<f64>, r: Option<f64>) -> Option<f64> {
    match op {
        Some(op) => scalar(op, l.unwrap_or(0.0), r.unwrap_or(0.0)),
        None => l.or(r),
    }
}

/// Calculate the scan for the periods `ts` (ascending) in the range r (r limits the time dimension to ts).
/// `before` has the values of the Metric at each period t - 1 that is not in ts. The result is in the packing `pack`.
#[allow(clippy::too_many_arguments)]
pub(crate) fn chain_scan(links: &[Link], before: &Cube, pack: &Packing, dim: DimId, ts: &[u32], cat: &Catalog, src: &[Src], r: &Restrict, b: &Budget) -> Result<Cube> {
    use Operand::*;
    let cfg = &cat.cfg;
    let p = pack.pos(dim).expect("scan の軸");
    let size = cat.dims[dim].size as usize;
    const ENTRY: usize = size_of::<(u64, f64)>();
    let by_period = |cells: &[(u64, f64)]| -> Result<Vec<FxHashMap<u64, f64>>> {
        b.take((size * (size_of::<usize>() + size_of::<FxHashMap<u64, f64>>())).div_ceil(CELL_BYTES), "scan の加数")?;
        let mut count = vec![0usize; size];
        for &(k, _) in cells {
            count[pack.get(k, p) as usize] += 1;
        }
        b.take(count.iter().map(|&n| table_cells(n, ENTRY)).sum(), "scan の加数")?;
        let mut out: Vec<FxHashMap<u64, f64>> = count.iter().map(|&n| FxHashMap::with_capacity_and_hasher(n, Default::default())).collect();
        for &(k, v) in cells {
            out[pack.get(k, p) as usize].insert(pack.clear(k, p), v);
        }
        Ok(out)
    };
    let mut operands = Vec::with_capacity(links.len());
    b.take(before.cells.capacity(), "scan の加数")?;
    for l in links {
        let c = eval_with(l.operand, cat, src, r, b)?;
        // COALESCE does not expand its operand, thus it needs all dimensions of the Metric
        let fits = match l.op {
            None => same_set(c.dims(), &pack.dims),
            Some(_) => c.dims().iter().all(|d| pack.dims.contains(d)),
        };
        if !fits {
            return Err("scan の式の軸が Metric の軸と一致しない".into());
        }
        operands.push(match l.op {
            Some(Op::Mul | Op::Div) => {
                let proj = Proj::new(pack, &c.pack);
                b.take(table_cells(c.cells.len(), ENTRY), "scan の加数")?;
                Lookup(c.cells.into_iter().collect(), c.pack, proj)
            }
            _ => {
                let e = expand(c, &pack.dims, cat, r, b)?;
                if &e.pack != pack {
                    b.take(e.cells.len(), "scan の加数")?;
                }
                ByPeriod(by_period(&e.into_repacked(cfg, pack).cells)?)
            }
        });
    }
    let mut before = if &before.pack == pack {
        by_period(&before.cells)?
    } else {
        b.take(before.cells.len(), "scan の加数")?;
        by_period(&before.repack(cfg, pack).cells)?
    };
    let mut acc: FxHashMap<u64, f64> = FxHashMap::default();
    let mut out = Vec::new();
    let mut last = None;
    for &t in ts {
        if t == 0 {
            acc.clear();
        } else if last != Some(t - 1) {
            acc = std::mem::take(&mut before[t as usize - 1]);
        }
        last = Some(t);
        // The accumulator gets at most the operand cells at t. Grow it before the merge, while the old and the new table both live.
        let new: usize = operands.iter().map(|o| if let ByPeriod(cells) = o { cells[t as usize].len() } else { 0 }).sum();
        let grown = if acc.len() + new > acc.capacity() { table_cells(acc.len() + new, ENTRY) } else { 0 };
        b.need(out.len() + acc.len() + new + table_cells(acc.capacity(), ENTRY) + grown, "scan の結果")?;
        acc.reserve(new);
        for (l, operand) in links.iter().zip(&operands) {
            match operand {
                ByPeriod(cells) => {
                    let f = |a: Option<f64>, x: Option<f64>| {
                        let v = if l.acc_left { outer(l.op, a, x) } else { outer(l.op, x, a) };
                        v.expect("一方に値があれば和集合は値を返す")
                    };
                    let cells = &cells[t as usize];
                    for (k, a) in acc.iter_mut() {
                        *a = f(Some(*a), cells.get(k).copied());
                    }
                    for (&k, &x) in cells {
                        acc.entry(k).or_insert_with(|| f(None, Some(x)));
                    }
                }
                // An intersection: a cell stays only if the operand has a value, and a division by 0 is blank
                Lookup(cells, cp, proj) => acc.retain(|&k, a| {
                    let op = l.op.expect("* と /");
                    let f = |x: f64| if l.acc_left { scalar(op, *a, x) } else { scalar(op, x, *a) };
                    match cells.get(&proj.apply(pack, cp, k | pack.put(p, t))).and_then(|&x| f(x)) {
                        Some(v) => {
                            *a = v;
                            true
                        }
                        None => false,
                    }
                }),
            }
        }
        // Grow out to the length that need() counted, not to the next power of two
        out.reserve_exact(acc.len());
        out.extend(acc.iter().map(|(&k, &v)| (k | pack.put(p, t), v)));
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
    fn chain_finds_only_the_supported_shapes() {
        let me = |i: usize| i == 0;
        let affine = bin(Op::Add, Box::new(bin(Op::Mul, prev(1), Node::Const(0.9, Kind::Num))), Node::Ref(1));
        assert_eq!(chain(&affine, 0, &me).map(|l| l.len()), Some(2));
        assert!(chain(&bin(Op::Add, prev(2), Node::Ref(1)), 0, &me).is_none(), "PREVIOUS(dim, 2)");
        let agg = Node::Remove { child: Box::new(Node::Ref(1)), dim: 1, agg: Agg::Sum };
        assert!(chain(&bin(Op::Add, prev(1), agg), 0, &me).is_none(), "an aggregation in an operand");
    }
}
