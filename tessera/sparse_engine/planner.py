"""計算計画と再計算の段取り（Planner）。

エンジンは 2 つの役を持つ。格納と評価（engine.Store）と、ここで決める計画と段取り（Planner）である。
Model は式の型検査、計算計画、影響範囲、再計算をすべて engine.planner に任せ、自分では持たない。

PyPlanner は Python による参照実装で、Store の口（evaluate、replace_diff など）だけを使って段取りを組む。
ReferenceEngine が使う。RustEngine は同じ段取りを Rust で行う RustPlanner を持ち、意味は PyPlanner と同じ
（tests/test_expr_coverage.py が突き合わせる）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .delta import DeltaPlan, plan_for, rename
from .evaluate import (Catalog, Edge, FormulaError, Restrict, Type, affected, collect_refs, estimate, infer,
                       resolve, union_region)
from .expr import BinOp, Const, Expr, Filter, Ref


@dataclass
class Step:
    names: tuple[str, ...]
    scan_dim: str | None = None  # None なら通常の 1 Metric の計算


@dataclass(frozen=True)
class CompiledPlan:
    """Model が作った計算計画のうち、Planner が段取りに使う部分。

    steps は依存先が先の順の計算の段階、levels は互いに依存しない段階を段ごとにまとめたもの。
    delta は差分集計する Metric とその計画、sources は差分集計で変更前の値が要る Metric。
    """
    steps: list
    levels: list
    delta: dict
    sources: frozenset


class Planner(Protocol):
    """計算計画と再計算の段取り。cat は Model（軸、Metric の定義と型、格納データの読み出し元）。"""

    def check(self, written: Expr, cat: Catalog) -> tuple[Expr, Type, list[str]]:
        """利用者が書いた式を評価できる形に直して型を検査し、(評価に使う式, 型, 警告) を返す。
        型の誤りは FormulaError。"""

    def plan(self, formulas: dict[str, Expr | None], dims: dict[str, tuple[str, ...]],
             cat: Catalog) -> tuple[list[Step], dict[str, list[Edge]], list[list[Step]]]:
        """依存グラフから計算計画を作る。formulas は Metric 名 -> 評価に使う式（入力は None）。
        返すのは (依存先が先の順の段階, 依存グラフ, 段ごとの段階)。循環の誤りは FormulaError。"""

    def estimate(self, expr: Expr, cat: Catalog, cells: dict[str, float]) -> float:
        """型を決めた式の結果のセル数の見積もり（上限）。cells は Metric ごとのセル数。"""

    def delta_plan(self, expr: Expr, cat: Catalog) -> DeltaPlan | None:
        """式が差分集計の対象なら、その計画。"""

    def propagate(self, plan: CompiledPlan, cat: Catalog, changed: dict[str, Restrict],
                  added: dict[str, frozenset[str]] | None = None) -> dict[str, Restrict]:
        """入力の変更範囲と追加したメンバーを計画の順に伝え、影響を受ける全 Metric の範囲（changed を含む）。"""

    def affected(self, expr: Expr, cat: Catalog, regions: dict[str, Restrict], added=None,
                 removed=None) -> Restrict | None:
        """1 つの式の影響範囲。regions は Metric 名 -> 変更範囲。"""

    def removal_regions(self, plan: CompiledPlan, stores: dict[str, Any], cat: Catalog, dim: str,
                        member: str) -> dict[str, Restrict]:
        """入力を空にしたあと、軸 dim のメンバー member を消すと値が変わる範囲
        （計算 Metric -> 消すメンバーを除いた範囲）。"""

    def recalc(self, plan: CompiledPlan, stores: dict[str, Any], counts: dict[str, Any], cat: Catalog,
               changed: dict[str, Restrict], added: dict[str, frozenset[str]], olds: dict[str, Any],
               forced: dict[str, Restrict], full: bool = False) -> tuple[list[tuple[str, bool]], Callable]:
        """差分再計算。stores と counts（Metric ごとの格納データと差分集計の件数）をその場で書き換える。
        changed は入力の変更範囲、added は追加したメンバー、olds は差分集計の集計元になる入力の変更前の値、
        forced は必ず計算し直す計算 Metric の範囲。full なら全体を計算し直す（changed などは使わない）。
        返すのは、計算し直した (Metric, 差分集計か) の列と、(Metric, 範囲) の列を返す関数。
        途中で失敗したら、途中まで書き換えた計算 Metric が残ってよい（Model が次に全体を計算し直す）。"""


# 差分集計の後半で使う式（名前は _apply_delta の作業データ）
_ALIVE = BinOp(">", Ref("__new_count"), Const(0.0))
_NEW_COUNT = Ref("__old_count") + Ref("__d_count")
_NEW_VALUE = Filter(Ref("__old_value") + Ref("__d_value"), _ALIVE)
_KEPT_COUNT = Filter(Ref("__new_count"), _ALIVE)


class _Overlay:
    """cat に作業データ（差分集計の変更前後の値など）を重ねた読み出し元。ほかは cat のまま。"""

    def __init__(self, cat, engine, work: dict[str, Any], types: dict[str, Type]):
        self._cat, self._engine, self._work, self._types = cat, engine, work, types

    def __getattr__(self, name: str):
        return getattr(self._cat, name)

    def metric_type(self, name: str) -> Type:
        return self._types[name] if name in self._types else self._cat.metric_type(name)

    def source(self, name: str) -> Any:
        return self._work[name] if name in self._work else self._cat.source(name)

    def read(self, name: str, restrict: Restrict | None) -> Any:
        return self._engine.view(self.source(name), restrict or None, self)


class PyPlanner:
    """Python による Planner の参照実装。engine は格納と評価に使う Store。"""

    def __init__(self, engine):
        self.engine = engine
        self._delta_exprs: dict[str, tuple] = {}  # Metric -> (計画, 式, 件数の差分の式, 値の差分の式)

    # ------------------------------------------------ 型検査と計画

    def check(self, written: Expr, cat: Catalog) -> tuple[Expr, Type, list[str]]:
        formula = resolve(written, cat)  # 軸の名前、Metric を使った BY を評価できる形に
        warnings: list[str] = []
        return formula, infer(formula, cat, warnings), warnings

    def plan(self, formulas, dims, cat):
        edges = {n: [] if f is None else list(collect_refs(f, cat)) for n, f in formulas.items()}
        steps = [_make_step(cat, scc, edges) for scc in _tarjan(edges)]
        return steps, edges, _levels(steps, edges)

    def estimate(self, expr: Expr, cat: Catalog, cells) -> float:
        return estimate(expr, cat, cells)[1]

    def delta_plan(self, expr: Expr, cat: Catalog) -> DeltaPlan | None:
        return plan_for(expr, cat)

    # ------------------------------------------------ 影響範囲

    def affected(self, expr, cat, regions, added=None, removed=None):
        return affected(expr, cat, regions, added, removed)

    def propagate(self, plan, cat, changed, added=None):
        regions = dict(changed)
        for step in plan.steps:
            if step.scan_dim is None:
                m = cat.metrics[step.names[0]]
                if m.formula is not None and (r := affected(m.formula, cat, regions, added)) is not None:
                    regions[m.id] = r
                continue
            regions.update(_scan_regions(cat, step, regions, added))
        return regions

    def removal_regions(self, plan, stores, cat, dim, member):
        """計算 Metric がそのメンバーを指す値を持つのは、そのメンバーのセル自身（軸の値）か、
        それを前月参照や引き下ろしで運んだセルだけなので、消えるセルからの伝搬で足りる。"""
        eng = self.engine
        point = frozenset([member])
        added, removed = {dim: point}, {dim: member}

        def has_cells(name: str) -> bool:
            return dim in cat.metrics[name].dims and eng.size(eng.filter(stores[name], {dim: point}, cat)) > 0

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

        for step in plan.steps:
            if step.scan_dim is None:
                m = cat.metrics[step.names[0]]
                if m.formula is not None:
                    settle(m.id, affected(m.formula, cat, changes, added, removed))
                continue
            for n in step.names:  # scan の中の前月参照は、消えるセルからも伝わる
                if has_cells(n):
                    changes[n] = {dim: point}
            scanned = _scan_regions(cat, step, changes, added, removed)
            for n in step.names:
                changes.pop(n, None)
                settle(n, scanned.get(n))
        return todo

    # ------------------------------------------------ 再計算

    def recalc(self, plan, stores, counts, cat, changed, added, olds, forced, full=False):
        run = _Run(self, plan, stores, counts, cat)
        if full:
            run.all()
        else:
            run.changes(dict(changed), added, dict(olds), forced)
        return run.done, lambda: run.named

    def delta_exprs(self, name: str, formula: Expr, plan: DeltaPlan) -> tuple[Expr, Expr]:
        """件数と値の差分を求める式。Metric ごとに一度だけ作る（エンジンが変換結果をキャッシュできるように）。
        式が読む __new{i} と __old{i} は、集計元と対応表（plan.source、plan.aux）の変更後と変更前の値。"""
        cached = self._delta_exprs.get(name)
        if cached is None or cached[0] is not plan or cached[1] is not formula:
            names = (plan.source, *plan.aux)
            new = {n: f"__new{i}" for i, n in enumerate(names)}
            old = {n: f"__old{i}" for i, n in enumerate(names)}

            def diff(f: Expr) -> Expr:
                return BinOp("-", rename(f, new), rename(f, old))

            count_f = plan.count if plan.count is not None else formula
            cached = self._delta_exprs[name] = (plan, formula, diff(count_f), diff(formula))
        return cached[2], cached[3]


class _Run:
    """PyPlanner.recalc の 1 回分。"""

    def __init__(self, planner: PyPlanner, plan: CompiledPlan, stores: dict, counts: dict, cat):
        self.planner, self.eng, self.plan = planner, planner.engine, plan
        self.stores, self.counts, self.cat = stores, counts, cat
        self.done: list[tuple[str, bool]] = []
        self.named: list[tuple[str, Restrict]] = []

    def all(self) -> None:
        """全体を計算し直す。同じ段の Metric はまとめて評価し、差分集計する SUM は件数も同時に求める。"""
        eng, cat = self.eng, self.cat
        for level in self.plan.levels:
            batch = [cat.metrics[s.names[0]] for s in level
                     if s.scan_dim is None and cat.metrics[s.names[0]].formula is not None]
            fused = [m for m in batch if m.id in self.plan.delta and self.plan.delta[m.id].count is not None]
            plain = [m for m in batch if m not in fused]
            for m, result in zip(plain, eng.evaluate_many([(m.formula, {}) for m in plain], cat)):
                self._replace(m, {}, result)
            for m in fused:
                value, count = eng.evaluate_with_count(m.formula, self.plan.delta[m.id].count, cat, {})
                self._replace(m, {}, value)
                self.counts[m.id] = eng.replace(self.counts[m.id], {}, eng.reorder(count, m.dims), cat)
            for step in level:
                if step.scan_dim is not None:
                    self._scan(step, {n: {} for n in step.names}, True)

    def changes(self, regions: dict, added: dict, olds: dict, forced: dict) -> None:
        """計画の順に、計算しながら影響範囲を伝える。各 Metric は書き戻すときに新旧の値を比べ、
        実際に値が変わったセルだけを下流への影響範囲にする（変わらなければ下流は計算しない）。"""
        eng, cat, sources = self.eng, self.cat, self.plan.sources
        for step in self.plan.steps:
            if step.scan_dim is not None:
                seeded = regions | {n: forced[n] for n in step.names if n in forced}
                active = {n: r for n, r in _scan_regions(cat, step, seeded, added).items()
                          if cat.metrics[n].formula is not None}
                if not active:
                    continue
                for n, r in active.items():
                    if n in sources:
                        olds[n] = eng.filter(self.stores[n], r or None, cat)
                self._scan(step, active, False)
                regions.update(active)
                continue
            m = cat.metrics[step.names[0]]
            if m.formula is None:
                continue
            region = union_region(affected(m.formula, cat, regions, added), forced.get(m.id))
            if region is None:
                continue
            if m.id in sources:
                olds[m.id] = eng.filter(self.stores[m.id], region or None, cat)
            dp = self.plan.delta.get(m.id)
            if dp is not None and m.id not in forced and self._delta_applicable(dp, regions, olds):
                changed = self._apply_delta(m, dp, region, regions, olds)
            else:
                changed = self._recompute(m, region)
            if changed is not None:
                regions[m.id] = changed

    def _recompute(self, m, region: Restrict) -> Restrict | None:
        """m の region を式から計算し直す。差分集計する SUM は、各グループの件数も求め直す。"""
        eng, cat = self.eng, self.cat
        dp = self.plan.delta.get(m.id)
        if dp is not None and dp.count is not None:
            value, counts = eng.evaluate_with_count(m.formula, dp.count, cat, region)
            changed = self._replace(m, region, value, diff=True)
            self.counts[m.id] = eng.replace(self.counts[m.id], region, eng.reorder(counts, m.dims), cat)
            return changed
        return self._replace(m, region, eng.evaluate(m.formula, cat, region), diff=True)

    @staticmethod
    def _delta_range(dp: DeltaPlan, regions: dict[str, Restrict]) -> Restrict | None:
        """集計元と対応表の変更範囲を合わせた範囲。どれも変わっていなければ None。"""
        r = None
        for n in (dp.source, *dp.aux):
            if n in regions:
                r = union_region(r, regions[n])
        return r

    def _delta_applicable(self, dp: DeltaPlan, regions: dict[str, Restrict], olds: dict[str, Any]) -> bool:
        changed = [n for n in (dp.source, *dp.aux) if n in regions]
        # 範囲が Metric 全体に広がるなら、差分より計算し直すほうが速い
        return bool(changed) and all(n in olds for n in changed) and bool(self._delta_range(dp, regions))

    def _apply_delta(self, m, dp: DeltaPlan, region: Restrict, regions: dict[str, Restrict],
                     olds: dict[str, Any]) -> Restrict | None:
        """集計元（と対応表）の変更前後の差分を集計し、region 内の既存の値と件数に足し込む。

        集計元と対応表を、変更範囲を合わせた範囲 r で切り出す。変更後はそのまま、変更前は
        変わった部分だけ変更前の値に戻したもの。両者で同じ集計をして引けば、変わった行の寄与の差になる。
        """
        eng, cat = self.eng, self.cat
        r = self._delta_range(dp, regions)
        d_count_f, d_value_f = self.planner.delta_exprs(m.id, m.formula, dp)
        work, types = {}, {}
        for i, n in enumerate((dp.source, *dp.aux)):
            new = eng.filter(self.stores[n], r, cat)
            old = new
            if n in regions:
                # 変更前の値は、実際に変わった範囲（regions[n]）の分だけ戻す。格納全体ではなく、
                # 切り出したばかりの小さな new から複製して作る
                before = eng.filter(olds[n], regions[n], cat)
                old = eng.replace(eng.filter(new, r, cat), regions[n], before, cat)
            work[f"__new{i}"], work[f"__old{i}"] = new, old
            types[f"__new{i}"] = types[f"__old{i}"] = cat.metric_type(n)

        old_value = eng.filter(self.stores[m.id], region or None, cat)
        old_count = eng.filter(self.counts[m.id], region or None, cat) if dp.count is not None else old_value
        own = Type(m.dims, "number")
        types |= {"__old_value": own, "__old_count": own, "__d_value": own, "__d_count": own, "__new_count": own}
        view = _Overlay(cat, eng, work, types)
        d_count = eng.evaluate(d_count_f, view, None)
        d_value = eng.evaluate(d_value_f, view, None) if dp.count is not None else d_count
        view = _Overlay(cat, eng, {"__old_value": old_value, "__old_count": old_count,
                                   "__d_value": d_value, "__d_count": d_count}, types)
        view._work["__new_count"] = eng.evaluate(_NEW_COUNT, view, None)
        new_value = eng.evaluate(_NEW_VALUE, view, None)  # 件数が 0 になったグループは空にする
        kept_count = eng.evaluate(_KEPT_COUNT, view, None)

        self.stores[m.id], changed = eng.replace_diff(self.stores[m.id], region,
                                                        eng.reorder(new_value, m.dims), cat)
        if dp.count is not None:
            self.counts[m.id] = eng.replace(self.counts[m.id], region, eng.reorder(kept_count, m.dims), cat)
        self.done.append((m.id, True))
        self.named.append((m.id, region))
        return self._widened(changed)

    def _widened(self, changed: Restrict | None) -> Restrict | None:
        """値が変わったセルの範囲から、全メンバーにわたる軸を外す（その軸は「全体」として扱う）。
        範囲が全体になれば、下流の書き戻しは並べ直すだけで済み、差分集計より計算し直しを選べる。
        Rust のエンジンは、大きな Metric では範囲が大半を占めるときも全体にする（plan.rs）。"""
        if changed is None:
            return None
        return {d: ms for d, ms in changed.items() if len(ms) < len(self.cat.dimensions[d].members)}

    def _replace(self, m, region: Restrict, new: Any, diff: bool = False) -> Restrict | None:
        """Metric の region 内のセルを new で置き換える。region 外のセルはそのまま残す。
        diff なら、値が実際に変わったセルを囲む範囲を返す（変化なしなら None）。"""
        eng, cat = self.eng, self.cat
        new = eng.reorder(new, m.dims)
        changed = None
        if diff:
            self.stores[m.id], changed = eng.replace_diff(self.stores[m.id], region, new, cat)
            changed = self._widened(changed)
        else:
            self.stores[m.id] = eng.replace(self.stores[m.id], region, new, cat)
        self.done.append((m.id, False))
        self.named.append((m.id, region))
        return changed

    def _scan(self, step: Step, active: dict[str, Restrict], full: bool) -> None:
        """active（scan に含まれる Metric -> 計算し直す範囲）を、時間軸に沿って 1 時点ずつ計算する。"""
        eng, cat, dim = self.eng, self.cat, step.scan_dim
        if full:
            for n in step.names:
                m = cat.metrics[n]
                self.stores[n] = eng.empty(m.dims, m.kind, cat.layout.get(n), cat=cat)

        # 格納データにその場で 1 時点ずつ書き込む。次の時点の PREVIOUS は、書き込んだばかりの
        # 前の時点（範囲の外なら元のまま）を読む
        for t in cat.dimension(dim).members:
            for n, r in active.items():
                if dim in r and t not in r[dim]:
                    continue
                m = cat.metrics[n]
                sub = {**r, dim: frozenset([t])}
                sliced = eng.reorder(eng.evaluate(m.formula, cat, sub), m.dims)
                self.stores[n] = eng.replace(self.stores[n], sub, sliced, cat)
        for n, r in active.items():
            self.done.append((n, False))
            self.named.append((n, r))


def _scan_regions(cat, step: Step, regions: dict[str, Restrict], added: dict[str, frozenset[str]] | None,
                  removed: dict[str, str] | None = None) -> dict[str, Restrict]:
    """scan に含まれる Metric の影響範囲。互いを参照し合うので、範囲が増えなくなるまで
    伝搬を繰り返す（範囲は単調に広がるだけで有限なので必ず止まる）。regions に scan の
    Metric 自身の範囲があれば、そこから始める。"""
    local: dict[str, Restrict | None] = {n: regions.get(n) for n in step.names}
    while True:
        env = regions | {n: r for n, r in local.items() if r is not None}
        grown = False
        for n in step.names:
            r = union_region(local[n], affected(cat.metrics[n].formula, cat, env, added, removed))
            if r != local[n]:
                local[n] = env[n] = r
                grown = True
        if not grown:
            return {n: r for n, r in local.items() if r is not None}


def _make_step(cat, scc: list[str], edges: dict[str, list[Edge]]) -> Step:
    members = set(scc)
    internal = [(src, e) for src in scc for e in edges[src] if e.target in members]
    if not internal:
        return Step((scc[0],))

    lag_dims = {d for _, e in internal for d, n in e.lags if n >= 1}
    if len(lag_dims) != 1:
        raise FormulaError("cycle_no_lag", members=sorted(members))
    dim = lag_dims.pop()
    same_time: dict[str, set[str]] = {n: set() for n in scc}
    for src, e in internal:
        if dim not in cat.metrics[src].dims:
            raise FormulaError("cycle_scan_dim", src=src, dim=dim)
        if dim in e.broken:
            raise FormulaError("cycle_broken", src=src, target=e.target, dim=dim)
        if e.lag(dim) < 0:
            raise FormulaError("cycle_future", src=src, target=e.target)
        if e.lag(dim) == 0:
            same_time[src].add(e.target)

    # 同じ時点どうしの依存（ずらし 0）は非循環でなければならない
    order = _tarjan(same_time)
    if any(len(c) > 1 or c[0] in same_time[c[0]] for c in order):
        raise FormulaError("cycle_same_time", members=sorted(members))
    return Step(tuple(c[0] for c in order), scan_dim=dim)


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
