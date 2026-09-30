//! 式の評価。意味は Python の参照実装（evaluate.py）と同じ。値は f64 で、真偽値は 1.0 / 0.0、空のセルはキーがないことで表す。

#[allow(unused_imports)]
use crate::*;

mod agg;
mod join;
pub(crate) use agg::*;
pub(crate) use join::*;

#[inline]
pub(crate) fn b2f(x: bool) -> f64 {
    if x {
        1.0
    } else {
        0.0
    }
}

/// 同じ詰め方で差分のない格納データどうしの INNER JOIN（A * B、A > B など）。どちらの本体も
/// Cube に写さずに、キーの順に突き合わせる。当てはまらなければ None。
fn join_stores(op: Op, l: &Node, rt: &Node, src: &[Src], r: &Restrict) -> Option<Cube> {
    let kind = match op {
        Op::Mul | Op::Div => Kind::Num,
        Op::Eq | Op::Ne | Op::Lt | Op::Le | Op::Gt | Op::Ge => Kind::Bool,
        _ => return None,
    };
    let (Node::Ref(i), Node::Ref(j)) = (l, rt) else { return None };
    let (Src::Store(a), Src::Store(b)) = (&src[*i], &src[*j]) else { return None };
    if !r.is_all() || a.pack != b.pack || a.base_rows().is_none() || b.base_rows().is_none() {
        return None;
    }
    let cells = merge_sorted(&**a, &**b, false, |x, y| scalar(op, x.unwrap(), y.unwrap()));
    Some(Cube { pack: a.pack.clone(), kind, cells })
}

pub(crate) fn scalar(op: Op, a: f64, b: f64) -> Option<f64> {
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
pub(crate) fn kleene(op: Op, a: Option<f64>, b: Option<f64>) -> Option<f64> {
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

/// 式を評価する。途中結果が予算（cat.cfg.max_bytes）を超えるなら Err。
pub fn eval(node: &Node, cat: &Catalog, src: &[Src], r: &Restrict) -> Result<Cube> {
    eval_with(node, cat, src, r, &Budget::new(cat.cfg.max_bytes))
}

/// 予算 b のもとで式を評価する（並列に評価するときは、式ごとに予算を分けて渡す）。
pub fn eval_with(node: &Node, cat: &Catalog, src: &[Src], r: &Restrict, b: &Budget) -> Result<Cube> {
    let mark = b.mark();
    let out = node_value(node, cat, src, r, b)?;
    b.settle(mark, &out)?;
    Ok(out)
}

fn node_value(node: &Node, cat: &Catalog, src: &[Src], r: &Restrict, bud: &Budget) -> Result<Cube> {
    let cfg = &cat.cfg;
    let ev = |n: &Node| eval_with(n, cat, src, r, bud);
    match node {
        Node::Ref(i) => Ok(src[*i].read(cfg, r)),

        Node::DimRef(d) => {
            let pack = Packing::new(&[*d], cat)?;
            let cells = members(cat, *d, r).into_iter().map(|m| (pack.put(0, m), m as f64)).collect();
            Ok(Cube { pack, kind: Kind::Num, cells })
        }

        Node::Const(v, kind) => Ok(Cube { pack: Packing::new(&[], cat)?, kind: *kind, cells: vec![(0, *v)] }),
        Node::MemberConst(_, m) => Ok(Cube { pack: Packing::new(&[], cat)?, kind: Kind::Num, cells: vec![(0, *m as f64)] }),
        Node::By { .. } | Node::ByMetric { .. } => Err("型を決めていない式は評価できない".into()),

        Node::Bin(op, l, rt, _) => {
            if let Some(c) = join_stores(*op, l, rt, src, r) {
                return Ok(c);
            }
            let a = ev(l)?;
            let b = match op {
                // INNER JOIN の演算は、左が小さければ右を左のメンバーに絞って評価する
                Op::Mul | Op::Div | Op::Eq | Op::Ne | Op::Lt | Op::Le | Op::Gt | Op::Ge => match semi(r, &a, cat) {
                    Some(r2) => eval_with(rt, cat, src, &r2, bud)?,
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
                    let (a, b) = (expand(&a, &dims, cat, r, bud)?, expand(&b, &dims, cat, r, bud)?);
                    let op = *op;
                    Ok(union(cfg, a, b, Kind::Num, move |x, y| scalar(op, x.unwrap_or(0.0), y.unwrap_or(0.0))))
                }
                Op::And | Op::Or => {
                    let dims = merge(a.dims(), b.dims());
                    let (a, b) = (expand(&a, &dims, cat, r, bud)?, expand(&b, &dims, cat, r, bud)?);
                    let op = *op;
                    Ok(union(cfg, a, b, Kind::Bool, move |x, y| kleene(op, x, y)))
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
                cells: map_cells(cfg, &c.cells, |k, v| ((v != 0.0) == want).then_some((k, v))),
            };
            // 各分岐は、条件がその値になるセルのメンバーに絞って評価する
            let branch = |n: &Node, part: &Cube| match semi(r, part, cat) {
                Some(r2) => eval_with(n, cat, src, &r2, bud),
                None => eval_with(n, cat, src, r, bud),
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
                out.cells.extend(expand(p, &dims, cat, r, bud)?.cells);
            }
            Ok(out)
        }

        Node::Filter(child, cond) => {
            let x = ev(child)?;
            let c = match semi(r, &x, cat) {
                Some(r2) => eval_with(cond, cat, src, &r2, bud)?,
                None => ev(cond)?,
            };
            let keep = Cube { pack: c.pack.clone(), kind: Kind::Bool, cells: map_cells(cfg, &c.cells, |k, v| (v != 0.0).then_some((k, v))) };
            intersect(&x, &keep, x.kind, cat, |a, _| Some(a))
        }

        Node::Coalesce(first, second) => {
            let s = ev(second)?;
            let f = ev(first)?.repack(cfg, &s.pack);
            let kind = s.kind;
            Ok(union(cfg, f, s, kind, |a, b| a.or(b)))
        }

        Node::On(child, other) => {
            let x = ev(child)?;
            let o = match semi(r, &x, cat) {
                Some(r2) => eval_with(other, cat, src, &r2, bud)?,
                None => ev(other)?,
            };
            intersect(&x, &o, x.kind, cat, |a, _| Some(a))
        }

        Node::Expand(child, dims) => {
            let c = ev(child)?;
            expand(&c, &[c.dims(), &dims[..]].concat(), cat, r, bud)
        }

        Node::IsBlank(child, _) => {
            let c = ev(child)?;
            let present: FxHashSet<u64> = c.cells.iter().map(|c| c.0).collect();
            let cells = dense(&c, cat, r, bud)?.into_iter().map(|k| (k, b2f(!present.contains(&k)))).collect();
            Ok(Cube { pack: c.pack, kind: Kind::Bool, cells })
        }

        Node::IfBlank(child, value, _, _) => {
            let c = ev(child)?;
            let present: FxHashMap<u64, f64> = c.cells.iter().copied().collect();
            let cells = dense(&c, cat, r, bud)?.into_iter().map(|k| (k, *present.get(&k).unwrap_or(value))).collect();
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
            let rows = rows_of(child, cat, src, &sub, bud)?;
            let dims = replace_dim(&rows.pack().dims, *s, *dst);
            let out = Packing::new(&dims, cat)?;
            if few_groups(cfg, *agg, combos_in(&dims, cat, r), rows.len()) {
                let xp = rows.pack();
                let (ps, pd) = (xp.pos(*s).unwrap(), out.pos(*dst).unwrap());
                let rest = Proj::new(xp, &out);
                let groups = fold_groups(cfg, &rows, |k| {
                    let t = mp.fwd[xp.get(k, ps) as usize];
                    (t >= 0).then(|| rest.apply(xp, &out, k) | out.put(pd, t as u32))
                });
                return Ok(finish_groups(groups, out, *agg));
            }
            let c = rows.into_cube();
            let (ps, pd) = (c.pack.pos(*s).unwrap(), out.pos(*dst).unwrap());
            let rest = Proj::new(&c.pack, &out);
            let pairs = map_cells(cfg, &c.cells, |k, v| {
                let t = mp.fwd[c.pack.get(k, ps) as usize];
                (t >= 0).then(|| (rest.apply(&c.pack, &out, k) | out.put(pd, t as u32), v))
            });
            Ok(group(cfg, pairs, out, *agg))
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
            let c = eval_with(child, cat, src, &sub, bud)?;
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
            let sub = r.without(&[*dim]);
            // Metric を使った BY の集約（Remove(On(x, AsAxis(V, T)), D)）は、対応表を引きながら集計する
            if let Node::On(x, edges) = &**child {
                if let Node::AsAxis { child: v, dim: target } = &**edges {
                    if let (Node::Ref(vi), true) = (&**v, sub.is_all()) {
                        if let Src::Store(vs) = &src[*vi] {
                            let rows = rows_of(x, cat, src, &sub, bud)?;
                            if let Some(c) = remove_by_table(&rows, *dim, *target, vs, *agg, cat, r)? {
                                return Ok(c);
                            }
                            // 引けなければ、結合してから集計する（On と同じ）
                            let xc = rows.into_cube();
                            let o = match semi(&sub, &xc, cat) {
                                Some(r2) => eval_with(edges, cat, src, &r2, bud)?,
                                None => eval_with(edges, cat, src, &sub, bud)?,
                            };
                            let joined = intersect(&xc, &o, xc.kind, cat, |a, _| Some(a))?;
                            return remove_rows(Rows::Owned(joined), *dim, *agg, cat, r);
                        }
                    }
                }
            }
            remove_rows(rows_of(child, cat, src, &sub, bud)?, *dim, *agg, cat, r)
        }

        Node::AsAxis { child, dim } => {
            // メンバー番号を値に持つ Cube を、そのメンバーを dim の座標に持つ表（値は 1）にする
            let c = eval_with(child, cat, src, &r.without(&[*dim]), bud)?;
            let out = Packing::new(&[c.dims(), &[*dim]].concat(), cat)?;
            let proj = Proj::new(&c.pack, &out);
            let (p, size) = (out.pos(*dim).unwrap(), cat.dims[*dim].size as f64);
            let cells = map_cells(cfg, &c.cells, |k, v| {
                (v >= 0.0 && v < size).then(|| (proj.apply(&c.pack, &out, k) | out.put(p, v as u32), 1.0))
            });
            Ok(Cube { pack: out, kind: Kind::Num, cells }.filter(cfg, r))
        }

        Node::Select { child, dim, member, .. } => {
            // member の切り口を取り出し、dim を外す
            let c = eval_with(child, cat, src, &r.with(*dim, Sel::new(vec![*member], cat.dims[*dim].size)), bud)?;
            let p = c.pack.pos(*dim).unwrap();
            let dims: Vec<DimId> = c.dims().iter().copied().filter(|d| d != dim).collect();
            let out = Packing::new(&dims, cat)?;
            let proj = Proj::new(&c.pack, &out);
            let cells = map_cells(cfg, &c.cells, |k, v| (c.pack.get(k, p) == *member).then(|| (proj.apply(&c.pack, &out, k), v)));
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
            let c = eval_with(child, cat, src, &sub, bud)?;
            let p = c.pack.pos(*dim).unwrap();
            let cells = map_cells(cfg, &c.cells, |k, v| {
                let t = c.pack.get(k, p) as i64 + n;
                (0..size).contains(&t).then(|| (c.pack.clear(k, p) | c.pack.put(p, t as u32), v))
            });
            Ok(Cube { pack: c.pack, kind: c.kind, cells }.filter(cfg, r))
        }
    }
}
