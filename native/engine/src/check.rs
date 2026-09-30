//! 式の型推論（軸と値の種類）。意味と文言は Python の参照実装（evaluate.infer）と同じで、
//! テスト（tests/test_expr_coverage.py）で突き合わせる。
//!
//! 型を決めながら、評価と影響範囲が使う情報も式に書き込む。
//! - Bin、If、IsBlank、IfBlank の grow: 軸にメンバーを追加したとき、新しいメンバーへ値が広がる軸
//! - By: 式が持つ軸から、集約（ByAgg）か引き下ろし（ByLookup）かを決める

use crate::{Agg, Catalog, DimId, Node, Op, Result};

/// 値の種類。number、boolean、または軸のメンバー（member:<軸>）。
#[derive(Clone, Debug, PartialEq)]
pub enum TKind {
    Num,
    Bool,
    Member(DimId),
}

/// 式の型。軸の並びと値の種類。
#[derive(Clone, Debug, PartialEq)]
pub struct Ty {
    pub dims: Vec<DimId>,
    pub kind: TKind,
}

/// 型推論の環境。Ref の番号ごとの型と名前（文言用）。
pub struct Env<'a> {
    pub cat: &'a Catalog,
    pub types: &'a [Ty],
}

fn kind_name(k: &TKind, cat: &Catalog) -> String {
    match k {
        TKind::Num => "number".into(),
        TKind::Bool => "boolean".into(),
        TKind::Member(d) => format!("member:{}", cat.dims[*d].name),
    }
}

/// Python の list の表示（['A', 'B']）。
fn list_repr(dims: &[DimId], cat: &Catalog) -> String {
    format!("[{}]", dims.iter().map(|&d| format!("'{}'", cat.dims[d].name)).collect::<Vec<_>>().join(", "))
}

/// Python の tuple の表示（('A', 'B')、('A',)、()）。
fn tuple_repr(dims: &[DimId], cat: &Catalog) -> String {
    let items: Vec<String> = dims.iter().map(|&d| format!("'{}'", cat.dims[d].name)).collect();
    match items.len() {
        0 => "()".into(),
        1 => format!("({},)", items[0]),
        _ => format!("({})", items.join(", ")),
    }
}

fn ty_repr(t: &Ty, cat: &Catalog) -> String {
    format!("Type(dims={}, kind='{}')", tuple_repr(&t.dims, cat), kind_name(&t.kind, cat))
}

fn merge(a: &[DimId], b: &[DimId]) -> Vec<DimId> {
    let mut out = a.to_vec();
    out.extend(b.iter().copied().filter(|d| !a.contains(d)));
    out
}

fn same_set(a: &[DimId], b: &[DimId]) -> bool {
    a.len() == b.len() && a.iter().all(|d| b.contains(d))
}

fn replace(dims: &[DimId], old: DimId, new: DimId) -> Vec<DimId> {
    dims.iter().map(|&d| if d == old { new } else { d }).collect()
}

fn op_name(op: Op) -> &'static str {
    match op {
        Op::Add => "+",
        Op::Sub => "-",
        Op::Mul => "*",
        Op::Div => "/",
        Op::Eq => "=",
        Op::Ne => "<>",
        Op::Lt => "<",
        Op::Le => "<=",
        Op::Gt => ">",
        Op::Ge => ">=",
        Op::And => "and",
        Op::Or => "or",
    }
}

fn need(t: &Ty, kind: TKind, what: &str, cat: &Catalog) -> Result<()> {
    if t.kind != kind {
        return Err(format!("{what} には {} が必要だが {} が渡された", kind_name(&kind, cat), kind_name(&t.kind, cat)));
    }
    Ok(())
}

fn agg_kind(agg: Agg, t: &Ty, what: &str, cat: &Catalog) -> Result<TKind> {
    match agg {
        Agg::First => Ok(t.kind.clone()),
        Agg::Count => Ok(TKind::Num),
        _ => {
            need(t, TKind::Num, &format!("{what} の {}", agg_name(agg)), cat)?;
            Ok(TKind::Num)
        }
    }
}

fn agg_name(agg: Agg) -> &'static str {
    match agg {
        Agg::Sum => "sum",
        Agg::Avg => "avg",
        Agg::Min => "min",
        Agg::Max => "max",
        Agg::Count => "count",
        Agg::First => "first",
    }
}

/// 結果の軸 dims のうち covered にない軸へ、この項の値が複製されるかを調べる。
/// 項が定数（軸なし）なら警告だけ出して許す。軸を持つ Metric なら、別の軸への暗黙の展開は
/// セル数が爆発しうるのでエラーにし、expand か on で意図を書かせる。
fn check_expand(warnings: &mut Vec<String>, what: &str, dims: &[DimId], covered: &[DimId], own: &[DimId], cat: &Catalog) -> Result<()> {
    let missing: Vec<DimId> = dims.iter().copied().filter(|d| !covered.contains(d)).collect();
    if missing.is_empty() {
        return Ok(());
    }
    if !own.is_empty() {
        let names = missing.iter().map(|&d| cat.dims[d].name.clone()).collect::<Vec<_>>().join(", ");
        return Err(format!(
            "{what}（軸 {}）に {} 軸がない。全メンバーへ展開するなら [EXPAND: {names}]、相手に値があるセルだけなら [ON: 相手] を付ける（Python の DSL では .expand / .on）",
            list_repr(own, cat),
            list_repr(&missing, cat)
        ));
    }
    warnings.push(format!("{what}が {} 方向に全メンバーへ展開される（密化）", list_repr(&missing, cat)));
    Ok(())
}

/// 式の型を決める。密になる演算は warnings に積む。grow と By の種類を式に書き込む。
pub fn infer(node: &mut Node, env: &Env<'_>, warnings: &mut Vec<String>) -> Result<Ty> {
    let cat = env.cat;
    match node {
        Node::Ref(i) => Ok(env.types[*i].clone()),
        Node::Const(_, kind) => Ok(Ty { dims: vec![], kind: if *kind == crate::Kind::Bool { TKind::Bool } else { TKind::Num } }),
        Node::DimRef(d) => Ok(Ty { dims: vec![*d], kind: TKind::Member(*d) }),
        Node::MemberConst(d, _) => Ok(Ty { dims: vec![], kind: TKind::Member(*d) }),

        Node::Bin(op, l, r, grow) => {
            let (op, lt, rt) = (*op, infer(l, env, warnings)?, infer(r, env, warnings)?);
            let dims = merge(&lt.dims, &rt.dims);
            let name = op_name(op);
            let kind = match op {
                Op::Add | Op::Sub | Op::Mul | Op::Div => {
                    need(&lt, TKind::Num, &format!("'{name}' の左辺"), cat)?;
                    need(&rt, TKind::Num, &format!("'{name}' の右辺"), cat)?;
                    TKind::Num
                }
                Op::Eq | Op::Ne => {
                    if lt.kind != rt.kind {
                        return Err(format!("'{name}' の両辺の種類が違う: {} と {}", kind_name(&lt.kind, cat), kind_name(&rt.kind, cat)));
                    }
                    TKind::Bool
                }
                Op::Lt | Op::Le | Op::Gt | Op::Ge => {
                    if matches!(lt.kind, TKind::Member(_)) || matches!(rt.kind, TKind::Member(_)) {
                        // メンバーの大小は、順序付きの軸（時間など）で、並び順で比べる
                        if lt.kind != rt.kind {
                            return Err(format!("'{name}' の両辺の種類が違う: {} と {}", kind_name(&lt.kind, cat), kind_name(&rt.kind, cat)));
                        }
                        let TKind::Member(d) = lt.kind else { unreachable!() };
                        if !cat.dims[d].ordered {
                            return Err(format!("'{name}': {} は順序付きの軸ではないので大小を比べられない（= と <> は使える）", cat.dims[d].name));
                        }
                    } else {
                        need(&lt, TKind::Num, &format!("'{name}' の左辺"), cat)?;
                        need(&rt, TKind::Num, &format!("'{name}' の右辺"), cat)?;
                    }
                    TKind::Bool
                }
                Op::And | Op::Or => {
                    need(&lt, TKind::Bool, &format!("'{name}' の左辺"), cat)?;
                    need(&rt, TKind::Bool, &format!("'{name}' の右辺"), cat)?;
                    TKind::Bool
                }
            };
            if matches!(op, Op::Add | Op::Sub | Op::And | Op::Or) {
                // 片側だけのセルも結果に残る演算
                check_expand(warnings, &format!("'{name}' の左辺"), &dims, &lt.dims, &lt.dims, cat)?;
                check_expand(warnings, &format!("'{name}' の右辺"), &dims, &rt.dims, &rt.dims, cat)?;
                *grow = dims.iter().copied().filter(|d| !lt.dims.contains(d) || !rt.dims.contains(d)).collect();
            }
            Ok(Ty { dims, kind })
        }

        Node::Not(c) => {
            let t = infer(c, env, warnings)?;
            need(&t, TKind::Bool, "NOT", cat)?;
            Ok(t)
        }

        Node::If(cond, then, else_, grow) => {
            let ct = infer(cond, env, warnings)?;
            need(&ct, TKind::Bool, "IF の条件", cat)?;
            let mut branches = vec![("IF の THEN", infer(then, env, warnings)?)];
            if let Some(e) = else_ {
                branches.push(("IF の ELSE", infer(e, env, warnings)?));
            }
            if branches.len() == 2 && branches[0].1.kind != branches[1].1.kind {
                let kinds: Vec<String> = branches.iter().map(|(_, t)| format!("'{}'", kind_name(&t.kind, cat))).collect();
                return Err(format!("IF の THEN と ELSE の種類が違う: [{}]", kinds.join(", ")));
            }
            let mut dims = ct.dims.clone();
            for (_, t) in &branches {
                dims = merge(&dims, &t.dims);
            }
            for (what, t) in &branches {
                check_expand(warnings, what, &dims, &merge(&ct.dims, &t.dims), &t.dims, cat)?;
            }
            *grow = dims.iter().copied().filter(|d| branches.iter().any(|(_, b)| !merge(&ct.dims, &b.dims).contains(d))).collect();
            Ok(Ty { dims, kind: branches[0].1.kind.clone() })
        }

        Node::Filter(child, cond) => {
            let (t, ct) = (infer(child, env, warnings)?, infer(cond, env, warnings)?);
            need(&ct, TKind::Bool, "FILTER の条件", cat)?;
            let extra: Vec<DimId> = ct.dims.iter().copied().filter(|d| !t.dims.contains(d)).collect();
            if !extra.is_empty() {
                return Err(format!("FILTER の条件が対象にない軸 {} を持っている", list_repr(&extra, cat)));
            }
            Ok(t)
        }

        Node::Expand(child, dims) => {
            let t = infer(child, env, warnings)?;
            for &d in dims.iter() {
                if t.dims.contains(&d) {
                    return Err(format!("EXPAND {}: すでに軸にある", cat.dims[d].name));
                }
            }
            let mut seen = Vec::new();
            for &d in dims.iter() {
                if seen.contains(&d) {
                    return Err(format!("EXPAND {}: 軸が重複している", list_repr(dims, cat)));
                }
                seen.push(d);
            }
            Ok(Ty { dims: [t.dims.as_slice(), dims.as_slice()].concat(), kind: t.kind })
        }

        Node::On(child, other) => {
            let (t, ot) = (infer(child, env, warnings)?, infer(other, env, warnings)?);
            Ok(Ty { dims: merge(&t.dims, &ot.dims), kind: t.kind })
        }

        Node::Coalesce(first, second) => {
            let (ft, st) = (infer(first, env, warnings)?, infer(second, env, warnings)?);
            if !same_set(&ft.dims, &st.dims) || ft.kind != st.kind {
                return Err(format!("上書きの軸と種類が式と一致しない: {} と {}", ty_repr(&ft, cat), ty_repr(&st, cat)));
            }
            Ok(st)
        }

        Node::IsBlank(child, grow) => {
            let t = infer(child, env, warnings)?;
            if !t.dims.is_empty() {
                warnings.push(format!("ISBLANK が {} の全組み合わせに展開される（密化）", list_repr(&t.dims, cat)));
            }
            *grow = t.dims.clone();
            Ok(Ty { dims: t.dims, kind: TKind::Bool })
        }

        Node::IfBlank(child, _, is_bool, grow) => {
            let t = infer(child, env, warnings)?;
            let vkind = if *is_bool { TKind::Bool } else { TKind::Num };
            if vkind != t.kind {
                return Err(format!("IFBLANK の既定値は {} でなければならない", kind_name(&t.kind, cat)));
            }
            if !t.dims.is_empty() {
                warnings.push(format!("IFBLANK が {} の全組み合わせに展開される（密化）", list_repr(&t.dims, cat)));
            }
            *grow = t.dims.clone();
            Ok(t)
        }

        Node::By { child, src, dst, map, agg, dim, prop } => {
            let t = infer(child, env, warnings)?;
            let (src, dst, map, agg) = (*src, *dst, *map, *agg);
            let what = format!("BY {dim}.{prop}");
            let target = cat.dims[dst].name.clone();
            let (resolved, ty) = if t.dims.contains(&src) {
                if t.dims.contains(&dst) {
                    return Err(format!("{what}: 集約先の {target} がすでに軸にある"));
                }
                let agg = agg.unwrap_or(Agg::Sum);
                let kind = agg_kind(agg, &t, &what, cat)?;
                let child = std::mem::replace(child, Box::new(Node::Const(0.0, crate::Kind::Num)));
                (Node::ByAgg { child, src, dst, map, agg }, Ty { dims: replace(&t.dims, src, dst), kind })
            } else if t.dims.contains(&dst) {
                if agg.is_some() {
                    return Err(format!("{what}: 引き下ろし（lookup）に集計関数は指定できない"));
                }
                let child = std::mem::replace(child, Box::new(Node::Const(0.0, crate::Kind::Num)));
                (Node::ByLookup { child, src, dst, map }, Ty { dims: replace(&t.dims, dst, src), kind: t.kind })
            } else {
                return Err(format!("{what}: 式の軸 {} に {dim} も {target} もない", tuple_repr(&t.dims, cat)));
            };
            *node = resolved;
            Ok(ty)
        }
        Node::ByAgg { child, src, dst, agg, .. } => {
            // 型を決めた式をもう一度検査したとき（誤りはないので、型を決め直すだけ）
            let t = infer(child, env, warnings)?;
            let (src, dst, agg) = (*src, *dst, *agg);
            let kind = agg_kind(agg, &t, "BY", cat)?;
            Ok(Ty { dims: replace(&t.dims, src, dst), kind })
        }
        Node::ByLookup { child, src, dst, .. } => {
            let t = infer(child, env, warnings)?;
            let (src, dst) = (*src, *dst);
            Ok(Ty { dims: replace(&t.dims, dst, src), kind: t.kind })
        }

        Node::ByMetric { child, src, metric, agg, dim, prop } => {
            // `child[BY agg: D.V]`（V はメンバー型の Metric）を、既存の演算の組み合わせにする。
            // V の各セル（例: 社員 e・月 m）は、そのときの D のメンバー e の所属先 t を持つ。
            // AsAxis(V, T) はそれを「(e, m, t) の位置に 1 がある表」にしたもので、
            //   集約:     child ⋈ 対応表 を D について集計する   -> D が T に置き換わる
            //   引き下ろし: child ⋈ 対応表 から T を外す（各行の T は 1 つ） -> T が D（と V の軸）に置き換わる
            let (src, metric, agg) = (*src, *metric, *agg);
            let what = format!("BY {dim}.{prop}");
            let vt = env.types[metric].clone();
            let TKind::Member(target) = vt.kind else {
                return Err(format!("{what}: {prop} はメンバー型の Metric ではない（{}）", kind_name(&vt.kind, cat)));
            };
            if !vt.dims.contains(&src) {
                return Err(format!("{what}: {prop} の軸 {} に {dim} がない", tuple_repr(&vt.dims, cat)));
            }
            let t = infer(child, env, warnings)?;
            let edges_dims = [vt.dims.as_slice(), &[target]].concat();
            let joined = merge(&t.dims, &edges_dims); // On(child, AsAxis(V)) の軸
            let (remove, agg, ty) = if t.dims.contains(&src) {
                if t.dims.contains(&target) {
                    return Err(format!("{what}: 集約先の {} がすでに軸にある", cat.dims[target].name));
                }
                let missing: Vec<DimId> = vt.dims.iter().copied().filter(|d| !t.dims.contains(d)).collect();
                if !missing.is_empty() {
                    return Err(format!("{what}: 式が {prop} の軸 {} を持っていない", list_repr(&missing, cat)));
                }
                let agg = agg.unwrap_or(Agg::Sum);
                let kind = agg_kind(agg, &Ty { dims: joined.clone(), kind: t.kind.clone() }, &format!("REMOVE {}", cat.dims[src].name), cat)?;
                (src, agg, Ty { dims: joined.iter().copied().filter(|d| *d != src).collect(), kind })
            } else if t.dims.contains(&target) {
                if agg.is_some() {
                    return Err(format!("{what}: 引き下ろし（lookup）に集計関数は指定できない"));
                }
                (target, Agg::First, Ty { dims: joined.iter().copied().filter(|d| *d != target).collect(), kind: t.kind.clone() })
            } else {
                return Err(format!("{what}: 式の軸 {} に {dim} も {} もない", tuple_repr(&t.dims, cat), cat.dims[target].name));
            };
            let child = std::mem::replace(child, Box::new(Node::Const(0.0, crate::Kind::Num)));
            let edges = Box::new(Node::AsAxis { child: Box::new(Node::Ref(metric)), dim: target });
            *node = Node::Remove { child: Box::new(Node::On(child, edges)), dim: remove, agg };
            Ok(ty)
        }

        Node::Remove { child, dim, agg } => {
            let t = infer(child, env, warnings)?;
            let (dim, agg) = (*dim, *agg);
            let what = format!("REMOVE {}", cat.dims[dim].name);
            if !t.dims.contains(&dim) {
                return Err(format!("{what}: 式の軸 {} にない", tuple_repr(&t.dims, cat)));
            }
            let kind = agg_kind(agg, &t, &what, cat)?;
            Ok(Ty { dims: t.dims.iter().copied().filter(|d| *d != dim).collect(), kind })
        }

        Node::Shift { child, dim, .. } => {
            let t = infer(child, env, warnings)?;
            let dim = *dim;
            if !t.dims.contains(&dim) {
                return Err(format!("PREVIOUS {}: 式の軸 {} にない", cat.dims[dim].name, tuple_repr(&t.dims, cat)));
            }
            if !cat.dims[dim].ordered {
                return Err(format!("PREVIOUS {}: 順序付きの軸ではない", cat.dims[dim].name));
            }
            Ok(t)
        }

        Node::AsAxis { child, dim } => {
            let t = infer(child, env, warnings)?;
            let dim = *dim;
            need(&t, TKind::Member(dim), "対応表", cat)?;
            Ok(Ty { dims: [t.dims.as_slice(), &[dim]].concat(), kind: TKind::Num })
        }

        Node::Select { child, dim, name, .. } => {
            let t = infer(child, env, warnings)?;
            let dim = *dim;
            if !t.dims.contains(&dim) {
                return Err(format!("SELECT {}.\"{name}\": 式の軸 {} に {} がない", cat.dims[dim].name, tuple_repr(&t.dims, cat), cat.dims[dim].name));
            }
            Ok(Ty { dims: t.dims.iter().copied().filter(|d| *d != dim).collect(), kind: t.kind })
        }
    }
}

// ------------------------------------------------------------------ セル数の見積もり

/// 軸の全組み合わせの数（dims の順に掛ける。Python の参照実装と同じ順にして、丸めを揃える）。
fn dense(dims: &[DimId], cat: &Catalog) -> f64 {
    dims.iter().fold(1.0, |acc, &d| acc * cat.dims[d].size as f64)
}

/// dims のうち own にない軸の全組み合わせの数（own の値がその方向へ複製される倍率）。
fn spread(dims: &[DimId], own: &[DimId], cat: &Catalog) -> f64 {
    dims.iter().filter(|d| !own.contains(d)).fold(1.0, |acc, &d| acc * cat.dims[d].size as f64)
}

fn without(dims: &[DimId], dim: DimId) -> Vec<DimId> {
    dims.iter().copied().filter(|d| *d != dim).collect()
}

/// 型を決めた式（infer の後）の結果の軸と、セル数の上限の見積もり。refs は Ref の番号ごとの
/// (軸, セル数)。値のあるセルだけが結果に残る演算は小さい側で、片側だけでも残る演算は和で、
/// 全組み合わせに値を作る演算は軸の大きさの積で見積もり、どれも結果の軸の全組み合わせで頭打ちにする。
/// 意味は Python の参照実装（evaluate.estimate）と同じ。
pub fn estimate(node: &Node, cat: &Catalog, refs: &[(Vec<DimId>, f64)]) -> Result<(Vec<DimId>, f64)> {
    let est = |n: &Node| estimate(n, cat, refs);
    let (dims, n) = match node {
        Node::Ref(i) => refs[*i].clone(),
        Node::Const(..) | Node::MemberConst(..) => (vec![], 1.0),
        Node::DimRef(d) => (vec![*d], cat.dims[*d].size as f64),
        Node::Bin(op, l, r, _) => {
            let ((ld, ln), (rd, rn)) = (est(l)?, est(r)?);
            let dims = merge(&ld, &rd);
            let (a, b) = (ln * spread(&dims, &ld, cat), rn * spread(&dims, &rd, cat));
            let n = if matches!(op, Op::Add | Op::Sub | Op::And | Op::Or) { a + b } else { a.min(b) };
            (dims, n)
        }
        Node::Not(c) | Node::Shift { child: c, .. } => est(c)?,
        Node::If(cond, then, else_, _) => {
            let (cd, cn) = est(cond)?;
            let mut branches = vec![est(then)?];
            if let Some(e) = else_ {
                branches.push(est(e)?);
            }
            let mut dims = cd.clone();
            for (bd, _) in &branches {
                dims = merge(&dims, bd);
            }
            let mut n = 0.0;
            for (bd, bn) in &branches {
                let part = merge(&cd, bd);
                n += (cn * spread(&part, &cd, cat)).min(bn * spread(&part, bd, cat)) * spread(&dims, &part, cat);
            }
            (dims, n)
        }
        Node::Filter(child, cond) => {
            let ((d, n), (cd, cn)) = (est(child)?, est(cond)?);
            let m = cn * spread(&d, &cd, cat);
            (d, n.min(m))
        }
        Node::On(child, other) => {
            let ((d, n), (od, on)) = (est(child)?, est(other)?);
            let dims = merge(&d, &od);
            let n = (n * spread(&dims, &d, cat)).min(on * spread(&dims, &od, cat));
            (dims, n)
        }
        Node::Expand(child, ds) => {
            let (d, n) = est(child)?;
            (merge(&d, ds), n * dense(ds, cat))
        }
        Node::IsBlank(child, _) | Node::IfBlank(child, ..) => {
            let (d, _) = est(child)?;
            let n = dense(&d, cat);
            (d, n)
        }
        Node::ByAgg { child, src, dst, .. } => {
            let (d, n) = est(child)?;
            (replace(&d, *src, *dst), n)
        }
        Node::ByLookup { child, src, dst, .. } => {
            let (d, n) = est(child)?;
            (replace(&d, *dst, *src), n * cat.dims[*src].size as f64)
        }
        Node::Remove { child, dim, .. } | Node::Select { child, dim, .. } => {
            let (d, n) = est(child)?;
            (without(&d, *dim), n)
        }
        Node::AsAxis { child, dim } => {
            let (d, n) = est(child)?;
            ([d.as_slice(), &[*dim]].concat(), n)
        }
        Node::Coalesce(first, second) => {
            let ((_, fnum), (sd, sn)) = (est(first)?, est(second)?);
            (sd, fnum + sn)
        }
        Node::By { .. } | Node::ByMetric { .. } => return Err("セル数の見積もりは型を決めた式にだけ使える".into()),
    };
    let cap = dense(&dims, cat);
    Ok((dims, n.min(cap)))
}
