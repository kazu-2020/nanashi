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
| IFBLANK / ISBLANK | 全メンバー（密）              |
"""
from __future__ import annotations

import operator
from collections import defaultdict
from dataclasses import dataclass
from itertools import product
from typing import Iterator, Literal, Protocol

from .core import Cube, Dimension
from .expr import (AGGREGATORS, ARITH, COMPARE, LOGIC, BinOp, By, Const, Expand, Expr, Filter,
                   If, IfBlank, IsBlank, Not, On, Ref, Remove, Shift)

Restrict = dict[str, frozenset[str]]
Kind = Literal["number", "boolean"]


class FormulaError(Exception):
    pass


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
        raise FormulaError(f"{dim} にプロパティ {prop} がない")
    return props[prop]


# ---------------------------------------------------------------- 型推論

def _need(t: Type, kind: Kind, what: str) -> None:
    if t.kind != kind:
        raise FormulaError(f"{what} には {kind} が必要だが {t.kind} が渡された")


def _agg_kind(agg: str, t: Type, what: str) -> Kind:
    if agg not in AGGREGATORS:
        raise FormulaError(f"未知の集計関数 {agg}")
    if agg != "count":
        _need(t, "number", f"{what} の {agg}")
    return "number"


def _check_expand(warnings: list[str], what: str, dims: tuple[str, ...],
                  covered: tuple[str, ...], own: tuple[str, ...]) -> None:
    """結果の軸 dims のうち covered にない軸へ、この項の値が複製されるかを調べる。

    項が定数（軸なし）なら警告だけ出して許す。軸を持つ Metric なら、別の軸への暗黙の展開は
    セル数が爆発しうるのでエラーにし、expand か on で意図を書かせる。
    """
    missing = [d for d in dims if d not in covered]
    if not missing:
        return
    if own:
        args = ", ".join(repr(d) for d in missing)
        raise FormulaError(
            f"{what}（軸 {list(own)}）に {missing} 軸がない。全メンバーへ展開するなら "
            f".expand({args})、相手に値があるセルだけなら .on(相手) を使う")
    warnings.append(f"{what}が {missing} 方向に全メンバーへ展開される（密化）")


def infer(expr: Expr, cat: Catalog, warnings: list[str]) -> Type:
    """式の出力の型（軸と値の種類）を返す。密になる演算は warnings に積む。"""
    match expr:
        case Ref(name):
            return cat.metric_type(name)

        case Const(value):
            return Type((), "boolean" if isinstance(value, bool) else "number")

        case BinOp(op, left, right):
            lt, rt = infer(left, cat, warnings), infer(right, cat, warnings)
            dims = _merge(lt.dims, rt.dims)
            if op in ARITH:
                _need(lt, "number", f"'{op}' の左辺")
                _need(rt, "number", f"'{op}' の右辺")
                kind: Kind = "number"
            elif op in COMPARE:
                if op in ("=", "<>"):
                    if lt.kind != rt.kind:
                        raise FormulaError(f"'{op}' の両辺の種類が違う: {lt.kind} と {rt.kind}")
                else:
                    _need(lt, "number", f"'{op}' の左辺")
                    _need(rt, "number", f"'{op}' の右辺")
                kind = "boolean"
            elif op in LOGIC:
                _need(lt, "boolean", f"'{op}' の左辺")
                _need(rt, "boolean", f"'{op}' の右辺")
                kind = "boolean"
            else:
                raise FormulaError(f"未知の演算子 {op}")
            if op in {"+", "-"} | LOGIC:  # 片側だけのセルも結果に残る演算
                _check_expand(warnings, f"'{op}' の左辺", dims, lt.dims, lt.dims)
                _check_expand(warnings, f"'{op}' の右辺", dims, rt.dims, rt.dims)
            return Type(dims, kind)

        case Not(child):
            t = infer(child, cat, warnings)
            _need(t, "boolean", "NOT")
            return t

        case If(cond, then, else_):
            ct = infer(cond, cat, warnings)
            _need(ct, "boolean", "IF の条件")
            branches = [("IF の THEN", infer(then, cat, warnings))]
            if else_ is not None:
                branches.append(("IF の ELSE", infer(else_, cat, warnings)))
            if len({t.kind for _, t in branches}) > 1:
                raise FormulaError(f"IF の THEN と ELSE の種類が違う: {[t.kind for _, t in branches]}")
            dims = _merge(ct.dims, *(t.dims for _, t in branches))
            for what, t in branches:
                _check_expand(warnings, what, dims, _merge(ct.dims, t.dims), t.dims)
            return Type(dims, branches[0][1].kind)

        case Filter(child, cond):
            t, ct = infer(child, cat, warnings), infer(cond, cat, warnings)
            _need(ct, "boolean", "FILTER の条件")
            if extra := [d for d in ct.dims if d not in t.dims]:
                raise FormulaError(f"FILTER の条件が対象にない軸 {extra} を持っている")
            return t

        case Expand(child, dims):
            t = infer(child, cat, warnings)
            for d in dims:
                cat.dimension(d)
                if d in t.dims:
                    raise FormulaError(f"EXPAND {d}: すでに軸にある")
            if len(set(dims)) != len(dims):
                raise FormulaError(f"EXPAND {list(dims)}: 軸が重複している")
            return Type(t.dims + tuple(dims), t.kind)

        case On(child, other):
            t, ot = infer(child, cat, warnings), infer(other, cat, warnings)
            return Type(_merge(t.dims, ot.dims), t.kind)

        case IsBlank(child):
            t = infer(child, cat, warnings)
            if t.dims:
                warnings.append(f"ISBLANK が {list(t.dims)} の全組み合わせに展開される（密化）")
            return Type(t.dims, "boolean")

        case By(child, dim, prop, agg):
            t = infer(child, cat, warnings)
            target, _ = _property(cat, dim, prop)
            if dim in t.dims:
                if target in t.dims:
                    raise FormulaError(f"BY {dim}.{prop}: 集約先の {target} がすでに軸にある")
                kind = _agg_kind(agg or "sum", t, f"BY {dim}.{prop}")
                return Type(_replace(t.dims, dim, target), kind)
            if target in t.dims:
                if agg is not None:
                    raise FormulaError(f"BY {dim}.{prop}: 引き下ろし（lookup）に集計関数は指定できない")
                return Type(_replace(t.dims, target, dim), t.kind)
            raise FormulaError(f"BY {dim}.{prop}: 式の軸 {t.dims} に {dim} も {target} もない")

        case Remove(child, dim, agg):
            t = infer(child, cat, warnings)
            if dim not in t.dims:
                raise FormulaError(f"REMOVE {dim}: 式の軸 {t.dims} にない")
            kind = _agg_kind(agg, t, f"REMOVE {dim}")
            return Type(tuple(x for x in t.dims if x != dim), kind)

        case Shift(child, dim, _):
            t = infer(child, cat, warnings)
            if dim not in t.dims:
                raise FormulaError(f"PREVIOUS {dim}: 式の軸 {t.dims} にない")
            if not cat.dimension(dim).ordered:
                raise FormulaError(f"PREVIOUS {dim}: 順序付きの軸ではない")
            return t

        case IfBlank(child, value):
            t = infer(child, cat, warnings)
            vkind = "boolean" if isinstance(value, bool) else "number"
            if vkind != t.kind:
                raise FormulaError(f"IFBLANK の既定値は {t.kind} でなければならない")
            if t.dims:
                warnings.append(f"IFBLANK が {list(t.dims)} の全組み合わせに展開される（密化）")
            return t
    raise TypeError(expr)


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
        case Const():
            return
        case BinOp(_, left, right):
            yield from collect_refs(left, cat, lags, broken)
            yield from collect_refs(right, cat, lags, broken)
        case If(cond, then, else_):
            for e in (cond, then, else_):
                if e is not None:
                    yield from collect_refs(e, cat, lags, broken)
        case Filter(child, cond) | On(child, cond):
            yield from collect_refs(child, cat, lags, broken)
            yield from collect_refs(cond, cat, lags, broken)
        case Not(child) | IsBlank(child) | IfBlank(child, _) | Expand(child, _):
            yield from collect_refs(child, cat, lags, broken)
        case Shift(child, dim, n):
            yield from collect_refs(child, cat, {**lags, dim: lags.get(dim, 0) + n}, broken)
        case Remove(child, dim, _):
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


def affected(expr: Expr, cat: Catalog, changed: dict[str, Restrict]) -> Restrict | None:
    """changed（Metric 名 -> 変更範囲）のもとで、式の結果のうち値が変わりうる範囲を返す。"""
    match expr:
        case Ref(name):
            return changed.get(name)
        case Const():
            return None
        case BinOp(_, left, right) | Filter(left, right) | On(left, right):
            return union_region(affected(left, cat, changed), affected(right, cat, changed))
        case If(cond, then, else_):
            r = union_region(affected(cond, cat, changed), affected(then, cat, changed))
            return r if else_ is None else union_region(r, affected(else_, cat, changed))
        case Not(child) | IsBlank(child) | IfBlank(child, _) | Expand(child, _):
            return affected(child, cat, changed)
        case Remove(child, dim, _):
            r = affected(child, cat, changed)
            return None if r is None else _without(r, dim)
        case Shift(child, dim, n):
            r = affected(child, cat, changed)
            if r is None or dim not in r:
                return r
            d = cat.dimension(dim)
            return _nonempty({**r, dim: frozenset(t for m in r[dim] if (t := d.offset(m, n)) is not None)})
        case By(child, dim, prop, _):
            r = affected(child, cat, changed)
            if r is None:
                return None
            target, mapping = _property(cat, dim, prop)
            out = _without(r, dim, target)
            if dim in infer(child, cat, []).dims:  # 集約: 変わった社員の部署が変わる
                if dim in r:
                    out[target] = frozenset(mapping[m] for m in r[dim] if m in mapping)
            elif target in r:  # 引き下ろし: 変わった部署に属する社員が変わる
                out[dim] = frozenset(m for m, t in mapping.items() if t in r[target])
            return _nonempty(out)
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
    members = cat.dimension(dim).members
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
