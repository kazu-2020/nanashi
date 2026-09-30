"""式の誤りと警告の文言。文言はここだけに持つ。

参照実装（evaluate.py、planner.py）も Rust の実装（check.rs、graph.rs）も、誤りを「コードと値」で返し、
文言はこの表から作る。利用者（HTTP の応答など）はコード（FormulaError.code）で誤りを見分けられる。

値は文字列、数、軸の名前の tuple や list（Python の表示のまま埋める）、または文言の断片（Msg）。
書式の指定 `join<区切り>`（例 `{missing:join, }`）は、列を区切りでつなぐ。
"""
from __future__ import annotations

import string
from typing import Any, NamedTuple

MESSAGES: dict[str, str] = {
    # 断片（ほかの文言の {what} などに埋める）
    "left": "'{op}' の左辺",
    "right": "'{op}' の右辺",
    "not": "NOT",
    "if_cond": "IF の条件",
    "if_then": "IF の THEN",
    "if_else": "IF の ELSE",
    "filter_cond": "FILTER の条件",
    "edges": "対応表",
    "by": "BY {dim}.{prop}",
    "remove": "REMOVE {dim}",
    "agg_of": "{what} の {agg}",
    "key_bits": "{dim} {bits} ビット",
    "dense_ops": "。密になる演算: {ops:join、}",

    # 型の誤り
    "need_kind": "{what} には {want} が必要だが {got} が渡された",
    "unknown_agg": "未知の集計関数 {agg}",
    "unknown_op": "未知の演算子 {op}",
    "not_expanded": "{what}（軸 {own}）に {missing} 軸がない。全メンバーへ展開するなら [EXPAND: {missing:join, }]、"
                    "相手に値があるセルだけなら [ON: 相手] を付ける（Python の DSL では .expand / .on）",
    "unknown_member": '{dim}."{member}": {dim} にメンバー {member!r} がない',
    "operand_kinds": "'{op}' の両辺の種類が違う: {left} と {right}",
    "unordered_compare": "'{op}': {dim} は順序付きの軸ではないので大小を比べられない（= と <> は使える）",
    "if_kinds": "IF の THEN と ELSE の種類が違う: {kinds}",
    "filter_dims": "FILTER の条件が対象にない軸 {extra} を持っている",
    "expand_present": "EXPAND {dim}: すでに軸にある",
    "expand_repeated": "EXPAND {dims}: 軸が重複している",
    "coalesce_mismatch": "上書きの軸と種類が式と一致しない: 式は軸 {first_dims} の {first_kind}、上書きは軸 {second_dims} の {second_kind}",
    "ifblank_kind": "IFBLANK の既定値は {kind} でなければならない",
    "no_property": "{dim} にプロパティ {prop} がない",
    "no_property_or_metric": "{dim} にプロパティ {prop} がなく、同じ名前の Metric もない",
    "by_target_present": "{what}: 集約先の {target} がすでに軸にある",
    "by_lookup_agg": "{what}: 引き下ろし（lookup）に集計関数は指定できない",
    "by_no_dims": "{what}: 式の軸 {dims} に {dim} も {target} もない",
    "by_not_member": "{what}: {prop} はメンバー型の Metric ではない（{kind}）",
    "by_metric_no_dim": "{what}: {prop} の軸 {dims} に {dim} がない",
    "by_metric_missing": "{what}: 式が {prop} の軸 {missing} を持っていない",
    "remove_absent": "{what}: 式の軸 {dims} にない",
    "previous_absent": "PREVIOUS {dim}: 式の軸 {dims} にない",
    "previous_unordered": "PREVIOUS {dim}: 順序付きの軸ではない",
    "select_absent": 'SELECT {dim}."{member}": 式の軸 {dims} に {dim} がない',
    "select_member": 'SELECT {dim}."{member}": {dim} にメンバー {member!r} がない',
    "key_too_wide": "式の途中の結果の軸 [{dims:join, }] が 64 ビットのキーに収まらない（{bits:join, }）。"
                    "先に集計して軸を減らしてから組み合わせるか、メンバー数の多い軸を持つ Metric を分ける",

    # Metric の定義の誤り
    "unknown_dim": "未知の軸 {name}",
    "unknown_metric": "未知の Metric {name}",
    "formula_dims": "{metric}: 式の軸 {dims} が宣言した軸 {declared} と一致しない",
    "formula_kind": "{metric}: 式の値は {kind} だが {declared} として宣言されている",
    "too_many_cells": "{metric}: 結果のセル数が最大 {cells:,.0f} と見積もられ、上限 {limit:,} を超える{dense}。"
                      "値のあるセルだけに絞るなら [ON: 相手] か FILTER を使う。上限は Model(max_cells=...) で変えられる",
    "syntax": "{row} 行 {col} 文字目: {detail}\n  {line}\n  {caret}",

    # 循環参照
    "cycle_no_lag": "循環参照: {members}（時間方向のずらしを通らない循環がある）",
    "cycle_scan_dim": "循環参照: {src} が scan 軸 {dim} を持たない",
    "cycle_broken": "循環参照: {src} -> {target} の経路で {dim} を集約・付け替えている",
    "cycle_future": "循環参照: {src} が {target} の未来の値を参照している",
    "cycle_same_time": "循環参照: {members} が同じ時点で循環している",

    # 警告
    "densify": "{what}が {missing} 方向に全メンバーへ展開される（密化）",
    "isblank_dense": "ISBLANK が {dims} の全組み合わせに展開される（密化）",
    "ifblank_dense": "IFBLANK が {dims} の全組み合わせに展開される（密化）",
}


class Msg(NamedTuple):
    """文言の断片。ほかの文言の値として埋めると、その場で文言にする。"""
    code: str
    params: dict[str, Any]


def msg(code: str, **params) -> Msg:
    return Msg(code, params)


class _Formatter(string.Formatter):
    def format_field(self, value, spec):
        if spec.startswith("join"):
            return spec[4:].join(self.format_field(v, "") for v in value)
        if isinstance(value, Msg):
            return render(value.code, value.params)
        return super().format_field(value, spec)


_formatter = _Formatter()


def render(code: str, params: dict[str, Any]) -> str:
    return _formatter.format(MESSAGES[code], **params)


def from_rust(code: str, params: dict[str, Any]) -> Msg:
    """Rust の返したコードと値。値のうち (コード, 値の dict) の組は断片にする。"""
    def value(v):
        if isinstance(v, tuple) and len(v) == 2 and isinstance(v[1], dict):
            return from_rust(*v)
        if isinstance(v, list):
            return [value(x) for x in v]
        return v
    return Msg(code, {k: value(v) for k, v in params.items()})
