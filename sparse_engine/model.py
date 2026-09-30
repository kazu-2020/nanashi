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

from collections import defaultdict
from dataclasses import dataclass, field
from statistics import mean
from typing import Any, Mapping

from .core import Cube, Dimension, Key
from .delta import DeltaPlan, plan_for, substitute
from .engine import Engine, default_engine
from .evaluate import (Edge, FormulaError, Kind, Restrict, Type, affected, collect_refs, infer,
                       union_region)
from .expr import BinOp, Const, Expr, Filter, Ref
from .parser import parse


@dataclass
class Metric:
    name: str
    dims: tuple[str, ...]
    kind: Kind = "number"
    formula: Expr | None = None  # None なら入力 Metric
    partition: str | None = None  # 明示した分割軸。None なら自動で選ぶ


@dataclass
class Step:
    names: tuple[str, ...]
    scan_dim: str | None = None  # None なら通常の 1 Metric の計算


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
    slice_log: list[tuple[str, Restrict]] = field(default_factory=list)  # 再計算した範囲（観察用）
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

    # ------------------------------------------------ 定義

    def add_dimension(self, name: str, members, *, ordered: bool = False) -> Dimension:
        self.dimensions[name] = Dimension(name, members, ordered=ordered)
        return self.dimensions[name]

    def add_property(self, dim: str, prop: str, target: str, mapping: Mapping[str, str]) -> None:
        self.dimension(dim).add_property(prop, self.dimension(target), mapping)
        self._invalidate()

    def add_input(self, name: str, dims, cells: Mapping[Key, float | bool] | None = None,
                  *, kind: Kind = "number", storage: Any = None, partition: str | None = None) -> None:
        """cells は {キー: 値}。大量のデータはエンジンの格納形式で storage に渡してもよい。"""
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
                    partition: str | None = None) -> None:
        """formula は AST か式の文字列。文字列の構文エラーはここで ParseError になる。"""
        if isinstance(formula, str):
            formula = parse(formula, self_name=name)
        dims = tuple(dims)
        self.metrics[name] = Metric(name, dims, kind, formula, self._check_partition(name, dims, partition))
        self._invalidate()

    @staticmethod
    def _check_partition(name: str, dims: tuple[str, ...], partition: str | None) -> str | None:
        if partition is not None and partition not in dims:
            raise ValueError(f"{name}: 分割軸 {partition} が軸 {dims} にない")
        return partition

    # ------------------------------------------------ 入力

    def set_cell(self, name: str, value: float | bool | None, **coords: str) -> None:
        m = self.metrics[name]
        if m.formula is not None:
            raise ValueError(f"{name} は計算 Metric なので直接入力できない")
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
        return self.engine.to_cube(self._values[name], self)

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
            w: list[str] = []
            t = infer(m.formula, self, w)
            if set(t.dims) != set(m.dims):
                raise FormulaError(f"{m.name}: 式の軸 {t.dims} が宣言した軸 {m.dims} と一致しない")
            if t.kind != m.kind:
                raise FormulaError(f"{m.name}: 式の値は {t.kind} だが {m.kind} として宣言されている")
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
        return {plan.source for plan in self._delta.values()}

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

    def _propagate(self, changed: dict[str, Restrict]) -> dict[str, Restrict]:
        """入力の変更範囲を計画の順に伝え、影響を受ける全 Metric の範囲を返す（changed を含む）。"""
        regions = dict(changed)
        for step in self._plan:
            if step.scan_dim is None:
                m = self.metrics[step.names[0]]
                if m.formula is not None and (r := affected(m.formula, self, regions)) is not None:
                    regions[m.name] = r
                continue
            # 互いを参照し合うので、影響範囲が増えなくなるまで伝搬を繰り返す。
            # 範囲は単調に広がるだけで有限なので必ず止まる
            local: dict[str, Restrict | None] = {n: None for n in step.names}
            while True:
                env = regions | {n: r for n, r in local.items() if r is not None}
                grown = False
                for n in step.names:
                    r = union_region(local[n], affected(self.metrics[n].formula, self, env))
                    if r != local[n]:
                        local[n] = env[n] = r
                        grown = True
                if not grown:
                    break
            regions.update({n: r for n, r in local.items() if r is not None})
        return regions

    # ------------------------------------------------ 再計算

    def recalc(self) -> None:
        self._compile()
        if not self._full and not self._changed:
            return
        full, self._full = self._full, False
        if full:
            self._changed.clear()
            self._old_cells.clear()
            self._recalc_all()
            return
        regions = self._propagate(self._changed)
        # 差分集計の集計元について、変更前の範囲を確保しておく
        sources = self._delta_sources()
        olds = {} if full else {n: self._old_input_slice(n, regions[n])
                                for n in self._changed if n in sources}
        self._changed.clear()
        self._old_cells.clear()
        for step in self._plan:
            active = {n: regions[n] for n in step.names
                      if n in regions and self.metrics[n].formula is not None}
            if not active:
                continue
            for n, r in active.items():
                if not full and n in sources:
                    olds[n] = self.engine.filter(self._values[n], r or None, self)
            if step.scan_dim is not None:
                self._scan(step, active, full)
                continue
            m = self.metrics[step.names[0]]
            region = active[m.name]
            plan = self._delta.get(m.name)
            if plan is not None and not full and regions.get(plan.source) and plan.source in olds:
                self._apply_delta(m, plan, region, olds[plan.source], regions[plan.source])
                continue
            if plan is not None and plan.count is not None:
                value, counts = self.engine.evaluate_with_count(m.formula, plan.count, self, region)
                self._replace(m, region, value)
                counts = self.engine.reorder(counts, m.dims)
                self._counts[m.name] = self.engine.replace(self._counts[m.name], region, counts, self)
            else:
                self._replace(m, region, self.engine.evaluate(m.formula, self, region))

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
        old = self.engine.filter(self._values[name], region or None, self)
        for key, value in self._old_cells.get(name, {}).items():
            old = self.engine.write(old, key, value, self)
        return old

    def _apply_delta(self, m: Metric, plan: DeltaPlan, region: Restrict, old_src: Any,
                     src_region: Restrict) -> None:
        """集計元の変更前後の差分を集計し、region 内の既存の値と件数に足し込む。"""
        eng, src = self.engine, plan.source
        new_src = eng.filter(self._values[src], src_region or None, self)
        d_count_f, d_value_f = self._delta_exprs(m, plan)

        old_value = eng.filter(self._values[m.name], region or None, self)
        old_count = (eng.filter(self._counts[m.name], region or None, self)
                     if plan.count is not None else old_value)
        self._work = {"__new": new_src, "__old": old_src}
        src_type, own = self.metric_type(src), Type(m.dims, "number")
        self._temp_types = {"__new": src_type, "__old": src_type, "__old_value": own,
                            "__old_count": own, "__d_value": own, "__d_count": own, "__new_count": own}
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

        self._values[m.name] = eng.replace(self._values[m.name], region, eng.reorder(new_value, m.dims), self)
        if plan.count is not None:
            self._counts[m.name] = eng.replace(self._counts[m.name], region,
                                               eng.reorder(kept_count, m.dims), self)
        self.eval_log.append(m.name)
        self.slice_log.append((m.name, region))
        self.delta_log.append(m.name)

    def _delta_exprs(self, m: Metric, plan: DeltaPlan) -> tuple[Expr, Expr]:
        """件数と値の差分を求める式。Metric ごとに一度だけ作る（エンジンが変換結果をキャッシュできるように）。"""
        cached = self._delta_cache.get(m.name)
        if cached is None or cached[0] is not plan:
            src = plan.source

            def diff(f: Expr) -> Expr:
                return BinOp("-", substitute(f, src, "__new"), substitute(f, src, "__old"))

            count_f = plan.count if plan.count is not None else m.formula
            cached = self._delta_cache[m.name] = (plan, diff(count_f), diff(m.formula))
        return cached[1], cached[2]

    def _replace(self, m: Metric, region: Restrict, new: Any) -> None:
        """Metric の region 内のセルを new で置き換える。region 外のセルはそのまま残す。"""
        new = self.engine.reorder(new, m.dims)
        self._values[m.name] = self.engine.replace(self._values[m.name], region, new, self)
        self.eval_log.append(m.name)
        self.slice_log.append((m.name, region))

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
