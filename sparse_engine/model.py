"""Model: Metric 単位の依存グラフ、計算計画、スライス単位の差分再計算。

計算計画は依存グラフの強連結成分（SCC）をトポロジカル順に並べたもの。
循環は「全員が同じ順序付き軸を持ち、循環が必ず正のずらし（prev）を通る」場合だけ許し、
その軸に沿った scan として 1 時点ずつ計算する。それ以外の循環はエラーにする。

入力セルを変えると、その座標を影響範囲として計画の順に下流へ伝え、各 Metric は
影響範囲だけを計算し直して差し替える。式や軸の定義を変えたときは全体を計算し直す。

集計だけの Metric（SUM / COUNT）は、集計元の変わった行の差分を足し込んで更新する（delta.py）。

エンジンが Metric を分割して持つ場合、分割軸は Metric ごとに明示するか、自動で選ぶ。
自動では、各入力 Metric の 1 セルを変えたときの影響範囲を伝え、触れるパーティションの
割合が平均で最も小さい軸を選ぶ。
"""
from __future__ import annotations

import dataclasses
import itertools
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import mean
from typing import Any, Mapping

from .core import Cube, Dimension, Key
from .delta import DeltaPlan, plan_for, rename
from .engine import Engine, default_engine
from .evaluate import (Edge, FormulaError, Kind, Restrict, Type, affected, collect_refs, infer,
                       member_kind, resolve, union_region)
from .expr import BinOp, Coalesce, Const, Expr, Filter, Ref, mentions_member, rename_member
from .parser import parse


@dataclass
class Metric:
    name: str
    dims: tuple[str, ...]
    kind: Kind = "number"
    formula: Expr | None = None  # 評価に使う式（計画を作るときに written から解決する）。None なら入力 Metric
    partition: str | None = None  # 明示した分割軸。None なら自動で選ぶ
    written: Expr | None = None  # 利用者が書いた元の式（保存や表示に使う）
    overridable: bool = False  # True なら set_cell で式の結果を手入力で上書きできる

    @property
    def override_name(self) -> str:
        return f"__override__{self.name}"


@dataclass
class Step:
    names: tuple[str, ...]
    scan_dim: str | None = None  # None なら通常の 1 Metric の計算


class SliceLog:
    """再計算した (Metric, 範囲) の記録（観察用）。エンジンがメンバーの番号で返した範囲は、
    読まれたときに名前へ直す（1 回の変更で何百もの範囲を直すと、それだけで時間がかかるため）。"""

    def __init__(self):
        self._items: list[tuple[str, Restrict]] = []
        self._later: list = []  # 名前に直した記録を返す関数

    def _flush(self) -> list[tuple[str, Restrict]]:
        for named in self._later:
            self._items.extend(named())
        self._later.clear()
        return self._items

    def append(self, item: tuple[str, Restrict]) -> None:
        self._flush().append(item)

    def extend_later(self, named) -> None:
        self._later.append(named)

    def clear(self) -> None:
        self._items.clear()
        self._later.clear()

    def __iter__(self):
        return iter(self._flush())

    def __len__(self) -> int:
        return len(self._flush())

    def __getitem__(self, i):
        return self._flush()[i]

    def __eq__(self, other) -> bool:
        return self._flush() == list(other)

    def __repr__(self) -> str:
        return repr(self._flush())


@dataclass
class Model:
    engine: Engine = field(default_factory=default_engine)
    auto_layout: bool = True  # False なら分割軸はエンジンの既定（メンバー数が最も多い軸）
    delta_aggregation: bool = True  # False なら集計も普通に計算し直す
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    layout: dict[str, str | None] = field(default_factory=dict)  # Metric ごとの分割軸
    metrics: dict[str, Metric] = field(default_factory=dict)
    warnings: dict[str, list[str]] = field(default_factory=dict)
    eval_log: list[str] = field(default_factory=list)  # 再計算した Metric 名（観察用）
    slice_log: SliceLog = field(default_factory=lambda: SliceLog())  # 再計算した範囲（観察用）
    delta_log: list[str] = field(default_factory=list)  # 差分集計で更新した Metric（観察用）
    _values: dict[str, Any] = field(default_factory=dict)  # エンジンごとの格納形式
    _plan: list[Step] | None = None
    _levels: list[list[Step]] = field(default_factory=list)  # 依存関係の段ごとの計画（全体の再計算用）
    _full: bool = True  # 次の recalc で全体を計算し直すか
    _changed: dict[str, Restrict] = field(default_factory=dict)  # 入力 Metric の変更範囲
    _reads: dict[str, Restrict] | None = None  # scan の下見で記録する読み出し範囲
    _work: dict[str, Any] = field(default_factory=dict)  # scan 中の読み出し元（切り出し済み）
    _delta: dict[str, DeltaPlan] = field(default_factory=dict)  # 差分集計する Metric -> 計画
    _counts: dict[str, Any] = field(default_factory=dict)  # 差分集計する SUM の各グループの件数
    _old_cells: dict[str, dict[Key, Any]] = field(default_factory=dict)  # 入力の変更前の値
    _old_slices: dict[str, Any] = field(default_factory=dict)  # 範囲ごと空にした入力の、変更前の値
    _added: dict[str, set[str]] = field(default_factory=dict)  # 前回の再計算以降に追加したメンバー
    _temp_types: dict[str, Type] = field(default_factory=dict)  # 差分計算中の一時的な名前の型
    _delta_cache: dict[str, tuple] = field(default_factory=dict)  # Metric -> (計画, 件数の差分の式, 値の差分の式)

    # ------------------------------------------------ Catalog

    def dimension(self, name: str) -> Dimension:
        if name not in self.dimensions:
            raise FormulaError(f"未知の軸 {name}")
        return self.dimensions[name]

    def metric_type(self, name: str) -> Type:
        if name in self._temp_types:
            return self._temp_types[name]
        if name not in self.metrics:
            raise FormulaError(f"未知の Metric {name}")
        m = self.metrics[name]
        return Type(m.dims, m.kind)

    def source(self, name: str) -> Any:
        """name の読み出し元（差分集計の作業データがあればそれ、なければ格納データ）。

        自分で範囲を絞り込めるエンジン（Rust）は、read ではなくこれで読み出し元を受け取る。
        """
        return self._work[name] if name in self._work else self._values[name]

    def read(self, name: str, restrict: Restrict | None) -> Any:
        """name を restrict の範囲に絞って返す（参照実装の評価器が使う）。"""
        return self.engine.view(self.source(name), restrict or None, self)

    def refresh(self) -> None:
        """全体を計算し直す。差分集計を続けてたまった浮動小数点の誤差もなくなる。"""
        self._full = True
        self.recalc()

    # ------------------------------------------------ 複製

    def fork(self) -> Model:
        """このモデルの複製。ホワットイフ分析のように、元を壊さずに入力や上書きを試すのに使う。

        複製は計算済みの状態から始まり、以後の変更（入力、上書き、メンバーの追加）は互いに
        影響しない。Rust のエンジンでは格納データを共有し、書き換えた Metric だけを最初の
        書き込みのときに複製するので、複製そのものは Metric の数に比例する時間で済む。
        """
        self.recalc()
        other = Model(engine=self.engine, auto_layout=self.auto_layout, delta_aggregation=self.delta_aggregation)
        other.dimensions = {n: d.copy() for n, d in self.dimensions.items()}
        other.engine = self.engine.fork(other)
        other.metrics = {n: dataclasses.replace(m) for n, m in self.metrics.items()}
        other._values = {n: self.engine.share(v) for n, v in self._values.items()}
        other._counts = {n: self.engine.share(v) for n, v in self._counts.items()}
        # 計算計画は定義だけに依存するので、そのまま引き継ぐ
        other.layout, other.warnings = dict(self.layout), dict(self.warnings)
        other._plan, other._levels = self._plan, self._levels
        other._delta, other._delta_cache = dict(self._delta), dict(self._delta_cache)
        other._full = False
        return other

    # ------------------------------------------------ 保存と読み込み

    def save(self, path) -> None:
        """定義と入力データをディレクトリ path に保存する（計算 Metric は読み込み後に計算し直す）。"""
        from .storage import save
        save(self, path)

    @classmethod
    def load(cls, path, engine: Engine | None = None) -> Model:
        """save で保存したディレクトリから Model を作る。engine を省略すると既定のエンジン。"""
        from .storage import load
        return load(path, engine)

    # ------------------------------------------------ 定義

    def add_dimension(self, name: str, members, *, ordered: bool = False) -> Dimension:
        if name in self.metrics:
            raise ValueError(f"{name}: 同じ名前の Metric がある（式の中で軸と区別できなくなる）")
        self.dimensions[name] = Dimension(name, members, ordered=ordered)
        return self.dimensions[name]

    def add_property(self, dim: str, prop: str, target: str, mapping: Mapping[str, str]) -> None:
        self.dimension(dim).add_property(prop, self.dimension(target), mapping)
        self._invalidate()

    def add_input(self, name: str, dims, cells: Mapping[Key, float | bool] | None = None,
                  *, kind: Kind = "number", storage: Any = None, partition: str | None = None) -> None:
        """cells は {キー: 値}。大量のデータはエンジンの格納形式で storage に渡してもよい。"""
        self._check_name(name)
        self._check_kind(name, kind)
        dims = tuple(dims)
        for d in dims:
            self.dimension(d)
        self.metrics[name] = Metric(name, dims, kind, partition=self._check_partition(name, dims, partition))
        self._invalidate()
        if storage is not None:
            self._values[name] = storage
            return
        checked = {key: self._check(name, key, value) for key, value in (cells or {}).items()}
        self._values[name] = self.engine.from_cells(dims, kind, {k: v for k, v in checked.items()
                                                                 if v is not None}, self, partition)

    def add_formula(self, name: str, dims, formula: Expr | str, *, kind: Kind = "number",
                    partition: str | None = None, overridable: bool = False) -> None:
        """formula は AST か式の文字列。文字列の構文エラーはここで ParseError になる。

        overridable なら、set_cell で式の結果を手入力で上書きできる。上書きした値は式より優先され、
        下流にもそのまま伝わる。set_cell で None を入れると、そのセルは式の結果に戻る。
        """
        self._check_name(name)
        self._check_kind(name, kind)
        if isinstance(formula, str):
            formula = parse(formula, self_name=name)
        dims = tuple(dims)
        m = Metric(name, dims, kind, formula, self._check_partition(name, dims, partition), formula, overridable)
        self.metrics[name] = m
        if overridable and m.override_name not in self.metrics:  # 読み込みでは上書き値が先に入る
            self.add_input(m.override_name, dims, kind=kind, partition=partition)
        self._invalidate()

    def _check_name(self, name: str) -> None:
        if name in self.dimensions:
            raise ValueError(f"{name}: 同じ名前の軸がある（式の中で軸と区別できなくなる）")

    def _check_kind(self, name: str, kind: Kind) -> None:
        if kind in ("number", "boolean"):
            return
        if kind.startswith("member:") and kind.removeprefix("member:") in self.dimensions:
            return
        raise ValueError(f"{name}: 値の種類は number、boolean、member:<軸名> のいずれか（{kind!r}）")

    @staticmethod
    def _check_partition(name: str, dims: tuple[str, ...], partition: str | None) -> str | None:
        if partition is not None and partition not in dims:
            raise ValueError(f"{name}: 分割軸 {partition} が軸 {dims} にない")
        return partition

    # ------------------------------------------------ 按分

    def spread(self, name: str, total: float, *, how: str = "proportional",
               where: Mapping[str, str] | None = None, **coords: str) -> int:
        """入力 Metric の範囲に、合計が total になるよう値を配る。書き込んだセルの数を返す。

            m.spread("Budget", 12000, Version="予算", Month="m01", where={"Product.Category": "ハード"})

        範囲は、coords で指定した軸はそのメンバー、それ以外の軸は全メンバー（where の
        「軸.プロパティ」が一致するものだけ）。how="proportional" なら、範囲に今ある値の比率で配る。
        今の値の合計が 0 のときや how="even" のときは、今値のあるセルへ均等に配る。
        値のあるセルが 1 つもなければ、範囲の全組み合わせへ均等に配る。
        """
        m = self.metrics[name]
        if m.formula is not None or m.kind != "number":
            raise ValueError(f"{name}: 按分できるのは number の入力 Metric だけ")
        if how not in ("proportional", "even"):
            raise ValueError(f"how は proportional か even（{how!r}）")
        region: dict[str, frozenset[str]] = {}
        for d, member in coords.items():
            if d not in m.dims:
                raise ValueError(f"{name}: 軸 {d} がない")
            if member not in self.dimension(d):
                raise ValueError(f"{name}: {d} にメンバー {member!r} がない")
            region[d] = frozenset([member])
        for path, value in (where or {}).items():
            d, _, prop = path.partition(".")
            if d not in m.dims or prop not in self.dimension(d).properties:
                raise ValueError(f"{name}: where の {path!r} は「軸.プロパティ」ではない")
            mapping = self.dimension(d).properties[prop][1]
            chosen = frozenset(x for x in self.dimension(d).members if mapping.get(x) == value)
            region[d] = region.get(d, chosen) & chosen
        self.recalc()
        current = self.engine.to_cube(self.engine.filter(self._values[name], region or None, self), self)
        cells = dict(current.cells)
        weight = sum(cells.values())
        if cells and how == "proportional" and weight != 0:
            new = {k: total * v / weight for k, v in cells.items()}
        else:
            keys = list(cells) or list(itertools.product(
                *(sorted(region[d], key=self.dimension(d)._index.get) if d in region
                  else self.dimension(d).members for d in m.dims)))
            if not keys:
                raise ValueError(f"{name}: 按分先のセルがない")
            new = {k: total / len(keys) for k in keys}
        for k, v in new.items():
            self.set_cell(name, v, **dict(zip(m.dims, k)))
        return len(new)

    # ------------------------------------------------ メンバーの追加

    def add_member(self, dim: str, member: str, **properties: str) -> None:
        """軸 dim の末尾にメンバーを足す。properties でプロパティの値も設定できる。

            m.add_member("Product", "p2000", Category="c03")

        新しいメンバーはどの Metric でも空で始まる。全メンバーへ値を広げる演算（X + 1、IFBLANK、
        引き下ろし、前月参照など）は新しいメンバーにも値を作るので、次の再計算でその範囲を計算する。
        """
        d = self.dimension(dim)
        for prop, value in properties.items():
            if prop not in d.properties:
                raise ValueError(f"{dim} にプロパティ {prop} がない")
            if value not in self.dimension(d.properties[prop][0]):
                raise ValueError(f"{dim}.{prop}: {d.properties[prop][0]} に {value!r} がない")
        d.add_member(member)
        for prop, value in properties.items():
            d.set_property_value(prop, member, value, self.dimension(d.properties[prop][0]))
        self.engine.dimension_changed(self, dim)
        # メンバーが増えて、格納データのキーに収まらなくなったら詰め直す
        for store in (self._values, self._counts):
            for name in store:
                store[name] = self.engine.fit(store[name], self)
        self._added.setdefault(dim, set()).add(member)

    # ------------------------------------------------ メンバーの名前の変更と削除

    def rename_member(self, dim: str, old: str, new: str) -> None:
        """軸 dim のメンバー old の名前を new にする。

        値も計算結果も変わらない（エンジンの中ではメンバーを番号で持つ）ので、計算し直さない。
        プロパティの対応表、メンバー型の Metric、式の中の `dim."old"` もすべて新しい名前になる。
        """
        d = self.dimension(dim)
        self.recalc()  # 変更範囲はメンバー名で持つので、ためている変更を先に片付ける
        self.slice_log._flush()  # 記録を今の名前で直しておく
        d.rename_member(old, new)
        for other in self.dimensions.values():
            for prop, (target, mapping) in list(other.properties.items()):
                if target == dim and old in mapping.values():
                    other.properties[prop] = (target, {k: new if v == old else v for k, v in mapping.items()})
        self.engine.dimension_changed(self, dim)
        for name, m in self.metrics.items():
            if dim in m.dims:
                self._values[name] = self.engine.rename_member(self._values[name], dim, old, new, self)
                if name in self._counts:
                    self._counts[name] = self.engine.rename_member(self._counts[name], dim, old, new, self)
            if m.written is not None:
                m.written = rename_member(m.written, dim, old, new)
                m.formula = rename_member(m.formula, dim, old, new)
        for name, plan in self._delta.items():
            if plan.count is not None:
                self._delta[name] = dataclasses.replace(plan, count=rename_member(plan.count, dim, old, new))
        self._delta_cache.clear()

    def remove_member(self, dim: str, member: str) -> None:
        """軸 dim からメンバーを消す。

        そのメンバーのセルはすべての Metric から消え、プロパティの対応表からも外れる
        （そのメンバーを参照先にしていたメンバーは、参照先なしになる）。メンバー型の Metric で
        そのメンバーを指していた値は空になる。式が `dim."member"` を書いていれば消せない。

        2 段階で計算し直す。まず、入力のうちそのメンバーのセルと、そのメンバーを指す値を空にして、
        普通の入力の変更として計算し直す（差分集計と、値の変化による絞り込みが効く）。
        次にメンバーそのものを消し、それでも変わるところだけを計算し直す。空になったメンバーを
        消して変わるのは、全メンバーへ値を広げる演算がそのメンバーに作っていたセル（を集計した値）と、
        そのメンバーをまたぐ前月参照だけである。
        """
        d = self.dimension(dim)
        if member not in d:
            raise ValueError(f"{dim}: メンバー {member!r} がない")
        for m in self.metrics.values():
            if m.written is not None and mentions_member(m.written, dim, member):
                raise ValueError(f'{m.name} の式が {dim}."{member}" を参照しているので消せない')
        self.recalc()
        index = d._index[member]
        point = {dim: frozenset([member])}
        values_kind = member_kind(dim)
        eng = self.engine

        def pointing(name: str) -> Restrict | None:
            """name（メンバー型の Metric）で、消すメンバーを指すセルを囲む範囲。"""
            if self.metrics[name].kind != values_kind:
                return None
            return eng.region_of_value(self._values[name], index, self)

        def has_cells(name: str) -> bool:
            return dim in self.metrics[name].dims and eng.size(eng.filter(self._values[name], point, self)) > 0

        # 1. 入力を空にして、普通の変更として計算し直す
        sources = self._delta_sources()
        for name, m in self.metrics.items():
            if m.formula is not None:
                continue
            here, there = has_cells(name), pointing(name)
            r = union_region(point if here else None, there)
            if r is None:
                continue
            if name in sources:
                self._old_slices[name] = eng.filter(self._values[name], r or None, self)
            if here:
                empty = eng.empty(m.dims, m.kind, self.layout.get(name), cat=self)
                self._values[name] = eng.replace(self._values[name], point, empty, self)
            if there is not None:
                self._values[name] = eng.drop_value(self._values[name], index, self)
            self._changed[name] = r
        self.recalc()

        # 2. メンバーを消して変わる範囲を、消す前の軸と対応表のもとで求める。下流に伝えるのは、
        #    そのメンバーのセルが実際にある Metric の消えるセルと、計算し直す範囲だけにする
        todo = self._removal_regions(dim, member, has_cells)
        self.slice_log._flush()  # 記録はメンバーの番号で持っていることがあるので、詰める前に名前へ直す
        d.remove_member(member)
        for other in self.dimensions.values():
            for prop, (target, mapping) in list(other.properties.items()):
                if target == dim and member in mapping.values():
                    other.properties[prop] = (target, {k: v for k, v in mapping.items() if v != member})
        eng.dimension_changed(self, dim, renumbered=True)
        for name, m in self.metrics.items():
            values = m.kind == values_kind
            if dim in m.dims or values:
                self._values[name] = eng.remove_member(self._values[name], dim, index, member, values, self)
            if dim in m.dims and name in self._counts:
                self._counts[name] = eng.remove_member(self._counts[name], dim, index, member, False, self)
        for step in self._plan:
            if step.scan_dim is not None:
                active = {n: todo[n] for n in step.names if n in todo}
                if active:
                    self._scan(step, active, False)
            elif step.names[0] in todo:
                self._recompute(self.metrics[step.names[0]], todo[step.names[0]])

    def _removal_regions(self, dim: str, member: str, has_cells) -> dict[str, Restrict]:
        """入力を空にしたあと、メンバーを消すと値が変わる範囲（計算 Metric -> 消すメンバーを除いた範囲）。

        計算 Metric がそのメンバーを指す値を持つのは、そのメンバーのセル自身（軸の値）か、
        それを前月参照や引き下ろしで運んだセルだけなので、消えるセルからの伝搬で足りる。
        """
        point = frozenset([member])
        added, removed = {dim: point}, {dim: member}

        def surviving(r: Restrict | None) -> Restrict | None:
            if r is None or dim not in r:
                return r
            rest = r[dim] - point
            return {**r, dim: rest} if rest else None

        changes: dict[str, Restrict] = {}  # 下流から見て変わる範囲（消えるセルと、計算し直す範囲）
        todo: dict[str, Restrict] = {}

        def settle(name: str, r: Restrict | None) -> None:
            r = surviving(r)
            if r is not None:
                todo[name] = r
            c = union_region({dim: point} if has_cells(name) else None, r)
            if c is not None:
                changes[name] = c

        for step in self._plan:
            if step.scan_dim is None:
                m = self.metrics[step.names[0]]
                if m.formula is not None:
                    settle(m.name, affected(m.formula, self, changes, added, removed))
                continue
            for n in step.names:  # scan の中の前月参照は、消えるセルからも伝わる
                if has_cells(n):
                    changes[n] = {dim: point}
            scanned = self._scan_regions(step, changes, added, removed)
            for n in step.names:
                changes.pop(n, None)
                settle(n, scanned.get(n))
        return todo

    # ------------------------------------------------ 入力

    def set_cell(self, name: str, value: float | bool | None, **coords: str) -> None:
        m = self.metrics[name]
        if m.formula is not None:
            if not m.overridable:
                raise ValueError(f"{name} は計算 Metric なので直接入力できない"
                                 "（上書きしたいなら add_formula で overridable=True にする）")
            return self.set_cell(m.override_name, value, **coords)
        key = tuple(coords[d] for d in m.dims)
        value = self._check(name, key, value)
        if self._plan is not None and name in self._delta_sources():
            # 差分集計には変更前の値が要る。前回の再計算以降で最初に触れたときの値を覚えておく
            old = self._old_cells.setdefault(name, {})
            if key not in old:
                point = {d: frozenset([member]) for d, member in zip(m.dims, key)}
                cube = self.engine.to_cube(self.engine.filter(self._values[name], point, self), self)
                old[key] = cube.cells.get(key)
        self._values[name] = self.engine.write(self._values[name], key, value, self)
        point = {d: frozenset([member]) for d, member in zip(m.dims, key)}
        self._changed[name] = union_region(self._changed.get(name), point)

    def _check(self, name: str, key: Key, value: float | bool | None) -> float | bool | None:
        """キーと値を検査し、格納する値（None は空）を返す。"""
        m = self.metrics[name]
        if len(key) != len(m.dims):
            raise ValueError(f"{name}: キー {key} の長さが軸 {m.dims} と合わない")
        for d, member in zip(m.dims, key):
            if member not in self.dimension(d):
                raise ValueError(f"{name}: {d} に {member!r} がない")
        if value is None:
            return None
        if m.kind.startswith("member:"):
            d = self.dimension(m.kind.removeprefix("member:"))
            if not isinstance(value, str) or value not in d:
                raise ValueError(f"{name} は {d.name} のメンバーを値に持つ Metric: {value!r}")
            return float(d._index[value])  # エンジンにはメンバーの番号で持たせる
        if m.kind == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"{name} は boolean の Metric: {value!r}")
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} は number の Metric: {value!r}")
        return float(value)

    # ------------------------------------------------ 参照

    def value(self, name: str) -> Cube:
        self.recalc()
        cube = self.engine.to_cube(self._values[name], self)
        kind = self.metrics[name].kind
        if kind.startswith("member:"):  # メンバーの番号を名前に戻す
            members = self.dimension(kind.removeprefix("member:")).members
            cube = Cube(cube.dims, {k: members[int(v)] for k, v in cube.cells.items()})
        return cube

    def raw(self, name: str) -> Any:
        """エンジンの格納形式のまま返す（大きな Metric を Cube に変換しないため）。"""
        self.recalc()
        return self._values[name]

    def get(self, name: str, **coords: str) -> float | None:
        return self.value(name).get(**coords)

    # ------------------------------------------------ 計算計画

    def _invalidate(self) -> None:
        self._plan = None
        self._full = True

    def _compile(self) -> None:
        if self._plan is not None:
            return
        edges: dict[str, list[Edge]] = {}
        self.warnings = {}
        for m in self.metrics.values():
            if m.formula is None:
                edges[m.name] = []
                continue
            m.formula = resolve(m.written, self)  # 軸の名前、Metric を使った BY を評価できる形に
            w: list[str] = []
            t = infer(m.formula, self, w)
            if set(t.dims) != set(m.dims):
                raise FormulaError(f"{m.name}: 式の軸 {t.dims} が宣言した軸 {m.dims} と一致しない")
            if t.kind != m.kind:
                raise FormulaError(f"{m.name}: 式の値は {t.kind} だが {m.kind} として宣言されている")
            if m.overridable:  # 検査は利用者が書いた式で済ませてから包む
                m.formula = Coalesce(Ref(m.override_name), m.formula)  # 上書きがあればそれを優先
            self.warnings[m.name] = w
            edges[m.name] = list(collect_refs(m.formula, self))

        self._plan = [self._make_step(scc, edges) for scc in _tarjan(edges)]
        self._levels = _levels(self._plan, edges)
        self._apply_layout()
        self._delta = {}
        if self.delta_aggregation:
            for step in self._plan:
                m = self.metrics[step.names[0]]
                if step.scan_dim is None and m.formula is not None:
                    if (plan := plan_for(m.formula, self)) is not None:
                        self._delta[m.name] = plan
        self._counts = {n: self.engine.empty(self.metrics[n].dims, "number", self.layout[n], cat=self)
                        for n, plan in self._delta.items() if plan.count is not None}

    def _delta_sources(self) -> set[str]:
        """差分集計で、変更前の値が要る Metric（集計元と対応表）。"""
        return {n for plan in self._delta.values() for n in (plan.source, *plan.aux)}

    def _make_step(self, scc: list[str], edges: dict[str, list[Edge]]) -> Step:
        members = set(scc)
        internal = [(src, e) for src in scc for e in edges[src] if e.target in members]
        if not internal:
            return Step((scc[0],))

        lag_dims = {d for _, e in internal for d, n in e.lags if n >= 1}
        if len(lag_dims) != 1:
            raise FormulaError(f"循環参照: {sorted(members)}（時間方向のずらしを通らない循環がある）")
        dim = lag_dims.pop()
        same_time: dict[str, set[str]] = {n: set() for n in scc}
        for src, e in internal:
            if dim not in self.metrics[src].dims:
                raise FormulaError(f"循環参照: {src} が scan 軸 {dim} を持たない")
            if dim in e.broken:
                raise FormulaError(f"循環参照: {src} -> {e.target} の経路で {dim} を集約・付け替えている")
            if e.lag(dim) < 0:
                raise FormulaError(f"循環参照: {src} が {e.target} の未来の値を参照している")
            if e.lag(dim) == 0:
                same_time[src].add(e.target)

        # 同じ時点どうしの依存（ずらし 0）は非循環でなければならない
        order = _tarjan(same_time)
        if any(len(c) > 1 or c[0] in same_time[c[0]] for c in order):
            raise FormulaError(f"循環参照: {sorted(members)} が同じ時点で循環している")
        return Step(tuple(c[0] for c in order), scan_dim=dim)

    # ------------------------------------------------ 分割軸

    def _apply_layout(self) -> None:
        """Metric ごとの分割軸を決め、格納データをその軸で持ち直す。"""
        self.layout = self._choose_layout()
        for name, m in self.metrics.items():
            want = self.layout[name]
            if m.formula is None:
                if self.engine.partition_of(self._values[name]) != want:
                    self._values[name] = self.engine.repartition(self._values[name], want, self)
            elif name not in self._values or self.engine.partition_of(self._values[name]) != want:
                # 計算 Metric は計画を作り直した直後に全体を計算し直すので、空で持ち直してよい
                self._values[name] = self.engine.empty(m.dims, m.kind, want, cat=self)

    def _choose_layout(self) -> dict[str, str | None]:
        partitions = getattr(self.engine, "partitions", 1)
        samples: dict[str, list[Restrict]] = defaultdict(list)
        if self.auto_layout and partitions > 1:
            # 各入力 Metric の 1 セル（各軸の先頭メンバー）を変えたときの影響範囲を集める
            for src, m in self.metrics.items():
                if m.formula is None and m.dims and all(self.dimensions[d].members for d in m.dims):
                    point = {d: frozenset([self.dimensions[d].members[0]]) for d in m.dims}
                    for name, region in self._propagate({src: point}).items():
                        samples[name].append(region)

        def by_members(d: str) -> int:
            return len(self.dimensions[d].members)

        def touched(d: str, region: Restrict) -> float:
            """region を書き換えるときに触れるパーティションの割合。"""
            if d not in region:
                return 1.0
            dim = self.dimensions[d]
            width = -(-len(dim.members) // partitions)
            total = -(-len(dim.members) // width)
            return len({dim._index[x] // width for x in region[d]}) / total

        layout = {}
        for name, m in self.metrics.items():
            if m.partition is not None or not m.dims:
                layout[name] = m.partition
            elif not samples[name]:
                layout[name] = max(m.dims, key=by_members)
            else:
                layout[name] = min(m.dims, key=lambda d: (mean(touched(d, r) for r in samples[name]),
                                                          -by_members(d)))
        return layout

    # ------------------------------------------------ 影響範囲

    def _propagate(self, changed: dict[str, Restrict],
                   added: dict[str, frozenset[str]] | None = None) -> dict[str, Restrict]:
        """入力の変更範囲と追加したメンバーを計画の順に伝え、影響を受ける全 Metric の範囲を返す
        （changed を含む）。"""
        regions = dict(changed)
        for step in self._plan:
            if step.scan_dim is None:
                m = self.metrics[step.names[0]]
                if m.formula is not None and (r := affected(m.formula, self, regions, added)) is not None:
                    regions[m.name] = r
                continue
            regions.update(self._scan_regions(step, regions, added))
        return regions

    def _scan_regions(self, step: Step, regions: dict[str, Restrict],
                      added: dict[str, frozenset[str]] | None,
                      removed: dict[str, str] | None = None) -> dict[str, Restrict]:
        """scan に含まれる Metric の影響範囲。互いを参照し合うので、範囲が増えなくなるまで
        伝搬を繰り返す（範囲は単調に広がるだけで有限なので必ず止まる）。regions に scan の
        Metric 自身の範囲があれば、そこから始める。"""
        local: dict[str, Restrict | None] = {n: regions.get(n) for n in step.names}
        while True:
            env = regions | {n: r for n, r in local.items() if r is not None}
            grown = False
            for n in step.names:
                r = union_region(local[n], affected(self.metrics[n].formula, self, env, added, removed))
                if r != local[n]:
                    local[n] = env[n] = r
                    grown = True
            if not grown:
                return {n: r for n, r in local.items() if r is not None}

    # ------------------------------------------------ 再計算

    def recalc(self) -> None:
        self._compile()
        if not self._full and not self._changed and not self._added:
            return
        full, self._full = self._full, False
        added = {d: frozenset(ms) for d, ms in self._added.items()}
        self._added.clear()
        if full:
            self._changed.clear()
            self._old_cells.clear()
            self._old_slices.clear()
            self._recalc_all()
            return
        # 計画の順に、計算しながら影響範囲を伝える。各 Metric は書き戻すときに新旧の値を比べ、
        # 実際に値が変わったセルだけを下流への影響範囲にする（変わらなければ下流は計算しない）
        regions: dict[str, Restrict] = dict(self._changed)
        # 差分集計の集計元と対応表について、変更前の値を確保しておく
        sources = self._delta_sources()
        olds = {n: self._old_input_slice(n, regions[n]) for n in self._changed if n in sources}
        self._changed.clear()
        self._old_cells.clear()
        self._old_slices.clear()
        fast = getattr(self.engine, "recalc_changes", None)
        if fast is not None:  # 段取りごとエンジンに任せる（Rust）。意味は以下の Python の経路と同じ
            done, named = fast(self, regions, added, olds)
            self.eval_log.extend(n for n, _ in done)
            self.delta_log.extend(n for n, delta in done if delta)
            self.slice_log.extend_later(named)
            return
        for step in self._plan:
            if step.scan_dim is not None:
                active = {n: r for n, r in self._scan_regions(step, regions, added).items()
                          if self.metrics[n].formula is not None}
                if not active:
                    continue
                for n, r in active.items():
                    if n in sources:
                        olds[n] = self.engine.filter(self._values[n], r or None, self)
                self._scan(step, active, False)
                regions.update(active)
                continue
            m = self.metrics[step.names[0]]
            if m.formula is None or (region := affected(m.formula, self, regions, added)) is None:
                continue
            if m.name in sources:
                olds[m.name] = self.engine.filter(self._values[m.name], region or None, self)
            plan = self._delta.get(m.name)
            if plan is not None and self._delta_applicable(plan, regions, olds):
                changed = self._apply_delta(m, plan, region, regions, olds)
            else:
                changed = self._recompute(m, region, diff=True)
            if changed is not None:
                regions[m.name] = changed

    def _recompute(self, m: Metric, region: Restrict, diff: bool = False) -> Restrict | None:
        """m の region を式から計算し直す。差分集計する SUM は、各グループの件数も求め直す。"""
        plan = self._delta.get(m.name)
        if plan is not None and plan.count is not None:
            value, counts = self.engine.evaluate_with_count(m.formula, plan.count, self, region)
            changed = self._replace(m, region, value, diff)
            counts = self.engine.reorder(counts, m.dims)
            self._counts[m.name] = self.engine.replace(self._counts[m.name], region, counts, self)
            return changed
        return self._replace(m, region, self.engine.evaluate(m.formula, self, region), diff)

    def _recalc_all(self) -> None:
        """全体を計算し直す。同じ段の Metric はまとめて評価し、差分集計する SUM は件数も同時に求める。"""
        for level in self._levels:
            batch = [self.metrics[s.names[0]] for s in level
                     if s.scan_dim is None and self.metrics[s.names[0]].formula is not None]
            fused = [m for m in batch if m.name in self._delta and self._delta[m.name].count is not None]
            plain = [m for m in batch if m not in fused]
            for m, result in zip(plain, self.engine.evaluate_many([(m.formula, {}) for m in plain], self)):
                self._replace(m, {}, result)
            for m in fused:
                value, count = self.engine.evaluate_with_count(m.formula, self._delta[m.name].count, self, {})
                self._replace(m, {}, value)
                count = self.engine.reorder(count, m.dims)
                self._counts[m.name] = self.engine.replace(self._counts[m.name], {}, count, self)
            for step in level:
                if step.scan_dim is not None:
                    self._scan(step, {n: {} for n in step.names}, True)

    def _old_input_slice(self, name: str, region: Restrict) -> Any:
        """入力 Metric の region の、変更前の値。今の値から、触れたセルだけ覚えておいた値に戻す。"""
        if name in self._old_slices:  # 範囲ごと空にした（メンバーの削除）。region はその範囲
            return self._old_slices[name]
        old = self.engine.filter(self._values[name], region or None, self)
        for key, value in self._old_cells.get(name, {}).items():
            old = self.engine.write(old, key, value, self)
        return old

    @staticmethod
    def _delta_range(plan: DeltaPlan, regions: dict[str, Restrict]) -> Restrict | None:
        """集計元と対応表の変更範囲を合わせた範囲。どれも変わっていなければ None。"""
        r = None
        for n in (plan.source, *plan.aux):
            if n in regions:
                r = union_region(r, regions[n])
        return r

    def _delta_applicable(self, plan: DeltaPlan, regions: dict[str, Restrict], olds: dict[str, Any]) -> bool:
        changed = [n for n in (plan.source, *plan.aux) if n in regions]
        # 範囲が Metric 全体に広がるなら、差分より計算し直すほうが速い
        return bool(changed) and all(n in olds for n in changed) and bool(self._delta_range(plan, regions))

    def _apply_delta(self, m: Metric, plan: DeltaPlan, region: Restrict,
                     regions: dict[str, Restrict], olds: dict[str, Any]) -> Restrict | None:
        """集計元（と対応表）の変更前後の差分を集計し、region 内の既存の値と件数に足し込む。

        集計元と対応表を、変更範囲を合わせた範囲 r で切り出す。変更後はそのまま、変更前は
        変わった部分だけ変更前の値に戻したもの。両者で同じ集計をして引けば、変わった行の寄与の差になる。
        """
        eng = self.engine
        r = self._delta_range(plan, regions)
        d_count_f, d_value_f = self._delta_exprs(m, plan)
        work, types = {}, {}
        for i, n in enumerate((plan.source, *plan.aux)):
            new = eng.filter(self._values[n], r, self)
            old = new
            if n in regions:
                # 変更前の値は、実際に変わった範囲（regions[n]）の分だけ戻す。格納全体ではなく、
                # 切り出したばかりの小さな new から複製して作る
                before = eng.filter(olds[n], regions[n], self)
                old = eng.replace(eng.filter(new, r, self), regions[n], before, self)
            work[f"__new{i}"], work[f"__old{i}"] = new, old
            types[f"__new{i}"] = types[f"__old{i}"] = self.metric_type(n)

        old_value = eng.filter(self._values[m.name], region or None, self)
        old_count = (eng.filter(self._counts[m.name], region or None, self)
                     if plan.count is not None else old_value)
        own = Type(m.dims, "number")
        self._work = work
        self._temp_types = types | {"__old_value": own, "__old_count": own, "__d_value": own,
                                    "__d_count": own, "__new_count": own}
        try:
            d_count = eng.evaluate(d_count_f, self, None)
            d_value = eng.evaluate(d_value_f, self, None) if plan.count is not None else d_count
            self._work = {"__old_value": old_value, "__old_count": old_count,
                          "__d_value": d_value, "__d_count": d_count}
            new_count = eng.evaluate(_NEW_COUNT, self, None)
            self._work["__new_count"] = new_count
            # 件数が 0 になったグループは空にする
            new_value = eng.evaluate(_NEW_VALUE, self, None)
            kept_count = eng.evaluate(_KEPT_COUNT, self, None)
        finally:
            self._work = {}
            self._temp_types = {}

        self._values[m.name], changed = eng.replace_diff(self._values[m.name], region,
                                                         eng.reorder(new_value, m.dims), self)
        if plan.count is not None:
            self._counts[m.name] = eng.replace(self._counts[m.name], region,
                                               eng.reorder(kept_count, m.dims), self)
        self.eval_log.append(m.name)
        self.slice_log.append((m.name, region))
        self.delta_log.append(m.name)
        return changed

    def _delta_exprs(self, m: Metric, plan: DeltaPlan) -> tuple[Expr, Expr]:
        """件数と値の差分を求める式。Metric ごとに一度だけ作る（エンジンが変換結果をキャッシュできるように）。"""
        cached = self._delta_cache.get(m.name)
        if cached is None or cached[0] is not plan:
            names = (plan.source, *plan.aux)
            new = {n: f"__new{i}" for i, n in enumerate(names)}
            old = {n: f"__old{i}" for i, n in enumerate(names)}

            def diff(f: Expr) -> Expr:
                return BinOp("-", rename(f, new), rename(f, old))

            count_f = plan.count if plan.count is not None else m.formula
            cached = self._delta_cache[m.name] = (plan, diff(count_f), diff(m.formula))
        return cached[1], cached[2]

    def _replace(self, m: Metric, region: Restrict, new: Any, diff: bool = False) -> Restrict | None:
        """Metric の region 内のセルを new で置き換える。region 外のセルはそのまま残す。

        diff なら、値が実際に変わったセルを囲む範囲を返す（変化なしなら None）。
        """
        new = self.engine.reorder(new, m.dims)
        changed = None
        if diff:
            self._values[m.name], changed = self.engine.replace_diff(self._values[m.name], region, new, self)
        else:
            self._values[m.name] = self.engine.replace(self._values[m.name], region, new, self)
        self.eval_log.append(m.name)
        self.slice_log.append((m.name, region))
        return changed

    def _scan(self, step: Step, active: dict[str, Restrict], full: bool) -> None:
        """active（scan に含まれる Metric -> 計算し直す範囲）を、時間軸に沿って 1 時点ずつ計算する。"""
        dim = step.scan_dim
        if full:
            for n in step.names:
                m = self.metrics[n]
                self._values[n] = self.engine.empty(m.dims, m.kind, self.layout.get(n), cat=self)

        # 格納データにその場で 1 時点ずつ書き込む。次の時点の PREVIOUS は、書き込んだばかりの
        # 前の時点（範囲の外なら元のまま）を読む
        for t in self.dimension(dim).members:
            for n, r in active.items():
                if dim in r and t not in r[dim]:
                    continue
                m = self.metrics[n]
                sub = {**r, dim: frozenset([t])}
                sliced = self.engine.reorder(self.engine.evaluate(m.formula, self, sub), m.dims)
                self._values[n] = self.engine.replace(self._values[n], sub, sliced, self)
        for n, r in active.items():
            self.eval_log.append(n)
            self.slice_log.append((n, r))


# 差分集計の後半で使う式（名前は _apply_delta の作業データ）
_ALIVE = BinOp(">", Ref("__new_count"), Const(0.0))
_NEW_COUNT = Ref("__old_count") + Ref("__d_count")
_NEW_VALUE = Filter(Ref("__old_value") + Ref("__d_value"), _ALIVE)
_KEPT_COUNT = Filter(Ref("__new_count"), _ALIVE)


def _levels(plan: list[Step], edges: dict[str, list[Edge]]) -> list[list[Step]]:
    """計画を依存関係の段に分ける。同じ段のステップは互いに依存しない。"""
    level_of: dict[str, int] = {}
    levels: list[list[Step]] = []
    for step in plan:  # plan は依存先が先の順
        deps = {e.target for n in step.names for e in edges[n]} - set(step.names)
        level = max((level_of[d] + 1 for d in deps), default=0)
        for n in step.names:
            level_of[n] = level
        while len(levels) <= level:
            levels.append([])
        levels[level].append(step)
    return levels


def _tarjan(graph: dict[str, list[Edge] | set[str]]) -> list[list[str]]:
    """強連結成分を「依存先が先」の順で返す。"""
    def targets(v):
        return [e.target if isinstance(e, Edge) else e for e in graph[v]]

    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    out: list[list[str]] = []

    def visit(v: str) -> None:
        index[v] = low[v] = len(index)
        stack.append(v)
        on_stack.add(v)
        for w in targets(v):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            out.append(comp)

    for v in graph:
        if v not in index:
            visit(v)
    return out
