"""差分集計: 集計元の変わった行の差分だけを、既存の集計結果に足し込む。

対象は「1 つの Metric を集計していくだけ」の式。一番内側が SUM か COUNT で、外側がすべて SUM
なら結果は足し算で分解できるので、

    新しい値 = 古い値 + 集計(集計元の変更後の範囲) - 集計(集計元の変更前の範囲)

で更新できる。SUM は「値のあるセルが 1 つもなければ空」なので、差分だけでは 0 と空を
区別できない。そのため各グループの件数を裏で持ち、件数が 0 になったら空にする。
MIN / MAX / AVG は差分に分解できないので対象外（普通に計算し直す）。
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .evaluate import Catalog, infer
from .expr import By, Expr, Ref, Remove


@dataclass(frozen=True)
class DeltaPlan:
    source: str               # 集計元の Metric
    count: Expr | None        # 各グループの件数を求める式。None なら Metric 自身が件数（COUNT の集計）


def plan_for(formula: Expr, cat: Catalog) -> DeltaPlan | None:
    aggs: list[str] = []  # 外側から内側の順
    e = formula
    while isinstance(e, (By, Remove)):
        if isinstance(e, By):
            if e.dim not in infer(e.child, cat, []).dims:
                return None  # 引き下ろし（lookup）は集計ではない
            aggs.append(e.agg or "sum")
        else:
            aggs.append(e.agg)
        e = e.child
    if not aggs or not isinstance(e, Ref):
        return None
    if aggs[-1] not in ("sum", "count") or any(a != "sum" for a in aggs[:-1]):
        return None
    return DeltaPlan(e.name, None if aggs[-1] == "count" else _inner_count(formula))


def _inner_count(e: Expr) -> Expr:
    """一番内側の集計を COUNT に置き換えた式（外側の SUM はそのまま）。"""
    if isinstance(e.child, Ref):
        return replace(e, agg="count")
    return replace(e, child=_inner_count(e.child))


def substitute(e: Expr, old: str, new: str) -> Expr:
    """集計の連なりの中の Ref(old) を Ref(new) に置き換える。"""
    if isinstance(e, Ref):
        return Ref(new) if e.name == old else e
    return replace(e, child=substitute(e.child, old, new))
