"""Rust（nanashi_core）によるエンジン。

Metric の格納データと評価の途中結果は Rust 側に置き、Python にはハンドルだけを返す。
式は初回に Rust 側の構文木へ変換してキャッシュし、評価は Rust で行う。
1 回の評価の固定コストがほぼないので、小さな範囲の評価を Metric ごとに何百回繰り返しても速い。

格納データは分割軸を最上位ビットに詰めた整数キーの B 木で持つ。分割軸で絞った範囲の
読み書きは、その範囲の行数だけで済む。
"""
from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

import nanashi_core

from .core import Cube
from .delta import COUNTED, DeltaPlan
from .evaluate import Catalog, Edge, FormulaError, Type, member_kind
from .expr import (AsAxis, BinOp, By, Coalesce, Const, DimRef, Expand, Expr, Filter, If, IfBlank,
                   IsBlank, Member, Not, On, Ref, Remove, Select, Shift)


class LazyEdges(MutableMapping):
    """Rust が返した依存グラフ。Metric 名 -> Edge の列に直すのは、初めて使うときだけ
    （計画を作るたびに全部の辺を Python のオブジェクトにすると、計画そのものより時間がかかる）。"""

    def __init__(self, build):
        self._build = build
        self._data: dict | None = None

    def _fill(self) -> dict:
        if self._data is None:
            self._data = self._build()
        return self._data

    def __getitem__(self, key):
        return self._fill()[key]

    def __setitem__(self, key, value):
        self._fill()[key] = value

    def __delitem__(self, key):
        del self._fill()[key]

    def __iter__(self):
        return iter(self._fill())

    def __len__(self):
        return len(self._fill())


class RustEngine:
    name = "rust"
    partitions = 1 << 20  # 分割軸のメンバーごとに範囲検索できる（分割軸の自動選択に使う）
    key_bits = 64  # 1 セルのキーは各軸のメンバー番号を詰めた 64 ビット整数

    def __init__(self):
        self.core = nanashi_core.Core()
        self._dims: dict[str, tuple[Any, int]] = {}  # 軸名 -> (Dimension, 番号)
        self._names: dict[int, str] = {}
        self._maps: dict[tuple[str, str], tuple[dict | None, int]] = {}  # (軸, プロパティ) -> (対応表, 番号)
        self._exprs: dict[int, tuple] = {}  # id(式) -> (式, 変換結果, 読む名前, 型, 警告)
        self._plan: tuple[Any, Any, list[str]] | None = None  # (Model の計算計画, Rust の計算計画, Metric 名)
        self._prop_plan: tuple[Any, Any, list[str]] | None = None  # 影響範囲の伝搬だけに使う、式だけの計画

    def fork(self, cat: Catalog) -> RustEngine:
        """cat（複製したモデル）用のエンジン。Rust 側の軸と対応表を引き継ぎ、番号も同じにする。"""
        other = RustEngine.__new__(RustEngine)
        other.core = self.core.fork()
        other._dims = {name: (cat.dimension(name), i) for name, (_, i) in self._dims.items()}
        other._names = dict(self._names)
        other._maps = dict(self._maps)
        other._exprs = dict(self._exprs)  # 式の変換結果は軸と対応表の番号だけに依存するので共有してよい
        other._plan = self._plan  # 計算計画も Metric の番号と式だけに依存する（複製は同じ計画を持つ）
        other._prop_plan = self._prop_plan
        return other

    def share(self, store):
        return self.core.share(store)

    # ------------------------------------------------ 名前 -> 番号

    def _dim(self, cat: Catalog, name: str) -> int:
        d = cat.dimension(name)
        cached = self._dims.get(name)
        if cached is not None and cached[0] is d:
            return cached[1]
        i = self.core.add_dim(len(d.members), d.ordered, name)
        self._dims[name] = (d, i)
        self._names[i] = name
        return i

    def _map(self, cat: Catalog, dim: str, prop: str) -> int:
        """プロパティの対応表の番号。対応表が変わったら、同じ番号のまま中身を置き換える
        （コンパイル済みの式が番号を持っているので、番号は変えない）。"""
        target, mapping = cat.dimension(dim).properties[prop]
        cached = self._maps.get((dim, prop))
        if cached is not None and cached[0] is mapping:
            return cached[1]
        src, dst = cat.dimension(dim), cat.dimension(target)
        fwd = [-1] * len(src.members)
        for s, t in mapping.items():
            fwd[src._index[s]] = dst._index[t]
        if cached is None:
            i = self.core.add_mapping(len(dst.members), fwd)
        else:
            i = cached[1]
            self.core.set_mapping(i, len(dst.members), fwd)
        self._maps[(dim, prop)] = (mapping, i)
        return i

    def _region(self, cat: Catalog, region) -> list[tuple[int, list[int]]]:
        out = []
        for d, ms in (region or {}).items():
            index = cat.dimension(d)._index
            out.append((self._dim(cat, d), [index[m] for m in ms]))
        return out

    def _region_lenient(self, cat: Catalog, region) -> list | None:
        """_region と同じだが、記録したあとで名前を変えたり消したりしたメンバーは読み飛ばす
        （分割軸の選択に使う影響範囲は、古い名前を含みうる）。どの軸も残らなければ None（影響なし）。"""
        out = []
        for d, ms in (region or {}).items():
            index = cat.dimension(d)._index
            kept = [index[m] for m in ms if m in index]
            if not kept:
                return None
            out.append((self._dim(cat, d), kept))
        return out

    def _index(self, cat: Catalog, dims, partition: str | None) -> int | None:
        if not dims:
            return None
        if partition is None:
            partition = max(dims, key=lambda d: len(cat.dimension(d).members))
        return self._dim(cat, partition)

    # ------------------------------------------------ 式の変換と型検査

    def check(self, expr: Expr, cat: Catalog) -> tuple[Type, list[str]]:
        """式の型（軸と値の種類）と警告。型の誤りは FormulaError（文言は Python の参照実装と同じ）。
        変換した式は取っておき、評価に使い回す。"""
        _, _, t, warnings = self._compile_full(expr, cat)
        return t, list(warnings)

    def _compile(self, expr: Expr, cat: Catalog) -> tuple[Any, list[str]]:
        compiled, names, _, _ = self._compile_full(expr, cat)
        return compiled, names

    def _compile_full(self, expr: Expr, cat: Catalog) -> tuple[Any, list[str], Type, list[str]]:
        cached = self._exprs.get(id(expr))
        if cached is not None and cached[0] is expr:
            return cached[1:]
        names: list[str] = []
        tree = self._tree(expr, cat, names)
        types = [self._type(cat, cat.metric_type(n)) for n in names]
        try:
            compiled, dims, kind, d, warnings = self.core.compile(tree, names, types)
        except ValueError as e:
            raise FormulaError(str(e)) from None
        t = Type(tuple(self._names[i] for i in dims), member_kind(self._names[d]) if kind == "member" else kind)
        self._exprs[id(expr)] = (expr, compiled, names, t, warnings)
        return compiled, names, t, warnings

    def _type(self, cat: Catalog, t: Type) -> tuple[list[int], str, int]:
        """Python の型を Rust に渡す形（軸の番号、種類、メンバー型なら軸の番号）にする。"""
        dims = [self._dim(cat, d) for d in t.dims]
        if t.kind.startswith("member:"):
            return dims, "member", self._dim(cat, t.kind.removeprefix("member:"))
        return dims, t.kind, -1

    def _tree(self, e: Expr, cat: Catalog, names: list[str]) -> tuple:
        """式を Rust の構文木（タプル）にする。名前を番号に直すだけで、型の検査は Rust が行う
        （メンバーの名前の検査だけはここで行い、文言は参照実装の型推論と同じにする）。"""
        t = lambda x: self._tree(x, cat, names)
        match e:
            case Ref(name):
                if name not in names:
                    names.append(name)
                return ("ref", names.index(name))
            case Const(value):
                return ("const", float(value), isinstance(value, bool))
            case DimRef(dim):
                return ("dimref", self._dim(cat, dim))
            case Member(dim, member):
                d = cat.dimension(dim)
                if member not in d:
                    raise FormulaError(f'{dim}."{member}": {dim} にメンバー {member!r} がない')
                return ("member", self._dim(cat, dim), d._index[member])
            case BinOp(op, left, right):
                return ("bin", op, t(left), t(right))
            case Not(child):
                return ("not", t(child))
            case If(cond, then, else_):
                return ("if", t(cond), t(then), None if else_ is None else t(else_))
            case Filter(child, cond):
                return ("filter", t(child), t(cond))
            case On(child, other):
                return ("on", t(child), t(other))
            case Coalesce(first, second):
                return ("coalesce", t(first), t(second))
            case Expand(child, dims):
                return ("expand", t(child), [self._dim(cat, d) for d in dims])
            case IsBlank(child):
                return ("isblank", t(child))
            case IfBlank(child, value):
                return ("ifblank", t(child), float(value), isinstance(value, bool))
            case By(child, dim, prop, agg):
                props = cat.dimension(dim).properties
                if prop in props:
                    target, _ = props[prop]
                    return ("by", t(child), self._dim(cat, dim), self._dim(cat, target), self._map(cat, dim, prop),
                            agg, dim, prop)
                if prop not in getattr(cat, "metrics", {}):
                    raise FormulaError(f"{dim} にプロパティ {prop} がなく、同じ名前の Metric もない")
                if prop not in names:  # 対応表がメンバー型の Metric。書き換えは Rust の型検査が行う
                    names.append(prop)
                return ("bymetric", t(child), self._dim(cat, dim), names.index(prop), agg, dim, prop)
            case Remove(child, dim, agg):
                return ("remove", t(child), self._dim(cat, dim), agg)
            case Shift(child, dim, n):
                return ("shift", t(child), self._dim(cat, dim), n)
            case AsAxis(child, dim):
                return ("asaxis", t(child), self._dim(cat, dim))
            case Select(child, dim, member):
                d = cat.dimension(dim)
                if member not in d:
                    raise FormulaError(f'SELECT {dim}."{member}": {dim} にメンバー {member!r} がない')
                return ("select", t(child), self._dim(cat, dim), d._index[member], member)
        raise TypeError(e)

    # ------------------------------------------------ 差分再計算の段取り

    def recalc_changes(self, plan, stores: dict, counts: dict, cat: Catalog, changed: dict, added: dict,
                       olds: dict, forced: dict, full: bool = False) -> tuple[list, Any]:
        """Model.recalc の差分の経路を Rust で行う。plan は Model.compiled()、stores と counts は
        Metric ごとの格納データと差分集計の件数、changed は入力の変更範囲、added は追加したメンバー、
        olds は差分集計の集計元になる入力の変更前の値、forced は必ず計算し直す計算 Metric の範囲。
        格納データはその場で書き換わる。

        再計算した (Metric の番号, 差分集計か, 範囲) の記録と、それを名前に直す関数を返す。
        """
        rplan, names = self._plan_for(plan, cat)
        index = {n: i for i, n in enumerate(names)}
        region = lambda r: self._region(cat, r)
        log = self.core.recalc_changes(
            rplan,
            [stores[n] for n in names],
            [counts.get(n) for n in names],
            [(index[n], region(r)) for n, r in changed.items()],
            [(self._dim(cat, d), [cat.dimension(d)._index[x] for x in ms]) for d, ms in added.items()],
            [(index[n], h) for n, h in olds.items()],
            [(index[n], region(r)) for n, r in forced.items()], full)

        def named(entries):
            return [(names[i], self._to_names(cat, r)) for i, _, r in entries]
        return [(names[i], delta) for i, delta, _ in log], lambda: named(log)

    def _to_names(self, cat: Catalog, region) -> dict:
        """Rust の範囲（軸の番号 -> メンバー番号の列）を、メンバー名の範囲にする。"""
        return {self._names[d]: frozenset(cat.dimension(self._names[d]).members[j] for j in ms) for d, ms in region}

    # ------------------------------------------------ 計算計画

    def plan(self, formulas: dict, dims: dict, cat: Catalog) -> tuple[list, list]:
        """依存グラフから計算計画を作る。formulas は Metric 名 -> 評価する式（入力は None）、dims は軸。
        返すのは、依存先が先の順の段階 (名前の組, scan の軸) と、段ごとの段階の番号の列と、
        依存グラフ（Metric 名 -> Edge の列）。循環の誤りは FormulaError（文言は Python の参照実装と同じ）。"""
        names = list(formulas)
        index = {n: i for i, n in enumerate(names)}
        items = []
        for n in names:
            f = formulas[n]
            if f is None:
                items.append(None)
            else:
                compiled, reads = self._compile(f, cat)
                items.append((compiled, [index[r] for r in reads]))
        try:
            steps, levels, edges = self.core.plan(items, names, [[self._dim(cat, d) for d in dims[n]] for n in names])
        except ValueError as e:
            raise FormulaError(str(e)) from None
        def graph():
            return {n: [Edge(names[t], tuple(sorted((self._names[d], k) for d, k in lags)),
                             frozenset(self._names[d] for d in broken)) for t, lags, broken in es]
                    for n, es in zip(names, edges)}
        return ([(tuple(names[i] for i in ms), None if d is None else self._names[d]) for ms, d in steps], levels,
                LazyEdges(graph))

    # ------------------------------------------------ 影響範囲

    def _prop_plan_for(self, plan, cat: Catalog) -> tuple[Any, list[str]]:
        """影響範囲の伝搬に使う、式だけの Rust の計算計画（差分集計の計画は要らない。分割軸を選ぶ時点では
        まだできていない）。計画を作り直すまで使い回す。"""
        if self._prop_plan is not None and self._prop_plan[0] is plan.steps:
            return self._prop_plan[1], self._prop_plan[2]
        names = list(cat.metrics)
        index = {n: i for i, n in enumerate(names)}

        def bound(expr):
            compiled, reads = self._compile(expr, cat)
            return compiled, [index[n] for n in reads]

        metrics = [(None if m.formula is None else bound(m.formula), False, False) for m in cat.metrics.values()]
        levels = [[(None if s.scan_dim is None else self._dim(cat, s.scan_dim), [index[n] for n in s.names])
                   for s in level] for level in plan.levels]
        rplan = self.core.make_plan(metrics, levels)
        self._prop_plan = (plan.steps, rplan, names)
        return rplan, names

    def _added(self, cat: Catalog, added) -> list:
        return [(self._dim(cat, d), [cat.dimension(d)._index[x] for x in ms]) for d, ms in (added or {}).items()]

    def propagate(self, plan, cat: Catalog, changed: dict, added=None) -> dict:
        """入力の変更範囲と追加したメンバーを計画の順に伝え、影響を受ける全 Metric の範囲（changed を含む）。"""
        rplan, names = self._prop_plan_for(plan, cat)
        index = {n: i for i, n in enumerate(names)}
        out = self.core.propagate(rplan, [(index[n], self._region(cat, r)) for n, r in changed.items()],
                                  self._added(cat, added))
        return {names[i]: self._to_names(cat, r) for i, r in out}

    def removal_regions(self, plan, stores: dict, cat: Catalog, dim: str, member: str) -> dict:
        """軸 dim のメンバー member を消すと値が変わる範囲（計算 Metric -> 消すメンバーを除いた範囲）。"""
        rplan, names = self._prop_plan_for(plan, cat)
        out = self.core.removal_regions(rplan, [stores[n] for n in names], self._dim(cat, dim),
                                        cat.dimension(dim)._index[member])
        return {names[i]: self._to_names(cat, r) for i, r in out}

    def affected(self, expr: Expr, cat: Catalog, regions: dict, added=None, removed=None):
        """1 つの式の影響範囲。regions は Metric 名 -> 変更範囲。"""
        compiled, names = self._compile(expr, cat)
        regs = [self._region_lenient(cat, regions[n]) if n in regions else None for n in names]
        gone = None
        if removed:
            (d, m), = removed.items()
            gone = (self._dim(cat, d), cat.dimension(d)._index[m])
        r = self.core.affected(compiled, regs, self._added(cat, added), gone)
        return None if r is None else self._to_names(cat, r)

    def _plan_for(self, plan, cat: Catalog) -> tuple[Any, list[str]]:
        """Model の計算計画（CompiledPlan）を Rust の計算計画にする。計画を作り直すまで使い回す。
        差分集計する Metric の件数の式と差分の式は Rust が作る。"""
        if self._plan is not None and self._plan[0] is plan.steps:
            return self._plan[1], self._plan[2]
        names = list(cat.metrics)
        index = {n: i for i, n in enumerate(names)}

        def bound(expr):
            compiled, reads = self._compile(expr, cat)
            return compiled, [index[n] for n in reads]

        metrics = [(None if m.formula is None else bound(m.formula), n in plan.delta, n in plan.sources)
                   for n, m in cat.metrics.items()]
        levels = [[(None if s.scan_dim is None else self._dim(cat, s.scan_dim), [index[n] for n in s.names])
                   for s in level] for level in plan.levels]
        rplan = self.core.make_plan(metrics, levels)
        self._plan = (plan.steps, rplan, names)
        return rplan, names

    def delta_plan(self, expr: Expr, cat: Catalog):
        """式が差分集計の対象なら、その計画（集計元、件数が要るかの印、対応表）。対象でなければ None。"""
        compiled, names = self._compile(expr, cat)
        found = self.core.delta_plan(compiled)
        if found is None:
            return None
        source, aux, needs_count = found
        return DeltaPlan(names[source], COUNTED if needs_count else None, tuple(names[a] for a in aux))

    # ------------------------------------------------ Engine

    def empty(self, dims, kind, partition=None, cat=None):
        ids = [self._dim(cat, d) for d in dims]
        return self.core.empty(ids, self._index(cat, dims, partition), kind == "boolean")

    def from_cells(self, dims, kind, cells, cat, partition=None):
        indexes = [cat.dimension(d)._index for d in dims]
        cols = [[indexes[i][k[i]] for k in cells] for i in range(len(dims))]
        values = [float(v) for v in cells.values()]
        return self.core.from_rows([self._dim(cat, d) for d in dims], self._index(cat, dims, partition),
                                   kind == "boolean", cols, values)

    def from_arrays(self, dims, kind, cols: dict, cat, partition=None):
        """軸ごとのメンバー番号の numpy 配列と、値の配列 __v から作る（大量投入用）。"""
        import numpy as np
        arrays = [np.ascontiguousarray(cols[d], dtype=np.uint32) for d in dims]
        values = np.ascontiguousarray(cols["__v"], dtype=np.float64)
        return self.core.from_arrays([self._dim(cat, d) for d in dims], self._index(cat, dims, partition),
                                     kind == "boolean", arrays, values)

    def partition_of(self, store):
        i = self.core.index_dim(store)
        return None if i is None else self._names[i]

    def repartition(self, store, partition, cat):
        return self.core.repartition(store, None if partition is None else self._dim(cat, partition))

    def dimension_changed(self, cat, dim, renumbered=False):
        """メンバー数と、dim が関わる対応表を Rust 側に反映する。"""
        if renumbered:  # 変換済みの式はメンバーの番号（定数、SELECT）を持っているので作り直す
            self._exprs.clear()
            self._plan = None
            self._prop_plan = None
        if dim in self._dims:
            d, i = self._dims[dim]
            if d is cat.dimension(dim):
                self.core.resize_dim(i, len(d.members))
        for (src, prop), (mapping, i) in list(self._maps.items()):
            target, current = cat.dimension(src).properties[prop]
            if dim in (src, target) or current is not mapping:
                self._maps[(src, prop)] = (None, i)  # 次の _map で中身を置き換えさせる
                self._map(cat, src, prop)

    def rename_member(self, store, dim, old, new, cat):
        return store  # キーはメンバーの番号なので、名前が変わっても何もしなくてよい

    def remove_member(self, store, dim, index, member, values, cat):
        self.core.remove_member(store, self._dim(cat, dim), index, values)
        return store

    def fit(self, store, cat):
        repacked = self.core.fit(store)
        return store if repacked is None else repacked

    def write(self, store, key, value, cat):
        dims = [self._names[i] for i in self.core.metric_dims(store)]
        codes = [cat.dimension(d)._index[m] for d, m in zip(dims, key)]
        self.core.write(store, codes, None if value is None else float(value))
        return store

    def evaluate(self, expr, cat, restrict):
        compiled, names = self._compile(expr, cat)
        return self.core.evaluate(compiled, [cat.source(n) for n in names], self._region(cat, restrict))

    def evaluate_many(self, items, cat):
        jobs = []
        for expr, restrict in items:
            compiled, names = self._compile(expr, cat)
            jobs.append((compiled, [cat.source(n) for n in names], self._region(cat, restrict)))
        return self.core.evaluate_many(jobs)

    def evaluate_with_count(self, expr, count_expr, cat, restrict):
        if count_expr is COUNTED:  # 件数の式は Rust が式から作る
            compiled, names = self._compile(expr, cat)
            sources = [cat.source(n) for n in names]
            region = self._region(cat, restrict)
            value, count = self.core.evaluate_many([(compiled, sources, region),
                                                    (self.core.count_formula(compiled), sources, region)])
            return value, count
        return self.evaluate_many([(expr, restrict), (count_expr, restrict)], cat)

    def filter(self, store, restrict, cat):
        return self.core.filter(store, self._region(cat, restrict))

    view = filter

    def reorder(self, c, dims):
        return c  # 書き戻すときに格納データの詰め方へ並べ直すので、ここでは何もしない

    def replace(self, store, region, new, cat):
        if store is None:
            dims = [self._names[i] for i in self._dims_of(new)]
            return self.core.store_from(new, self._index(cat, dims, None))
        self.core.replace(store, self._region(cat, region), new)
        return store

    def replace_diff(self, store, region, new, cat):
        return store, self._named(store, self.core.replace_diff(store, self._region(cat, region), new), cat)

    def drop_value(self, store, value, cat):
        self.core.drop_value(store, float(value))
        return store

    def region_of_value(self, store, value, cat):
        return self._named(store, self.core.region_of_value(store, float(value)), cat)

    def _named(self, store, sets, cat):
        """軸ごとのメンバー番号の集合を、メンバー名の範囲にする。"""
        if sets is None:
            return None
        dims = [self._names[i] for i in self.core.metric_dims(store)]
        return {d: frozenset(cat.dimension(d).members[j] for j in ms) for d, ms in zip(dims, sets)}

    def _dims_of(self, handle) -> list[int]:
        if isinstance(handle, nanashi_core.StoreHandle):
            return self.core.metric_dims(handle)
        raise TypeError("評価結果から直接格納データを作るには、軸を指定した empty() を使う")

    def to_cube(self, store, cat):
        cols, values, is_bool = self.core.rows(store)
        dims = tuple(self._names[i] for i in self.core.metric_dims(store))
        members = [cat.dimension(d).members for d in dims]
        if is_bool:
            values = [v != 0.0 for v in values]
        return Cube(dims, {tuple(members[j][cols[j][i]] for j in range(len(dims))): values[i]
                           for i in range(len(values))})

    def to_arrays(self, store, cat) -> dict:
        """軸ごとのメンバー番号の numpy 配列と、値の配列 __v（保存用。Python のオブジェクトを作らない）。"""
        cols, values, _ = self.core.arrays(store)
        dims = [self._names[i] for i in self.core.metric_dims(store)]
        return {d: c for d, c in zip(dims, cols)} | {"__v": values}

    def get(self, store, key, cat):
        dims = [self._names[i] for i in self.core.metric_dims(store)]
        v = self.core.get(store, [cat.dimension(d)._index[m] for d, m in zip(dims, key)])
        return v if v is None or not self._is_bool(store) else v != 0.0

    def rows(self, store, restrict, cat, offset=0, limit=None):
        cols, values, is_bool, total = self.core.rows_in(store, self._region(cat, restrict), offset, limit)
        dims = [self._names[i] for i in self.core.metric_dims(store)]
        members = [cat.dimension(d).members for d in dims]
        rows = [(tuple(members[j][cols[j][i]] for j in range(len(dims))), values[i] != 0.0 if is_bool else values[i])
                for i in range(len(values))]
        return rows, total

    def aggregate(self, store, dims, keep, agg, restrict, cat):
        """範囲を切り出してから、残さない軸を REMOVE で集計する（式の変換結果はキャッシュしない）。
        REMOVE は軸を 1 つずつ外すので、avg は sum と count を別々に集計してから割り、count は最初の軸だけ
        count で数えて残りは sum で足す（min、max、sum はそのまま重ねられる）。"""
        sliced = self.core.filter(store, self._region(cat, restrict))
        gone = [self._dim(cat, d) for d in dims if d not in keep]
        order = tuple(d for d in dims if d in keep)
        if not gone:  # 外す軸がなければ、各セルがそのまま 1 件のグループ
            cube = self.to_cube(sliced, cat).reorder(order)
            return Cube(order, {k: 1.0 for k in cube.cells}) if agg == "count" else cube

        source = ([self._dim(cat, d) for d in dims], "boolean" if self._is_bool(store) else "number", -1)

        def run(first: str, rest: str) -> Cube:
            tree = ("ref", 0)
            for i, d in enumerate(gone):
                tree = ("remove", tree, d, first if i == 0 else rest)
            compiled, *_ = self.core.compile(tree, ["__q"], [source])
            cube = self.core.evaluate(compiled, [sliced], [])
            return self.to_cube(self.core.store_from(cube, None), cat).reorder(order)

        if agg == "avg":
            total, count = run("sum", "sum"), run("count", "sum")
            return Cube(order, {k: v / count.cells[k] for k, v in total.cells.items()})
        if agg == "count":
            return run("count", "sum")
        return run(agg, agg)

    def _is_bool(self, store) -> bool:
        return bool(self.core.is_bool(store))

    def size(self, store) -> int:
        return self.core.size(store)

    def same(self, a, b) -> bool:
        return self.core.same_store(a, b)

    def diff(self, old, new):
        return self.core.diff_stores(old, new)
