//! 式の型推論（軸と値の種類）。意味と誤り（コードと値。文言は Python の messages.MESSAGES）は
//! Python の参照実装（evaluate.infer）と同じで、テスト（tests/test_expr_coverage.py）で突き合わせる。
//!
//! 型を決めながら、評価と影響範囲が使う情報も式に書き込む。
//! - Bin、If、IsBlank、IfBlank の grow: 軸にメンバーを追加したとき、新しいメンバーへ値が広がる軸
//! - By: 式が持つ軸から、集約（ByAgg）か引き下ろし（ByLookup）かを決める

use crate::{bits_for, Agg, Arg, Catalog, DimId, Diag, Node, Op};

type Result<T> = std::result::Result<T, Diag>;

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

fn kind(k: &TKind, cat: &Catalog) -> Arg {
    match k {
        TKind::Num => "number".into(),
        TKind::Bool => "boolean".into(),
        TKind::Member(d) => format!("member:{}", cat.dims[*d].name).into(),
    }
}

fn name(d: DimId, cat: &Catalog) -> Arg {
    Arg::Str(cat.dims[d].name.clone())
}

/// 軸の名前の list（Python の参照実装が list で埋めるところ）。
fn list(dims: &[DimId], cat: &Catalog) -> Arg {
    Arg::List(dims.iter().map(|&d| name(d, cat)).collect())
}

/// 軸の名前の tuple（Python の参照実装が型の軸 Type.dims を埋めるところ）。
fn tuple(dims: &[DimId], cat: &Catalog) -> Arg {
    Arg::Tuple(dims.iter().map(|&d| name(d, cat)).collect())
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

fn need(t: &Ty, want: TKind, what: Diag, cat: &Catalog) -> Result<()> {
    if t.kind != want {
        return Err(Diag::new("need_kind").arg("what", what).arg("want", kind(&want, cat)).arg("got", kind(&t.kind, cat)));
    }
    Ok(())
}

fn agg_kind(agg: Agg, t: &Ty, what: Diag, cat: &Catalog) -> Result<TKind> {
    match agg {
        Agg::First => Ok(t.kind.clone()),
        Agg::Count => Ok(TKind::Num),
        _ => {
            need(t, TKind::Num, Diag::new("agg_of").arg("what", what).arg("agg", agg_name(agg)), cat)?;
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
fn check_expand(warnings: &mut Vec<Diag>, what: Diag, dims: &[DimId], covered: &[DimId], own: &[DimId], cat: &Catalog) -> Result<()> {
    let missing: Vec<DimId> = dims.iter().copied().filter(|d| !covered.contains(d)).collect();
    if missing.is_empty() {
        return Ok(());
    }
    if !own.is_empty() {
        return Err(Diag::new("not_expanded").arg("what", what).arg("own", list(own, cat)).arg("missing", list(&missing, cat)));
    }
    warnings.push(Diag::new("densify").arg("what", what).arg("missing", list(&missing, cat)));
    Ok(())
}

/// 式の型を決める。密になる演算は warnings に積む。grow と By の種類を式に書き込む。
/// 式の型（軸と値の種類）を決める。途中の結果も含めて、各ノードの結果の軸の組み合わせが 64 ビットの
/// キーに収まるかも確かめる（収まらなければ、評価の途中でなく、ここで直し方を示してエラーにする）。
pub fn infer(node: &mut Node, env: &Env<'_>, warnings: &mut Vec<Diag>) -> Result<Ty> {
    let ty = infer_node(node, env, warnings)?;
    key_fits(&ty.dims, env.cat)?;
    Ok(ty)
}

/// 軸の組み合わせが、1 セルのキー（64 ビット）に収まるか。
pub fn key_fits(dims: &[DimId], cat: &Catalog) -> Result<()> {
    let total: u32 = dims.iter().map(|&d| bits_for(cat.dims[d].size)).sum();
    if total <= 64 {
        return Ok(());
    }
    let bits = dims.iter().map(|&d| Arg::Msg(Diag::new("key_bits").arg("dim", name(d, cat)).arg("bits", bits_for(cat.dims[d].size)))).collect();
    Err(Diag::new("key_too_wide").arg("dims", list(dims, cat)).arg("bits", Arg::List(bits)))
}

fn infer_node(node: &mut Node, env: &Env<'_>, warnings: &mut Vec<Diag>) -> Result<Ty> {
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
            let (left, right) = (Diag::new("left").arg("op", name), Diag::new("right").arg("op", name));
            let kinds = || Diag::new("operand_kinds").arg("op", name).arg("left", kind(&lt.kind, cat)).arg("right", kind(&rt.kind, cat));
            let result = match op {
                Op::Add | Op::Sub | Op::Mul | Op::Div => {
                    need(&lt, TKind::Num, left.clone(), cat)?;
                    need(&rt, TKind::Num, right.clone(), cat)?;
                    TKind::Num
                }
                Op::Eq | Op::Ne => {
                    if lt.kind != rt.kind {
                        return Err(kinds());
                    }
                    TKind::Bool
                }
                Op::Lt | Op::Le | Op::Gt | Op::Ge => {
                    if matches!(lt.kind, TKind::Member(_)) || matches!(rt.kind, TKind::Member(_)) {
                        // メンバーの大小は、順序付きの軸（時間など）で、並び順で比べる
                        if lt.kind != rt.kind {
                            return Err(kinds());
                        }
                        let TKind::Member(d) = lt.kind else { unreachable!() };
                        if !cat.dims[d].ordered {
                            return Err(Diag::new("unordered_compare").arg("op", name).arg("dim", self::name(d, cat)));
                        }
                    } else {
                        need(&lt, TKind::Num, left.clone(), cat)?;
                        need(&rt, TKind::Num, right.clone(), cat)?;
                    }
                    TKind::Bool
                }
                Op::And | Op::Or => {
                    need(&lt, TKind::Bool, left.clone(), cat)?;
                    need(&rt, TKind::Bool, right.clone(), cat)?;
                    TKind::Bool
                }
            };
            if matches!(op, Op::Add | Op::Sub | Op::And | Op::Or) {
                // 片側だけのセルも結果に残る演算
                check_expand(warnings, left, &dims, &lt.dims, &lt.dims, cat)?;
                check_expand(warnings, right, &dims, &rt.dims, &rt.dims, cat)?;
                *grow = dims.iter().copied().filter(|d| !lt.dims.contains(d) || !rt.dims.contains(d)).collect();
            }
            Ok(Ty { dims, kind: result })
        }

        Node::Not(c) => {
            let t = infer(c, env, warnings)?;
            need(&t, TKind::Bool, Diag::new("not"), cat)?;
            Ok(t)
        }

        Node::If(cond, then, else_, grow) => {
            let ct = infer(cond, env, warnings)?;
            need(&ct, TKind::Bool, Diag::new("if_cond"), cat)?;
            let mut branches = vec![("if_then", infer(then, env, warnings)?)];
            if let Some(e) = else_ {
                branches.push(("if_else", infer(e, env, warnings)?));
            }
            if branches.len() == 2 && branches[0].1.kind != branches[1].1.kind {
                return Err(Diag::new("if_kinds").arg("kinds", Arg::List(branches.iter().map(|(_, t)| kind(&t.kind, cat)).collect())));
            }
            let mut dims = ct.dims.clone();
            for (_, t) in &branches {
                dims = merge(&dims, &t.dims);
            }
            for (what, t) in &branches {
                check_expand(warnings, Diag::new(what), &dims, &merge(&ct.dims, &t.dims), &t.dims, cat)?;
            }
            *grow = dims.iter().copied().filter(|d| branches.iter().any(|(_, b)| !merge(&ct.dims, &b.dims).contains(d))).collect();
            Ok(Ty { dims, kind: branches[0].1.kind.clone() })
        }

        Node::Filter(child, cond) => {
            let (t, ct) = (infer(child, env, warnings)?, infer(cond, env, warnings)?);
            need(&ct, TKind::Bool, Diag::new("filter_cond"), cat)?;
            let extra: Vec<DimId> = ct.dims.iter().copied().filter(|d| !t.dims.contains(d)).collect();
            if !extra.is_empty() {
                return Err(Diag::new("filter_dims").arg("extra", list(&extra, cat)));
            }
            Ok(t)
        }

        Node::Expand(child, dims) => {
            let t = infer(child, env, warnings)?;
            for &d in dims.iter() {
                if t.dims.contains(&d) {
                    return Err(Diag::new("expand_present").arg("dim", name(d, cat)));
                }
            }
            let mut seen = Vec::new();
            for &d in dims.iter() {
                if seen.contains(&d) {
                    return Err(Diag::new("expand_repeated").arg("dims", list(dims, cat)));
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
                return Err(Diag::new("coalesce_mismatch")
                    .arg("first_dims", tuple(&ft.dims, cat))
                    .arg("first_kind", kind(&ft.kind, cat))
                    .arg("second_dims", tuple(&st.dims, cat))
                    .arg("second_kind", kind(&st.kind, cat)));
            }
            Ok(st)
        }

        Node::IsBlank(child, grow) => {
            let t = infer(child, env, warnings)?;
            if !t.dims.is_empty() {
                warnings.push(Diag::new("isblank_dense").arg("dims", list(&t.dims, cat)));
            }
            *grow = t.dims.clone();
            Ok(Ty { dims: t.dims, kind: TKind::Bool })
        }

        Node::IfBlank(child, _, is_bool, grow) => {
            let t = infer(child, env, warnings)?;
            let vkind = if *is_bool { TKind::Bool } else { TKind::Num };
            if vkind != t.kind {
                return Err(Diag::new("ifblank_kind").arg("kind", kind(&t.kind, cat)));
            }
            if !t.dims.is_empty() {
                warnings.push(Diag::new("ifblank_dense").arg("dims", list(&t.dims, cat)));
            }
            *grow = t.dims.clone();
            Ok(t)
        }

        Node::By { child, src, dst, map, agg, dim, prop } => {
            let t = infer(child, env, warnings)?;
            let (src, dst, map, agg) = (*src, *dst, *map, *agg);
            let what = Diag::new("by").arg("dim", &*dim).arg("prop", &*prop);
            let target = name(dst, cat);
            let (resolved, ty) = if t.dims.contains(&src) {
                if t.dims.contains(&dst) {
                    return Err(Diag::new("by_target_present").arg("what", what).arg("target", target));
                }
                let agg = agg.unwrap_or(Agg::Sum);
                let kind = agg_kind(agg, &t, what, cat)?;
                let child = std::mem::replace(child, Box::new(Node::Const(0.0, crate::Kind::Num)));
                (Node::ByAgg { child, src, dst, map, agg }, Ty { dims: replace(&t.dims, src, dst), kind })
            } else if t.dims.contains(&dst) {
                if agg.is_some() {
                    return Err(Diag::new("by_lookup_agg").arg("what", what));
                }
                let child = std::mem::replace(child, Box::new(Node::Const(0.0, crate::Kind::Num)));
                (Node::ByLookup { child, src, dst, map }, Ty { dims: replace(&t.dims, dst, src), kind: t.kind })
            } else {
                return Err(Diag::new("by_no_dims").arg("what", what).arg("dims", tuple(&t.dims, cat)).arg("dim", &*dim).arg("target", target));
            };
            *node = resolved;
            Ok(ty)
        }
        Node::ByAgg { child, src, dst, agg, .. } => {
            // 型を決めた式をもう一度検査したとき（誤りはないので、型を決め直すだけ）
            let t = infer(child, env, warnings)?;
            let (src, dst, agg) = (*src, *dst, *agg);
            let kind = agg_kind(agg, &t, Diag::new("by").arg("dim", name(src, cat)).arg("prop", name(dst, cat)), cat)?;
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
            let what = Diag::new("by").arg("dim", &*dim).arg("prop", &*prop);
            let vt = env.types[metric].clone();
            let TKind::Member(target) = vt.kind else {
                return Err(Diag::new("by_not_member").arg("what", what).arg("prop", &*prop).arg("kind", kind(&vt.kind, cat)));
            };
            if !vt.dims.contains(&src) {
                return Err(Diag::new("by_metric_no_dim").arg("what", what).arg("prop", &*prop).arg("dims", tuple(&vt.dims, cat)).arg("dim", &*dim));
            }
            let t = infer(child, env, warnings)?;
            let edges_dims = [vt.dims.as_slice(), &[target]].concat();
            let joined = merge(&t.dims, &edges_dims); // On(child, AsAxis(V)) の軸
            // 書き換えたあとの結合は D と T の両方を持つ。結果の軸が収まっても結合が収まらなければ、
            // 評価の途中でなくここで拒否する（書き換えたノードは型検査を通らないので、ここで確かめる）
            key_fits(&joined, cat)?;
            let (remove, agg, ty) = if t.dims.contains(&src) {
                if t.dims.contains(&target) {
                    return Err(Diag::new("by_target_present").arg("what", what).arg("target", name(target, cat)));
                }
                let missing: Vec<DimId> = vt.dims.iter().copied().filter(|d| !t.dims.contains(d)).collect();
                if !missing.is_empty() {
                    return Err(Diag::new("by_metric_missing").arg("what", what).arg("prop", &*prop).arg("missing", list(&missing, cat)));
                }
                let agg = agg.unwrap_or(Agg::Sum);
                let remove = Diag::new("remove").arg("dim", name(src, cat));
                let kind = agg_kind(agg, &Ty { dims: joined.clone(), kind: t.kind.clone() }, remove, cat)?;
                (src, agg, Ty { dims: joined.iter().copied().filter(|d| *d != src).collect(), kind })
            } else if t.dims.contains(&target) {
                if agg.is_some() {
                    return Err(Diag::new("by_lookup_agg").arg("what", what));
                }
                (target, Agg::First, Ty { dims: joined.iter().copied().filter(|d| *d != target).collect(), kind: t.kind.clone() })
            } else {
                return Err(Diag::new("by_no_dims").arg("what", what).arg("dims", tuple(&t.dims, cat)).arg("dim", &*dim).arg("target", name(target, cat)));
            };
            let child = std::mem::replace(child, Box::new(Node::Const(0.0, crate::Kind::Num)));
            let edges = Box::new(Node::AsAxis { child: Box::new(Node::Ref(metric)), dim: target });
            *node = Node::Remove { child: Box::new(Node::On(child, edges)), dim: remove, agg };
            Ok(ty)
        }

        Node::Remove { child, dim, agg } => {
            let t = infer(child, env, warnings)?;
            let (dim, agg) = (*dim, *agg);
            let what = Diag::new("remove").arg("dim", name(dim, cat));
            if !t.dims.contains(&dim) {
                return Err(Diag::new("remove_absent").arg("what", what).arg("dims", tuple(&t.dims, cat)));
            }
            let kind = agg_kind(agg, &t, what, cat)?;
            Ok(Ty { dims: t.dims.iter().copied().filter(|d| *d != dim).collect(), kind })
        }

        Node::Shift { child, dim, .. } => {
            let t = infer(child, env, warnings)?;
            let dim = *dim;
            if !t.dims.contains(&dim) {
                return Err(Diag::new("previous_absent").arg("dim", name(dim, cat)).arg("dims", tuple(&t.dims, cat)));
            }
            if !cat.dims[dim].ordered {
                return Err(Diag::new("previous_unordered").arg("dim", name(dim, cat)));
            }
            Ok(t)
        }

        Node::AsAxis { child, dim } => {
            let t = infer(child, env, warnings)?;
            let dim = *dim;
            need(&t, TKind::Member(dim), Diag::new("edges"), cat)?;
            Ok(Ty { dims: [t.dims.as_slice(), &[dim]].concat(), kind: TKind::Num })
        }

        Node::Select { child, dim, name, .. } => {
            let t = infer(child, env, warnings)?;
            let dim = *dim;
            if !t.dims.contains(&dim) {
                return Err(Diag::new("select_absent").arg("dim", self::name(dim, cat)).arg("member", &*name).arg("dims", tuple(&t.dims, cat)));
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
pub fn estimate(node: &Node, cat: &Catalog, refs: &[(Vec<DimId>, f64)]) -> crate::Result<(Vec<DimId>, f64)> {
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
