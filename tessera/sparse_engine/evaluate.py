"""型推論（軸と値の種類）と、式の評価。

評価は `restrict`（軸 -> 対象メンバー集合）を受け取り、結果をその範囲に絞って計算する。
時間方向の scan はこれを使って 1 時点ずつ、他の軸についてはまとめて計算する。

空の扱いは「結果が空でないセルの集合（support）」で決める。

| 演算              | support                       |
|-------------------|-------------------------------|
| + -               | 和集合（空は 0）              |
| x.expand(dims)    | x × dims の全メンバー（密）    |
| x.on(y)           | x ∩ y（値は x）                |
| * /               | 共通部分（0 除算は空）         |
| 比較 = <> < ...    | 共通部分                      |
| and / or          | 和集合（三値論理で空が残る）   |
| not               | 変わらない                    |
| IF(c, a, b)       | c の support の部分集合        |
| FILTER(x, c)      | x ∩ {c が TRUE}               |
| x[SELECT: D."m"]  | x の D = m の切り口（D は外れる） |
| IFBLANK / ISBLANK | 全メンバー（密）              |
"""
from __future__ import annotations

import operator
from collections import defaultdict
from dataclasses import dataclass, fields, replace
from itertools import product
from typing import Iterator, Mapping, Protocol

from .core import Cube, Dimension
from .expr import (AGGREGATIONS, AGGREGATORS, ARITH, COMPARE, LOGIC, AsAxis, BinOp, By, Coalesce, Const, DimRef,
                   Expand, Expr, Filter, If, IfBlank, IsBlank, Member, Not, On, Ref, Remove, Select,
                   Shift)
from .messages import Msg, msg, render

Restrict = dict[str, frozenset[str]]  # dimension id -> member ids
# "number" / "boolean"、または軸のメンバー "member:<軸名>"（式の途中だけで使い、Metric には格納しない）
Kind = str


def member_kind(dim: str) -> Kind:
    return f"member:{dim}"


class FormulaError(Exception):
    """式の誤り。code は messages.MESSAGES のキーで、params は文言に埋める値。"""

    def __init__(self, code: str, **params):
        super().__init__(render(code, params))
        self.code, self.params = code, params


@dataclass(frozen=True)
class Type:
    dims: tuple[str, ...]
    kind: Kind


class Catalog(Protocol):
    def dimension(self, name: str) -> Dimension: ...
    def metric_type(self, name: str) -> Type: ...
    def read(self, name: str, restrict: Restrict | None) -> Cube:
        """Metric の値を restrict の範囲に絞って返す。"""


def _merge(*dims_list: tuple[str, ...]) -> tuple[str, ...]:
    out: tuple[str, ...] = ()
    for dims in dims_list:
        out += tuple(d for d in dims if d not in out)
    return out


def _replace(dims: tuple[str, ...], old: str, new: str) -> tuple[str, ...]:
    return tuple(new if d == old else d for d in dims)


def _property(cat: Catalog, dim: str, prop: str) -> tuple[str, dict[str, str]]:
    props = cat.dimension(dim).properties
    if prop not in props:
        raise FormulaError("no_property", dim=dim, prop=prop)
    return props[prop]


# ---------------------------------------------------------------- 型推論

def _need(t: Type, kind: Kind, what: Msg) -> None:
    if t.kind != kind:
        raise FormulaError("need_kind", what=what, want=kind, got=t.kind)


def _agg_kind(agg: str, t: Type, what: Msg) -> Kind:
    if agg not in AGGREGATIONS:
        raise FormulaError("unknown_agg", agg=agg)
    a = AGGREGATIONS[agg]
    if a.numeric:
        _need(t, "number", msg("agg_of", what=what, agg=agg))
    return t.kind if a.kind is None else a.kind


def _check_expand(warnings: list[Msg], what: Msg, dims: tuple[str, ...],
                  covered: tuple[str, ...], own: tuple[str, ...]) -> None:
    """結果の軸 dims のうち covered にない軸へ、この項の値が複製されるかを調べる。

    項が定数（軸なし）なら警告だけ出して許す。軸を持つ Metric なら、別の軸への暗黙の展開は
    セル数が爆発しうるのでエラーにし、expand か on で意図を書かせる。
    """
    missing = [d for d in dims if d not in covered]
    if not missing:
        return
    if own:
        raise FormulaError("not_expanded", what=what, own=list(own), missing=missing)
    warnings.append(msg("densify", what=what, missing=missing))


def infer(expr: Expr, cat: Catalog, warnings: list[Msg]) -> Type:
    """The type (dimensions and kind) of the result of expr. A dense operation adds a Msg to warnings."""
    match expr:
        case Ref(name):
            return cat.metric_type(name)

        case Const(value):
            return Type((), "boolean" if isinstance(value, bool) else "number")

        case DimRef(dim):
            cat.dimension(dim)
            return Type((dim,), member_kind(dim))

        case Member(dim, member):
            if member not in cat.dimension(dim)._by_id:
                raise FormulaError("unknown_member", dim=dim, member=member)
            return Type((), member_kind(dim))

        case BinOp(op, left, right):
            lt, rt = infer(left, cat, warnings), infer(right, cat, warnings)
            dims = _merge(lt.dims, rt.dims)
            left_, right_ = msg("left", op=op), msg("right", op=op)
            if op in ARITH:
                _need(lt, "number", left_)
                _need(rt, "number", right_)
                kind: Kind = "number"
            elif op in COMPARE:
                if op in ("=", "<>"):
                    if lt.kind != rt.kind:
                        raise FormulaError("operand_kinds", op=op, left=lt.kind, right=rt.kind)
                elif lt.kind.startswith("member:") or rt.kind.startswith("member:"):
                    # メンバーの大小は、順序付きの軸（時間など）で、並び順で比べる
                    if lt.kind != rt.kind:
                        raise FormulaError("operand_kinds", op=op, left=lt.kind, right=rt.kind)
                    d = lt.kind.removeprefix("member:")
                    if not cat.dimension(d).ordered:
                        raise FormulaError("unordered_compare", op=op, dim=d)
                else:
                    _need(lt, "number", left_)
                    _need(rt, "number", right_)
                kind = "boolean"
            elif op in LOGIC:
                _need(lt, "boolean", left_)
                _need(rt, "boolean", right_)
                kind = "boolean"
            else:
                raise FormulaError("unknown_op", op=op)
            if op in {"+", "-"} | LOGIC:  # 片側だけのセルも結果に残る演算
                _check_expand(warnings, left_, dims, lt.dims, lt.dims)
                _check_expand(warnings, right_, dims, rt.dims, rt.dims)
            return Type(dims, kind)

        case Not(child):
            t = infer(child, cat, warnings)
            _need(t, "boolean", msg("not"))
            return t

        case If(cond, then, else_):
            ct = infer(cond, cat, warnings)
            _need(ct, "boolean", msg("if_cond"))
            branches = [(msg("if_then"), infer(then, cat, warnings))]
            if else_ is not None:
                branches.append((msg("if_else"), infer(else_, cat, warnings)))
            if len({t.kind for _, t in branches}) > 1:
                raise FormulaError("if_kinds", kinds=[t.kind for _, t in branches])
            dims = _merge(ct.dims, *(t.dims for _, t in branches))
            for what, t in branches:
                _check_expand(warnings, what, dims, _merge(ct.dims, t.dims), t.dims)
            return Type(dims, branches[0][1].kind)

        case Filter(child, cond):
            t, ct = infer(child, cat, warnings), infer(cond, cat, warnings)
            _need(ct, "boolean", msg("filter_cond"))
            if extra := [d for d in ct.dims if d not in t.dims]:
                raise FormulaError("filter_dims", extra=extra)
            return t

        case Expand(child, dims):
            t = infer(child, cat, warnings)
            for d in dims:
                cat.dimension(d)
                if d in t.dims:
                    raise FormulaError("expand_present", dim=d)
            if len(set(dims)) != len(dims):
                raise FormulaError("expand_repeated", dims=list(dims))
            return Type(t.dims + tuple(dims), t.kind)

        case On(child, other):
            t, ot = infer(child, cat, warnings), infer(other, cat, warnings)
            return Type(_merge(t.dims, ot.dims), t.kind)

        case Coalesce(first, second):
            ft, st = infer(first, cat, warnings), infer(second, cat, warnings)
            if set(ft.dims) != set(st.dims) or ft.kind != st.kind:
                raise FormulaError("coalesce_mismatch", first_dims=ft.dims, first_kind=ft.kind,
                                   second_dims=st.dims, second_kind=st.kind)
            return st

        case IsBlank(child):
            t = infer(child, cat, warnings)
            if t.dims:
                warnings.append(msg("isblank_dense", dims=list(t.dims)))
            return Type(t.dims, "boolean")

        case By(child, dim, prop, agg):
            t = infer(child, cat, warnings)
            target, _ = _property(cat, dim, prop)
            what = msg("by", dim=dim, prop=prop)
            if dim in t.dims:
                if target in t.dims:
                    raise FormulaError("by_target_present", what=what, target=target)
                kind = _agg_kind(agg or "sum", t, what)
                return Type(_replace(t.dims, dim, target), kind)
            if target in t.dims:
                if agg is not None:
                    raise FormulaError("by_lookup_agg", what=what)
                return Type(_replace(t.dims, target, dim), t.kind)
            raise FormulaError("by_no_dims", what=what, dims=t.dims, dim=dim, target=target)

        case Remove(child, dim, agg):
            t = infer(child, cat, warnings)
            what = msg("remove", dim=dim)
            if dim not in t.dims:
                raise FormulaError("remove_absent", what=what, dims=t.dims)
            kind = _agg_kind(agg, t, what)
            return Type(tuple(x for x in t.dims if x != dim), kind)

        case Shift(child, dim, _):
            t = infer(child, cat, warnings)
            if dim not in t.dims:
                raise FormulaError("previous_absent", dim=dim, dims=t.dims)
            if not cat.dimension(dim).ordered:
                raise FormulaError("previous_unordered", dim=dim)
            return t

        case AsAxis(child, dim):
            t = infer(child, cat, warnings)
            _need(t, member_kind(dim), msg("edges"))
            return Type(t.dims + (dim,), "number")

        case Select(child, dim, member):
            t = infer(child, cat, warnings)
            if dim not in t.dims:
                raise FormulaError("select_absent", dim=dim, member=member, dims=t.dims)
            if member not in cat.dimension(dim)._by_id:
                raise FormulaError("select_member", dim=dim, member=member)
            return Type(tuple(d for d in t.dims if d != dim), t.kind)

        case IfBlank(child, value):
            t = infer(child, cat, warnings)
            vkind = "boolean" if isinstance(value, bool) else "number"
            if vkind != t.kind:
                raise FormulaError("ifblank_kind", kind=t.kind)
            if t.dims:
                warnings.append(msg("ifblank_dense", dims=list(t.dims)))
            return t
    raise TypeError(expr)


# ---------------------------------------------------------------- セル数の見積もり

def combos(cat: Catalog, dims) -> float:
    """dims の全組み合わせの数（dims の順に掛ける。Rust と同じ順にして、丸めを揃える）。"""
    n = 1.0
    for d in dims:
        n *= float(len(cat.dimension(d).members))
    return n


def estimate(expr: Expr, cat: Catalog, cells: Mapping[str, float]) -> tuple[tuple[str, ...], float]:
    """型を決めた式の結果の軸と、セル数の上限の見積もり。cells は Metric ごとのセル数。

    値のあるセルだけが結果に残る演算は小さい側で、片側だけでも残る演算は和で、全組み合わせに
    値を作る演算は軸の大きさの積で見積もり、どれも結果の軸の全組み合わせで頭打ちにする。
    意味は Rust（check.rs の estimate）と同じ。
    """
    def est(e: Expr) -> tuple[tuple[str, ...], float]:
        return estimate(e, cat, cells)

    def spread(dims, own) -> float:  # own の値が、dims のうち own にない軸の方向へ複製される倍率
        return combos(cat, [d for d in dims if d not in own])

    match expr:
        case Ref(name):
            dims, n = cat.metric_type(name).dims, float(cells[name])
        case Const() | Member():
            dims, n = (), 1.0
        case DimRef(dim):
            dims, n = (dim,), combos(cat, (dim,))
        case BinOp(op, left, right):
            (ld, ln), (rd, rn) = est(left), est(right)
            dims = _merge(ld, rd)
            a, b = ln * spread(dims, ld), rn * spread(dims, rd)
            n = a + b if op in {"+", "-"} | LOGIC else min(a, b)
        case Not(child) | Shift(child, _, _):
            dims, n = est(child)
        case If(cond, then, else_):
            cd, cn = est(cond)
            branches = [est(then)] + ([est(else_)] if else_ is not None else [])
            dims = _merge(cd, *(bd for bd, _ in branches))
            n = 0.0
            for bd, bn in branches:
                part = _merge(cd, bd)
                n += min(cn * spread(part, cd), bn * spread(part, bd)) * spread(dims, part)
        case Filter(child, cond):
            (dims, n), (cd, cn) = est(child), est(cond)
            n = min(n, cn * spread(dims, cd))
        case On(child, other):
            (d, n), (od, on) = est(child), est(other)
            dims = _merge(d, od)
            n = min(n * spread(dims, d), on * spread(dims, od))
        case Expand(child, ds):
            d, n = est(child)
            dims, n = _merge(d, ds), n * combos(cat, ds)
        case IsBlank(child) | IfBlank(child, _):
            dims, _ = est(child)
            n = combos(cat, dims)
        case By(child, dim, prop, _):
            d, n = est(child)
            target, _ = _property(cat, dim, prop)
            if dim in d:  # 集約
                dims = _replace(d, dim, target)
            else:  # 引き下ろし: target の値が、対応する dim の各メンバーへ配られる
                dims, n = _replace(d, target, dim), n * combos(cat, (dim,))
        case Remove(child, dim, _) | Select(child, dim, _):
            d, n = est(child)
            dims = tuple(x for x in d if x != dim)
        case AsAxis(child, dim):
            d, n = est(child)
            dims = d + (dim,)
        case Coalesce(first, second):
            (_, fn), (dims, sn) = est(first), est(second)
            n = fn + sn
        case _:
            raise TypeError(expr)
    return dims, min(n, combos(cat, dims))


# ---------------------------------------------------------------- 名前の解決

def bind(expr: Expr, cat, names: Mapping[str, str] | None = None) -> Expr:
    """Change the names of a parsed formula to ids: a Metric or dimension Ref, the dimension of Member, Select,
    Expand, By, Remove, Shift and AsAxis, the member of Member and Select, and By.prop (a property id, or a
    Metric id when it names a Metric).

    names gives more name -> id pairs (the Metric that the formula defines). A field that already holds an id
    stays. An unknown name raises FormulaError (unknown_metric, unknown_dim, unknown_member, select_member,
    no_property_or_metric) with the name that the user wrote.
    """
    def id_of(name: str) -> str | None:
        if names and name in names:
            return names[name]
        return cat._metric_ids.get(name)

    def dim(name: str) -> str:
        return cat.dimension(name).id

    def go(e: Expr) -> Expr:
        if isinstance(e, Ref):
            if e.name in cat.metrics or e.name in cat.dimensions:
                return e
            if (id := id_of(e.name)) is not None:
                return Ref(id)
            if e.name in cat._dim_ids:
                return Ref(cat._dim_ids[e.name])
            raise FormulaError("unknown_metric", name=e.name)
        changes = {f.name: r for f in fields(e)
                   if isinstance(v := getattr(e, f.name), Expr) and (r := go(v)) is not v}
        if isinstance(e, Expand):
            dims = tuple(dim(d) for d in e.dims)
            if dims != e.dims:
                changes["dims"] = dims
        elif isinstance(e, (Member, Select, By, Remove, Shift, AsAxis)):
            if (d := dim(e.dim)) != e.dim:
                changes["dim"] = d
            if isinstance(e, (Member, Select)):
                member = cat.dimensions[d].find_member(e.member)
                if member is None:
                    code = "unknown_member" if isinstance(e, Member) else "select_member"
                    raise FormulaError(code, dim=e.dim, member=e.member)
                if member != e.member:
                    changes["member"] = member
            if isinstance(e, By):
                props = cat.dimensions[d]
                prop = props.find_prop(e.prop)
                if prop is None and e.prop not in cat.metrics:
                    prop = id_of(e.prop)
                    if prop is None:
                        raise FormulaError("no_property_or_metric", dim=e.dim, prop=e.prop)
                if prop is not None and prop != e.prop:
                    changes["prop"] = prop
        return replace(e, **changes) if changes else e
    return go(expr)


def resolve(expr: Expr, cat, by_metric: bool = True) -> Expr:
    """式を評価できる形に直す。変わらなければ同じオブジェクトを返す
    （エンジンが式の変換結果を同一性でキャッシュしているため）。

    - 軸の名前を指す Ref を DimRef（各セルのメンバー）にする
    - `X[BY agg: Employee.DeptOf]` で DeptOf がプロパティではなくメンバー型の Metric なら、
      対応表（AsAxis）との結合と集計に書き換える。月ごとに変わる所属のような、時間で変わる階層を扱える
      （by_metric=False なら書き換えず、エンジンの型検査に任せる。Rust の check.rs が同じ書き換えをする）
    """
    if isinstance(expr, Ref):
        return DimRef(expr.name) if expr.name in cat.dimensions else expr
    changes = {}
    for f in fields(expr):
        v = getattr(expr, f.name)
        if isinstance(v, Expr):
            r = resolve(v, cat, by_metric)
            if r is not v:
                changes[f.name] = r
    out = replace(expr, **changes) if changes else expr
    if by_metric and isinstance(out, By) and out.prop not in cat.dimension(out.dim).properties:
        return _by_metric(out, cat)
    return out


def _by_metric(e: By, cat) -> Expr:
    """`child[BY agg: D.V]`（V はメンバー型の Metric）を、既存の演算の組み合わせにする。

    V の各セル（例: 社員 e・月 m）は、そのときの D のメンバー e の所属先 t を持つ。
    AsAxis(V, T) はそれを「(e, m, t) の位置に 1 がある表」にしたもので、
      集約:     child ⋈ 対応表 を D について集計する   -> D が T に置き換わる
      引き下ろし: child ⋈ 対応表 から T を外す（各行の T は 1 つ） -> T が D（と V の軸）に置き換わる
    """
    if e.prop not in getattr(cat, "metrics", {}):
        raise FormulaError("no_property_or_metric", dim=e.dim, prop=e.prop)
    what = msg("by", dim=e.dim, prop=e.prop)
    vt = cat.metric_type(e.prop)
    if not vt.kind.startswith("member:"):
        raise FormulaError("by_not_member", what=what, prop=e.prop, kind=vt.kind)
    target = vt.kind.removeprefix("member:")
    if e.dim not in vt.dims:
        raise FormulaError("by_metric_no_dim", what=what, prop=e.prop, dims=vt.dims, dim=e.dim)
    ct = infer(e.child, cat, [])
    edges = AsAxis(Ref(e.prop), target)
    if e.dim in ct.dims:
        if target in ct.dims:
            raise FormulaError("by_target_present", what=what, target=target)
        if missing := [d for d in vt.dims if d not in ct.dims]:
            raise FormulaError("by_metric_missing", what=what, prop=e.prop, missing=missing)
        return Remove(On(e.child, edges), e.dim, e.agg or "sum")
    if target in ct.dims:
        if e.agg is not None:
            raise FormulaError("by_lookup_agg", what=what)
        return Remove(On(e.child, edges), target, "first")
    raise FormulaError("by_no_dims", what=what, dims=ct.dims, dim=e.dim, target=target)


# ---------------------------------------------------------------- 依存関係

@dataclass(frozen=True)
class Edge:
    """Metric -> 参照先 Metric。lags は参照経路上での各軸方向のずらし量の合計。"""
    target: str
    lags: tuple[tuple[str, int], ...]
    broken: frozenset[str]  # 経路上で集約・付け替えされた軸（ずらし量を信用できない）

    def lag(self, dim: str) -> int:
        return dict(self.lags).get(dim, 0)


def collect_refs(expr: Expr, cat: Catalog,
                 lags: dict[str, int] | None = None,
                 broken: frozenset[str] = frozenset()) -> Iterator[Edge]:
    lags = lags or {}
    match expr:
        case Ref(name):
            yield Edge(name, tuple(sorted(lags.items())), broken)
        case Const() | DimRef() | Member():
            return
        case BinOp(_, left, right):
            yield from collect_refs(left, cat, lags, broken)
            yield from collect_refs(right, cat, lags, broken)
        case If(cond, then, else_):
            for e in (cond, then, else_):
                if e is not None:
                    yield from collect_refs(e, cat, lags, broken)
        case Filter(child, cond) | On(child, cond) | Coalesce(child, cond):
            yield from collect_refs(child, cat, lags, broken)
            yield from collect_refs(cond, cat, lags, broken)
        case Not(child) | IsBlank(child) | IfBlank(child, _) | Expand(child, _) | AsAxis(child, _):
            yield from collect_refs(child, cat, lags, broken)
        case Shift(child, dim, n):
            yield from collect_refs(child, cat, {**lags, dim: lags.get(dim, 0) + n}, broken)
        case Remove(child, dim, _) | Select(child, dim, _):
            yield from collect_refs(child, cat, lags, broken | {dim})
        case By(child, dim, prop, _):
            target, _ = _property(cat, dim, prop)
            yield from collect_refs(child, cat, lags, broken | {dim, target})


# ---------------------------------------------------------------- 影響範囲

# 影響範囲（Region）は Restrict と同じ形で表す。キーにない軸は「全メンバー」を意味し、
# {} は Metric 全体。None は「影響なし」。軸ごとの集合の直積なので、離れた 2 セルの変更は
# それらを囲む箱に広がる（過大評価だが正しい）。


def union_region(a: Restrict | None, b: Restrict | None) -> Restrict | None:
    if a is None:
        return b
    if b is None:
        return a
    return {d: a[d] | b[d] for d in a.keys() & b.keys()}


def _nonempty(region: Restrict) -> Restrict | None:
    return None if any(not ms for ms in region.values()) else region


def affected(expr: Expr, cat: Catalog, changed: dict[str, Restrict],
             added: dict[str, frozenset[str]] | None = None,
             removed: dict[str, str] | None = None) -> Restrict | None:
    """changed（Metric 名 -> 変更範囲）のもとで、式の結果のうち値が変わりうる範囲を返す。

    added（軸名 -> 追加したメンバー）を渡すと、全メンバーへ値を広げる演算（+ - AND OR の展開、
    IF の分岐の展開、EXPAND、IFBLANK / ISBLANK、引き下ろし、前月参照）が新しいメンバーに
    作るセルの範囲も含める。新しいメンバーはどの Metric でも空なので、それ以外の演算は影響しない。

    removed（軸名 -> これから消すメンバー）を渡すと、前月参照の読み先が変わる時点も含める
    （Feb を消すと、Mar の前月は Feb から Jan になる）。消すメンバーそのもののセルは、
    changed と added に入れて伝える。
    """
    af = lambda e: affected(e, cat, changed, added, removed)

    def grow(r: Restrict | None, dims) -> Restrict | None:
        """dims のうちメンバーを追加した軸について、新しいメンバーの範囲を足す。"""
        for d in dims:
            if added and d in added:
                r = union_region(r, {d: added[d]})
        return r

    match expr:
        case Ref(name):
            return changed.get(name)
        case Const() | Member():
            return None
        case DimRef(dim):
            return grow(None, [dim])  # 追加したメンバーのセルが増える
        case BinOp(op, left, right):
            r = union_region(af(left), af(right))
            if added and op in ("+", "-", "and", "or"):
                ld, rd = infer(left, cat, []).dims, infer(right, cat, []).dims
                r = grow(r, [d for d in _merge(ld, rd) if d not in ld or d not in rd])
            return r
        case Filter(left, right) | On(left, right) | Coalesce(left, right):
            return union_region(af(left), af(right))
        case If(cond, then, else_):
            r = union_region(af(cond), af(then))
            if else_ is not None:
                r = union_region(r, af(else_))
            if added:
                cd = infer(cond, cat, []).dims
                branches = [infer(b, cat, []).dims for b in (then, else_) if b is not None]
                dims = _merge(cd, *branches)
                r = grow(r, [d for d in dims if any(d not in _merge(cd, b) for b in branches)])
            return r
        case Not(child) | AsAxis(child, _):
            return af(child)
        case IsBlank(child) | IfBlank(child, _):
            r = af(child)
            return grow(r, infer(child, cat, []).dims) if added else r
        case Expand(child, dims):
            return grow(af(child), dims)
        case Remove(child, dim, _):
            r = af(child)
            return None if r is None else _without(r, dim)
        case Select(child, dim, member):
            r = af(child)
            if r is None or (dim in r and member not in r[dim]):
                return None  # 変更が選んだメンバーに届かない
            return _without(r, dim)
        case Shift(child, dim, n):
            r = af(child)
            if r is not None and dim in r:
                d = cat.dimension(dim)
                r = _nonempty({**r, dim: frozenset(t for m in r[dim] if (t := d.offset(m, n)) is not None)})
            r = grow(r, [dim])  # 末尾に足した時点には、ずらした値が入りうる
            if removed and dim in removed:  # the periods that start to read across the removed member
                d = cat.dimension(dim)
                p = d._by_id[removed[dim]]
                span = range(p + 1, p + n + 1) if n > 0 else range(p + n, p)
                shifted = frozenset(d.ids[q] for q in span if 0 <= q < len(d.members))
                if shifted:
                    r = union_region(r, {dim: shifted})
            return r
        case By(child, dim, prop, _):
            r = af(child)
            aggregate = dim in infer(child, cat, []).dims
            if r is not None:
                target, mapping = _property(cat, dim, prop)
                out = _without(r, dim, target)
                if aggregate:  # 集約: 変わった社員の部署が変わる
                    if dim in r:
                        out[target] = frozenset(mapping[m] for m in r[dim] if m in mapping)
                elif target in r:  # 引き下ろし: 変わった部署に属する社員が変わる
                    out[dim] = frozenset(m for m, t in mapping.items() if t in r[target])
                r = _nonempty(out)
            return r if aggregate else grow(r, [dim])  # 新しい社員にも部署の値が配られる
    raise TypeError(expr)


def inside(key: tuple, dims: tuple[str, ...], region: Restrict) -> bool:
    return all(key[i] in region[d] for i, d in enumerate(dims) if d in region)


# ---------------------------------------------------------------- 評価

def _div(a: float, b: float) -> float | None:
    return None if b == 0 else a / b  # 0 除算は空


def _and(a: bool | None, b: bool | None) -> bool | None:
    if a is False or b is False:
        return False
    return None if a is None or b is None else True


def _or(a: bool | None, b: bool | None) -> bool | None:
    if a is True or b is True:
        return True
    return None if a is None or b is None else False


OPS = {
    "+": operator.add, "-": operator.sub, "*": operator.mul, "/": _div,
    "=": operator.eq, "<>": operator.ne,
    "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
    "and": _and, "or": _or,
}


def _members(cat: Catalog, dim: str, restrict: Restrict | None) -> list[str]:
    """The member ids of dim in number order, only those in restrict."""
    members = cat.dimension(dim).ids
    if restrict and dim in restrict:
        return [m for m in members if m in restrict[dim]]
    return members


def _filter(cube: Cube, restrict: Restrict | None) -> Cube:
    if not restrict:
        return cube
    checks = [(i, restrict[d]) for i, d in enumerate(cube.dims) if d in restrict]
    if not checks:
        return cube
    return Cube(cube.dims, {k: v for k, v in cube.cells.items()
                            if all(k[i] in s for i, s in checks)})


def _without(restrict: Restrict | None, *dims: str) -> Restrict:
    return {k: v for k, v in (restrict or {}).items() if k not in dims}


def _where(cube: Cube, flag: bool) -> Cube:
    return Cube(cube.dims, {k: v for k, v in cube.cells.items() if v is flag})


def _intersect(l: Cube, r: Cube, fn) -> Cube:
    """INNER JOIN。両側に値があるセルだけ結果を持つ。fn が None を返したセルは空。"""
    shared = [d for d in l.dims if d in r.dims]
    li = [l.dims.index(d) for d in shared]
    ri = [r.dims.index(d) for d in shared]
    r_extra = [i for i, d in enumerate(r.dims) if d not in l.dims]
    index: dict[tuple, list] = defaultdict(list)
    for rk, rv in r.cells.items():
        index[tuple(rk[i] for i in ri)].append((tuple(rk[i] for i in r_extra), rv))
    cells = {}
    for lk, lv in l.cells.items():
        for extra, rv in index.get(tuple(lk[i] for i in li), ()):
            v = fn(lv, rv)
            if v is not None:
                cells[lk + extra] = v
    return Cube(l.dims + tuple(r.dims[i] for i in r_extra), cells)


def _expand(cube: Cube, dims: tuple[str, ...], cat: Catalog, restrict: Restrict | None) -> Cube:
    """足りない軸の全メンバーへ値を複製する。"""
    missing = tuple(d for d in dims if d not in cube.dims)
    if not missing:
        return cube.reorder(dims)
    spans = [_members(cat, d, restrict) for d in missing]
    cells = {k + combo: v for k, v in cube.cells.items() for combo in product(*spans)}
    return Cube(cube.dims + missing, cells).reorder(dims)


def _union(l: Cube, r: Cube, fn, blank) -> Cube:
    """FULL OUTER JOIN。片側が空のセルは blank を渡して fn を呼ぶ。両側とも空なら空のまま。"""
    cells = {}
    for k in l.cells.keys() | r.cells.keys():
        v = fn(l.cells.get(k, blank), r.cells.get(k, blank))
        if v is not None:
            cells[k] = v
    return Cube(l.dims, cells)


def _dense(dims: tuple[str, ...], cat: Catalog, restrict: Restrict | None):
    return product(*(_members(cat, d, restrict) for d in dims))


def evaluate(expr: Expr, cat: Catalog, restrict: Restrict | None = None) -> Cube:
    match expr:
        case Ref(name):
            return cat.read(name, restrict)

        case Const(value):
            return Cube((), {(): value})

        case DimRef(dim):
            index = cat.dimension(dim)._by_id
            return Cube((dim,), {(m,): float(index[m]) for m in _members(cat, dim, restrict)})

        case Member(dim, member):
            return Cube((), {(): float(cat.dimension(dim)._by_id[member])})

        case BinOp(op, left, right):
            l = evaluate(left, cat, restrict)
            r = evaluate(right, cat, restrict)
            if op in {"*", "/"} | COMPARE:
                return _intersect(l, r, OPS[op])
            dims = _merge(l.dims, r.dims)
            blank = None if op in LOGIC else 0.0
            return _union(_expand(l, dims, cat, restrict), _expand(r, dims, cat, restrict),
                          OPS[op], blank)

        case Not(child):
            c = evaluate(child, cat, restrict)
            return Cube(c.dims, {k: not v for k, v in c.cells.items()})

        case If(cond, then, else_):
            # TRUE のセルは THEN と、FALSE のセルは ELSE と INNER JOIN する。空の条件はどちらにも入らない
            c = evaluate(cond, cat, restrict)
            parts = [_intersect(_where(c, True), evaluate(then, cat, restrict), lambda _, v: v)]
            if else_ is not None:
                parts.append(_intersect(_where(c, False), evaluate(else_, cat, restrict), lambda _, v: v))
            dims = _merge(*(p.dims for p in parts))
            cells = {}
            for p in parts:
                cells.update(_expand(p, dims, cat, restrict).cells)
            return Cube(dims, cells)

        case Filter(child, cond):
            c = evaluate(child, cat, restrict)
            keep = _where(evaluate(cond, cat, restrict), True)
            return _intersect(c, keep, lambda v, _: v)

        case Expand(child, dims):
            c = evaluate(child, cat, restrict)
            return _expand(c, c.dims + dims, cat, restrict)

        case On(child, other):
            return _intersect(evaluate(child, cat, restrict), evaluate(other, cat, restrict),
                              lambda v, _: v)

        case Coalesce(first, second):
            f, sc = evaluate(first, cat, restrict), evaluate(second, cat, restrict)
            return Cube(sc.dims, sc.cells | f.reorder(sc.dims).cells)

        case IsBlank(child):
            c = evaluate(child, cat, restrict)
            return Cube(c.dims, {k: k not in c.cells for k in _dense(c.dims, cat, restrict)})

        case IfBlank(child, value):
            c = evaluate(child, cat, restrict)
            return Cube(c.dims, {k: c.cells.get(k, value) for k in _dense(c.dims, cat, restrict)})

        case By(child, dim, prop, agg):
            target, mapping = _property(cat, dim, prop)
            if dim in infer(child, cat, []).dims:
                # 集約: dim のメンバーを target のメンバーへ寄せて集計する
                sub = _without(restrict, dim, target)
                if restrict and target in restrict:
                    sub[dim] = frozenset(m for m, t in mapping.items() if t in restrict[target])
                c = evaluate(child, cat, sub)
                i = c.dims.index(dim)
                groups: dict[tuple, list] = defaultdict(list)
                for k, v in c.cells.items():
                    if (t := mapping.get(k[i])) is not None:
                        groups[k[:i] + (t,) + k[i + 1:]].append(v)
                fn = AGGREGATORS[agg or "sum"]
                return Cube(_replace(c.dims, dim, target), {k: fn(vs) for k, vs in groups.items()})
            # 引き下ろし: target の値を、そこへ対応する dim の各メンバーへ配る
            srcs = _members(cat, dim, restrict)
            sub = _without(restrict, dim, target)
            if restrict and dim in restrict:
                sub[target] = frozenset(mapping[s] for s in srcs if s in mapping)
            c = evaluate(child, cat, sub)
            i = c.dims.index(target)
            fanout: dict[str, list[str]] = defaultdict(list)
            for s in srcs:
                if s in mapping:
                    fanout[mapping[s]].append(s)
            cells = {k[:i] + (s,) + k[i + 1:]: v
                     for k, v in c.cells.items() for s in fanout.get(k[i], ())}
            return Cube(_replace(c.dims, target, dim), cells)

        case AsAxis(child, dim):
            c = evaluate(child, cat, _without(restrict, dim))
            members = cat.dimension(dim).ids
            cells = {k + (members[int(v)],): 1.0 for k, v in c.cells.items()}
            return _filter(Cube(c.dims + (dim,), cells), restrict)

        case Select(child, dim, member):
            c = evaluate(child, cat, {**(restrict or {}), dim: frozenset([member])})
            i = c.dims.index(dim)
            return Cube(c.dims[:i] + c.dims[i + 1:],
                        {k[:i] + k[i + 1:]: v for k, v in c.cells.items() if k[i] == member})

        case Remove(child, dim, agg):
            c = evaluate(child, cat, _without(restrict, dim))
            i = c.dims.index(dim)
            groups = defaultdict(list)
            for k, v in c.cells.items():
                groups[k[:i] + k[i + 1:]].append(v)
            fn = AGGREGATORS[agg]
            return Cube(c.dims[:i] + c.dims[i + 1:], {k: fn(vs) for k, vs in groups.items()})

        case Shift(child, dim, n):
            d = cat.dimension(dim)
            sub = restrict
            if restrict and dim in restrict:
                src = (d.offset(t, -n) for t in restrict[dim])
                sub = {**restrict, dim: frozenset(s for s in src if s is not None)}
            c = evaluate(child, cat, sub)
            i = c.dims.index(dim)
            cells = {}
            for k, v in c.cells.items():
                if (t := d.offset(k[i], n)) is not None:
                    cells[k[:i] + (t,) + k[i + 1:]] = v
            return _filter(Cube(c.dims, cells), restrict)

    raise TypeError(expr)
