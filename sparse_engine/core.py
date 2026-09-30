"""Dimension と疎な Cube。

Cube は値のあるセルだけを dict に持つ。キーが存在しないセルが「空 (blank)」で、
0 とは区別する。None を値として保存することはない。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

Key = tuple[str, ...]


def _show(v: float | bool) -> str:
    return ("TRUE" if v else "FALSE") if isinstance(v, bool) else f"{v:g}"


class Dimension:
    """名前付きの軸。メンバーは後から増えてよいが、Metric が持つ軸の組は固定。"""

    def __init__(self, name: str, members: Iterable[str], *, ordered: bool = False):
        self.name = name
        self.members: list[str] = list(members)
        self.ordered = ordered  # True の軸だけ prev（時間方向のずらし）を許す
        self._index = {m: i for i, m in enumerate(self.members)}
        if len(self._index) != len(self.members):
            raise ValueError(f"{name}: メンバーが重複している")
        # プロパティ名 -> (参照先の Dimension 名, {メンバー -> 参照先メンバー})
        self.properties: dict[str, tuple[str, dict[str, str]]] = {}

    def __contains__(self, member: str) -> bool:
        return member in self._index

    def add_property(self, prop: str, target: Dimension, mapping: Mapping[str, str]) -> None:
        for src, dst in mapping.items():
            if src not in self:
                raise ValueError(f"{self.name}.{prop}: 未知のメンバー {src!r}")
            if dst not in target:
                raise ValueError(f"{self.name}.{prop}: {target.name} に {dst!r} がない")
        self.properties[prop] = (target.name, dict(mapping))

    def add_member(self, member: str) -> None:
        """末尾にメンバーを足す。順序付きの軸では、最後の時点の次になる。"""
        if member in self._index:
            raise ValueError(f"{self.name}: メンバー {member!r} はすでにある")
        self._index[member] = len(self.members)
        self.members.append(member)

    def set_property_value(self, prop: str, member: str, value: str, target: Dimension) -> None:
        """member のプロパティ prop を value にする。対応表は新しい dict に置き換える
        （エンジンが、キャッシュした対応表の変更を同一性で検知できるように）。"""
        if prop not in self.properties:
            raise ValueError(f"{self.name} にプロパティ {prop} がない")
        if member not in self:
            raise ValueError(f"{self.name}.{prop}: 未知のメンバー {member!r}")
        if value not in target:
            raise ValueError(f"{self.name}.{prop}: {target.name} に {value!r} がない")
        target_name, mapping = self.properties[prop]
        self.properties[prop] = (target_name, {**mapping, member: value})

    def offset(self, member: str, n: int) -> str | None:
        """順序付き軸で n 個先のメンバー。範囲外なら None。"""
        i = self._index[member] + n
        return self.members[i] if 0 <= i < len(self.members) else None


@dataclass
class Cube:
    dims: tuple[str, ...]
    cells: dict[Key, float | bool] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.cells)

    def get(self, **coords: str) -> float | bool | None:
        return self.cells.get(tuple(coords[d] for d in self.dims))

    def reorder(self, dims: tuple[str, ...]) -> Cube:
        if dims == self.dims:
            return self
        idx = [self.dims.index(d) for d in dims]
        return Cube(dims, {tuple(k[i] for i in idx): v for k, v in self.cells.items()})

    def format(self, order: Mapping[str, Dimension] | None = None) -> str:
        """order を渡すと、各軸のメンバー順で行を並べる。"""
        def sort_key(item):
            if order is None:
                return item[0]
            return tuple(order[d]._index[m] for d, m in zip(self.dims, item[0]))
        head = " | ".join(self.dims) or "(scalar)"
        rows = [" | ".join(k) + f" = {_show(v)}" for k, v in sorted(self.cells.items(), key=sort_key)]
        return "\n".join([head, *rows]) if rows else f"{head}\n(空)"
