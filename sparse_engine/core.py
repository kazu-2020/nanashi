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
    """名前付きの軸。メンバーは後から増やす、消す、名前を変えることができるが、Metric が持つ軸の組は固定。

    メンバーは並び順の番号（位置）で扱う。位置はメンバーを消すと詰まるので、記録や保存のために
    変わらない ID も持つ（ids は位置ごとの ID）。ID は名前を変えても変わらず、消した ID は再利用しない。
    Model に登録した軸の ID は、Model が軸や Metric と重ならないように振る。
    """

    def __init__(self, name: str, members: Iterable[str], *, ordered: bool = False,
                 ids: Iterable[int] | None = None, id: int = 0):
        self.name = name
        self.id = id
        self.members: list[str] = list(members)
        self.ordered = ordered  # True の軸だけ prev（時間方向のずらし）を許す
        self._index = {m: i for i, m in enumerate(self.members)}
        if len(self._index) != len(self.members):
            raise ValueError(f"{name}: メンバーが重複している")
        self.set_ids(range(1, len(self.members) + 1) if ids is None else ids)
        # プロパティ名 -> (参照先の Dimension 名, {メンバー -> 参照先メンバー})
        self.properties: dict[str, tuple[str, dict[str, str]]] = {}

    def set_ids(self, ids: Iterable[int]) -> None:
        """位置ごとのメンバーの ID を設定する（保存したモデルを読み込むとき）。"""
        ids = [int(i) for i in ids]
        if len(ids) != len(self.members) or len(set(ids)) != len(ids):
            raise ValueError(f"{self.name}: メンバーの ID はメンバーと同じ数で、重複しないこと")
        self.ids = ids
        self._by_id = {i: pos for pos, i in enumerate(ids)}

    def id_of(self, member: str) -> int:
        """メンバーの変わらない ID。"""
        if member not in self._index:
            raise ValueError(f"{self.name}: メンバー {member!r} がない")
        return self.ids[self._index[member]]

    def member_of(self, id: int) -> str:
        """ID のメンバーの今の名前。消したメンバーの ID なら ValueError。"""
        if id not in self._by_id:
            raise ValueError(f"{self.name}: ID {id} のメンバーがない")
        return self.members[self._by_id[id]]

    def copy(self) -> Dimension:
        """同じメンバーとプロパティを持つ別の Dimension。対応表の dict は共有する
        （プロパティの変更は set_property_value で新しい dict に置き換えるので、共有しても干渉しない）。"""
        other = Dimension(self.name, self.members, ordered=self.ordered, ids=self.ids, id=self.id)
        other.properties = dict(self.properties)
        return other

    def __contains__(self, member: str) -> bool:
        return member in self._index

    def add_property(self, prop: str, target: Dimension, mapping: Mapping[str, str]) -> None:
        for src, dst in mapping.items():
            if src not in self:
                raise ValueError(f"{self.name}.{prop}: 未知のメンバー {src!r}")
            if dst not in target:
                raise ValueError(f"{self.name}.{prop}: {target.name} に {dst!r} がない")
        self.properties[prop] = (target.name, dict(mapping))

    def add_member(self, member: str, id: int | None = None) -> None:
        """末尾にメンバーを足す。順序付きの軸では、最後の時点の次になる。
        id を省くと、この軸の中で使ったことのない ID を振る（Model は自分で振った ID を渡す）。"""
        if member in self._index:
            raise ValueError(f"{self.name}: メンバー {member!r} はすでにある")
        if id is None:
            id = max(self.ids, default=0) + 1
        if id in self._by_id:
            raise ValueError(f"{self.name}: ID {id} はすでにある")
        self._index[member] = len(self.members)
        self._by_id[id] = len(self.members)
        self.members.append(member)
        self.ids.append(id)

    def rename_member(self, old: str, new: str) -> None:
        """メンバーの名前を変える。番号（並び順）はそのまま。自分のプロパティの対応表も新しい dict にする。"""
        if old not in self._index:
            raise ValueError(f"{self.name}: メンバー {old!r} がない")
        if not isinstance(new, str) or not new:
            raise ValueError(f"{self.name}: メンバー名は空でない文字列: {new!r}")
        if new in self._index:
            raise ValueError(f"{self.name}: メンバー {new!r} はすでにある")
        i = self._index.pop(old)
        self._index[new] = i
        self.members[i] = new
        for prop, (target, mapping) in list(self.properties.items()):
            if old in mapping:
                self.properties[prop] = (target, {(new if k == old else k): v for k, v in mapping.items()})

    def remove_member(self, member: str) -> None:
        """メンバーを消す。後ろのメンバーの番号は 1 つずつ詰まる。自分のプロパティの対応表からも消す。"""
        if member not in self._index:
            raise ValueError(f"{self.name}: メンバー {member!r} がない")
        pos = self._index[member]
        del self.members[pos]
        del self.ids[pos]
        self._index = {m: i for i, m in enumerate(self.members)}
        self._by_id = {i: p for p, i in enumerate(self.ids)}
        for prop, (target, mapping) in list(self.properties.items()):
            if member in mapping:
                self.properties[prop] = (target, {k: v for k, v in mapping.items() if k != member})

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
