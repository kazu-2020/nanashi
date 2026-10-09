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
from .delta import COUNTED, DeltaPlan
from .engine import parquet_columns, parquet_value
from .planner import Step
from .evaluate import Catalog, Edge, FormulaError, Type, member_kind, resolve
from .expr import (AsAxis, BinOp, By, Coalesce, Const, DimRef, Expand, Expr, Filter, If, IfBlank,
                   IsBlank, Member, Not, On, Ref, Remove, Select, Shift)
from .messages import from_rust


def _formula_error(e: nanashi_core.Diagnostic) -> FormulaError:
    code, params = from_rust(*e.args)
    return FormulaError(code, **params)


EXPRS_MAX = 20_000  # 式の変換結果を覚えておく数の上限


class RustEngine:
    name = "rust"
    partitions = 1 << 20  # 分割軸のメンバーごとに範囲検索できる（分割軸の自動選択に使う）
    key_bits = 64  # 1 セルのキーは各軸のメンバー番号を詰めた 64 ビット整数

    def __init__(self, **config):
        """config has the tuning values for the speed of Rust (nanashi_core.Core gets them; the results do not change)."""
        self.core = nanashi_core.Core(**config)
        self._dims: dict[str, tuple[Any, int]] = {}  # dimension id -> (Dimension, Rust number)
        self._names: dict[int, str] = {}  # Rust number -> dimension id
        self._maps: dict[tuple[str, str], tuple[dict | None, int]] = {}  # (dim id, prop id) -> (mapping, number)
        self._exprs: dict[int, tuple] = {}  # id(expr) -> (expr, compiled, read ids, type, warnings, read types)
        self._widths: dict[str, int] = {}  # dimension id -> the bit width at the type check
        self.planner = RustPlanner(self)

    def fork(self, cat: Catalog) -> RustEngine:
        """cat（複製したモデル）用のエンジン。Rust 側の軸と対応表を引き継ぎ、番号も同じにする。"""
        other = RustEngine.__new__(RustEngine)
        other.core = self.core.fork()
        other._dims = {d: (cat.dimension(d), i) for d, (_, i) in self._dims.items()}
        other._names = dict(self._names)
        other._maps = dict(self._maps)
        other._exprs = dict(self._exprs)
        other._widths = dict(self._widths)  # 式の変換結果は軸と対応表の番号だけに依存するので共有してよい
        other.planner = self.planner.fork(other)
        return other

    def share(self, store):
        return self.core.share(store)

    # ------------------------------------------------ 名前 -> 番号

    def _dim(self, cat: Catalog, name: str) -> int:
        """The Rust number of the dimension (an id). The Rust name of the dimension is its id, so a diagnostic
        carries the id, and Model._shown_params shows the current name."""
        d = cat.dimension(name)
        cached = self._dims.get(d.id)
        if cached is not None and cached[0] is d:
            return cached[1]
        i = self.core.add_dim(len(d.members), d.ordered, d.id)
        self._dims[d.id] = (d, i)
        self._widths[d.id] = max(1, (len(d.members) - 1).bit_length())
        self._names[i] = d.id
        return i

    def _map(self, cat: Catalog, dim: str, prop: str) -> int:
        """The Rust number of the mapping of the property (dim and prop are ids). A changed mapping keeps its
        number and gets new contents, because the compiled formulas hold the number."""
        target, mapping = cat.dimension(dim).properties[prop]
        cached = self._maps.get((dim, prop))
        if cached is not None and cached[0] is mapping:
            return cached[1]
        src, dst = cat.dimension(dim), cat.dimension(target)
        fwd = [-1] * len(src.members)
        for s, t in mapping.items():
            fwd[src._by_id[s]] = dst._by_id[t]
        if cached is None:
            i = self.core.add_mapping(len(dst.members), fwd)
        else:
            i = cached[1]
            self.core.set_mapping(i, len(dst.members), fwd)
        self._maps[(dim, prop)] = (mapping, i)
        return i

    def _region(self, cat: Catalog, region) -> list[tuple[int, list[int]]]:
        """A restrict (dimension id -> member ids) as (Rust dimension number, member numbers) pairs."""
        out = []
        for d, ms in (region or {}).items():
            index = cat.dimension(d)._by_id
            out.append((self._dim(cat, d), [index[m] for m in ms]))
        return out

    def _region_lenient(self, cat: Catalog, region) -> list | None:
        """The same as _region, but a member that was removed after the range was recorded is skipped (the
        ranges that select the partition dimension can hold one). None if no dimension stays (no effect)."""
        out = []
        for d, ms in (region or {}).items():
            index = cat.dimension(d)._by_id
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

    def _compile(self, expr: Expr, cat: Catalog) -> tuple[Any, list[str]]:
        compiled, names, _, _ = self._compile_full(expr, cat)
        return compiled, names

    def _compile_full(self, expr: Expr, cat: Catalog) -> tuple[Any, list[str], Type, list[str]]:
        cached = self._exprs.get(id(expr))
        if cached is not None and cached[0] is expr:
            # 型検査の結果と変換結果は参照先の型に依存する。参照先の軸や値の種類が変わったら作り直す
            # （Model は定義を変えても同じ式オブジェクトを渡してくる）
            _, compiled, names, t, warnings, reads = cached
            if all(cat.metric_type(n) == r for n, r in zip(names, reads)):
                return compiled, names, t, warnings
        names: list[str] = []
        tree = self._tree(expr, cat, names)
        reads = [cat.metric_type(n) for n in names]
        try:
            compiled, dims, kind, d, found = self.core.compile(tree, names, [self._type(cat, r) for r in reads])
        except nanashi_core.Diagnostic as e:
            raise _formula_error(e) from None
        warnings = [from_rust(code, params) for code, params in found]
        t = Type(tuple(self._names[i] for i in dims), member_kind(self._names[d]) if kind == "member" else kind)
        if len(self._exprs) >= EXPRS_MAX:  # 定義を何度も変えても増え続けないように、溢れたら作り直させる
            self._exprs.clear()
        self._exprs[id(expr)] = (expr, compiled, names, t, warnings, reads)
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
                if member not in d._by_id:
                    raise FormulaError("unknown_member", dim=dim, member=member)
                return ("member", self._dim(cat, dim), d._by_id[member])
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
                    raise FormulaError("no_property_or_metric", dim=dim, prop=prop)
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
                if member not in d._by_id:
                    raise FormulaError("select_member", dim=dim, member=member)
                return ("select", t(child), self._dim(cat, dim), d._by_id[member], member)
        raise TypeError(e)

    def _to_ids(self, cat: Catalog, region) -> dict:
        """A Rust range (dimension number -> member numbers) as a restrict (dimension id -> member ids)."""
        return {self._names[d]: frozenset(cat.dimension(self._names[d]).ids[j] for j in ms) for d, ms in region}

    # ------------------------------------------------ Store

    def empty(self, dims, kind, partition=None, cat=None):
        ids = [self._dim(cat, d) for d in dims]
        return self.core.empty(ids, self._index(cat, dims, partition), kind == "boolean")

    def from_cells(self, dims, kind, cells, cat, partition=None):
        indexes = [cat.dimension(d)._by_id for d in dims]
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

    def to_parquet(self, store, dims, kind, cat, meta) -> bytes:
        stored = [self._names[i] for i in self.core.metric_dims(store)]
        if stored != [cat.dimension(d).id for d in dims]:
            name = lambda ds: [cat.dimension(d).name for d in ds]
            raise ValueError(f"格納データの軸 {name(stored)} が {name(dims)} と違う")
        return self.core.store_to_parquet(store, parquet_columns(dims, cat), parquet_value(kind), list(meta.items()))

    def from_parquet(self, data, dims, kind, cat, partition=None):
        return self.core.store_from_parquet(data, [self._dim(cat, d) for d in dims], self._index(cat, dims, partition),
                                            parquet_value(kind), parquet_columns(dims, cat))

    def partition_of(self, store):
        i = self.core.index_dim(store)
        return None if i is None else self._names[i]

    def repartition(self, store, partition, cat):
        return self.core.repartition(store, None if partition is None else self._dim(cat, partition))

    def dimension_changed(self, cat, dim, renumbered=False):
        """Send the member count of dim (an id) and the mappings that use dim to Rust. If the catalog does not
        have dim (a removed dimension), forget it and its mappings. Rust keeps its numbers, and nothing uses them."""
        if dim not in cat.dimensions:
            self._dims.pop(dim, None)
            self._widths.pop(dim, None)
            self._maps = {k: v for k, v in self._maps.items() if k[0] != dim}
            return
        if renumbered:  # 変換済みの式はメンバーの番号（定数、SELECT）を持っているので作り直す
            self._exprs.clear()
            self.planner.forget()
        if dim in self._dims:
            d, i = self._dims[dim]
            if d is cat.dimension(dim):
                self.core.resize_dim(i, len(d.members))
                # 型検査は途中の結果がキーに収まるかも確かめているので、軸のビット幅が変わったら検査し直す
                width = max(1, (len(d.members) - 1).bit_length())
                if self._widths.get(dim, width) != width:
                    self._exprs.clear()
                self._widths[dim] = width
        for (src, prop), (mapping, i) in list(self._maps.items()):
            target, current = cat.dimension(src).properties[prop]
            if dim in (src, target) or current is not mapping:
                self._maps[(src, prop)] = (None, i)  # 次の _map で中身を置き換えさせる
                self._map(cat, src, prop)

    def remove_member(self, store, dim, index, member, values, cat):
        self.core.remove_member(store, self._dim(cat, dim), index, values)
        return store

    def fit(self, store, cat):
        repacked = self.core.fit(store)
        return store if repacked is None else repacked

    def write(self, store, key, value, cat):
        dims = [self._names[i] for i in self.core.metric_dims(store)]
        codes = [cat.dimension(d)._by_id[m] for d, m in zip(dims, key)]
        self.core.write(store, codes, None if value is None else float(value))
        return store

    def write_many(self, store, cols, values, cat):
        self.core.write_many(store, cols, values)  # int や bool も f64 として受け取る
        return store

    def columns(self, store, restrict, cat):
        cols, values, is_bool, _ = self.core.rows_in(store, self._region(cat, restrict))
        return cols, [v != 0.0 for v in values] if is_bool else values

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
        """The member number sets of each dimension of the store as a restrict (dimension id -> member ids)."""
        if sets is None:
            return None
        dims = [self._names[i] for i in self.core.metric_dims(store)]
        return {d: frozenset(cat.dimension(d).ids[j] for j in ms) for d, ms in zip(dims, sets)}

    def _dims_of(self, handle) -> list[int]:
        if isinstance(handle, nanashi_core.StoreHandle):
            return self.core.metric_dims(handle)
        raise TypeError("評価結果から直接格納データを作るには、軸を指定した empty() を使う")

    def to_cube(self, store, cat):
        cols, values, is_bool = self.core.rows(store)
        dims = tuple(self._names[i] for i in self.core.metric_dims(store))
        members = [cat.dimension(d).ids for d in dims]
        if is_bool:
            values = [v != 0.0 for v in values]
        return Cube(dims, {tuple(members[j][cols[j][i]] for j in range(len(dims))): values[i]
                           for i in range(len(values))})

    def get(self, store, key, cat):
        dims = [self._names[i] for i in self.core.metric_dims(store)]
        v = self.core.get(store, [cat.dimension(d)._by_id[m] for d, m in zip(dims, key)])
        return v if v is None or not self._is_bool(store) else v != 0.0

    def rows(self, store, restrict, cat, offset=0, limit=None):
        dims = [self._names[i] for i in self.core.metric_dims(store)]
        ranks = [cat.dimension(d).rank_table() for d in dims]
        cols, values, is_bool, total = self.core.rows_in(store, self._region(cat, restrict), ranks, offset, limit)
        members = [cat.dimension(d).ids for d in dims]
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

    def size_hint(self, store) -> int:
        return self.core.size_hint(store)

    def memory(self, store) -> dict:
        rows, base, delta_rows, delta, index = self.core.memory(store)
        return {"rows": rows, "base": base, "delta_rows": delta_rows, "delta": delta, "index": index}

    def same(self, a, b) -> bool:
        return self.core.same_store(a, b)

    def diff_block(self, old, new):
        return self.core.diff_block(old, new)

    def apply_block(self, store, block, dim_ids, value_ids):
        self.core.apply_block(store, block, dim_ids, value_ids)
        return store


class RustPlanner:
    """Planner（planner.py）を Rust で行う。式の変換と軸の番号は RustEngine のものを使う。"""

    def __init__(self, engine: RustEngine):
        self.e = engine
        self._plan: tuple[Any, Any, list[str]] | None = None  # (Model の計算計画, Rust の計算計画, Metric 名)
        self._prop_plan: tuple[Any, Any, list[str]] | None = None  # 影響範囲の伝搬だけに使う、式だけの計画

    def fork(self, engine: RustEngine) -> RustPlanner:
        other = RustPlanner(engine)
        other._plan = self._plan  # 計算計画も Metric の番号と式だけに依存する（複製は同じ計画を持つ）
        other._prop_plan = self._prop_plan
        return other

    def forget(self) -> None:
        """Rust の計算計画を捨てる（メンバーの番号が詰まって、変換済みの式を作り直すとき）。"""
        self._plan = None
        self._prop_plan = None

    def check(self, written: Expr, cat: Catalog) -> tuple[Expr, Type, list[str]]:
        """式を評価できる形に直して型を検査する。Metric を使った BY は、Python でなく Rust の型検査が
        書き換える（check.rs）。型の誤りは FormulaError（コードと値は Python の参照実装と同じ）。
        変換した式は取っておき、評価に使い回す。"""
        formula = resolve(written, cat, by_metric=False)
        _, _, t, warnings = self.e._compile_full(formula, cat)
        return formula, t, list(warnings)

    def estimate(self, expr: Expr, cat: Catalog, cells) -> float:
        """型を決めた式の結果のセル数の見積もり（上限）。cells は Metric ごとのセル数。
        意味は参照実装（evaluate.estimate）と同じ。"""
        compiled, names = self.e._compile(expr, cat)
        refs = [(self.e._type(cat, cat.metric_type(n))[0], float(cells[n])) for n in names]
        return self.e.core.estimate(compiled, refs)

    # ------------------------------------------------ 差分再計算の段取り

    def recalc(self, plan, stores: dict, counts: dict, cat: Catalog, changed: dict, added: dict,
               olds: dict, forced: dict, full: bool = False) -> tuple[list, Any]:
        """PyPlanner.recalc と同じ段取りを Rust で行う。plan は Model.compiled()、stores と counts は
        Metric ごとの格納データと差分集計の件数、changed は入力の変更範囲、added は追加したメンバー、
        olds は差分集計の集計元になる入力の変更前の値、forced は必ず計算し直す計算 Metric の範囲。
        格納データはその場で書き換わる。

        再計算した (Metric の番号, 差分集計か, 範囲) の記録と、それを名前に直す関数を返す。
        """
        rplan, names = self._plan_for(plan, cat)
        index = {n: i for i, n in enumerate(names)}
        region = lambda r: self.e._region(cat, r)
        log = self.e.core.recalc_changes(
            rplan,
            [stores[n] for n in names],
            [counts.get(n) for n in names],
            [(index[n], region(r)) for n, r in changed.items()],
            self._added(cat, added),
            [(index[n], h) for n, h in olds.items()],
            [(index[n], region(r)) for n, r in forced.items()], full)

        def named(entries):
            return [(names[i], self.e._to_ids(cat, r)) for i, _, r in entries]
        return [(names[i], delta) for i, delta, _ in log], lambda: named(log)

    # ------------------------------------------------ 計算計画

    def plan(self, formulas: dict, dims: dict, cat: Catalog) -> tuple[list, Any, list]:
        """Make the calculation plan and the dependency graph (Metric name -> list of Edge).
        A cycle causes a FormulaError (the code and the values are the same as in the reference implementation)."""
        names = list(formulas)
        index = {n: i for i, n in enumerate(names)}
        items = []
        for n in names:
            f = formulas[n]
            if f is None:
                items.append(None)
            else:
                compiled, reads = self.e._compile(f, cat)
                items.append((compiled, [index[r] for r in reads]))
        try:
            steps, levels, edges = self.e.core.plan(items, names, [[self.e._dim(cat, d) for d in dims[n]] for n in names])
        except nanashi_core.Diagnostic as e:
            raise _formula_error(e) from None

        graph = {n: [Edge(names[t], tuple(sorted((self.e._names[d], k) for d, k in lags)),
                          frozenset(self.e._names[d] for d in broken)) for t, lags, broken in es]
                 for n, es in zip(names, edges)}
        plan = [Step(tuple(names[i] for i in ms), None if d is None else self.e._names[d]) for ms, d in steps]
        return plan, graph, [[plan[i] for i in level] for level in levels]

    # ------------------------------------------------ 影響範囲

    def _added(self, cat: Catalog, added) -> list:
        return [(self.e._dim(cat, d), [cat.dimension(d)._by_id[x] for x in ms]) for d, ms in (added or {}).items()]

    def propagate(self, plan, cat: Catalog, changed: dict, added=None) -> dict:
        """入力の変更範囲と追加したメンバーを計画の順に伝え、影響を受ける全 Metric の範囲（changed を含む）。"""
        rplan, names = self._plan_for(plan, cat, prop=True)
        index = {n: i for i, n in enumerate(names)}
        out = self.e.core.propagate(rplan, [(index[n], self.e._region(cat, r)) for n, r in changed.items()],
                                  self._added(cat, added))
        return {names[i]: self.e._to_ids(cat, r) for i, r in out}

    def removal_regions(self, plan, stores: dict, cat: Catalog, dim: str, member: str) -> dict:
        """The ranges that change when the member (an id) of dim goes (formula Metric -> the range without it)."""
        rplan, names = self._plan_for(plan, cat, prop=True)
        out = self.e.core.removal_regions(rplan, [stores[n] for n in names], self.e._dim(cat, dim),
                                        cat.dimension(dim)._by_id[member])
        return {names[i]: self.e._to_ids(cat, r) for i, r in out}

    def affected(self, expr: Expr, cat: Catalog, regions: dict, added=None, removed=None):
        """1 つの式の影響範囲。regions は Metric 名 -> 変更範囲。"""
        compiled, names = self.e._compile(expr, cat)
        regs = [self.e._region_lenient(cat, regions[n]) if n in regions else None for n in names]
        gone = None
        if removed:
            (d, m), = removed.items()
            gone = (self.e._dim(cat, d), cat.dimension(d)._by_id[m])
        r = self.e.core.affected(compiled, regs, self._added(cat, added), gone)
        return None if r is None else self.e._to_ids(cat, r)

    def _plan_for(self, plan, cat: Catalog, *, prop: bool = False) -> tuple[Any, list[str]]:
        """Make the Rust calculation plan from the Model plan (CompiledPlan). Use it again until the plan changes.
        If prop is true, make the plan for the propagation of the affected range. It has only the formulas,
        because the incremental aggregation plan is not ready when the engine selects the partition dimension.
        Otherwise, Rust makes the count and delta formulas of the Metrics with incremental aggregation."""
        slot = "_prop_plan" if prop else "_plan"
        cached = getattr(self, slot)
        if cached is not None and cached[0] is plan.steps:
            return cached[1], cached[2]
        names = list(cat.metrics)
        index = {n: i for i, n in enumerate(names)}

        def bound(expr):
            compiled, reads = self.e._compile(expr, cat)
            return compiled, [index[n] for n in reads]

        metrics = [(None if m.formula is None else bound(m.formula), not prop and n in plan.delta,
                    not prop and n in plan.sources) for n, m in cat.metrics.items()]
        levels = [[(None if s.scan_dim is None else self.e._dim(cat, s.scan_dim), [index[n] for n in s.names])
                   for s in level] for level in plan.levels]
        rplan = self.e.core.make_plan(metrics, levels)
        setattr(self, slot, (plan.steps, rplan, names))
        return rplan, names

    def delta_plan(self, expr: Expr, cat: Catalog):
        """式が差分集計の対象なら、その計画（集計元、件数が要るかの印、対応表）。対象でなければ None。"""
        compiled, names = self.e._compile(expr, cat)
        found = self.e.core.delta_plan(compiled)
        if found is None:
            return None
        source, aux, needs_count = found
        return DeltaPlan(names[source], COUNTED if needs_count else None, tuple(names[a] for a in aux))
