"""差分集計: 集計元の変わった行の差分だけを、既存の集計結果に足し込む。

対象は「1 つの Metric を集計していくだけ」の式。途中で SELECT で切り口を取っても、
Metric を使った BY の対応表（On(x, AsAxis(V))）と結合してもよい。一番内側が SUM か COUNT で、
外側がすべて SUM なら結果は集計元について足し算で分解できるので、

    新しい値 = 古い値 + 集計(集計元の変更後の範囲) - 集計(集計元の変更前の範囲)

で更新できる。SUM は「値のあるセルが 1 つもなければ空」なので、差分だけでは 0 と空を
区別できない。そのため各グループの件数を裏で持ち、件数が 0 になったら空にする。
MIN / MAX / AVG は差分に分解できないので対象外（普通に計算し直す）。
対応表（aux）も同じ再計算で変わったとき（異動など）は、変わった行について「古い集計元・古い対応表
での寄与」を引き、「新しい集計元・新しい対応表での寄与」を足す。
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace

from .evaluate import Catalog, infer
from .expr import AsAxis, By, Expr, On, Ref, Remove, Select


@dataclass(frozen=True)
class DeltaPlan:
    source: str               # 集計元の Metric
    count: Expr | None        # 各グループの件数を求める式。None なら Metric 自身が件数（COUNT の集計）
    aux: tuple[str, ...] = ()  # 変わっていない前提で読む Metric（Metric を使った BY の対応表）


def plan_for(formula: Expr, cat: Catalog) -> DeltaPlan | None:
    aggs: list[str] = []  # 外側から内側の順
    aux: list[str] = []
    e = formula
    while True:
        if isinstance(e, By):
            if e.dim not in infer(e.child, cat, []).dims:
                return None  # 引き下ろし（lookup）は集計ではない
            aggs.append(e.agg or "sum")
        elif isinstance(e, Remove):
            aggs.append(e.agg)
        elif isinstance(e, On) and isinstance(e.other, AsAxis) and isinstance(e.other.child, Ref):
            aux.append(e.other.child.name)  # 対応表との結合は、集計元について線形
        elif not isinstance(e, Select):  # SELECT は切り口を取り出すだけで、分解を崩さない
            break
        e = e.child
    if not aggs or not isinstance(e, Ref):
        return None
    if aggs[-1] not in ("sum", "count") or any(a != "sum" for a in aggs[:-1]):
        return None
    return DeltaPlan(e.name, None if aggs[-1] == "count" else _inner_count(formula), tuple(aux))


def _has_agg(e: Expr) -> bool:
    return isinstance(e, (By, Remove)) or (isinstance(e, (Select, On)) and _has_agg(e.child))


def _inner_count(e: Expr) -> Expr:
    """一番内側の集計を COUNT に置き換えた式（外側の SUM と SELECT はそのまま）。"""
    if isinstance(e, (By, Remove)) and not _has_agg(e.child):
        return replace(e, agg="count")
    return replace(e, child=_inner_count(e.child))


def rename(e: Expr, names: dict[str, str]) -> Expr:
    """式の中の Ref の名前を names に従って置き換える（集計元と、対応表の AsAxis の中も）。"""
    if isinstance(e, Ref):
        return Ref(names[e.name]) if e.name in names else e
    changes = {f.name: rename(v, names) for f in fields(e) if isinstance(v := getattr(e, f.name), Expr)}
    return replace(e, **changes) if changes else e
