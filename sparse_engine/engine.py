"""格納と評価の実装（エンジン）の差し替え口。

Model は依存グラフ、計算計画、影響範囲の伝搬だけを受け持ち、セルの格納と式の評価は
エンジンに任せる。ReferenceEngine は dict を使った参照実装で、他のエンジンの正解になる。
"""
from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol

from .core import Cube, Key
from .evaluate import Catalog, Kind, Restrict, _filter, evaluate, inside
from .expr import AGGREGATORS, Expr


@dataclass(frozen=True)
class CompiledPlan:
    """Model が作った計算計画のうち、エンジンが差分再計算の段取りに使う部分。

    steps は依存先が先の順の計算の段階、levels は互いに依存しない段階を段ごとにまとめたもの。
    delta は差分集計する Metric とその計画、sources は差分集計で変更前の値が要る Metric。
    delta_exprs(metric, plan) は差分集計の (件数の差分の式, 値の差分の式)。
    """
    steps: list
    levels: list
    delta: dict
    sources: frozenset
    delta_exprs: Callable


class Engine(Protocol):
    """格納と評価の差し替え口。

    必須の口に加えて、次の任意の口を持つエンジンは Model がそれを使う。
    - recalc_changes(plan, stores, counts, cat, changed, added, olds, forced):
        差分再計算の段取りごと引き受ける（Rust）。意味は Model.recalc の Python の経路と同じ。
    - from_arrays(dims, kind, cols, cat, partition):
        軸ごとのメンバー番号の numpy の配列と値の配列から、大量のセルを入れる（numpy はこの口だけが使う）。
    - to_parquet(storage, dims, kind, cat, meta) / from_parquet(data, dims, kind, cat, partition):
        入力 Metric の値を Parquet のバイト列で出し入れする（保存と読み込み）。列は parquet_columns と
        値の列 v（parquet_value の型）。meta はフッターに入れる文字列のキーと値。
    - diff_block(old, new): diff の代わりに、Python のオブジェクトにしない差（長さ、rows()、
        to_block(軸ごとの ID の表, 値の軸の ID の表, 値の種類) を持つ）。old が None なら new の全セル。
    - apply_block(storage, block, dim_ids, value_ids): 記録の変更の塊をまとめて書き込み、格納データを返す。
        今の軸にない ID のセルは飛ばす。
    - key_bits: 1 セルのキーの固定幅（ビット）。Model は軸の組み合わせがこれに収まるか検査する。
    - estimate(expr, cat, cells): 型を決めた式の結果のセル数の見積もり（意味は evaluate.estimate と同じ）。
    - size_hint(storage): 行数の上限。size が行を数え直すエンジン（Rust で差分があるとき）の代わりに、
        セル数の見積もりが使う。
    - memory(storage): 格納データが確保しているメモリ。rows（本体の行数）、base（本体）、delta_rows
        （差分の件数）、delta（差分）、index（索引）。単位はバイト。Model.memory が使う。
    """
    name: str

    def empty(self, dims: tuple[str, ...], kind: Kind, partition: str | None = None,
              cat: Catalog | None = None) -> Any: ...
    def from_cells(self, dims: tuple[str, ...], kind: Kind, cells: Mapping[Key, Any], cat: Catalog,
                   partition: str | None = None) -> Any: ...
    def partition_of(self, storage: Any) -> str | None:
        """格納データの分割軸。分割しないエンジンは None。"""
    def repartition(self, storage: Any, partition: str | None, cat: Catalog) -> Any:
        """分割軸を変えて持ち直す。"""
    def write(self, storage: Any, key: Key, value: Any, cat: Catalog) -> Any: ...
    def dimension_changed(self, cat: Catalog, dim: str, renumbered: bool = False) -> None:
        """軸 dim のメンバーが変わった（プロパティの対応表も変わりうる）ことを知らせる。
        renumbered なら、メンバーを消して後ろのメンバーの番号が詰まった。"""
    def fit(self, storage: Any, cat: Catalog) -> Any:
        """メンバーが増えたあとも格納データが使えるようにする（必要なら詰め直す）。"""
    def region_of_value(self, storage: Any, value: float, cat: Catalog) -> Restrict | None:
        """値が value のセルを囲む範囲。そういうセルがなければ None。"""
    def drop_value(self, storage: Any, value: float, cat: Catalog) -> Any:
        """値が value のセルを消した格納データ。"""
    def rename_member(self, storage: Any, dim: str, old: str, new: str, cat: Catalog) -> Any:
        """軸 dim のメンバー old を new と呼ぶようにした格納データ（番号は変わらない）。"""
    def remove_member(self, storage: Any, dim: str, index: int, member: str, values: bool,
                      cat: Catalog) -> Any:
        """軸 dim のメンバー member（番号 index）のセルを消し、後ろのメンバーの番号を詰めた格納データ。
        values なら値も dim のメンバー番号なので、member を指す値は空にし、後ろの番号を詰める。"""
    def evaluate(self, expr: Expr, cat: Catalog, restrict: Restrict) -> Any: ...
    def evaluate_many(self, items: list[tuple[Expr, Restrict]], cat: Catalog) -> list[Any]:
        """互いに独立な式をまとめて評価する。並列に実行できるエンジンはそうしてよい。"""
    def evaluate_with_count(self, expr: Expr, count_expr: Expr, cat: Catalog,
                            restrict: Restrict) -> tuple[Any, Any]:
        """差分集計する SUM の値と各グループの件数。1 回の集計で求められるエンジンはそうしてよい。"""
    def filter(self, storage: Any, restrict: Restrict | None, cat: Catalog) -> Any:
        """restrict の範囲に絞った格納データ（実体化したもの）を返す。"""
    def view(self, storage: Any, restrict: Restrict | None, cat: Catalog) -> Any:
        """式の評価中に読むための絞り込み。遅延評価のエンジンは実体化しなくてよい。"""
    def reorder(self, storage: Any, dims: tuple[str, ...]) -> Any: ...
    def replace(self, storage: Any, region: Restrict, new: Any, cat: Catalog) -> Any:
        """storage の region 内のセルを new で置き換える。region 外はそのまま残す。"""
    def replace_diff(self, storage: Any, region: Restrict, new: Any, cat: Catalog) -> tuple[Any, Restrict | None]:
        """replace と同じだが、値が実際に変わったセルを囲む範囲も返す（変化なしなら None）。"""
    def to_cube(self, storage: Any, cat: Catalog) -> Cube: ...
    def get(self, storage: Any, key: Key, cat: Catalog) -> Any:
        """1 セルの値（空なら None）。格納データ全体を読まないこと。"""
    def rows(self, storage: Any, restrict: Restrict, cat: Catalog, offset: int = 0,
             limit: int | None = None) -> tuple[list[tuple[Key, Any]], int]:
        """restrict の範囲の行を、宣言した軸の順のメンバー順に並べ、offset 件目から limit 件だけ返す
        （行の列と、範囲の全行数）。"""
    def aggregate(self, storage: Any, dims: tuple[str, ...], keep: tuple[str, ...], agg: str,
                  restrict: Restrict, cat: Catalog) -> Cube:
        """restrict の範囲を keep の軸だけ残して agg で集計した Cube。"""
    def fork(self, cat: Catalog) -> Engine:
        """複製したモデル cat 用のエンジン。"""
    def share(self, storage: Any) -> Any:
        """複製したモデルに渡す格納データ。以後の書き込みが互いに影響しないこと。"""
    def size(self, storage: Any) -> int: ...
    def same(self, a: Any, b: Any) -> bool:
        """a と b が同じ格納データ（複製しただけで、どちらにも書き込んでいない）か。わからなければ False。"""
    def diff(self, old: Any, new: Any) -> list | None:
        """old（変更前）と new（変更後）で値が違うセルの (軸ごとの位置の組, 変更前, 変更後) の列。
        空のセルは None。安く比べられなければ None を返す（呼び出し側が名前で比べる）。"""


class ReferenceEngine:
    name = "reference"

    def empty(self, dims, kind, partition=None, cat=None):
        return Cube(dims)

    def from_cells(self, dims, kind, cells, cat, partition=None):
        return Cube(dims, dict(cells))

    def partition_of(self, storage):
        return None

    def repartition(self, storage, partition, cat):
        return storage

    def dimension_changed(self, cat, dim, renumbered=False):
        pass  # Cube のキーはメンバー名なので、何もしなくてよい

    def rename_member(self, storage: Cube, dim, old, new, cat):
        if dim not in storage.dims:
            return storage
        i = storage.dims.index(dim)
        return Cube(storage.dims, {(k[:i] + (new,) + k[i + 1:] if k[i] == old else k): v
                                   for k, v in storage.cells.items()})

    def region_of_value(self, storage: Cube, value, cat):
        keys = [k for k, v in storage.cells.items() if v == value]
        if not keys:
            return None
        return {d: frozenset(k[i] for k in keys) for i, d in enumerate(storage.dims)}

    def drop_value(self, storage: Cube, value, cat):
        return Cube(storage.dims, {k: v for k, v in storage.cells.items() if v != value})

    def remove_member(self, storage: Cube, dim, index, member, values, cat):
        cells = storage.cells
        if dim in storage.dims:
            i = storage.dims.index(dim)
            cells = {k: v for k, v in cells.items() if k[i] != member}
        if values:
            cells = {k: v - 1.0 if v > index else v for k, v in cells.items() if v != index}
        return Cube(storage.dims, dict(cells))

    def fork(self, cat):
        return ReferenceEngine()

    def share(self, storage: Cube) -> Cube:
        return Cube(storage.dims, dict(storage.cells))  # dict は書き換えるので複製する

    def fit(self, storage, cat):
        return storage

    def write(self, storage: Cube, key, value, cat):
        if value is None:
            storage.cells.pop(key, None)
        else:
            storage.cells[key] = value
        return storage

    def evaluate(self, expr, cat, restrict):
        return evaluate(expr, cat, restrict or None)

    def evaluate_many(self, items, cat):
        return [self.evaluate(expr, cat, restrict) for expr, restrict in items]

    def evaluate_with_count(self, expr, count_expr, cat, restrict):
        return self.evaluate(expr, cat, restrict), self.evaluate(count_expr, cat, restrict)

    def filter(self, storage: Cube, restrict, cat):
        return _filter(storage, restrict)

    view = filter

    def reorder(self, storage: Cube, dims):
        return storage.reorder(dims)

    def replace(self, storage: Cube, region, new: Cube, cat):
        if not region:
            return new
        cells = storage.cells
        for k in [k for k in cells if inside(k, storage.dims, region)]:
            del cells[k]
        cells.update(new.cells)
        return storage

    def replace_diff(self, storage: Cube, region, new: Cube, cat):
        dims = storage.dims
        old = {k: v for k, v in storage.cells.items() if not region or inside(k, dims, region)}
        cells = new.reorder(dims).cells
        changed = [k for k in old.keys() | cells.keys() if old.get(k) != cells.get(k)]
        storage = self.replace(storage, region, new, cat)
        if not changed:
            return storage, None
        return storage, {d: frozenset(k[i] for k in changed) for i, d in enumerate(dims)}

    def to_cube(self, storage, cat):
        return storage

    def get(self, storage: Cube, key, cat):
        return storage.cells.get(key)

    def rows(self, storage: Cube, restrict, cat, offset=0, limit=None):
        cube = _filter(storage, restrict or None)
        order = [cat.dimension(d)._index for d in cube.dims]
        keys = sorted(cube.cells, key=lambda k: tuple(ix[m] for ix, m in zip(order, k)))
        total = len(keys)
        page = keys[offset:] if limit is None else keys[offset:offset + limit]
        return [(k, cube.cells[k]) for k in page], total

    def aggregate(self, storage: Cube, dims, keep, agg, restrict, cat):
        return aggregate_cube(_filter(storage, restrict or None), keep, agg)

    def to_parquet(self, storage: Cube, dims, kind, cat, meta: Mapping[str, str]) -> bytes:
        nanashi_core = native()
        order = [storage.dims.index(d) for d in dims]
        index = [cat.dimension(d)._index for d in dims]
        keys = list(storage.cells)
        cols = [[index[j][k[i]] for k in keys] for j, i in enumerate(order)]
        values = [float(v) for v in storage.cells.values()]
        return nanashi_core.write_parquet(parquet_columns(dims, cat), cols, values, parquet_value(kind),
                                          list(meta.items()))

    def from_parquet(self, data: bytes, dims, kind, cat, partition=None) -> Cube:
        nanashi_core = native()
        members = [cat.dimension(d).members for d in dims]
        cols, values = nanashi_core.read_parquet(data, parquet_columns(dims, cat), parquet_value(kind),
                                                 [len(ms) for ms in members])
        if kind == "boolean":
            values = [v != 0.0 for v in values]
        return Cube(tuple(dims), {tuple(members[j][c[r]] for j, c in enumerate(cols)): values[r]
                                  for r in range(len(values))})

    def size(self, storage: Cube) -> int:
        return len(storage)

    def same(self, a, b) -> bool:
        return a is b

    def diff(self, old, new):
        return None  # Cube はメンバー名で持つので、名前で比べてもらう


def native():
    """nanashi_core。保存と読み込み（Parquet）は、参照実装のエンジンでもこれを使う。"""
    try:
        import nanashi_core
    except ImportError:
        raise ImportError("保存と読み込みには nanashi_core が要る（README の「使い始める」の手順でビルドする）") from None
    return nanashi_core


def parquet_columns(dims, cat) -> list[str]:
    """Parquet の軸の列の名前。名前を変えても変わらない軸の ID で付ける。"""
    return [f"d{cat.dimension(d).id}" for d in dims]


def parquet_value(kind: Kind) -> str:
    """Parquet の値の列の種類（number、boolean、member）。"""
    return "member" if kind.startswith("member:") else kind


def aggregate_cube(cube: Cube, keep: Iterable[str], agg: str) -> Cube:
    """cube を keep の軸だけ残して agg で集計する（参照実装と、エンジンの結果の検査に使う）。"""
    keep = tuple(d for d in cube.dims if d in set(keep))
    idx = [cube.dims.index(d) for d in keep]
    groups: dict[tuple, list] = defaultdict(list)
    for k, v in cube.cells.items():
        groups[tuple(k[i] for i in idx)].append(v)
    fn = AGGREGATORS[agg]
    return Cube(keep, {k: fn(vs) for k, vs in groups.items()})


def default_engine() -> Engine:
    """環境変数 SPARSE_ENGINE（reference / rust）で既定のエンジンを選ぶ。テストを両方で回すため。"""
    if os.environ.get("SPARSE_ENGINE") == "rust":
        from .rust_engine import RustEngine
        return RustEngine()
    return ReferenceEngine()
