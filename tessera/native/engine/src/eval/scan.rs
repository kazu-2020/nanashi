//! A scan whose formula is a tree of +, -, *, /, IF and COALESCE over PREVIOUS(dim, n), for example
//! `IF(IsActual, Actual, PREVIOUS(Month) * 0.9 + PREVIOUS(Month, 2) * 0.1 + Margin)`.
//! The engine evaluates each operand one time for all periods, then runs the tree along the time dimension.
//! Each cell gets the same operations in the same order as one evaluation for each period, thus the values agree bit for bit.

#[allow(unused_imports)]
use crate::*;
use std::sync::atomic::{AtomicUsize, Ordering::Relaxed};

/// One node of the formula tree for one cell.
enum Step {
    /// The value of the Metric n periods before (n >= 1).
    Prev(u32),
    Operand(usize),
    Bin(Op, Box<Step>, Box<Step>),
    /// The condition is an operand (a lookup).
    If(usize, Box<Step>, Option<Box<Step>>),
    Coalesce(Box<Step>, Box<Step>),
}

/// How the tree reads an operand. This follows `eval_with`.
#[derive(Clone, Copy)]
enum Use {
    /// + and - and the branches of IF: the engine expands the operand to the dimensions of the Metric. It adds keys.
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
    let (mut operands, mut lags) = (Vec::new(), Vec::new());
    let root = step(node, dim, me, &mut operands, &mut lags)?;
    lags.sort_unstable();
    lags.dedup();
    Some(Tree { root, operands, lags })
}

fn step<'n>(node: &'n Node, dim: DimId, me: &dyn Fn(usize) -> bool, ops: &mut Vec<(&'n Node, Use)>, lags: &mut Vec<u32>) -> Option<Step> {
    let side = |n: &'n Node, u: Use, ops: &mut Vec<(&'n Node, Use)>, lags: &mut Vec<u32>| -> Option<Box<Step>> {
        if plain(n, me) {
            ops.push((n, u));
            Some(Box::new(Step::Operand(ops.len() - 1)))
        } else {
            step(n, dim, me, ops, lags).map(Box::new)
        }
    };
    match node {
        Node::Shift { child, dim: d, n } if *d == dim && *n >= 1 && matches!(**child, Node::Ref(i) if me(i)) => {
            let n = u32::try_from(*n).ok()?;
            lags.push(n);
            Some(Step::Prev(n))
        }
        Node::Bin(op @ (Op::Add | Op::Sub | Op::Mul | Op::Div), l, r, _) => {
            let u = if matches!(op, Op::Mul | Op::Div) { Use::Lookup } else { Use::Union };
            Some(Step::Bin(*op, side(l, u, ops, lags)?, side(r, u, ops, lags)?))
        }
        Node::Coalesce(l, r) => Some(Step::Coalesce(side(l, Use::Coalesce, ops, lags)?, side(r, Use::Coalesce, ops, lags)?)),
        Node::If(c, then, else_, _) if plain(c, me) => {
            ops.push((c, Use::Lookup));
            let c = ops.len() - 1;
            let then = side(then, Use::Union, ops, lags)?;
            let else_ = match else_ {
                Some(e) => Some(side(e, Use::Union, ops, lags)?),
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
    /// The cells in the dimensions of the Metric, in the order of `by_row`.
    ByRow(Vec<(u64, f64)>),
    /// A lookup with all dimensions of the Metric: the same as ByRow, but it adds no rows.
    Aligned(Vec<(u64, f64)>),
    /// The cells by their own key, and the projection from the key of the Metric.
    Lookup(FxHashMap<u64, f64>, Packing, Proj),
}

/// Rotate each key so that the time dimension is in the lowest bits, and sort the cells.
/// Then the cells of one row (a key without the time dimension) are contiguous and ordered by period.
fn by_row(cfg: &Config, mut cells: Vec<(u64, f64)>, shift: u32) -> Vec<(u64, f64)> {
    for c in &mut cells {
        c.0 = c.0.rotate_right(shift);
    }
    sort_cells(cfg, &mut cells);
    cells
}

/// The inputs of the tree for one cell.
struct At<'a> {
    t: usize,
    /// The key of the cell in the packing of the Metric.
    key: u64,
    operands: &'a [Operand],
    pack: &'a Packing,
    /// The values of each operand of this row, by period.
    vals: &'a [Vec<Option<f64>>],
    /// The values of the Metric in this row, by period: the results before t, or `before`.
    hist: &'a [Option<f64>],
}

impl At<'_> {
    fn operand(&self, i: usize) -> Option<f64> {
        match &self.operands[i] {
            Operand::ByRow(_) | Operand::Aligned(_) => self.vals[i][self.t],
            Operand::Lookup(cells, cp, proj) => cells.get(&proj.apply(self.pack, cp, self.key)).copied(),
        }
    }

    /// The value of the cell, with the semantics of `eval_with`.
    fn value(&self, s: &Step) -> Option<f64> {
        match s {
            Step::Prev(n) => self.hist[self.t.checked_sub(*n as usize)?],
            Step::Operand(i) => self.operand(*i),
            // A union: a blank side counts as 0
            Step::Bin(op @ (Op::Add | Op::Sub), l, r) => match (self.value(l), self.value(r)) {
                (None, None) => None,
                (a, b) => scalar(*op, a.unwrap_or(0.0), b.unwrap_or(0.0)),
            },
            // An intersection: a division by 0 is blank
            Step::Bin(op, l, r) => scalar(*op, self.value(l)?, self.value(r)?),
            // A blank condition goes to neither branch
            Step::If(c, then, else_) => {
                if self.operand(*c)? != 0.0 {
                    self.value(then)
                } else {
                    self.value(else_.as_deref()?)
                }
            }
            Step::Coalesce(l, r) => self.value(l).or_else(|| self.value(r)),
        }
    }
}

/// Calculate the scan for the periods `ts` (ascending) in the range r (r limits the time dimension to ts).
/// `before` has the values of the Metric at each period t - n (n in `tree.lags`) that is not in ts. The result is in the packing `pack`.
/// A row reads only its own earlier periods, thus the engine calculates the rows in parallel.
/// The scan stops with a budget error when `fits` is false for the count of the cells that it made.
#[allow(clippy::too_many_arguments)]
pub(crate) fn tree_scan(tree: &Tree, before: Cube, pack: &Packing, dim: DimId, ts: &[u32], cat: &Catalog, src: &[Src], r: &Restrict, b: &Budget, fits: &(dyn Fn(usize) -> bool + Sync)) -> Result<Cube> {
    let cfg = &cat.cfg;
    let p = pack.pos(dim).expect("scan の軸");
    let size = cat.dims[dim].size as usize;
    let (shift, tmask) = (pack.shifts[p], pack.masks[p]);
    let mut operands = Vec::with_capacity(tree.operands.len());
    const WHAT: &str = "scan の作業領域";
    b.take(before.cells.capacity(), WHAT)?;
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
            Use::Lookup if same_set(c.dims(), &pack.dims) => {
                if &c.pack != pack {
                    b.take(c.cells.len(), WHAT)?;
                }
                Operand::Aligned(by_row(cfg, c.into_repacked(cfg, pack).cells, shift))
            }
            Use::Lookup => {
                let proj = Proj::new(pack, &c.pack);
                b.take(table_cells(c.cells.len(), size_of::<(u64, f64)>()), WHAT)?;
                Operand::Lookup(c.cells.into_iter().collect(), c.pack, proj)
            }
            Use::Union | Use::Coalesce => {
                let e = expand(c, &pack.dims, cat, r, b)?;
                if &e.pack != pack {
                    b.take(e.cells.len(), WHAT)?;
                }
                Operand::ByRow(by_row(cfg, e.into_repacked(cfg, pack).cells, shift))
            }
        });
    }
    debug_assert!(&before.pack == pack);
    let before = by_row(cfg, before.cells, shift);
    let row = |k: u64| k & !tmask;
    // A cell gets a value only from an operand that adds keys or from an earlier value of its row. These are all such rows
    let mut rows = Vec::new();
    let adds = operands.iter().filter_map(|o| match o {
        Operand::ByRow(c) => Some(&c[..]),
        _ => None,
    });
    for c in adds.chain([&before[..]]).flatten() {
        if rows.last() != Some(&row(c.0)) {
            rows.push(row(c.0));
        }
    }
    rows.sort_unstable();
    rows.dedup();
    // A row key takes half a cell
    b.take(rows.capacity().div_ceil(2), WHAT)?;
    // The cells of each operand by row, and `before` as the last. A Lookup operand has no cells here and no buffer
    let mut srcs: Vec<Option<&[(u64, f64)]>> = operands
        .iter()
        .map(|o| match o {
            Operand::ByRow(c) | Operand::Aligned(c) => Some(&c[..]),
            Operand::Lookup(..) => None,
        })
        .collect();
    srcs.push(Some(&before));
    let parts = if cfg.par(srcs.iter().flatten().map(|s| s.len()).sum()) { rayon::current_num_threads() * 4 } else { 1 };
    // Each chunk that runs at the same time has a buffer of `size` slots for each source
    b.take(srcs.iter().flatten().count() * size * parts.min(rayon::current_num_threads()), WHAT)?;
    // A Budget is not Sync. The chunks count the capacity of their results here, and a chunk stops when it does not fit
    let room = b.room();
    let (made, pushed) = (AtomicUsize::new(0), AtomicUsize::new(0));
    let run = |chunk: &[u64]| -> Option<Vec<(u64, f64)>> {
        let mut pos: Vec<usize> = srcs.iter().map(|s| s.map_or(0, |s| s.partition_point(|c| c.0 < chunk[0]))).collect();
        // The cells of the last row of each source. Only their slots get a reset
        let mut last: Vec<usize> = pos.clone();
        let mut bufs: Vec<Vec<Option<f64>>> = srcs.iter().map(|s| vec![None; if s.is_some() { size } else { 0 }]).collect();
        let (mut out, mut told) = (Vec::new(), 0);
        for &w in chunk {
            for (((s, i), l), buf) in srcs.iter().zip(&mut pos).zip(&mut last).zip(&mut bufs) {
                let Some(s) = s else { continue };
                for c in &s[*l..*i] {
                    buf[(c.0 & tmask) as usize] = None;
                }
                // An Aligned operand can have rows that are not in `rows`
                while s.get(*i).is_some_and(|c| row(c.0) < w) {
                    *i += 1;
                }
                *l = *i;
                while let Some(&(k, v)) = s.get(*i).filter(|c| row(c.0) == w) {
                    buf[(k & tmask) as usize] = Some(v);
                    *i += 1;
                }
            }
            // A period in ts reads only earlier periods, and each of them in ts already has its new result here
            let (hist, vals) = bufs.split_last_mut().expect("srcs has before");
            let base = w.rotate_left(shift);
            if out.capacity() - out.len() < ts.len() {
                // Count the old buffer too: it lives while the realloc copies it
                let (old, grow) = (out.capacity(), out.capacity().max(ts.len()));
                let cells = pushed.fetch_add(out.len() - told, Relaxed) + out.len() - told;
                told = out.len();
                if made.fetch_add(old + grow, Relaxed) + old + grow > room || !fits(cells) {
                    return None;
                }
                // Reserve up to old + grow so the capacity equals the charged count
                out.reserve_exact(old + grow - out.len());
                made.fetch_sub(old, Relaxed);
            }
            for &t in ts {
                let key = base | pack.put(p, t);
                let v = At { t: t as usize, key, operands: &operands, pack, vals, hist }.value(&tree.root);
                hist[t as usize] = v;
                if let Some(v) = v {
                    out.push((key, v));
                }
            }
        }
        Some(out)
    };
    let outs: Option<Vec<Vec<(u64, f64)>>> = rows.par_chunks(rows.len().div_ceil(parts).max(1)).map(run).collect();
    let Some(mut outs) = outs else {
        b.need(room + 1, "scan の結果")?;
        unreachable!("room + 1 cells never fit");
    };
    let cells = if outs.len() == 1 {
        // The capacity can be larger than the length. Free the unused capacity: `replace` copies the result, and both copies exist at the same time
        let mut cells = outs.pop().expect("one chunk");
        cells.shrink_to_fit();
        cells
    } else {
        let n: usize = outs.iter().map(Vec::len).sum();
        b.need(made.load(Relaxed) + n, "scan の結果")?;
        outs.concat()
    };
    Ok(Cube { pack: pack.clone(), kind: Kind::Num, cells })
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
