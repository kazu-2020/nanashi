"""格納と評価の実装（エンジン）の差し替え口。

Model は依存グラフ、計算計画、影響範囲の伝搬だけを受け持ち、セルの格納と式の評価は
エンジンに任せる。ReferenceEngine は dict を使った参照実装で、他のエンジンの正解になる。
"""
from __future__ import annotations

import os
from typing import Any, Mapping, Protocol

from .core import Cube, Key
from .evaluate import Catalog, Kind, Restrict, _filter, evaluate, inside
from .expr import Expr


class Engine(Protocol):
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
    def fork(self, cat: Catalog) -> Engine:
        """複製したモデル cat 用のエンジン。"""
    def share(self, storage: Any) -> Any:
        """複製したモデルに渡す格納データ。以後の書き込みが互いに影響しないこと。"""
    def size(self, storage: Any) -> int: ...


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

    def size(self, storage: Cube) -> int:
        return len(storage)


def default_engine() -> Engine:
    """環境変数 SPARSE_ENGINE（reference / rust）で既定のエンジンを選ぶ。テストを両方で回すため。"""
    if os.environ.get("SPARSE_ENGINE") == "rust":
        from .rust_engine import RustEngine
        return RustEngine()
    return ReferenceEngine()
