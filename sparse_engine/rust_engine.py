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
from .evaluate import Catalog, infer
from .expr import (AsAxis, BinOp, By, Coalesce, Const, DimRef, Expand, Expr, Filter, If, IfBlank,
                   IsBlank, Member, Not, On, Ref, Remove, Select, Shift)


class RustEngine:
    name = "rust"
    partitions = 1 << 20  # 分割軸のメンバーごとに範囲検索できる（分割軸の自動選択に使う）

    def __init__(self):
        self.core = nanashi_core.Core()
        self._dims: dict[str, tuple[Any, int]] = {}  # 軸名 -> (Dimension, 番号)
        self._names: dict[int, str] = {}
        self._maps: dict[tuple[str, str], tuple[dict | None, int]] = {}  # (軸, プロパティ) -> (対応表, 番号)
        self._exprs: dict[int, tuple[Expr, Any, list[str]]] = {}  # id(式) -> (式, 変換結果, 読む名前)

    def fork(self, cat: Catalog) -> RustEngine:
        """cat（複製したモデル）用のエンジン。Rust 側の軸と対応表を引き継ぎ、番号も同じにする。"""
        other = RustEngine.__new__(RustEngine)
        other.core = self.core.fork()
        other._dims = {name: (cat.dimension(name), i) for name, (_, i) in self._dims.items()}
        other._names = dict(self._names)
        other._maps = dict(self._maps)
        other._exprs = dict(self._exprs)  # 式の変換結果は軸と対応表の番号だけに依存するので共有してよい
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
            case Member(dim, member):  # 値はメンバーの番号（順序付きの軸では並び順で比べられる）
                return ("const", float(cat.dimension(dim)._index[member]), False)
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
                return ("ifblank", t(child), float(value))
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

    def size(self, store) -> int:
        return self.core.size(store)
