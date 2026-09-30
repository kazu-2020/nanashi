"""Rust（nanashi_core）によるエンジン。

Metric の格納データと評価の途中結果は Rust 側に置き、Python にはハンドルだけを返す。
式は初回に Rust 側の構文木へ変換してキャッシュし、評価は Rust で行う。
1 回の評価の固定コストがほぼないので、小さな範囲の評価を Metric ごとに何百回繰り返しても速い。

格納データは分割軸を最上位ビットに詰めた整数キーの B 木で持つ。分割軸で絞った範囲の
読み書きは、その範囲の行数だけで済む。
"""
from __future__ import annotations

from typing import Any

import nanashi_core

from .core import Cube
from .evaluate import Catalog, _merge, infer
from .expr import (AsAxis, BinOp, By, Coalesce, Const, DimRef, Expand, Expr, Filter, If, IfBlank,
                   IsBlank, Member, Not, On, Ref, Remove, Select, Shift)


class _Typed:
    """一時的な名前（差分集計の作業データ）の型を足した Catalog。式の変換で型推論に使う。"""

    def __init__(self, cat: Catalog, types: dict):
        self._cat, self._types = cat, types

    def dimension(self, name: str):
        return self._cat.dimension(name)

    def metric_type(self, name: str):
        t = self._types.get(name)
        return t if t is not None else self._cat.metric_type(name)

    @property
    def metrics(self):
        return self._cat.metrics


class RustEngine:
    name = "rust"
    partitions = 1 << 20  # 分割軸のメンバーごとに範囲検索できる（分割軸の自動選択に使う）
    key_bits = 64  # 1 セルのキーは各軸のメンバー番号を詰めた 64 ビット整数

    def __init__(self):
        self.core = nanashi_core.Core()
        self._dims: dict[str, tuple[Any, int]] = {}  # 軸名 -> (Dimension, 番号)
        self._names: dict[int, str] = {}
        self._maps: dict[tuple[str, str], tuple[dict | None, int]] = {}  # (軸, プロパティ) -> (対応表, 番号)
        self._exprs: dict[int, tuple[Expr, Any, list[str]]] = {}  # id(式) -> (式, 変換結果, 読む名前)
        self._plan: tuple[Any, Any, list[str]] | None = None  # (Model の計算計画, Rust の計算計画, Metric 名)

    def fork(self, cat: Catalog) -> RustEngine:
        """cat（複製したモデル）用のエンジン。Rust 側の軸と対応表を引き継ぎ、番号も同じにする。"""
        other = RustEngine.__new__(RustEngine)
        other.core = self.core.fork()
        other._dims = {name: (cat.dimension(name), i) for name, (_, i) in self._dims.items()}
        other._names = dict(self._names)
        other._maps = dict(self._maps)
        other._exprs = dict(self._exprs)  # 式の変換結果は軸と対応表の番号だけに依存するので共有してよい
        other._plan = self._plan  # 計算計画も Metric の番号と式だけに依存する（複製は同じ計画を持つ）
        return other

    def share(self, store):
        return self.core.share(store)

    # ------------------------------------------------ 名前 -> 番号

    def _dim(self, cat: Catalog, name: str) -> int:
        d = cat.dimension(name)
        cached = self._dims.get(name)
        if cached is not None and cached[0] is d:
            return cached[1]
        i = self.core.add_dim(len(d.members), d.ordered)
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

    def _index(self, cat: Catalog, dims, partition: str | None) -> int | None:
        if not dims:
            return None
        if partition is None:
            partition = max(dims, key=lambda d: len(cat.dimension(d).members))
        return self._dim(cat, partition)

    # ------------------------------------------------ 式の変換

    def _compile(self, expr: Expr, cat: Catalog) -> tuple[Any, list[str]]:
        cached = self._exprs.get(id(expr))
        if cached is not None and cached[0] is expr:
            return cached[1], cached[2]
        names: list[str] = []
        compiled = self.core.compile(self._tree(expr, cat, names))
        self._exprs[id(expr)] = (expr, compiled, names)
        return compiled, names

    def _tree(self, e: Expr, cat: Catalog, names: list[str]) -> tuple:
        """式を Rust の構文木（タプル）にする。Bin、If、IsBlank、IfBlank には、メンバーを追加したときに
        値が広がる軸を添える（evaluate.affected の grow と同じ規則）。"""
        t = lambda x: self._tree(x, cat, names)
        ids = lambda dims: [self._dim(cat, d) for d in dims]
        dims_of = lambda x: infer(x, cat, []).dims
        match e:
            case Ref(name):
                if name not in names:
                    names.append(name)
                return ("ref", names.index(name))
            case Const(value):
                return ("const", float(value), isinstance(value, bool))
            case DimRef(dim):
                return ("dimref", self._dim(cat, dim))
            case Member(dim, member):  # 値はメンバーの番号（順序付きの軸では並び順で比べられる）
                return ("const", float(cat.dimension(dim)._index[member]), False)
            case BinOp(op, left, right):
                grow = []
                if op in ("+", "-", "and", "or"):
                    ld, rd = dims_of(left), dims_of(right)
                    grow = [d for d in _merge(ld, rd) if d not in ld or d not in rd]
                return ("bin", op, t(left), t(right), ids(grow))
            case Not(child):
                return ("not", t(child))
            case If(cond, then, else_):
                cd = dims_of(cond)
                branches = [dims_of(b) for b in (then, else_) if b is not None]
                grow = [d for d in _merge(cd, *branches) if any(d not in _merge(cd, b) for b in branches)]
                return ("if", t(cond), t(then), None if else_ is None else t(else_), ids(grow))
            case Filter(child, cond):
                return ("filter", t(child), t(cond))
            case On(child, other):
                return ("on", t(child), t(other))
            case Coalesce(first, second):
                return ("coalesce", t(first), t(second))
            case Expand(child, dims):
                return ("expand", t(child), [self._dim(cat, d) for d in dims])
            case IsBlank(child):
                return ("isblank", t(child), ids(dims_of(child)))
            case IfBlank(child, value):
                return ("ifblank", t(child), float(value), ids(dims_of(child)))
            case By(child, dim, prop, agg):
                target, _ = cat.dimension(dim).properties[prop]
                ids = (self._dim(cat, dim), self._dim(cat, target), self._map(cat, dim, prop))
                if dim in infer(child, cat, []).dims:
                    return ("byagg", t(child), *ids, agg or "sum")
                return ("bylookup", t(child), *ids)
            case Remove(child, dim, agg):
                return ("remove", t(child), self._dim(cat, dim), agg)
            case Shift(child, dim, n):
                return ("shift", t(child), self._dim(cat, dim), n)
            case AsAxis(child, dim):
                return ("asaxis", t(child), self._dim(cat, dim))
            case Select(child, dim, member):
                return ("select", t(child), self._dim(cat, dim), cat.dimension(dim)._index[member])
        raise TypeError(e)

    # ------------------------------------------------ 差分再計算の段取り

    def recalc_changes(self, plan, stores: dict, counts: dict, cat: Catalog, changed: dict, added: dict,
                       olds: dict, forced: dict) -> tuple[list, Any]:
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
            [(index[n], region(r)) for n, r in forced.items()])

        def named(entries):
            return [(names[i], {self._names[d]: frozenset(cat.dimension(self._names[d]).members[j] for j in ms)
                                for d, ms in r}) for i, _, r in entries]
        return [(names[i], delta) for i, delta, _ in log], lambda: named(log)

    def _plan_for(self, plan, cat: Catalog) -> tuple[Any, list[str]]:
        """Model の計算計画（CompiledPlan）を Rust の計算計画にする。計画を作り直すまで使い回す。"""
        if self._plan is not None and self._plan[0] is plan.steps:
            return self._plan[1], self._plan[2]
        names = list(cat.metrics)
        index = {n: i for i, n in enumerate(names)}

        def bound(expr):
            compiled, reads = self._compile(expr, cat)
            return compiled, [index[n] for n in reads]

        metrics = []
        for n in names:
            m = cat.metrics[n]
            count = delta = None
            dp = plan.delta.get(n)
            if dp is not None:
                count = None if dp.count is None else bound(dp.count)
                delta = self._delta_parts(plan, cat, m, dp, index)
            metrics.append((None if m.formula is None else bound(m.formula), count, delta, n in plan.sources))
        levels = [[(None if s.scan_dim is None else self._dim(cat, s.scan_dim), [index[n] for n in s.names])
                   for s in level] for level in plan.levels]
        rplan = self.core.make_plan(metrics, levels)
        self._plan = (plan.steps, rplan, names)
        return rplan, names

    def _delta_parts(self, plan, cat: Catalog, m, dp, index: dict) -> tuple:
        """差分集計の ([集計元, 対応表...], 件数の差分の式, 値の差分の式)。差分の式が読む __new{i} と
        __old{i}（plan.delta_exprs の作業データ）は、作業データの番号 2i と 2i + 1 にする。"""
        inputs = (dp.source, *dp.aux)
        d_count, d_value = plan.delta_exprs(m, dp)
        types = {}
        for i, n in enumerate(inputs):
            types[f"__new{i}"] = types[f"__old{i}"] = cat.metric_type(n)
        typed = _Typed(cat, types)  # 作業データの型を足した Catalog で変換する（cat は書き換えない）

        def slots(expr):
            compiled, reads = self._compile(expr, typed)
            return compiled, [2 * int(r[5:]) + (r.startswith("__old")) for r in reads]
        count = slots(d_count)
        value = None if dp.count is None else slots(d_value)
        return [index[n] for n in inputs], count, value

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

        def run(first: str, rest: str) -> Cube:
            tree = ("ref", 0)
            for i, d in enumerate(gone):
                tree = ("remove", tree, d, first if i == 0 else rest)
            cube = self.core.evaluate(self.core.compile(tree), [sliced], [])
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
