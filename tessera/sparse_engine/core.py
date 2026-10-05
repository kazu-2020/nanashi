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

    メンバーは番号で扱う（members と ids は番号ごとの名前と ID）。番号はエンジンのキーに詰める値で、
    足したメンバーは末尾の番号になり、メンバーを消すと後ろの番号が詰まる。記録や保存のために変わらない
    ID も持つ。ID は名前を変えても変わらず、消した ID は再利用しない。Model に登録した軸の ID は、Model が
    軸や Metric と重ならないように振る。

    並び順（順位）は番号と別に持つ。順序のない軸は、途中への挿入（add_member の at）と並び替え
    （move_member）で順位だけを変え、番号もセルも変えない。順序付きの軸は時系列のマスターで、
    並び順が計算の意味（前期、大小比較）を持つので、順位はいつも番号と同じにする。
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
        self._order: list[int] | None = None  # 順位 -> 番号。None なら番号の順のまま
        self._rank: dict[str, int] | None = None  # 名前 -> 順位（必要になったら作る）
        # プロパティ名 -> (参照先の Dimension 名, {メンバー -> 参照先メンバー})
        self.properties: dict[str, tuple[str, dict[str, str]]] = {}
        self.property_ids: dict[str, int] = {}  # property name -> handle (Model gives it with _new_id)

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
        other = Dimension.__new__(Dimension)  # 検査と表の作り直しを省く（トランザクションごとに複製するので）
        other.name, other.id, other.ordered = self.name, self.id, self.ordered
        other.members, other.ids = list(self.members), list(self.ids)
        other._index, other._by_id = dict(self._index), dict(self._by_id)
        other.properties = dict(self.properties)
        other.property_ids = dict(self.property_ids)
        other._order, other._rank = self._order, self._rank  # どちらも書き換えずに作り直すので共有してよい
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

    def add_member(self, member: str, id: int | None = None, at: int | None = None) -> None:
        """メンバーを足す。番号はいつも末尾になる。at を渡すと、並び順の at 番目（0 から）に入れる
        （省けば最後。順序付きの軸では最後の時点の次で、途中には入れられない）。
        id を省くと、この軸の中で使ったことのない ID を振る（Model は自分で振った ID を渡す）。"""
        if member in self._index:
            raise ValueError(f"{self.name}: メンバー {member!r} はすでにある")
        n = len(self.members)
        at = self._position(at, n + 1)
        if id is None:
            id = max(self.ids, default=0) + 1
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
        """メンバーを並び順の at 番目（0 から）に移す。番号は変えない。順序のない軸だけ。"""
        if member not in self._index:
            raise ValueError(f"{self.name}: メンバー {member!r} がない")
        at = self._position(at, len(self.members))
        order = self.order()
        order.remove(self._index[member])
        order.insert(at, self._index[member])
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
        self._rank = None  # 名前 -> 順位の表は名前を鍵にするので作り直す
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
        if self._order is not None:  # 後ろの番号が詰まるので、並び順の番号も詰める
            self._set_order([i - (i > pos) for i in self._order if i != pos])
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
        """order を渡すと、各軸のメンバーの並び順で行を並べる。"""
        ranks = None if order is None else [order[d].ranks() for d in self.dims]

        def sort_key(item):
            if ranks is None:
                return item[0]
            return tuple(r[m] for r, m in zip(ranks, item[0]))
        head = " | ".join(self.dims) or "(scalar)"
        rows = [" | ".join(k) + f" = {_show(v)}" for k, v in sorted(self.cells.items(), key=sort_key)]
        return "\n".join([head, *rows]) if rows else f"{head}\n(空)"
