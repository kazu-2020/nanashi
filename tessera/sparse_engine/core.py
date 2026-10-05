"""Dimension と疎な Cube。

Cube は値のあるセルだけを dict に持つ。キーが存在しないセルが「空 (blank)」で、
0 とは区別する。None を値として保存することはない。
"""
from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterable, Mapping

Key = tuple[str, ...]  # the member ids of one cell, in the order of the dimensions


def uuid7() -> str:
    """Make a UUIDv7 (RFC 9562): the Unix time in milliseconds, then random bits. Python 3.12 has no uuid.uuid7."""
    ms = time.time_ns() // 1_000_000
    rand_a, rand_b = secrets.randbits(12), secrets.randbits(62)
    return str(uuid.UUID(int=(ms << 80) | (7 << 76) | (rand_a << 64) | (2 << 62) | rand_b))


def _show(v: float | bool) -> str:
    return ("TRUE" if v else "FALSE") if isinstance(v, bool) else f"{v:g}"


class Dimension:
    """A named axis. Members can be added, removed and renamed later. The dimensions of a Metric are fixed.

    Each member has 3 values. The id (a UUID) is the key of the engine: the restricts, the reference cubes,
    the property maps and the formulas hold it. It does not change with a rename, and a removed id is not used
    again. The number (the position in members and ids) is the value that the engine packs in a cell key. A new
    member gets the last number, and a removal closes up the numbers after it. The name is for people: the
    formula text and the public reads show it. _index (name -> number) is the name index.

    The order (rank) is separate from the numbers. On a dimension without order, an insert (the at of
    add_member) and a move (move_member) change only the rank, not the numbers or the cells. An ordered
    dimension is a time master list: the order has a meaning in the calculation (the previous period,
    comparisons), so its rank is always the number.
    """

    def __init__(self, name: str, members: Iterable[str], *, ordered: bool = False,
                 ids: Iterable[str] | None = None, id: str = ""):
        self.name = name
        self.id = id  # The UUID. It does not change with a rename. It is the key of Model.dimensions
        self.members: list[str] = list(members)
        self.ordered = ordered  # Only an ordered dimension allows prev (a shift in time)
        self._index = {m: i for i, m in enumerate(self.members)}
        if len(self._index) != len(self.members):
            raise ValueError(f"{name}: メンバーが重複している")
        self.set_ids([uuid7() for _ in self.members] if ids is None else ids)
        self._order: list[int] | None = None  # 順位 -> 番号。None なら番号の順のまま
        self._rank: dict[str, int] | None = None  # 名前 -> 順位（必要になったら作る）
        # property id -> (the id of the target Dimension, {member -> target member})
        self.properties: dict[str, tuple[str, dict[str, str]]] = {}
        self.property_names: dict[str, str] = {}  # property id -> name
        self._props: dict[str, str] = {}  # property name -> id (the name index)

    def set_ids(self, ids: Iterable[str]) -> None:
        """Set the member id of each position (when a saved model is read)."""
        ids = list(ids)
        if len(ids) != len(self.members) or len(set(ids)) != len(ids):
            raise ValueError(f"{self.name}: メンバーの ID はメンバーと同じ数で、重複しないこと")
        self.ids = ids
        self._by_id = {i: pos for pos, i in enumerate(ids)}

    def id_of(self, member: str) -> str:
        """The id of the member with this name. ValueError if there is none."""
        if member not in self._index:
            raise ValueError(f"{self.name}: メンバー {member!r} がない")
        return self.ids[self._index[member]]

    def prop_of(self, prop: str) -> str:
        """The id of the property with this name. ValueError if there is none."""
        if prop not in self._props:
            raise ValueError(f"{self.name} にプロパティ {prop} がない")
        return self._props[prop]

    def member_of(self, id: str) -> str:
        """The current name of the member with this id. ValueError for the id of a removed member."""
        if id not in self._by_id:
            raise ValueError(f"{self.name}: ID {id} のメンバーがない")
        return self.members[self._by_id[id]]

    def copy(self) -> Dimension:
        """同じメンバーとプロパティを持つ別の Dimension。対応表の dict は共有する
        （プロパティの変更は set_property_value で新しい dict に置き換えるので、共有しても干渉しない）。"""
        other = Dimension.__new__(Dimension)  # 検査と表の作り直しを省く（トランザクションごとに複製するので）
        other.name, other.id, other.ordered = self.name, self.id, self.ordered
        other.members, other.ids = list(self.members), list(self.ids)
        other._index, other._by_id = dict(self._index), dict(self._by_id)
        other.properties = dict(self.properties)
        other.property_names, other._props = dict(self.property_names), dict(self._props)
        other._order, other._rank = self._order, self._rank  # どちらも書き換えずに作り直すので共有してよい
        return other

    def __contains__(self, member: str) -> bool:
        """Tell if a member has this name."""
        return member in self._index

    def add_property(self, id: str, name: str, target: Dimension, mapping: Mapping[str, str]) -> None:
        """Add the property, or replace its mapping ({member id: member id of target}). The name of a property
        that exists does not change here."""
        for src, dst in mapping.items():
            if src not in self._by_id:
                raise ValueError(f"{self.name}.{name}: 未知のメンバー {src!r}")
            if dst not in target._by_id:
                raise ValueError(f"{self.name}.{name}: {target.name} に {dst!r} がない")
        self.properties[id] = (target.id, dict(mapping))
        if id not in self.property_names:
            self.property_names[id] = name
            self._props[name] = id

    def rename_property(self, id: str, new: str) -> None:
        """Rename the property. Only the name and the name index change."""
        del self._props[self.property_names[id]]
        self.property_names[id] = new
        self._props[new] = id

    def add_member(self, member: str, id: str | None = None, at: int | None = None) -> None:
        """Add the member. Its number is always the last. With at, put it at position at (from 0) of the order
        (the default is the end. An ordered dimension accepts only the end). Without id, make a UUID."""
        if member in self._index:
            raise ValueError(f"{self.name}: メンバー {member!r} はすでにある")
        n = len(self.members)
        at = self._position(at, n + 1)
        if id is None:
            id = uuid7()
        if id in self._by_id:
            raise ValueError(f"{self.name}: ID {id} はすでにある")
        order = self.order() if at < n or self._order is not None else None  # 足す前の並び順
        self._index[member] = n
        self._by_id[id] = n
        self.members.append(member)
        self.ids.append(id)
        if order is not None:
            order.insert(at, n)
            self._set_order(order)

    def move_member(self, member: str, at: int) -> None:
        """Move the member (an id) to position at (from 0) of the order. The number does not change. Only a
        dimension without order."""
        if member not in self._by_id:
            raise ValueError(f"{self.name}: ID {member} のメンバーがない")
        at = self._position(at, len(self.members))
        order = self.order()
        order.remove(self._by_id[member])
        order.insert(at, self._by_id[member])
        self._set_order(order)

    def _position(self, at: int | None, size: int) -> int:
        """並び順の位置 at を検査する（None なら最後）。順序付きの軸では最後しか許さない。"""
        if at is None:
            return size - 1
        if not isinstance(at, int) or isinstance(at, bool) or not 0 <= at < size:
            raise ValueError(f"{self.name}: 並び順の位置は 0 から {size - 1} の整数: {at!r}")
        if self.ordered and at != size - 1:
            raise ValueError(f"{self.name} は順序付きの軸（時系列）なので、並び順を変えられない")
        return at

    def order(self) -> list[int]:
        """並び順に並べたメンバーの番号（新しいリスト）。"""
        return list(range(len(self.members))) if self._order is None else list(self._order)

    def set_order(self, order: Iterable[int]) -> None:
        """並び順を番号の列で設定する（保存したモデルを読み込むとき）。"""
        order = [int(i) for i in order]
        if sorted(order) != list(range(len(self.members))):
            raise ValueError(f"{self.name}: 並び順はすべてのメンバーの番号を 1 回ずつ含むこと")
        if self.ordered and order != list(range(len(self.members))):
            raise ValueError(f"{self.name} は順序付きの軸（時系列）なので、並び順を変えられない")
        self._set_order(order)

    def _set_order(self, order: list[int]) -> None:
        self._order = None if all(i == r for r, i in enumerate(order)) else order
        self._rank = None

    def in_order(self) -> list[str]:
        """並び順に並べたメンバーの名前。"""
        return list(self.members) if self._order is None else [self.members[i] for i in self._order]

    def ranks(self) -> Mapping[str, int]:
        """メンバーの名前 -> 並び順の位置。"""
        if self._order is None:
            return self._index
        if self._rank is None:
            self._rank = {self.members[i]: r for r, i in enumerate(self._order)}
        return self._rank

    def rank_table(self) -> list[int] | None:
        """番号 -> 並び順の位置の表。並び順が番号の順なら None。"""
        if self._order is None:
            return None
        table = [0] * len(self._order)
        for r, i in enumerate(self._order):
            table[i] = r
        return table

    def rename_member(self, id: str, new: str) -> None:
        """Rename the member with this id. Only the name and the name index change: the number, the order, the
        property maps and the cells hold the id."""
        if id not in self._by_id:
            raise ValueError(f"{self.name}: ID {id} のメンバーがない")
        if not isinstance(new, str) or not new:
            raise ValueError(f"{self.name}: メンバー名は空でない文字列: {new!r}")
        if new in self._index:
            raise ValueError(f"{self.name}: メンバー {new!r} はすでにある")
        i = self._by_id[id]
        del self._index[self.members[i]]
        self._index[new] = i
        self.members[i] = new
        self._rank = None  # the name -> rank table has names as keys, so make it again

    def rename_members(self, names: Mapping[str, str]) -> None:
        """Rename many members at one time ({member id: new name}). The names can change places (a replay)."""
        for i, n in names.items():
            self.members[self._by_id[i]] = n
        self._index = {m: i for i, m in enumerate(self.members)}
        if len(self._index) != len(self.members):
            raise ValueError(f"{self.name}: メンバーが重複している")
        self._rank = None  # the name -> rank table has names as keys, so make it again

    def remove_member(self, member: str) -> None:
        """Remove the member with this id. The numbers after it close up by 1. The member also goes from the
        maps of its own properties."""
        if member not in self._by_id:
            raise ValueError(f"{self.name}: ID {member} のメンバーがない")
        pos = self._by_id[member]
        del self.members[pos]
        del self.ids[pos]
        self._index = {m: i for i, m in enumerate(self.members)}
        self._by_id = {i: p for p, i in enumerate(self.ids)}
        if self._order is not None:  # 後ろの番号が詰まるので、並び順の番号も詰める
            self._set_order([i - (i > pos) for i in self._order if i != pos])
        for prop, (target, mapping) in list(self.properties.items()):
            if member in mapping:
                self.properties[prop] = (target, {k: v for k, v in mapping.items() if k != member})

    def set_property_value(self, prop: str, member: str, value: str, target: Dimension) -> None:
        """Set the property prop (an id) of the member (an id) to value (a member id of target). The mapping
        becomes a new dict, so an engine can detect the change of a cached mapping by identity."""
        if prop not in self.properties:
            raise ValueError(f"{self.name} にプロパティ {prop} がない")
        name = self.property_names[prop]
        if member not in self._by_id:
            raise ValueError(f"{self.name}.{name}: 未知のメンバー {member!r}")
        if value not in target._by_id:
            raise ValueError(f"{self.name}.{name}: {target.name} に {value!r} がない")
        target_id, mapping = self.properties[prop]
        self.properties[prop] = (target_id, {**mapping, member: value})

    def offset(self, member: str, n: int) -> str | None:
        """The id of the member n positions after the member with this id (an ordered dimension). None if it is
        out of the range."""
        i = self._by_id[member] + n
        return self.ids[i] if 0 <= i < len(self.members) else None


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
        """order を渡すと、各軸のメンバーの並び順で行を並べる。"""
        ranks = None if order is None else [order[d].ranks() for d in self.dims]

        def sort_key(item):
            if ranks is None:
                return item[0]
            return tuple(r[m] for r, m in zip(ranks, item[0]))
        head = " | ".join(self.dims) or "(scalar)"
        rows = [" | ".join(k) + f" = {_show(v)}" for k, v in sorted(self.cells.items(), key=sort_key)]
        return "\n".join([head, *rows]) if rows else f"{head}\n(空)"
