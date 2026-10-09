"""Model: Metric 単位の依存グラフ、計算計画、スライス単位の差分再計算。

計算計画は依存グラフの強連結成分（SCC）をトポロジカル順に並べたもの。
循環は「全員が同じ順序付き軸を持ち、循環が必ず正のずらし（prev）を通る」場合だけ許し、
その軸に沿った scan として 1 時点ずつ計算する。それ以外の循環はエラーにする。

入力セルを変えると、その座標を影響範囲として計画の順に下流へ伝え、各 Metric は
影響範囲だけを計算し直して差し替える。式や入力の定義を変えたときも、変えた Metric を計算し直して
値が変わったセルだけを下流へ伝える（Metric の軸か値の種類を変えたときだけ全体を計算し直す）。

複数の操作は transaction でまとめられ、失敗すれば取り消す。記録先（journal）を付けると、
確定したトランザクションごとに記録を残す（journal.py）。

集計だけの Metric（SUM / COUNT）は、集計元の変わった行の差分を足し込んで更新する（delta.py）。

エンジンが Metric を分割して持つ場合、分割軸は Metric ごとに明示するか、自動で選ぶ。
自動では、各入力 Metric の 1 セルを変えたときの影響範囲を伝え、触れるパーティションの
割合が平均で最も小さい軸を選ぶ。
"""
from __future__ import annotations

import contextlib
import dataclasses
import functools
import itertools
from collections import deque
import math
from dataclasses import dataclass, field
from statistics import mean
from collections.abc import MutableMapping
from typing import Any, Iterable, Mapping

from .core import Cube, Dimension, Key, uuid7
from .delta import DeltaPlan
from .engine import Store, default_engine
from .evaluate import Edge, FormulaError, Kind, Restrict, Type, bind, combos, member_kind, union_region
from .expr import (AGGREGATIONS, PUBLIC_AGGREGATIONS, Coalesce, Expr, Ref, mentions_dim, mentions_member,
                   references_metric, uses_property)
from .journal import LOG_VERSION, AlreadyCommitted, Transaction, changes, jsonable, now
from .messages import Msg, msg, render
from .parser import parse
from .planner import CompiledPlan, Step

Coords = Mapping[str, str | Iterable[str]]  # dimension id -> a member id, or a collection of member ids


class DuplicateId(Exception):
    """The UUID belongs to an object of a different kind, or to a removed object (a tombstone)."""


@dataclass
class Metric:
    name: str
    dims: tuple[str, ...]
    kind: Kind = "number"
    formula: Expr | None = None  # 評価に使う式（計画を作るときに written から解決する）。None なら入力 Metric
    partition: str | None = None  # 明示した分割軸。None なら自動で選ぶ
    written: Expr | None = None  # 利用者が書いた元の式（保存や表示に使う）
    overridable: bool = False  # True なら set_cell で式の結果を手入力で上書きできる
    id: str = ""  # The UUID. It does not change with a rename or a new definition. It is the key of Model.metrics
    override: str | None = None  # The id of the hidden override input. It is used only while overridable

    @property
    def override_name(self) -> str:
        return f"__override__{self.name}"


@dataclass
class Pending:
    """The changes that wait after the last recalculation. recalc consumes them and makes this empty.

    An operation of the Model only puts a change here. It does not calculate values. The ranges hold member ids,
    so a rename does not touch them.
    """
    full: bool = True  # 次の recalc で全体を計算し直すか
    changed: dict[str, Restrict] = field(default_factory=dict)  # 入力 Metric の変更範囲
    old_cells: dict[str, dict[Key, Any]] = field(default_factory=dict)  # 入力の変更前の値（差分集計用）
    old_slices: dict[str, Any] = field(default_factory=dict)  # 範囲ごと空にした入力の、変更前の値
    added: dict[str, set[str]] = field(default_factory=dict)  # dimension id -> the member ids added since the last recalc
    dirty: set[str] = field(default_factory=set)  # 前回の計画のあとで定義を変えた Metric
    forced: dict[str, Restrict] = field(default_factory=dict)  # 次の再計算で必ず計算し直す計算 Metric の範囲

    def empty(self) -> bool:
        return not (self.full or self.changed or self.added or self.forced)

    def forget(self, names) -> None:
        """消した Metric の分を捨てる。"""
        for n in names:
            for store in (self.changed, self.old_cells, self.old_slices, self.forced):
                store.pop(n, None)
        self.dirty -= set(names)


LOG_MAX = 10_000  # 観察用の記録の上限（古いものから捨てる）
DENSE = frozenset({"densify", "isblank_dense", "ifblank_dense"})  # the warning codes of a dense operation


class SliceLog:
    """An observation log of the recalculated (Metric id, range) items. A range that the engine gave as member
    numbers becomes member ids only when the log is read (to change hundreds of ranges after each change would
    take time). The name facade (named.py) shows the current names."""

    def __init__(self):
        self._items: list[tuple[str, Restrict]] = []
        self._later: list = []  # functions that give the items with member ids

    def _flush(self) -> list[tuple[str, Restrict]]:
        for named in self._later:
            self._items.extend(named())
        self._later.clear()
        if len(self._items) > LOG_MAX:
            del self._items[:len(self._items) - LOG_MAX]
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


def _operation(fn):
    """モデルを変える操作。トランザクションの中なら意図として記録する（操作の中から呼んだ操作は
    記録しない）。記録先があってトランザクションの外なら、1 回の呼び出しを 1 トランザクションにする。"""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        if self._frozen:
            raise ValueError("公開済みの版は書き換えられない（Workspace.write で書き込む）")
        txn = self._txn
        if txn is None:
            if self.journal is None:
                return fn(self, *args, **kwargs)
            with self.transaction():
                return wrapper(self, *args, **kwargs)
        if txn.depth == 0:
            txn.ops.append({"op": fn.__name__, "args": jsonable(list(args)), "kwargs": jsonable(kwargs)})
        txn.depth += 1
        try:
            return fn(self, *args, **kwargs)
        finally:
            txn.depth -= 1
    return wrapper


class _CellCounts(dict):
    """見積もりに使う Metric ごとのセル数。計算 Metric は known（見積もり）から、入力は今の行数から、
    初めて読まれたときに取る。読まれた名前を read に記録する。"""

    def __init__(self, model: Model, known: dict[str, float]):
        super().__init__()
        self.model, self.known, self.read = model, known, set()

    def __getitem__(self, name: str) -> float:
        self.read.add(name)
        return super().__getitem__(name)

    def __missing__(self, name: str) -> float:
        if name in self.known:
            n = self.known[name]
        else:
            n = float(self.model.engine.size_hint(self.model._values[name]))
        self[name] = n
        return n


_UNSET = object()


@dataclass(slots=True)
class MetricState:
    """Metric ごとに Model が持つ、定義（Metric）以外の状態。Metric を消す・名前を変えるときは、これを
    まるごと動かす（状態を 1 つ足しても、消すところと名前を変えるところを直さなくてよい）。
    まだ持っていない状態は _UNSET。"""
    value: Any = _UNSET           # 格納データ（エンジンごとの形式）
    count: Any = _UNSET           # 差分集計する SUM の各グループの件数
    delta: Any = _UNSET           # 差分集計の計画（DeltaPlan）
    partition: Any = _UNSET       # 分割軸（layout）
    warnings: Any = _UNSET        # 型検査の警告
    edges: Any = _UNSET           # 依存グラフの辺（この Metric -> 参照先）
    samples: Any = _UNSET         # この入力の 1 セルを変えたときの影響範囲（分割軸の選択に使う）
    estimate: Any = _UNSET        # 結果のセル数の見積もり（上限）
    estimate_refs: Any = _UNSET   # 見積もりが読んだ Metric
    estimate_users: Any = _UNSET  # この Metric を読む見積もり

    def fork(self, share) -> MetricState:
        """Model.fork 用の複製。格納データは share で共有し、書き足していく状態（samples）は写す。
        ほかの状態は定義だけに依存し、書き換えるときは丸ごと入れ替えるので、そのまま引き継ぐ。"""
        own = lambda v, f: v if v is _UNSET else f(v)
        return MetricState(own(self.value, share), own(self.count, share), self.delta, self.partition,
                           self.warnings, self.edges, own(self.samples, dict),
                           self.estimate, self.estimate_refs, self.estimate_users)


class _Field(MutableMapping):
    """Show one state of Model._state as a dict of Metric id -> value."""
    __slots__ = ("_states", "_name")

    def __init__(self, states: dict[str, MetricState], name: str):
        self._states, self._name = states, name

    def __getitem__(self, key: str):
        st = self._states.get(key)
        v = _UNSET if st is None else getattr(st, self._name)
        if v is _UNSET:
            raise KeyError(key)
        return v

    def get(self, key: str, default=None):
        st = self._states.get(key)
        v = _UNSET if st is None else getattr(st, self._name)
        return default if v is _UNSET else v

    def __contains__(self, key) -> bool:
        st = self._states.get(key)
        return st is not None and getattr(st, self._name) is not _UNSET

    def __setitem__(self, key: str, value) -> None:
        st = self._states.get(key)
        if st is None:
            st = self._states[key] = MetricState()
        setattr(st, self._name, value)

    def __delitem__(self, key: str) -> None:
        if key not in self:
            raise KeyError(key)
        setattr(self._states[key], self._name, _UNSET)

    def __iter__(self):
        name = self._name
        return (k for k, st in list(self._states.items()) if getattr(st, name) is not _UNSET)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __repr__(self) -> str:
        return repr(dict(self))


def _per_metric(name: str, doc: str) -> property:
    """Model._state の状態 name を dict のように読み書きする属性。代入すると、その状態だけを入れ替える。"""
    def get(self) -> _Field:
        return _Field(self._state, name)

    def set(self, mapping) -> None:
        mapping = dict(mapping)
        for k, st in self._state.items():
            setattr(st, name, mapping.pop(k, _UNSET))
        for k, v in mapping.items():
            self._state[k] = MetricState(**{name: v})
    return property(get, set, doc=doc)


@dataclass
class Model:
    engine: Store = field(default_factory=default_engine)
    auto_layout: bool = True  # False なら分割軸はエンジンの既定（メンバー数が最も多い軸）
    delta_aggregation: bool = True  # False なら集計も普通に計算し直す
    max_cells: int | None = 1_000_000_000  # 計算 Metric 1 つのセル数の見積もりの上限。None なら検査しない
    dimensions: dict[str, Dimension] = field(default_factory=dict)  # dimension id -> Dimension, in definition order
    _dim_ids: dict[str, str] = field(default_factory=dict)  # dimension name -> id (the name index)
    metrics: dict[str, Metric] = field(default_factory=dict)  # Metric id -> Metric, in definition order
    _metric_ids: dict[str, str] = field(default_factory=dict)  # Metric name -> id (the name index)
    # The recalculated Metrics (observation)
    eval_log: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_MAX))
    slice_log: SliceLog = field(default_factory=SliceLog)  # The recalculated ranges (observation)
    # The Metrics that incremental aggregation updated (observation)
    delta_log: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_MAX))
    _state: dict[str, MetricState] = field(default_factory=dict)  # Metric ごとの、定義以外の状態
    _plan: list[Step] | None = None
    _levels: list[list[Step]] = field(default_factory=list)  # 依存関係の段ごとの計画（全体の再計算用）
    _pending: Pending = field(default_factory=Pending)  # 前回の再計算のあとにためている変更
    tombstones: set[str] = field(default_factory=set)  # The UUIDs of removed objects. They are not used again
    journal: Any = None  # 記録先（journal.FileJournal など）。None なら記録しない
    seq: int = 0  # 確定した最後のトランザクションの通し番号
    last_record: dict | None = None  # 最後に確定したトランザクションの記録
    _txn: Transaction | None = None  # 実行中のトランザクション
    _frozen: bool = False  # 公開済みの版（Workspace）。書き換えない

    # Show each per-Metric state (_state) as a dict of Metric id -> value
    _values = _per_metric("value", "格納データ（エンジンごとの形式）")
    _counts = _per_metric("count", "差分集計する SUM の各グループの件数")
    _delta = _per_metric("delta", "差分集計する Metric -> 計画")
    layout = _per_metric("partition", "Metric ごとの分割軸")
    _warnings = _per_metric("warnings", "The type check warnings of each Metric (Msg, with ids in the values)")
    _edges = _per_metric("edges", "依存グラフ（Metric -> 参照先）")
    _samples = _per_metric("samples", "分割軸の選択に使った、入力ごとの影響範囲")
    cell_estimates = _per_metric("estimate", "計算 Metric のセル数の見積もり（上限）")
    _estimate_refs = _per_metric("estimate_refs", "見積もりが読んだ Metric")
    _estimate_users = _per_metric("estimate_users", "Metric -> それを読む見積もり")

    @property
    def warnings(self) -> dict[str, list[str]]:
        """The type check warnings of each Metric (Metric id -> texts with the current names)."""
        return {i: [render(w.code, self._shown_params(w.params)) for w in ws] for i, ws in self._warnings.items()}

    # ------------------------------------------------ Catalog

    def dimension(self, id: str) -> Dimension:
        """The dimension with this id. FormulaError unknown_dim if there is none."""
        d = self.dimensions.get(id)
        if d is None:
            raise FormulaError("unknown_dim", name=id)
        return d

    def _name_of(self, id: str) -> str:
        """The current name of a Metric, dimension, property or member id, or a kind "member:<dim id>" with the
        name. Another string comes back as it is."""
        if id.startswith("member:"):
            return "member:" + self._name_of(id.removeprefix("member:"))
        if id in self.metrics:
            return self.metrics[id].name
        if id in self.dimensions:
            return self.dimensions[id].name
        for d in self.dimensions.values():
            if id in d.property_names:
                return d.property_names[id]
            if id in d._by_id:
                return d.member_of(id)
        return id

    def metric(self, id: str) -> Metric:
        """The Metric with this id. ValueError if there is none."""
        m = self.metrics.get(id)
        if m is None:
            raise ValueError(f"Metric {id} がない")
        return m

    def metric_type(self, id: str) -> Type:
        if id not in self.metrics:
            raise FormulaError("unknown_metric", name=id)
        m = self.metrics[id]
        return Type(m.dims, m.kind)

    def source(self, id: str) -> Any:
        """The stored data of the Metric. An engine that restricts a range itself (Rust) gets the source here."""
        return self._values[id]

    def read(self, id: str, restrict: Restrict | None) -> Any:
        """The stored data of the Metric in the range restrict (the reference evaluator uses this)."""
        return self.engine.view(self.source(id), restrict or None, self)

    def compiled(self) -> CompiledPlan:
        """今の計算計画（Planner が段取りを組むのに使う）。"""
        return CompiledPlan(self._plan, self._levels, self._delta, frozenset(self._delta_sources()))

    def refresh(self) -> None:
        """全体を計算し直す。差分集計を続けてたまった浮動小数点の誤差もなくなる。"""
        if self._frozen:
            raise ValueError("公開済みの版は計算し直せない（Workspace.write の中で計算し直す）")
        self._pending.full = True
        self.recalc()

    def memory(self) -> dict[str, dict[str, int]]:
        """The memory (bytes) of the stored data of each Metric (Metric id -> row). Intermediate results and the
        plan are not included.

        rows は本体の行数、base・delta・index は本体・差分・索引、counts は差分集計の件数の格納データ
        （本体と差分と索引の合計）。バイト数を測れないエンジン（参照実装）は rows だけを返す。
        """
        mem = self.engine.memory
        out = {}
        for n, v in self._values.items():
            row = mem(v)
            if n in self._counts and "base" in row:
                c = mem(self._counts[n])
                row["counts"] = c["base"] + c["delta"] + c["index"]
            out[n] = row
        return out

    # ------------------------------------------------ 複製

    def fork(self) -> Model:
        """このモデルの複製。ホワットイフ分析のように、元を壊さずに入力や上書きを試すのに使う。

        複製は計算済みの状態から始まり、以後の変更（入力、上書き、メンバーの追加）は互いに
        影響しない。Rust のエンジンでは格納データを共有し、書き換えた Metric だけを最初の
        書き込みのときに複製するので、複製そのものは Metric の数に比例する時間で済む。
        """
        self.recalc()
        other = Model(engine=self.engine, auto_layout=self.auto_layout, delta_aggregation=self.delta_aggregation,
                      max_cells=self.max_cells)
        other.dimensions = {i: d.copy() for i, d in self.dimensions.items()}
        other._dim_ids = dict(self._dim_ids)
        other.engine = self.engine.fork(other)
        other.metrics = {i: dataclasses.replace(m) for i, m in self.metrics.items()}
        other._metric_ids = dict(self._metric_ids)
        other._state = {i: st.fork(self.engine.share) for i, st in self._state.items()}
        # 計算計画は定義だけに依存するので、そのまま引き継ぐ
        other._plan, other._levels = self._plan, self._levels
        other.tombstones = set(self.tombstones)
        other.seq = self.seq
        other._pending = Pending(full=False)
        return other

    # ------------------------------------------------ トランザクションと記録

    @contextlib.contextmanager
    def transaction(self, *, user: str | None = None, reason: str | None = None,
                    client_op_id: str | None = None, validate=None):
        """複数の操作を 1 つのトランザクションにまとめる。

            with m.transaction(user="alice", reason="予算の修正") as txn:
                m.set_cell("Budget", 100, Product="A", Month="Jan")
                m.spread("Budget", 1200, Product="B")
            txn.seq, txn.record  # 確定した通し番号と記録

        中で例外が起きたら、すべての操作を取り消して例外をそのまま投げる。抜けるときに再計算し、
        式のエラーなどで失敗しても取り消す。成功したら記録先（journal）に記録を追記してから確定する。
        追記に失敗しても取り消す。入れ子にすると、外側のトランザクションに含まれる。

        client_op_id を渡すと、同じ ID のトランザクションが確定済みなら AlreadyCommitted を投げる
        （中の操作は実行しない）。応答を受け取れなかった利用者が再送したときに、二重に確定しない。

        validate を渡すと、確定する前に記録を渡して呼ぶ。例外を投げれば取り消す（排他の確認などに使う）。
        """
        if self._txn is not None:
            yield self._txn
            return
        if self._frozen:
            raise ValueError("公開済みの版は書き換えられない（Workspace.write で書き込む）")
        if client_op_id is not None and self.journal is not None:
            if (seq := self.journal.seq_of(client_op_id)) is not None:
                raise AlreadyCommitted(seq)
        self.recalc()
        saved = self.fork()  # 取り消すときに戻す版（格納データの本体は共有するので安い）
        txn = self._txn = Transaction(user, reason, client_op_id)
        try:
            yield txn
            self.recalc()
            record = {"v": LOG_VERSION, "at": now(), "user": user, "reason": reason, "client_op_id": client_op_id,
                      "ops": txn.ops, "changes": changes(saved, self)}
            if validate is not None:
                validate(record)
            if self.journal is not None and txn.ops:
                self.seq = self.journal.append(record)
                record["seq"] = self.seq
                txn.seq = self.seq
            txn.record = self.last_record = record
        except BaseException:
            self._restore(saved)
            raise
        finally:
            self._txn = None

    def _restore(self, saved: Model) -> None:
        """Go back to the version saved from before the transaction (the observation logs and the journal stay)."""
        self.slice_log._flush()  # the log can hold member numbers: make them ids before the dimensions go back
        for f in dataclasses.fields(Model):
            if f.name not in _KEPT_ON_RESTORE:
                setattr(self, f.name, getattr(saved, f.name))

    def checkpoint(self) -> None:
        """今の状態のスナップショットを記録先に置く。開くときは、このスナップショットと、
        これより後の記録だけを読めばよくなる。"""
        if self.journal is None:
            raise ValueError("記録先（journal）がない")
        if self._txn is not None:
            raise ValueError("トランザクションの中ではスナップショットを取れない")
        self.recalc()
        self.journal.save_snapshot(self)

    # ------------------------------------------------ 保存と読み込み

    def save(self, path) -> None:
        """定義と入力データをディレクトリ path に保存する（計算 Metric は読み込み後に計算し直す）。"""
        from .storage import save
        save(self, path)

    @classmethod
    def load(cls, path, engine: Store | None = None) -> Model:
        """save で保存したディレクトリから Model を作る。engine を省略すると既定のエンジン。"""
        from .storage import load
        return load(path, engine)

    # ------------------------------------------------ 定義

    @_operation
    def add_dimension(self, name: str, members, *, ordered: bool = False, id: str | None = None) -> Dimension:
        """Add the dimension. id is its UUID (made if None). The members get new UUIDs (add_member sets one).
        If id is the UUID of a dimension, only its name changes (rename_dimension); the members stay."""
        id, old = self._same_object("軸", name, id, self.dimensions, self._dim_ids)
        if old is not None:
            if old.name != name:
                self.rename_dimension(id, name)
            return old
        self._check_dim_name(name)
        d = Dimension(name, members, ordered=ordered, id=id)
        self.dimensions[id] = d
        self._dim_ids[name] = id
        return d

    @_operation
    def rename_dimension(self, dim: str, new: str) -> None:
        """Rename the dimension. The formulas and all internal state hold the dimension id, so nothing is
        calculated again and no formula changes: the display shows the new name. The id does not change."""
        d = self.dimension(dim)
        if new == d.name:
            return
        self._check_dim_name(new)
        del self._dim_ids[d.name]
        d.name = new
        self._dim_ids[new] = d.id

    @_operation
    def remove_dimension(self, dim: str) -> None:
        """Remove the dimension (an id) with its members and properties. Only a dimension that nothing uses can
        go: no Metric has it in dims or as the kind, no formula holds it, and no property of another dimension
        points to it. Thus no value changes. The ids of the dimension, its members and its properties become
        tombstones."""
        d = self.dimension(dim)
        kind = member_kind(dim)
        users = [m.name for m in self.metrics.values() if not m.name.startswith("__") and (
            dim in m.dims or m.kind == kind or (m.written is not None and mentions_dim(m.written, dim)))]
        users += [f"{o.name}.{o.property_names[p]}" for o in self.dimensions.values() if o.id != dim
                  for p, (target, _) in o.properties.items() if target == dim]
        if users:
            raise ValueError(f"{d.name} は {', '.join(users)} が使っているので消せない")
        self.slice_log._flush()  # the log can hold member numbers: make them ids while the dimension is here
        self.tombstones.update([dim, *d.ids, *d.properties])
        del self.dimensions[dim]
        del self._dim_ids[d.name]
        self.engine.dimension_changed(self, dim)  # the engine forgets the dimension

    def _check_dim_name(self, name: str) -> None:
        # A replaced dimension would leave the Metrics on it with keys of the old members.
        if not isinstance(name, str) or not name:
            raise ValueError(f"軸の名前は空でない文字列: {name!r}")
        if name in self._dim_ids:
            raise ValueError(f"{name}: 同じ名前の軸がある")
        if name in self._metric_ids:
            raise ValueError(f"{name}: 同じ名前の Metric がある（式の中で軸と区別できなくなる）")

    # ------------------------------------------------ UUIDs

    @_operation
    def add_property(self, dim: str, prop: str, target: str, mapping: Mapping[str, str], *,
                     id: str | None = None) -> str:
        """Add the property prop (a member of dim -> a member of target) to the dimension dim (ids). id is its
        UUID. The members in mapping are ids. Return the property id.

        If id is the UUID of a property of dim, the mapping replaces the old one (a new name renames it).
        Then the Metrics that use the property in a formula (`[BY: dim.prop]`) are calculated again.
        If prop is the name of a property with a different UUID, raise ValueError.
        """
        d, t = self.dimension(dim), self.dimension(target)
        id, old = self._same_object("プロパティ", prop, id, d.properties, d._props, f"{d.name}.{prop}")
        if old is not None and d.property_names[id] != prop:
            self.rename_property(d.id, id, prop)
        if old is None:
            self._check_prop_name(d, prop)
        d.add_property(id, prop, t, mapping)
        self.engine.dimension_changed(self, d.id)  # the engine replaces the mapping that it holds
        for m in self.metrics.values():
            if m.written is not None and uses_property(m.written, d.id, id):
                self._redefine(m.id)
        return id

    @_operation
    def rename_property(self, dim: str, prop: str, new: str) -> None:
        """Rename the property (ids). Nothing is calculated again and no formula changes. The id does not change."""
        d = self.dimension(dim)
        prop = self._prop(d, prop)
        if new == d.property_names[prop]:
            return
        self._check_prop_name(d, new)
        d.rename_property(prop, new)

    @staticmethod
    def _prop(d: Dimension, prop: str) -> str:
        """Check the property id. ValueError if d does not have it."""
        if prop not in d.properties:
            raise ValueError(f"{d.name} にプロパティ {prop} がない")
        return prop

    @staticmethod
    def _check_prop_name(d: Dimension, name: str) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError(f"{d.name}: プロパティの名前は空でない文字列: {name!r}")
        if name in d._props:
            raise ValueError(f"{d.name}.{name}: 同じ名前のプロパティがある")

    @_operation
    def set_property_values(self, dim: str, prop: str, values: Mapping[str, str | None]) -> None:
        """Set the values of the property prop (ids) for some members of dim ({member id: member id}). A None
        value removes the value of the member.

        The values of the other members do not change. Then it calculates again like add_property.
        """
        d = self.dimension(dim)
        id = self._prop(d, prop)
        target, mapping = d.properties[id]
        mapping = dict(mapping)
        for member, value in values.items():
            self._member(d, member)
            if value is None:
                mapping.pop(member, None)
            else:
                mapping[member] = value
        self.add_property(d.id, d.property_names[id], target, mapping, id=id)

    def _same_object(self, what: str, name: str, id: str | None, objects: Mapping[str, Any],
                     names: Mapping[str, str], shown: str | None = None) -> tuple[str, Any]:
        """Return (id, the object of kind what that the id names, or None if it is new) for a definition.
        objects is the id -> object table and names the name index of that kind. A None id makes a UUIDv7.
        If name is the name of an object with a different id, raise ValueError. If the id is a tombstone,
        or it belongs to an object of a different kind, raise DuplicateId."""
        if id is None:
            id = uuid7()
        elif not isinstance(id, str) or not id:
            raise ValueError(f"ID は空でない文字列: {id!r}")
        elif id in self.tombstones:
            raise DuplicateId(f"ID {id} は消したオブジェクトのもの（再利用できない）")
        old = objects.get(id)
        if old is None:
            if (id in self.metrics or id in self.dimensions
                    or any(id in d._by_id or id in d.properties for d in self.dimensions.values())):
                raise DuplicateId(f"ID {id} は別の種類のオブジェクトのもの")
            if name in names:
                raise ValueError(f"{shown or name}: 同じ名前の{what}が別の ID にある")
        return id, old

    def _register(self, m: Metric, old: Metric | None) -> None:
        """Put the new definition m in the catalog. If the id names a Metric with a different name, rename it
        first."""
        if old is not None and old.name != m.name:
            self.metrics[m.id] = old  # rename the old definition (the override input follows it)
            self.rename_metric(old.id, m.name)
        self.metrics[m.id] = m
        if old is None:
            self._metric_ids[m.name] = m.id

    @_operation
    def add_input(self, name: str, dims: Iterable[str], cells: Mapping[Key, float | bool | str] | None = None,
                  *, kind: Kind = "number", storage: Any = None, partition: str | None = None,
                  id: str | None = None) -> str:
        """Add the input Metric and return its id. dims, partition and the dimension of kind ("member:<dim id>")
        are dimension ids. cells is {(member id, ...): value} (a member-type value is a member id). You can also
        give a large data set in the storage format of the engine (storage).

        id is the UUID of the Metric. If it is the UUID of a Metric, this definition replaces the old one
        (the name can change). If the dimensions and the kind are the same, the engine calculates the change
        of all cells incrementally (a formula Metric can become an input Metric).
        """
        self._check_name(name)
        kind = self._check_kind(name, kind)
        dims = tuple(self.dimension(d).id for d in dims)
        self._check_key_width(name, dims)
        id, old = self._same_object(" Metric ", name, id, self.metrics, self._metric_ids)
        new = Metric(name, dims, kind, partition=self._check_partition(name, dims, partition), id=id,
                     override=None if old is None else old.override)  # rename and remove still follow the link
        if storage is None:
            self.metrics[id] = new  # _check uses the dimensions of the registered Metric
            try:
                checked = {tuple(key): self._check(id, tuple(key), value) for key, value in (cells or {}).items()}
            except ValueError:
                if old is None:
                    del self.metrics[id]
                else:
                    self.metrics[id] = old
                raise
            storage = self.engine.from_cells(dims, kind, {k: v for k, v in checked.items() if v is not None},
                                             self, partition)
        if old is not None and self._plan is not None and self._same_type(old, new) and id not in self._pending.forced:
            # Incremental aggregation needs the old values. If changes wait for a recalc, go back to the values before them
            self._keep_old(id)
            # Only the cells with a different value go in the change range
            _, diff = self.engine.replace_diff(self.engine.share(self._pending.old_slices[id]), {}, storage, self)
            if diff is not None:
                self._pending.changed[id] = union_region(self._pending.changed.get(id), diff)
        self._register(new, old)
        self._values[id] = storage
        self._redefine(id, old)
        return id

    @_operation
    def add_formula(self, name: str, dims: Iterable[str], formula: Expr | str, *, kind: Kind = "number",
                    partition: str | None = None, overridable: bool = False, id: str | None = None) -> str:
        """Add the formula Metric and return its id. dims, partition and the dimension of kind are dimension ids.
        formula is an AST or the text of a formula, written with names. A syntax error in the text raises
        ParseError here, and a reference to an unknown Metric raises FormulaError (unknown_metric). The names in
        the formula become ids here (bind), so a rename does not change the formula.

        If overridable, set_cell can replace the result of the formula with a manual value. The value has
        priority over the formula, and goes downstream as it is. set_cell with None gives the cell the result
        of the formula again.

        id is the UUID of the Metric. If it is the UUID of a Metric, this definition replaces the old one
        (the name can change). If the dimensions and the kind are the same, the engine calculates the Metric
        again and sends only the changed cells downstream (an input Metric can become a formula Metric).
        """
        self._check_name(name)
        kind = self._check_kind(name, kind)
        if isinstance(formula, str):
            formula = parse(formula, self_name=name)
        dims = tuple(self.dimension(d).id for d in dims)
        self._check_key_width(name, dims)
        id, old = self._same_object(" Metric ", name, id, self.metrics, self._metric_ids)
        formula = bind(formula, self, {name: id})  # the formula can refer to the Metric that it defines
        m = Metric(name, dims, kind, formula, self._check_partition(name, dims, partition), formula, overridable,
                   id=id, override=None if old is None else old.override)  # also kept while not overridable
        if old is not None and old.formula is None and id in self._pending.changed:
            self._invalidate()  # an input with pending changes becomes a formula: calculate everything again
        self._register(m, old)
        if overridable and m.override is None:
            if m.override_name not in self._metric_ids:  # a load reads the override values first
                self.add_input(m.override_name, dims, kind=kind, partition=m.partition)
            m.override = self._metric_ids[m.override_name]
        self._redefine(id, old)
        return id

    # ------------------------------------------------ Metric の削除と名前の変更

    @_operation
    def remove_metric(self, id: str) -> None:
        """Remove the Metric (an id). Only a Metric that no formula refers to can go (no other value changes).
        The hidden override input of an overridable Metric goes with it. The UUIDs become tombstones."""
        m = self._own_metric(id)
        users = sorted(x.name for x in self.metrics.values()
                       if x.written is not None and x.id != m.id and references_metric(x.written, m.id))
        if users:
            raise ValueError(f"{m.name} は {', '.join(users)} の式が参照しているので消せない")
        gone = {m.id} | ({m.override} if m.override is not None else set())
        for i in gone:
            self.tombstones.add(i)
            del self._metric_ids[self.metrics[i].name]
            del self.metrics[i]
            self._state.pop(i, None)
            for regions in self._samples.values():
                regions.pop(i, None)
        self._pending.forget(gone)
        if self._plan is not None:  # no formula refers to it, so only its step leaves the plan
            self._plan = [s for s in self._plan if s.names[0] not in gone]
            self._levels = [[s for s in level if s.names[0] not in gone] for level in self._levels]

    @_operation
    def rename_metric(self, id: str, new: str) -> None:
        """Rename the Metric (an id). The formulas and all internal state hold the Metric id, so nothing is
        calculated again and no formula changes: the display of a formula shows the new name. The hidden
        override input of an overridable Metric follows the owner. The id does not change."""
        m = self._own_metric(id)
        if new == m.name:
            return
        if not isinstance(new, str) or not new or new.startswith("__"):
            raise ValueError(f"Metric の名前は空でなく、__ で始まらない文字列: {new!r}")
        if new in self._metric_ids:
            raise ValueError(f"{new}: 同じ名前の Metric がある")
        self._check_name(new)
        renamed = [(m, new)]
        if m.override is not None:
            renamed.append((self.metrics[m.override], f"__override__{new}"))
        for x, n in renamed:
            del self._metric_ids[x.name]
            x.name = n
            self._metric_ids[n] = x.id

    def _own_metric(self, id: str) -> Metric:
        """A Metric that the user can handle (the hidden override input goes only with its owner)."""
        m = self.metric(id)
        if m.name.startswith("__override__"):
            raise ValueError(f"{m.name} は上書き用の隠し入力なので、持ち主の Metric を通して扱う")
        return m

    @staticmethod
    def _same_type(old: Metric, new: Metric) -> bool:
        return old.dims == new.dims and old.kind == new.kind

    def _redefine(self, id: str, old: Metric | None = None) -> None:
        """Record that the definition of the Metric changed. The next recalculation checks only the changed
        Metrics, repairs the plan, calculates them again and sends only the changed cells downstream.

        If the dimensions or the kind changed, the formulas that refer to it need the type check again,
        so everything is calculated again.
        """
        if self._plan is None or (old is not None and not self._same_type(old, self.metrics[id])):
            self._invalidate()
            return
        self._pending.dirty.add(id)

    def _check_name(self, name: str) -> None:
        if name in self._dim_ids:
            raise ValueError(f"{name}: 同じ名前の軸がある（式の中で軸と区別できなくなる）")

    def _check_kind(self, name: str, kind: Kind) -> Kind:
        """Check the kind ("number", "boolean" or "member:<dim id>")."""
        if kind in ("number", "boolean"):
            return kind
        if isinstance(kind, str) and kind.startswith("member:") and kind.removeprefix("member:") in self.dimensions:
            return kind
        raise ValueError(f"{name}: 値の種類は number、boolean、member:<軸の ID> のいずれか（{kind!r}）")

    def _check_partition(self, name: str, dims: tuple[str, ...], partition: str | None) -> str | None:
        """Check the partition dimension (an id)."""
        if partition is None:
            return None
        if partition not in dims:
            raise ValueError(f"{name}: 分割軸 {partition} が軸 {self._dim_names(dims)} にない")
        return partition

    def _dim_names(self, dims) -> tuple[str, ...]:
        return tuple(self.dimensions[d].name for d in dims)

    def _check_key_width(self, name: str, dims: tuple[str, ...]) -> None:
        """If the engine holds the key of a cell in a fixed-width integer, make sure that the dimensions fit
        in that width."""
        width = self.engine.key_bits
        if width is None or not dims:
            return
        bits = {self.dimension(d).name: _bits(len(self.dimension(d).members)) for d in dims}
        if sum(bits.values()) > width:
            detail = ", ".join(f"{d} {b} ビット" for d, b in bits.items())
            raise ValueError(f"{name}: 軸の組み合わせが {width} ビットのキーに収まらない（{detail}）。"
                             "軸を減らすか、メンバー数の多い軸を持つ Metric を分ける")

    # ------------------------------------------------ 按分

    @_operation
    def spread(self, id: str, total: float, coords: Mapping[str, str] | None = None, *,
               how: str = "proportional", where: Mapping[str, str] | None = None) -> int:
        """Spread values over a range of an input Metric (an id) so that their sum is total. Return the number
        of written cells.

            m.spread(budget, 12000, {version: plan, month: m01}, where={f"{product}.{category}": hardware})

        The range is the given member of each dimension in coords (dimension id -> member id), and all members
        of the other dimensions (only the members whose "<dimension id>.<property id>" in where has the value,
        a member id). With how="proportional", the values follow the ratio of the current values in the range.
        If the current sum is 0, or how="even", each cell with a value gets the same share. If no cell has a
        value, each combination in the range gets the same share.
        """
        m = self.metric(id)
        name = m.name
        if m.formula is not None or m.kind != "number":
            raise ValueError(f"{name}: 按分できるのは number の入力 Metric だけ")
        if isinstance(total, bool) or not isinstance(total, (int, float)):
            raise ValueError(f"{name}: 按分する合計は数値（{total!r}）")
        if how not in ("proportional", "even"):
            raise ValueError(f"how は proportional か even（{how!r}）")
        region: Restrict = {}
        for d, member in self._coords(m, coords or {}).items():
            region[d] = frozenset([self._member(self.dimensions[d], member)])
        for path, value in (where or {}).items():
            dim, _, prop = path.partition(".")
            d = self.dimensions.get(dim)
            if d is None or d.id not in m.dims or prop not in d.properties:
                raise ValueError(f"{name}: where の {path!r} は「軸.プロパティ」ではない")
            target, mapping = d.properties[prop]
            chosen = frozenset(x for x in d.ids if mapping.get(x) == value)
            region[d.id] = region.get(d.id, chosen) & chosen
        self.recalc()
        # Spread over the member number columns (one for each dimension) and write them at one time, not cell by cell
        cols, values = self.engine.columns(self._values[m.id], region or None, self)
        weight = math.fsum(values)  # the order of the sum (different for each engine) must not change the result
        if values and how == "proportional" and weight != 0:
            new = [float(total * v / weight) for v in values]
        else:
            n = len(values)
            if not values:  # no cell has a value: all combinations in the range
                index = [self.dimension(d)._by_id for d in m.dims]
                picks = [sorted(ix[x] for x in region[d]) if d in region else range(len(ix))
                         for d, ix in zip(m.dims, index)]
                combos = list(itertools.product(*picks))
                if not combos:
                    raise ValueError(f"{name}: 按分先のセルがない")
                cols, n = [list(c) for c in zip(*combos)], len(combos)
            new = [float(total / n)] * n
        self._write_many(m.id, cols, new)
        return len(new)

    # ------------------------------------------------ メンバーの追加

    @_operation
    def add_member(self, dim: str, member: str, *, at: int | None = None, id: str | None = None,
                   properties: Mapping[str, str] | None = None) -> str:
        """Add the member to the dimension dim (an id) and return the member id. id is its UUID. properties sets
        the property values ({property id: member id of the target}).

            m.add_member(product, "p2000", properties={category: c03})
            m.add_member(account, "粗利", at=2)  # put it at position 2 (from 0) of the order

        Without at, the member goes to the end of the order. A dimension without order also accepts a position
        in the middle (the engine gives the number at the end and changes only the order, so the other cells
        do not change). An ordered dimension (a time series) accepts only the end.

        If id is the UUID of a member of dim, the member is defined again: it gets the name member, the position
        at (if given), and the property values. If member is the name of a different member, raise ValueError.

        A new member starts empty in each Metric. An operation that gives a value to all members (X + 1,
        IFBLANK, a drill-down, a reference to the previous month) also makes values for the new member,
        so the next recalculation calculates that range.
        """
        d = self.dimension(dim)
        props = {}
        for pid, value in (properties or {}).items():
            self._prop(d, pid)
            self._member(self.dimensions[d.properties[pid][0]], value)
            props[pid] = value
        if id is None and member in d._index:
            raise ValueError(f"{d.name}: メンバー {member!r} はすでにある")
        id, old = self._same_object("メンバー", member, id, d._by_id, d._index, f"{d.name}.{member}")
        if old is not None:
            if d.member_of(id) != member:
                self.rename_member(d.id, id, member)
            if at is not None:
                self.move_member(d.id, id, at)
            for pid, value in props.items():
                self.set_property_values(d.id, pid, {id: value})
            return id
        bits = _bits(len(d.members))
        d.add_member(member, id, at)
        if self.engine.key_bits is not None and _bits(len(d.members)) > bits:
            try:  # the bit width of the dimension grew: if a Metric or a formula no longer fits the key, fail
                self.engine.dimension_changed(self, d.id)
                self._check_widths(d.id)
            except Exception:
                d.remove_member(id)
                self.engine.dimension_changed(self, d.id)
                raise
        for pid, value in props.items():
            d.set_property_value(pid, id, value, self.dimensions[d.properties[pid][0]])
        self._member_added(d.id)
        self._pending.added.setdefault(d.id, set()).add(id)
        return id

    @_operation
    def move_member(self, dim: str, member: str, at: int) -> None:
        """Move the member of the dimension dim (ids) to position at (from 0) of the order.

            m.move_member(account, gross_profit, 2)

        The order is the order of rows() and of a list display. The number and the cells of the engine do not
        change, so nothing is calculated again. An ordered dimension (a time series) cannot change its order,
        because the order has a meaning in the calculation.
        """
        d = self.dimension(dim)
        d.move_member(self._member(d, member), at)

    def _check_widths(self, dim: str) -> None:
        """軸 dim のビット幅が増えたあとも、dim を持つ Metric と、すべての式の途中の結果が、エンジンの
        キーの幅に収まるか確かめる（式はエンジンの型検査が確かめる）。"""
        for m in self.metrics.values():
            if dim in m.dims:
                self._check_key_width(m.name, m.dims)
        with self._named_errors():
            for m in self.metrics.values():
                if m.written is not None:
                    self.engine.planner.check(m.written, self)

    def _member_added(self, dim: str) -> None:
        """軸 dim にメンバーを足したことをエンジンと格納データに反映する。"""
        self.engine.dimension_changed(self, dim)
        # メンバーが増えて、格納データのキーに収まらなくなったら詰め直す
        for store in (self._values, self._counts):
            for name in store:
                store[name] = self.engine.fit(store[name], self)

    # ------------------------------------------------ メンバーの名前の変更と削除

    @_operation
    def rename_member(self, dim: str, member: str, new: str) -> None:
        """Rename the member of the dimension dim (ids) to new.

        The formulas, the property maps, the stored data and the change ranges hold the member id, so nothing is
        calculated again and nothing else changes: the display shows the new name. The id does not change.
        """
        d = self.dimension(dim)
        member = self._member(d, member)
        if new != d.members[d._by_id[member]]:
            d.rename_member(member, new)

    @_operation
    def remove_member(self, dim: str, member: str) -> None:
        """Remove the member from the dimension dim (ids).

        The cells of the member go from all Metrics, and the member goes from the property mappings (a member
        that pointed to it has no target). A member-type value that pointed to it becomes blank. If a formula
        writes `dim."member"`, the member cannot go.

        The recalculation has 2 steps. First, the cells of the member and the values that point to it become
        blank in the inputs, and the engine calculates again as for a normal input change (incremental
        aggregation and the change-based restriction work). Then the member itself goes, and only the ranges
        that still change are calculated again: the cells that an operation that expands to all members made
        for the member (and their aggregates), and a previous-period reference across the member.
        """
        d = self.dimension(dim)
        self._member(d, member)
        for m in self.metrics.values():
            if m.written is not None and mentions_member(m.written, dim, member):
                raise ValueError(f'{m.name} の式が {d.name}."{d.member_of(member)}" を参照しているので消せない')
        self.recalc()
        index = d._by_id[member]
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

        # 1. Make the inputs blank and calculate again as for a normal change
        sources = self._delta_sources()
        for name, m in self.metrics.items():
            if m.formula is not None:
                continue
            here, there = has_cells(name), pointing(name)
            r = union_region(point if here else None, there)
            if r is None:
                continue
            if name in sources:
                self._pending.old_slices[name] = eng.filter(self._values[name], r or None, self)
            if here:
                empty = eng.empty(m.dims, m.kind, self.layout.get(name), cat=self)
                self._values[name] = eng.replace(self._values[name], point, empty, self)
            if there is not None:
                self._values[name] = eng.drop_value(self._values[name], index, self)
            self._pending.changed[name] = r
        self.recalc()

        # 2. Find the ranges that change when the member goes, with the dimension and the maps from before the
        #    removal. Send downstream only the cells of the Metrics that have the member, and the ranges to recalculate
        todo = self.engine.planner.removal_regions(self.compiled(), self._values, self, dim, member)
        self.slice_log._flush()  # the log can hold member numbers: make them ids before the numbers close up
        self.tombstones.add(member)
        self._drop_member(dim, member)
        self._pending.forced.update(todo)  # always calculate the ranges again (sent downstream as an input change)
        self.recalc()

    def _drop_member(self, dim: str, member: str) -> None:
        """Apply the removal of a member (an id) to the dimension (an id), the property mappings and the stored
        data (the positions close up). Nothing is calculated again."""
        d = self.dimensions[dim]
        index = d._by_id[member]
        values_kind = member_kind(dim)
        d.remove_member(member)
        for other in self.dimensions.values():
            for prop, (target, mapping) in list(other.properties.items()):
                if target == dim and member in mapping.values():
                    other.properties[prop] = (target, {k: v for k, v in mapping.items() if v != member})
        self.engine.dimension_changed(self, dim, renumbered=True)
        for name, m in self.metrics.items():
            values = m.kind == values_kind
            if name in self._values and (dim in m.dims or values):
                self._values[name] = self.engine.remove_member(self._values[name], dim, index, member, values, self)
            if dim in m.dims and name in self._counts:
                self._counts[name] = self.engine.remove_member(self._counts[name], dim, index, member, False, self)

    # ------------------------------------------------ 入力

    @_operation
    def set_cell(self, id: str, value: float | bool | str | None, coords: Mapping[str, str]) -> None:
        """Set one cell of the input Metric (an id). coords is dimension id -> member id. A member-type value is a
        member id. None makes the cell blank."""
        m = self.metric(id)
        if m.formula is not None:
            if not m.overridable:
                raise ValueError(f"{m.name} は計算 Metric なので直接入力できない"
                                 "（上書きしたいなら add_formula で overridable=True にする）")
            return self.set_cell(m.override, value, coords)
        key = self._key(m, coords)
        value = self._check(m.id, key, value)
        id = m.id  # the key holds member ids, as the change ranges do
        if self._plan is not None and id in self._delta_sources():
            # Incremental aggregation needs the old value: keep the value from the first touch after the last recalc
            old = self._pending.old_cells.setdefault(id, {})
            if key not in old:
                point = {d: frozenset([member]) for d, member in zip(m.dims, key)}
                cube = self.engine.to_cube(self.engine.filter(self._values[id], point, self), self)
                old[key] = cube.cells.get(key)
        self._values[id] = self.engine.write(self._values[id], key, value, self)
        point = {d: frozenset([member]) for d, member in zip(m.dims, key)}
        self._pending.changed[id] = union_region(self._pending.changed.get(id), point)

    def _write_many(self, id: str, cols: list[list[int]], values: list) -> None:
        """Write the member number columns cols (one for each dimension) and the checked values to the input
        Metric. The state is the same as after set_cell for each cell, but the change range grows one time."""
        m = self.metrics[id]
        if self._plan is not None and id in self._delta_sources():
            self._keep_old(id)  # keep all values of the last recalculation, not one cell at a time
        self._values[id] = self.engine.write_many(self._values[id], cols, values, self)
        members = [self.dimension(d).ids for d in m.dims]
        box = {d: frozenset(ms[i] for i in set(col)) for d, ms, col in zip(m.dims, members, cols)}
        self._pending.changed[id] = union_region(self._pending.changed.get(id), box)

    def _keep_old(self, id: str) -> None:
        """Keep the values of the input Metric from the last recalculation, for incremental aggregation (if not
        kept yet). The single cells that wait go back into the kept values."""
        if id not in self._pending.old_slices:
            before = self.engine.share(self._values[id])
            for key, value in self._pending.old_cells.pop(id, {}).items():
                before = self.engine.write(before, key, value, self)
            self._pending.old_slices[id] = before

    def _check(self, id: str, key: Key, value: float | bool | None) -> float | bool | None:
        """Check the key (member ids, from _key) and the value, and return the value to store (None is blank)."""
        m = self.metrics[id]
        if len(key) != len(m.dims):
            raise ValueError(f"{m.name}: キー {key} の長さが軸 {self._dim_names(m.dims)} と合わない")
        for d, member in zip(m.dims, key):
            if member not in self.dimensions[d]._by_id:
                raise ValueError(f"{m.name}: {self.dimensions[d].name} に {member!r} がない")
        if value is None:
            return None
        if m.kind.startswith("member:"):
            d = self.dimension(m.kind.removeprefix("member:"))
            if not isinstance(value, str) or value not in d._by_id:
                raise ValueError(f"{m.name} は {d.name} のメンバーを値に持つ Metric: {value!r}")
            return float(d._by_id[value])  # the engine holds a member value as its number
        if m.kind == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"{m.name} は boolean の Metric: {value!r}")
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{m.name} は number の Metric: {value!r}")
        return float(value)

    # ------------------------------------------------ 参照

    def value(self, id: str) -> Cube:
        """All cells of the Metric (an id). The Cube has dimension ids and member ids. For a large Metric, get,
        slice, rows and summarize read only a part faster."""
        self.recalc()
        m = self.metric(id)
        return self._shown(m, self.engine.to_cube(self._values[m.id], self))

    def raw(self, id: str) -> Any:
        """The stored data in the format of the engine (a large Metric is not changed into a Cube)."""
        self.recalc()
        return self._values[self.metric(id).id]

    def get(self, id: str, coords: Mapping[str, str]) -> float | bool | str | None:
        """The value of one cell (coords is dimension id -> member id). None if it is blank. Only that cell is
        read, not the whole Metric. A member-type value is a member id."""
        self.recalc()
        m = self.metric(id)
        key = self._key(m, coords)
        for d, member in zip(m.dims, key):
            if member not in self.dimensions[d]._by_id:
                return None  # the cell of a member that does not exist is blank (removed)
        return self._decode(m, self.engine.get(self._values[m.id], key, self))

    def slice(self, id: str, coords: Coords | None = None) -> Cube:
        """The cells in the range coords (dimension id -> a member id or a collection of them). A dimension
        that is not in coords keeps all members.

            m.slice(revenue, {product: p0001})                      # one product, all versions and months
            m.slice(revenue, {product: [p0001, p0002], month: m01})
        """
        self.recalc()
        m = self.metric(id)
        restrict = self._restrict(m, coords or {})
        return self._shown(m, self.engine.to_cube(self.engine.filter(self._values[m.id], restrict, self), self))

    def rows(self, id: str, coords: Coords | None = None, *, offset: int = 0,
             limit: int | None = None) -> tuple[list, int]:
        """The rows in the range coords, in the member order of the declared dimensions, from row offset and at
        most limit rows. Return ([(key of member ids, value), ...], the number of rows in the range). For a
        display or for paging."""
        self.recalc()
        m = self.metric(id)
        if offset < 0 or (limit is not None and limit < 0):
            raise ValueError("offset と limit は 0 以上")
        rows, total = self.engine.rows(self._values[m.id], self._restrict(m, coords or {}), self, offset, limit)
        return [(k, self._decode(m, v)) for k, v in rows], total

    def summarize(self, id: str, coords: Coords | None = None, *, keep: Iterable[str] = (),
                  agg: str = "sum") -> Cube:
        """Aggregate the range in coords and keep only the dimensions in keep (ids; SUM, AVG, MIN, MAX, COUNT).

            m.summarize(revenue, {product: [p0001, p0002]}, keep=[month])  # the monthly sum of 2 products
            m.summarize(revenue).cells[()]                                  # the grand total
        """
        self.recalc()
        m = self.metric(id)
        agg = agg.lower()
        if agg not in PUBLIC_AGGREGATIONS:
            raise ValueError(f"集計は {'、'.join(PUBLIC_AGGREGATIONS)} のいずれか（{agg!r}）")
        if AGGREGATIONS[agg].numeric and m.kind != "number":
            numeric = [a for a in PUBLIC_AGGREGATIONS if not AGGREGATIONS[a].numeric]
            raise ValueError(f"{m.name} は {m.kind} なので {agg} で集計できない（{'、'.join(numeric)} は使える）")
        keep = tuple(self._dim_in(m, d) for d in keep)
        return self.engine.aggregate(self._values[m.id], m.dims, keep, agg, self._restrict(m, coords or {}), self)

    @staticmethod
    def _missing(name: str, dim: str):
        raise ValueError(f"{name}: 軸 {dim} のメンバーを指定していない")

    def _dim_in(self, m: Metric, dim: str) -> str:
        """The dimension id dim of the Metric m. ValueError if m does not have it."""
        if dim not in m.dims:
            raise ValueError(f"{m.name}: 軸 {self._name_of(dim)} がない")
        return dim

    def _coords(self, m: Metric, coords: Mapping[str, Any]) -> dict[str, Any]:
        """Check that each dimension id in coords is a dimension of m (ValueError if not)."""
        if not isinstance(coords, Mapping):
            raise ValueError(f"{m.name}: 座標は {{軸の ID: メンバーの ID}} の対応（{coords!r}）")
        return {self._dim_in(m, d): v for d, v in coords.items()}

    def _key(self, m: Metric, coords: Mapping[str, str]) -> Key:
        """The member ids of one cell of m in the order of m.dims. coords is dimension id -> member id. An
        unknown member stays as it is (the caller reports it)."""
        by_id = self._coords(m, coords)
        return tuple(by_id[d] if d in by_id else self._missing(m.name, self.dimensions[d].name) for d in m.dims)

    def _restrict(self, m: Metric, coords: Coords) -> Restrict:
        """Check coords (dimension id -> a member id or a collection of them) and make the restrict form
        (dimension id -> member ids)."""
        out: Restrict = {}
        for d, ms in self._coords(m, coords).items():
            dim = self.dimensions[d]
            out[d] = frozenset(self._member(dim, x) for x in ([ms] if isinstance(ms, str) else ms))
        return out

    @staticmethod
    def _member(d: Dimension, member: str) -> str:
        """Check the member id. ValueError if d does not have it."""
        if member not in d._by_id:
            raise ValueError(f"{d.name} にメンバー {member!r} がない")
        return member

    def _decode(self, m: Metric, v):
        """The value of the engine (a float) as the caller sees it (a member-type value is a member id)."""
        if v is None:
            return None
        if m.kind.startswith("member:"):
            return self.dimensions[m.kind.removeprefix("member:")].ids[int(v)]
        if m.kind == "boolean":
            return bool(v)
        return v

    def _shown(self, m: Metric, cube: Cube) -> Cube:
        """The cube for a caller: the values of a member-type Metric become member ids. The caller does not get
        the stored data itself (the reference engine stores a Cube)."""
        if m.kind.startswith("member:"):
            ids = self.dimensions[m.kind.removeprefix("member:")].ids
            return Cube(cube.dims, {k: ids[int(v)] for k, v in cube.cells.items()})
        return Cube(cube.dims, dict(cube.cells)) if cube is self._values.get(m.id) else cube

    # ------------------------------------------------ 計算計画

    def _invalidate(self) -> None:
        self._plan = None
        self._pending.full = True

    def _compile(self) -> None:
        if self._plan is not None and not self._pending.dirty:
            return
        if self._plan is None:
            self._compile_all()
            return
        try:
            self._compile_changed()
        except Exception:
            self._invalidate()  # 計画を直せなければ、次は最初から作り直す（同じエラーならまた出る）
            raise

    def _checked(self, m: Metric) -> tuple[Expr, list[Msg]]:
        """Make the formula of m ready for evaluation and check its type. Return (the formula to evaluate,
        the warnings as Msg with ids)."""
        formula, t, w = self.engine.planner.check(m.written, self)
        if set(t.dims) != set(m.dims):
            raise FormulaError("formula_dims", metric=m.name, dims=t.dims, declared=m.dims)
        if t.kind != m.kind:
            raise FormulaError("formula_kind", metric=m.name, kind=t.kind, declared=m.kind)
        if m.overridable:  # 検査は利用者が書いた式で済ませてから包む
            formula = Coalesce(Ref(m.override), formula)  # 上書きがあればそれを優先
        return formula, w

    def _make_plan(self, formulas: dict[str, Expr | None]) -> tuple[list[Step], dict[str, list[Edge]], list[list[Step]]]:
        """依存グラフから計算計画（依存先が先の順の段階、依存グラフ、段ごとの段階）を作る。"""
        return self.engine.planner.plan(formulas, {n: self.metrics[n].dims for n in formulas}, self)

    def _compile_all(self) -> None:
        self._warnings = {}
        for m in self.metrics.values():
            if m.formula is not None:
                m.formula, self._warnings[m.id] = self._checked(m)
        formulas = {i: m.formula for i, m in self.metrics.items()}
        self._plan, self._edges, self._levels = self._make_plan(formulas)
        self.cell_estimates, self._estimate_refs, self._estimate_users = self._estimate_cells(
            formulas, self._plan, self._warnings)
        self._apply_layout()
        self._delta = {}
        if self.delta_aggregation:
            for step in self._plan:
                m = self.metrics[step.names[0]]
                if step.scan_dim is None and m.formula is not None:
                    if (plan := self._delta_plan(m.formula)) is not None:
                        self._delta[m.id] = plan
        self._counts = {n: self.engine.empty(self.metrics[n].dims, "number", self.layout[n], cat=self)
                        for n, plan in self._delta.items() if plan.count is not None}
        self._pending.dirty.clear()
        self._pending.forced.clear()

    def _compile_changed(self) -> None:
        """定義を変えた Metric（_dirty）だけを検査して、計算計画を直す。

        依存グラフの順序（強連結成分と段）は全体で作り直す（Metric の数に比例する程度で速い）。
        分割軸は新しい Metric だけ選び、既存の Metric は今の格納データのまま使う。
        変えた計算 Metric は、次の再計算で全体を計算し直す（_forced）。
        """
        dirty = [n for n in self.metrics if n in self._pending.dirty]
        checked = {n: self._checked(self.metrics[n]) for n in dirty if self.metrics[n].formula is not None}
        formulas = {n: checked[n][0] if n in checked else (None if n in self._pending.dirty else m.formula)
                    for n, m in self.metrics.items()}
        plan, edges, levels = self._make_plan(formulas)
        estimates = self._estimate_cells(formulas, plan, {n: w for n, (_, w) in checked.items()}, set(dirty))

        self.cell_estimates, self._estimate_refs, self._estimate_users = estimates
        for n, (formula, w) in checked.items():
            self.metrics[n].formula, self._warnings[n] = formula, w
        for n in dirty:
            if n not in checked:
                self._warnings.pop(n, None)
        scans = lambda steps: {n for s in steps if s.scan_dim is not None for n in s.names}
        moved = scans(self._plan) ^ scans(plan)  # scan に入った、または scan から出た Metric
        self._plan, self._edges, self._levels = plan, edges, levels
        self._layout_changed(dirty)

        # 差分集計の計画は、変えた Metric と scan に出入りした Metric だけ作り直す
        in_scan = scans(plan)
        for n in {*dirty, *moved}:
            self._delta.pop(n, None)
            self._counts.pop(n, None)
            m = self.metrics[n]
            if self.delta_aggregation and m.formula is not None and n not in in_scan:
                if (dp := self._delta_plan(m.formula)) is not None:
                    self._delta[n] = dp
                    if dp.count is not None:
                        self._counts[n] = self.engine.empty(m.dims, "number", self.layout[n], cat=self)
            if m.formula is not None:
                self._pending.forced[n] = {}  # 件数も含めて、全体を計算し直す
        self._pending.dirty.clear()

    def _estimate_cells(self, formulas: dict[str, Expr | None], plan: list[Step], warnings: dict[str, list[str]],
                        dirty: set[str] | None = None) -> tuple[dict, dict, dict]:
        """計算 Metric ごとの結果のセル数の見積もり（上限）。返すのは (見積もり, 各見積もりが読んだ
        Metric, 各 Metric を読む見積もり)。warnings は検査し直した Metric の警告（ほかは self.warnings）。

        入力は今のセル数を使い、計画の順に式から見積もる。dirty を渡すと、その Metric と、見積もりが
        変わった Metric を読む Metric だけを見積もり直し、ほかは前の見積もりを使う（定義の変更の
        費用を Metric の数に比例させないため。入力のセル数や軸のメンバー数が変わっただけでは見積もり直さない）。
        max_cells を超える Metric があれば、計画の順で最初のものを FormulaError にする。

        scan（前の時点の自分を読む Metric）は、まず自分たちを空として見積もり、その値が時間の軸の
        全時点へ持ち越されうるとして時点の数を掛ける。それを自分たちのセル数としてもう一度見積もり、
        小さいほうを取る。
        """
        one = self.engine.planner.estimate
        if dirty is None:
            out, refs, users, todo = {}, {}, {}, None
        else:
            out, refs, users = dict(self.cell_estimates), dict(self._estimate_refs), dict(self._estimate_users)
            todo = set(dirty)
            for n in dirty:
                if formulas.get(n) is None:  # 入力になった Metric は見積もらず、何も読まない
                    out.pop(n, None)
                    for x in refs.pop(n, ()):
                        users[x] = users.get(x, frozenset()) - {n}
            stack = list(dirty)
            while stack:  # 見積もりが変わりうるのは、定義を変えた Metric を（間接に）読む Metric だけ
                for u in users.get(stack.pop(), ()):
                    if u not in todo:
                        todo.add(u)
                        stack.append(u)
        cells = _CellCounts(self, out)
        changed = set(dirty or ())
        for step in plan:
            if todo is not None and todo.isdisjoint(step.names):
                continue
            names = [n for n in step.names if formulas[n] is not None]
            if todo is not None and all(n in out and n not in changed and n in refs and refs[n].isdisjoint(changed)
                                        for n in names):
                continue  # 読む Metric の見積もりが変わらなかった
            cells.read.clear()
            before = {n: out.get(n) for n in names}
            if step.scan_dim is None:
                for n in names:
                    out[n] = cells[n] = one(formulas[n], self, cells)
            else:
                cells.update({n: 0.0 for n in names})
                carried = sum(one(formulas[n], self, cells) for n in names)
                carried *= len(self.dimensions[step.scan_dim].members)
                cells.update({n: min(carried, combos(self, self.metrics[n].dims)) for n in names})
                for n in names:
                    out[n] = min(one(formulas[n], self, cells), cells[n])
                cells.update({n: out[n] for n in names})
            read = frozenset(cells.read)
            for n in names:
                old = refs.get(n, frozenset())
                if old != read:  # 逆引きは写してから書き換える（複製したモデルと共有しているため）
                    for x in old - read:
                        users[x] = users.get(x, frozenset()) - {n}
                    for x in read - old:
                        users[x] = users.get(x, frozenset()) | {n}
                    refs[n] = read
                if before[n] != out[n]:
                    changed.add(n)
                if self.max_cells is not None and out[n] > self.max_cells:
                    dense = [w for w in warnings.get(n, self._warnings.get(n, [])) if w.code in DENSE]
                    raise FormulaError("too_many_cells", metric=n, cells=out[n], limit=self.max_cells,
                                       dense=msg("dense_ops", ops=dense) if dense else "")
        return out, refs, users

    def _delta_plan(self, formula: Expr) -> DeltaPlan | None:
        """差分集計の対象なら、その計画。"""
        return self.engine.planner.delta_plan(formula, self)

    def _delta_sources(self) -> set[str]:
        """差分集計で、変更前の値が要る Metric（集計元と対応表）。"""
        return {n for plan in self._delta.values() for n in (plan.source, *plan.aux)}

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

    def _sample_point(self, name: str) -> Restrict | None:
        """分割軸の選択に使う、入力 Metric の 1 セル（各軸の先頭メンバー）。"""
        m = self.metrics[name]
        if m.formula is None and m.dims and all(self.dimensions[d].members for d in m.dims):
            return {d: frozenset([self.dimensions[d].ids[0]]) for d in m.dims}
        return None

    def _choose_layout(self) -> dict[str, str | None]:
        # 標本は手元の dict に集めてから _samples に入れる。_samples（_per_metric のビュー）は
        # たどるたびに全 Metric の状態を 1 周するので、Metric ごとに読むと Metric 数の 2 乗になる
        samples: dict[str, dict[str, Restrict]] = {}
        if self.auto_layout and self.engine.partitions > 1:
            # 各入力 Metric の 1 セルを変えたときの影響範囲を集める
            plan = self.compiled()
            for src in self.metrics:
                if (point := self._sample_point(src)) is not None:
                    samples[src] = self.engine.planner.propagate(plan, self, {src: point})
        self._samples = samples
        by_metric = _regions_by_metric(samples)
        return {name: self._pick_partition(name, by_metric) for name in self.metrics}

    def _pick_partition(self, name: str, samples: Mapping[str, list[Restrict]]) -> str | None:
        """name の分割軸。入力の 1 セルの変更で書き換えるときに、触れるパーティションの割合が
        平均で最も小さい軸を選ぶ（明示されていればそれ）。samples は Metric -> 標本の影響範囲の
        一覧（_regions_by_metric）で、呼び出し元が 1 回だけ作って渡す。"""
        m = self.metrics[name]
        if m.partition is not None or not m.dims:
            return m.partition
        partitions = self.engine.partitions

        def by_members(d: str) -> int:
            return len(self.dimensions[d].members)

        def touched(d: str, region: Restrict) -> float:
            """region を書き換えるときに触れるパーティションの割合。"""
            if d not in region:
                return 1.0
            dim = self.dimensions[d]
            width = -(-len(dim.members) // partitions)
            total = -(-len(dim.members) // width)
            # skip a member that was removed after the range was recorded (this only selects the partition dimension)
            return len({dim._by_id[x] // width for x in region[d] if x in dim._by_id}) / total

        regions = samples.get(name)
        if not regions:
            return max(m.dims, key=by_members)
        return min(m.dims, key=lambda d: (mean(touched(d, r) for r in regions), -by_members(d)))

    def _layout_changed(self, dirty: list[str]) -> None:
        """定義を変えた Metric の分割軸。新しい Metric は選び、既存の Metric は明示されたときだけ変える
        （格納データを持ち直さなければ、計算し直したときに値が変わったセルだけを下流へ伝えられる）。"""
        samples = dict(self._samples)  # 1 回だけ実体化する（影響範囲の dict は _samples と共有）
        if samples:
            for n in dirty:  # 分割軸の選択用の影響範囲に、変えた Metric の分を足す（計画の順）
                m = self.metrics[n]
                for src, regions in samples.items():
                    regions.pop(n, None)
                    if m.formula is not None and (r := self._affected(m.formula, regions)) is not None:
                        regions[n] = r
                if (point := self._sample_point(n)) is not None:
                    samples[n] = self._samples[n] = {n: point}
        by_metric = _regions_by_metric(samples)
        for n in dirty:
            m = self.metrics[n]
            if n not in self.layout or m.partition is not None:
                self.layout[n] = self._pick_partition(n, by_metric)
            want = self.layout[n]
            if n not in self._values:
                self._values[n] = self.engine.empty(m.dims, m.kind, want, cat=self)
            elif self.engine.partition_of(self._values[n]) != want:
                self._values[n] = self.engine.repartition(self._values[n], want, self)

    # ------------------------------------------------ 影響範囲

    def _propagate(self, changed: dict[str, Restrict],
                   added: dict[str, frozenset[str]] | None = None) -> dict[str, Restrict]:
        """入力の変更範囲と追加したメンバーを計画の順に伝え、影響を受ける全 Metric の範囲を返す
        （changed を含む）。"""
        return self.engine.planner.propagate(self.compiled(), self, changed, added)

    def _affected(self, formula: Expr, regions: dict[str, Restrict]) -> Restrict | None:
        """1 つの式の影響範囲。"""
        return self.engine.planner.affected(formula, self, regions)

    # ------------------------------------------------ 再計算

    def recalc(self) -> None:
        """ためている変更を計算に反映する。途中で失敗したら（式のエラー、エンジンの内部エラー）、
        途中まで書き換えた計算 Metric が残るので、次の recalc で全体を計算し直す（入力は書き換えない）。"""
        with self._named_errors():
            self._compile()
            if self._pending.empty():
                return
            try:
                self._recalc_pending()
            except BaseException:
                self._pending.full = True
                raise

    @contextlib.contextmanager
    def _named_errors(self):
        """Show Metric names in a FormulaError that leaves the Model. The type check and the plan work with
        Metric ids, so their error values can hold ids. This is the one point that changes them to names."""
        try:
            yield
        except FormulaError as e:
            e.params = self._shown_params(e.params)
            e.args = (render(e.code, e.params),)
            raise

    def _shown_params(self, params: dict) -> dict:
        """The params of a FormulaError or a warning with names in place of Metric, dimension and property ids.
        UUIDs are unique in the model, so one lookup serves all kinds."""
        def show(v):
            if isinstance(v, str):
                return self._name_of(v)
            if isinstance(v, Msg):
                return Msg(v.code, self._shown_params(v.params))
            if isinstance(v, list):
                if v and all(isinstance(x, str) and x in self.metrics for x in v):
                    return sorted(show(x) for x in v)  # a list of Metrics (a cycle) in name order, not id order
                return [show(x) for x in v]
            if isinstance(v, tuple):
                return tuple(show(x) for x in v)
            return v
        return {k: show(v) for k, v in params.items()}

    def _recalc_pending(self) -> None:
        full, self._pending.full = self._pending.full, False
        added = {d: frozenset(ms) for d, ms in self._pending.added.items()}
        self._pending.added.clear()
        if full:  # 全体を計算し直す
            regions, olds, added = {}, {}, {}
            forced = {n: {} for n, m in self.metrics.items() if m.formula is not None}
        else:
            regions = dict(self._pending.changed)
            # 差分集計の集計元と対応表について、変更前の値を確保しておく
            sources = self._delta_sources()
            olds = {n: self._old_input_slice(n, regions[n]) for n in self._pending.changed if n in sources}
            # 定義を変えた計算 Metric は、影響範囲に関係なく計算し直す（差分集計は使わない）
            forced = self._pending.forced
        self._pending.changed.clear()
        self._pending.old_cells.clear()
        self._pending.old_slices.clear()
        self._pending.forced = {}
        done, named = self.engine.planner.recalc(self.compiled(), self._values, self._counts, self, regions,
                                                 added, olds, forced, full)
        self.eval_log.extend(n for n, _ in done)
        self.delta_log.extend(n for n, delta in done if delta)
        self.slice_log.extend_later(named)

    def _old_input_slice(self, name: str, region: Restrict) -> Any:
        """入力 Metric の region の、変更前の値。今の値から、触れたセルだけ覚えておいた値に戻す。"""
        if name in self._pending.old_slices:  # 範囲ごと空にした（メンバーの削除）。region はその範囲
            return self._pending.old_slices[name]
        old = self.engine.filter(self._values[name], region or None, self)
        for key, value in self._pending.old_cells.get(name, {}).items():
            old = self.engine.write(old, key, value, self)
        return old


def _regions_by_metric(samples: Mapping[str, dict[str, Restrict]]) -> dict[str, list[Restrict]]:
    """入力ごとの標本（入力 -> {Metric: 影響範囲}）を、Metric -> 影響範囲の一覧に組み替える。
    分割軸を Metric ごとに選ぶときに、標本を Metric ごとに 1 周しないため。"""
    out: dict[str, list[Restrict]] = {}
    for regions in samples.values():
        for n, r in regions.items():
            out.setdefault(n, []).append(r)
    return out


def _bits(size: int) -> int:
    """size 個のメンバーの番号に要るビット数（Rust の key.rs の bits_for と同じ）。"""
    return max(1, (size - 1).bit_length())


_KEPT_ON_RESTORE = frozenset({"eval_log", "slice_log", "delta_log", "journal", "last_record"})
