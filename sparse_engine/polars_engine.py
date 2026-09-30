"""Polars による列指向のエンジン。

Cube は「軸ごとのメンバー番号（UInt32）の列 + 値の列 __v」の DataFrame で持つ。
空のセルは行がないことで表し、null を保存することはない。
演算の意味は evaluate.py の参照実装と同じで、テストでは参照実装と結果を突き合わせる。

    * / 比較 / IF / FILTER / ON  -> INNER JOIN
    + - AND OR                   -> FULL OUTER JOIN（AND / OR は Polars の三値論理そのまま）
    BY 集約 / REMOVE             -> マッピング表との JOIN + GROUP BY
    BY 引き下ろし                -> マッピング表との JOIN
    EXPAND / 定数の展開          -> メンバー表との CROSS JOIN
    SELECT / PREVIOUS            -> 番号の列に n を足す
"""
from __future__ import annotations

import operator
from dataclasses import dataclass
from functools import reduce
from typing import Callable

import polars as pl

from .core import Cube
from .evaluate import Catalog, Restrict, _merge, _property, _replace, _without, infer
from .expr import (BinOp, By, Const, Expand, Expr, Filter, If, IfBlank, IsBlank, Not, On, Ref,
                   Remove, Shift)

V = "__v"
VR = "__v__r"
CODE = pl.UInt32


def _dtype(kind: str) -> pl.DataType:
    return pl.Boolean if kind == "boolean" else pl.Float64


@dataclass
class PCube:
    """格納用は DataFrame、評価の途中は LazyFrame。式 1 つを 1 つのクエリプランにして最後に実行する。"""
    dims: tuple[str, ...]
    df: pl.DataFrame | pl.LazyFrame

    def __len__(self) -> int:
        return self.collect().height

    @property
    def lazy(self) -> pl.LazyFrame:
        return self.df.lazy() if isinstance(self.df, pl.DataFrame) else self.df

    @property
    def dtype(self) -> pl.DataType:
        schema = self.df.schema if isinstance(self.df, pl.DataFrame) else self.df.collect_schema()
        return schema[V]

    def collect(self) -> pl.DataFrame:
        return self.df if isinstance(self.df, pl.DataFrame) else self.df.collect()


def _empty(dims: tuple[str, ...], dtype: pl.DataType) -> PCube:
    return PCube(dims, pl.DataFrame(schema={**{d: CODE for d in dims}, V: dtype}))


def _const(value) -> PCube:
    dtype = pl.Boolean if isinstance(value, bool) else pl.Float64
    return PCube((), pl.DataFrame({V: pl.Series([value], dtype=dtype)}))


def _codes(cat: Catalog, dim: str, members) -> pl.Series:
    index = cat.dimension(dim)._index
    return pl.Series(sorted(index[m] for m in members), dtype=CODE)


def _member_frame(cat: Catalog, dim: str, restrict: Restrict | None) -> pl.LazyFrame:
    if restrict and dim in restrict:
        return pl.DataFrame({dim: _codes(cat, dim, restrict[dim])}).lazy()
    n = len(cat.dimension(dim).members)
    return pl.DataFrame({dim: pl.int_range(0, n, dtype=CODE, eager=True)}).lazy()


def _in_region(dims, region: Restrict, cat: Catalog) -> pl.Expr | None:
    conds = [pl.col(d).is_in(pl.lit(_codes(cat, d, region[d])).implode()) for d in dims if d in region]
    return reduce(operator.and_, conds) if conds else None


def _filter(c: PCube, restrict: Restrict | None, cat: Catalog) -> PCube:
    cond = _in_region(c.dims, restrict or {}, cat)
    return c if cond is None else PCube(c.dims, c.df.filter(cond))


def _apply(op: str, a: pl.Expr, b: pl.Expr) -> pl.Expr:
    match op:
        case "+": return a + b
        case "-": return a - b
        case "*": return a * b
        case "/": return pl.when(b != 0).then(a / b)  # 0 除算は空
        case "=": return a == b
        case "<>": return a != b
        case "<": return a < b
        case "<=": return a <= b
        case ">": return a > b
        case ">=": return a >= b
        case "and": return a & b
        case "or": return a | b
    raise ValueError(op)


def _left(a, b): return a
def _right(a, b): return b


def _intersect(l: PCube, r: PCube, fn: Callable[[pl.Expr, pl.Expr], pl.Expr]) -> PCube:
    """INNER JOIN。両側に値があるセルだけ結果を持つ。fn が null を返したセルは空。

    共有する軸がなければ CROSS JOIN。定数（1 行）や空の Cube との演算もこれで扱える。
    """
    shared = [d for d in l.dims if d in r.dims]
    dims = l.dims + tuple(d for d in r.dims if d not in l.dims)
    right = r.lazy.rename({V: VR})
    joined = l.lazy.join(right, on=shared, how="inner") if shared else l.lazy.join(right, how="cross")
    return PCube(dims, joined.select(*dims, fn(pl.col(V), pl.col(VR)).alias(V)).drop_nulls(V))


def _expand(c: PCube, dims: tuple[str, ...], cat: Catalog, restrict: Restrict | None) -> PCube:
    lf = c.lazy
    for d in dims:
        if d not in c.dims:
            lf = lf.join(_member_frame(cat, d, restrict), how="cross")
    return PCube(dims, lf.select(*dims, V))


def _union(l: PCube, r: PCube, op: str, blank) -> PCube:
    """FULL OUTER JOIN。片側が空のセルは blank として扱う。両側とも空なら空のまま。"""
    dims = l.dims
    right = r.lazy.rename({V: VR})
    if dims:
        joined = l.lazy.join(right, on=list(dims), how="full", coalesce=True)
    else:  # 0 行か 1 行どうし。足りない側は null で埋まり、両方 0 行なら 0 行
        joined = pl.concat([l.lazy, right], how="horizontal_extend")
    a, b = pl.col(V), pl.col(VR)
    if blank is not None:
        a, b = a.fill_null(blank), b.fill_null(blank)
    return PCube(dims, joined.select(*dims, _apply(op, a, b).alias(V)).drop_nulls(V))


def _dense(dims: tuple[str, ...], cat: Catalog, restrict: Restrict | None) -> pl.LazyFrame:
    frames = [_member_frame(cat, d, restrict) for d in dims]
    return reduce(lambda a, b: a.join(b, how="cross"), frames)


def _agg(agg: str) -> pl.Expr:
    match agg:
        case "sum": return pl.col(V).sum()
        case "avg": return pl.col(V).mean()
        case "min": return pl.col(V).min()
        case "max": return pl.col(V).max()
        case "count": return pl.len().cast(pl.Float64).alias(V)
    raise ValueError(agg)


def _group(c: PCube, dims: tuple[str, ...], agg: str) -> PCube:
    """dims ごとに集計する。入力が空なら結果も空（sum が 0 を返さないよう、軸がなくても GROUP BY する）。"""
    lf, keys = c.lazy, list(dims)
    if not keys:
        lf, keys = lf.with_columns(pl.lit(0).alias("__k")), ["__k"]
    return PCube(dims, lf.group_by(keys).agg(_agg(agg)).select(*dims, V))


_MAPPING_CACHE: dict[int, tuple[dict, pl.DataFrame]] = {}


def _mapping_frame(cat: Catalog, dim: str, target: str, mapping: dict[str, str]) -> pl.LazyFrame:
    cached = _MAPPING_CACHE.get(id(mapping))
    if cached is None or cached[0] is not mapping:
        src_index = cat.dimension(dim)._index
        dst_index = cat.dimension(target)._index
        df = pl.DataFrame({dim: pl.Series([src_index[s] for s in mapping], dtype=CODE),
                           target: pl.Series([dst_index[t] for t in mapping.values()], dtype=CODE)})
        cached = _MAPPING_CACHE[id(mapping)] = (mapping, df)
    return cached[1].lazy()


def _eval(expr: Expr, cat: Catalog, restrict: Restrict | None) -> PCube:
    ev = lambda e, r=restrict: _eval(e, cat, r)
    match expr:
        case Ref(name):
            return cat.read(name, restrict)

        case Const(value):
            return _const(value)

        case BinOp(op, left, right):
            l, r = ev(left), ev(right)
            if op in {"*", "/", "=", "<>", "<", "<=", ">", ">="}:
                return _intersect(l, r, lambda a, b: _apply(op, a, b))
            dims = _merge(l.dims, r.dims)
            blank = None if op in ("and", "or") else 0.0
            return _union(_expand(l, dims, cat, restrict), _expand(r, dims, cat, restrict), op, blank)

        case Not(child):
            c = ev(child)
            return PCube(c.dims, c.lazy.with_columns(~pl.col(V)))

        case If(cond, then, else_):
            c = ev(cond)
            parts = [_intersect(PCube(c.dims, c.lazy.filter(pl.col(V))), ev(then), _right)]
            if else_ is not None:
                parts.append(_intersect(PCube(c.dims, c.lazy.filter(~pl.col(V))), ev(else_), _right))
            dims = _merge(*(p.dims for p in parts))
            return PCube(dims, pl.concat([_expand(p, dims, cat, restrict).lazy for p in parts]))

        case Filter(child, cond):
            c = ev(cond)
            return _intersect(ev(child), PCube(c.dims, c.lazy.filter(pl.col(V))), _left)

        case On(child, other):
            return _intersect(ev(child), ev(other), _left)

        case Expand(child, dims):
            c = ev(child)
            return _expand(c, c.dims + dims, cat, restrict)

        case IsBlank(child):
            c = ev(child)
            if not c.dims:
                return PCube((), c.lazy.select((pl.len() == 0).alias(V)))
            lf = _dense(c.dims, cat, restrict).join(c.lazy, on=list(c.dims), how="left")
            return PCube(c.dims, lf.select(*c.dims, pl.col(V).is_null().alias(V)))

        case IfBlank(child, value):
            c = ev(child)
            if not c.dims:
                return PCube((), pl.concat([c.lazy, _const(value).lazy]).head(1))
            lf = _dense(c.dims, cat, restrict).join(c.lazy, on=list(c.dims), how="left")
            return PCube(c.dims, lf.select(*c.dims, pl.col(V).fill_null(value)))

        case By(child, dim, prop, agg):
            target, mapping = _property(cat, dim, prop)
            mf = _mapping_frame(cat, dim, target, mapping)
            if dim in infer(child, cat, []).dims:
                sub = _without(restrict, dim, target)
                if restrict and target in restrict:
                    sub[dim] = frozenset(m for m, t in mapping.items() if t in restrict[target])
                c = ev(child, sub)
                joined = PCube(_replace(c.dims, dim, target),
                               c.lazy.join(mf, on=dim, how="inner").drop(dim))
                return _group(joined, joined.dims, agg or "sum")
            sub = _without(restrict, dim, target)
            if restrict and dim in restrict:
                mf = mf.filter(pl.col(dim).is_in(pl.lit(_codes(cat, dim, restrict[dim])).implode()))
                sub[target] = frozenset(mapping[s] for s in restrict[dim] if s in mapping)
            c = ev(child, sub)
            dims = _replace(c.dims, target, dim)
            return PCube(dims, c.lazy.join(mf, on=target, how="inner").select(*dims, V))

        case Remove(child, dim, agg):
            c = ev(child, _without(restrict, dim))
            return _group(c, tuple(d for d in c.dims if d != dim), agg)

        case Shift(child, dim, n):
            d = cat.dimension(dim)
            sub = restrict
            if restrict and dim in restrict:
                src = (d.offset(t, -n) for t in restrict[dim])
                sub = {**restrict, dim: frozenset(s for s in src if s is not None)}
            c = ev(child, sub)
            shifted = pl.col(dim).cast(pl.Int64) + n
            lf = (c.lazy.with_columns(shifted.alias(dim))
                  .filter((pl.col(dim) >= 0) & (pl.col(dim) < len(d.members)))
                  .with_columns(pl.col(dim).cast(CODE)))
            return _filter(PCube(c.dims, lf), restrict, cat)

    raise TypeError(expr)


@dataclass
class PStore:
    """格納用の Cube。値のある行だけを、分割軸のメンバー番号の範囲ごとのパーティションに分けて持つ。

    空のパーティションは持たない。範囲を読み書きするときは、分割軸で絞れるなら該当する
    パーティションだけに触れ、それ以外はそのまま使い回す（DataFrame は不変なので共有してよい）。

    全体の再計算の結果はすぐには分割せず（pending）、分割軸で絞った読み書きが初めて来たときに
    分割する。全体の再計算を速く保つため。
    """
    dims: tuple[str, ...]
    parts: dict[int, pl.DataFrame]
    dtype: pl.DataType
    part_dim: str | None = None  # 分割軸
    flat: bool = False  # True なら分割せず parts[0] に全行を持つ（scan の作業用データ）
    pending: bool = False  # True なら parts[0] に全行があり、まだ分割していない


# 触れるパーティションがこれより多ければ、1 つずつではなく連結してまとめて処理する
BULK_THRESHOLD = 8


class PolarsEngine:
    name = "polars"

    def __init__(self, partitions: int = 256):
        self.partitions = partitions  # 1 Metric あたりのおおよその分割数。1 なら分割しない

    # ------------------------------------------------ 分割

    @staticmethod
    def _default_dim(dims, cat) -> str | None:
        """分割軸の指定がないときの既定: メンバー数が最も多い軸。"""
        return max(dims, key=lambda d: len(cat.dimension(d).members)) if dims else None

    def _width(self, dim: str, cat) -> int:
        """1 パーティションあたりのメンバー数。"""
        return max(1, -(-len(cat.dimension(dim).members) // self.partitions))

    def _keys(self, store: PStore, region, cat) -> set[int] | None:
        """region が触れるパーティション番号。分割軸で絞れないなら None（全パーティション）。"""
        dim = store.part_dim
        if store.flat or dim is None or not region or dim not in region:
            return None
        self.materialize(store, cat)
        index, width = cat.dimension(dim)._index, self._width(dim, cat)
        return {index[m] // width for m in region[dim]}

    def _split(self, dim: str | None, df: pl.DataFrame, cat, flat: bool = False) -> dict[int, pl.DataFrame]:
        if df.height == 0:
            return {}
        width = self._width(dim, cat) if dim is not None else 1
        if flat or dim is None or width >= len(cat.dimension(dim).members):
            return {0: df}
        keyed = df.with_columns((pl.col(dim) // width).alias("__pk"))
        return {k[0]: part for k, part in keyed.partition_by("__pk", as_dict=True, include_key=False).items()}

    def materialize(self, store: PStore, cat) -> None:
        """後回しにしていた分割を行う。中身は同じなので、その場で書き換えてよい。"""
        if store.pending:
            store.parts = self._split(store.part_dim, store.parts[0], cat)
            store.pending = False

    def _store(self, dims, df: pl.DataFrame, cat, partition: str | None = None) -> PStore:
        dim = partition if partition is not None else self._default_dim(dims, cat)
        return PStore(dims, self._split(dim, df, cat), df.schema[V], dim)

    def _frames(self, store: PStore, region, cat) -> list[pl.DataFrame]:
        keys = self._keys(store, region, cat)
        if keys is None:
            return list(store.parts.values())
        return [store.parts[k] for k in keys if k in store.parts]

    @staticmethod
    def _schema(store: PStore) -> dict:
        return {**{d: CODE for d in store.dims}, V: store.dtype}

    @staticmethod
    def _concat(store: PStore, frames: list[pl.DataFrame]) -> pl.DataFrame:
        if not frames:
            return _empty(store.dims, store.dtype).collect()
        return frames[0] if len(frames) == 1 else pl.concat(frames, rechunk=False)

    @staticmethod
    def _finish(df: pl.DataFrame) -> pl.DataFrame:
        return df.rechunk() if df.n_chunks() > 16 else df

    # ------------------------------------------------ Engine

    def empty(self, dims, kind, partition=None, cat=None):
        return PStore(dims, {}, _dtype(kind), partition)

    def partition_of(self, store: PStore):
        return store.part_dim

    def repartition(self, store: PStore, partition, cat):
        df = self._concat(store, list(store.parts.values()))
        return PStore(store.dims, self._split(partition, df, cat), store.dtype, partition)

    def from_cells(self, dims, kind, cells, cat, partition=None):
        cols = {d: pl.Series([cat.dimension(d)._index[k[i]] for k in cells], dtype=CODE)
                for i, d in enumerate(dims)}
        df = pl.DataFrame({**cols, V: pl.Series(list(cells.values()), dtype=_dtype(kind))})
        return self._store(dims, df, cat, partition)

    def from_frame(self, dims, kind, df: pl.DataFrame, cat, partition=None) -> PStore:
        """軸ごとのメンバー番号の列と __v 列を持つ DataFrame をそのまま使う（大量投入用）。"""
        df = df.select(*[pl.col(d).cast(CODE) for d in dims], pl.col(V).cast(_dtype(kind)))
        return self._store(tuple(dims), df, cat, partition)

    def write(self, store: PStore, key, value, cat):
        codes = [cat.dimension(d)._index[m] for d, m in zip(store.dims, key)]
        point = {d: frozenset([m]) for d, m in zip(store.dims, key)}
        rows = [] if value is None else [(codes, value)]  # None なら空に戻すだけ
        new = pl.DataFrame({**{d: pl.Series([c[i] for c, _ in rows], dtype=CODE) for i, d in enumerate(store.dims)},
                            V: pl.Series([v for _, v in rows], dtype=store.dtype)})
        return self.replace(store, point, PCube(store.dims, new), cat)

    def evaluate(self, expr, cat, restrict):
        """クエリプランのまま返す。書き戻し（replace）で、書き戻し処理と 1 回のクエリにまとめて実行する。"""
        c = _eval(expr, cat, restrict or None)
        return PCube(c.dims, c.lazy)

    def evaluate_many(self, items, cat):
        """複数のクエリプランを pl.collect_all で一度に実行する（並列実行と共通部分の共有）。"""
        cubes = [_eval(expr, cat, restrict or None) for expr, restrict in items]
        # 共通部分の共有（comm_subplan_elim）は、測ると切ったほうが速かった
        frames = pl.collect_all([c.lazy for c in cubes],
                                optimizations=pl.QueryOptFlags(comm_subplan_elim=False))
        return [PCube(c.dims, df) for c, df in zip(cubes, frames)]

    def evaluate_with_count(self, expr, count_expr, cat, restrict):
        """SUM だけの集計の連なりについて、合計と各グループの件数を 1 回の GROUP BY で求める。"""
        if restrict:
            return self.evaluate(expr, cat, restrict), self.evaluate(count_expr, cat, restrict)
        chain = []
        e = expr
        while isinstance(e, (By, Remove)):
            chain.append(e)
            e = e.child
        base = cat.read(e.name, None)
        lf, dims = base.lazy, base.dims
        for i, node in enumerate(reversed(chain)):  # 内側から
            if isinstance(node, By):
                target, mapping = _property(cat, node.dim, node.prop)
                lf = lf.join(_mapping_frame(cat, node.dim, target, mapping), on=node.dim, how="inner").drop(node.dim)
                dims = _replace(dims, node.dim, target)
            else:
                dims = tuple(d for d in dims if d != node.dim)
            keys = list(dims)
            if not keys:
                lf, keys = lf.with_columns(pl.lit(0).alias("__k")), ["__k"]
            count = pl.len().cast(pl.Float64) if i == 0 else pl.col("__c").sum()
            lf = lf.group_by(keys).agg(pl.col(V).sum(), count.alias("__c"))
        df = lf.select(*dims, V, "__c").collect()
        return PCube(dims, df.select(*dims, V)), PCube(dims, df.select(*dims, pl.col("__c").alias(V)))

    def view(self, store: PStore | PCube, restrict, cat):
        """式の評価中の読み出し。触れるパーティションだけを連結し、絞り込みは呼び出し元のプランに任せる。

        差分集計では評価結果（PCube）をそのまま読ませることもある。
        """
        if isinstance(store, PCube):
            return _filter(store, restrict, cat)
        df = self._concat(store, self._frames(store, restrict, cat))
        return _filter(PCube(store.dims, df.lazy()), restrict, cat)

    def filter(self, store: PStore, restrict, cat):
        """restrict の範囲を切り出す。scan の作業用データになるので、分割しない形で返す。"""
        df = self.view(store, restrict, cat).collect()
        return PStore(store.dims, {0: df} if df.height else {}, store.dtype, store.part_dim, flat=True)

    def reorder(self, c: PCube, dims):
        return c if c.dims == dims else PCube(dims, c.lazy.select(*dims, V))

    def replace(self, store: PStore | None, region, new: PCube | PStore, cat):
        """region 内のセルを new で置き換える。

        new はクエリプランのままでよい。触れるパーティションが 1 つなら、new の評価・古い行の
        絞り込み・連結を 1 回のクエリで行う（小さな書き換えでクエリの回数を増やさないため）。
        """
        if isinstance(new, PStore):
            new_lf = self._concat(new, list(new.parts.values())).lazy()
        else:
            new_lf = new.lazy
        if store is None:
            return self._store(new.dims, new_lf.collect(), cat)
        new_lf = new_lf.select(*store.dims, V).cast(self._schema(store))
        if not region:
            new_df = new_lf.collect()
            if store.flat or new_df.height == 0:
                parts = self._split(store.part_dim, new_df, cat, store.flat)
                return PStore(store.dims, parts, store.dtype, store.part_dim, store.flat)
            # 全体の置き換えは分割を後回しにする
            return PStore(store.dims, {0: new_df}, store.dtype, store.part_dim, pending=True)

        cond = _in_region(store.dims, region, cat)
        keys = self._keys(store, region, cat)
        parts = dict(store.parts)

        if store.flat or keys is None or len(keys) > BULK_THRESHOLD:
            # まとめて処理: 触れるパーティションの古い行を絞り込み、新しい行と 1 回のクエリで連結して分け直す
            touched = list(parts) if keys is None else [k for k in keys if k in parts]
            kept = [parts.pop(k).lazy().filter(~cond) for k in touched]
            merged = pl.concat([*kept, new_lf]).collect()
            for k, part in self._split(store.part_dim, merged, cat, store.flat).items():
                parts[k] = self._finish(part)
            return PStore(store.dims, parts, store.dtype, store.part_dim, store.flat)

        if len(keys) == 1:
            # 新しい行はすべて region 内、つまりこの 1 つのパーティションに入るので、振り分けは要らない
            (k,) = keys
            frames = ([parts[k].lazy().filter(~cond)] if k in parts else []) + [new_lf]
            merged = pl.concat(frames).collect()
        else:
            new_parts = self._split(store.part_dim, new_lf.collect(), cat)
            for k in keys:
                frames = ([parts[k].lazy().filter(~cond)] if k in parts else [])
                frames += [new_parts[k].lazy()] if k in new_parts else []
                if frames:
                    merged = pl.concat(frames).collect()
                    if merged.height:
                        parts[k] = self._finish(merged)
                    else:
                        parts.pop(k, None)
            return PStore(store.dims, parts, store.dtype, store.part_dim)
        if merged.height:
            parts[k] = self._finish(merged)
        else:
            parts.pop(k, None)
        return PStore(store.dims, parts, store.dtype, store.part_dim)

    def to_cube(self, store: PStore, cat):
        members = [cat.dimension(d).members for d in store.dims]
        n = len(members)
        return Cube(store.dims, {tuple(members[i][row[i]] for i in range(n)): row[n]
                                 for part in store.parts.values() for row in part.iter_rows()})

    def size(self, store: PStore) -> int:
        return sum(part.height for part in store.parts.values())
