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
from dataclasses import dataclass, field
from statistics import mean
from typing import Any, Mapping

from .core import Cube, Dimension, Key
from .delta import DeltaPlan, plan_for, rename
from .engine import Engine, default_engine
from .evaluate import (Edge, FormulaError, Kind, Restrict, Type, affected, collect_refs, infer,
                       member_kind, resolve, union_region)
from .expr import (BinOp, Coalesce, Const, Expr, Filter, Ref, mentions_member, references_metric,
                   rename_member, rename_metrics, uses_property)
from .journal import AlreadyCommitted, Transaction, changes, jsonable, now
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
    id: int = 0  # 変わらない ID（名前の変更や式の置き換えで変わらない。Model が振る）

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
    _edges: dict[str, list[Edge]] = field(default_factory=dict)  # 依存グラフ（Metric -> 参照先）
    _dirty: set[str] = field(default_factory=set)  # 前回の計画のあとで定義を変えた Metric
    _forced: dict[str, Restrict] = field(default_factory=dict)  # 次の再計算で必ず計算し直す計算 Metric の範囲
    _samples: dict[str, dict[str, Restrict]] = field(default_factory=dict)  # 分割軸の選択に使った、入力ごとの影響範囲
    _next_id: int = 1  # 次に振る ID（軸、メンバー、Metric で共通。消した ID は再利用しない）
    journal: Any = None  # 記録先（journal.FileJournal など）。None なら記録しない
    seq: int = 0  # 確定した最後のトランザクションの通し番号
    last_record: dict | None = None  # 最後に確定したトランザクションの記録
    _txn: Transaction | None = None  # 実行中のトランザクション
    _frozen: bool = False  # 公開済みの版（Workspace）。書き換えない

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
        if self._frozen:
            raise ValueError("公開済みの版は計算し直せない（Workspace.write の中で計算し直す）")
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
        other._edges = dict(self._edges)
        other._samples = {src: dict(regions) for src, regions in self._samples.items()}  # 定義を変えると書き足す
        other._next_id = self._next_id
        other.seq = self.seq
        other._full = False
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
            record = {"v": 1, "at": now(), "user": user, "reason": reason, "client_op_id": client_op_id,
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
        """トランザクションの前の版 saved に戻す（観察用の記録と記録先はそのまま）。"""
        self.slice_log._flush()  # 記録はメンバーの番号で持っていることがあるので、軸を戻す前に名前へ直す
        keep = {k: getattr(self, k) for k in ("eval_log", "slice_log", "delta_log", "journal", "last_record")}
        self.__dict__.update(saved.__dict__)
        self.__dict__.update(keep)

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
    def load(cls, path, engine: Engine | None = None) -> Model:
        """save で保存したディレクトリから Model を作る。engine を省略すると既定のエンジン。"""
        from .storage import load
        return load(path, engine)

    # ------------------------------------------------ 定義

    @_operation
    def add_dimension(self, name: str, members, *, ordered: bool = False) -> Dimension:
        if name in self.metrics:
            raise ValueError(f"{name}: 同じ名前の Metric がある（式の中で軸と区別できなくなる）")
        members = list(members)
        self.dimensions[name] = Dimension(name, members, ordered=ordered, id=self._new_id(),
                                          ids=[self._new_id() for _ in members])
        return self.dimensions[name]

    def _new_id(self) -> int:
        """軸、メンバー、Metric に振る、モデルの中で一意の ID。

        複製（fork）は同じ番号から振り続けるので、複製と元で別々に足したものが同じ ID になりうる
        （複製の変更を元へ取り込むときは、ID を振り直す必要がある）。
        """
        self._next_id += 1
        return self._next_id - 1

    def metric_name(self, id: int) -> str:
        """ID の Metric の今の名前。"""
        for m in self.metrics.values():
            if m.id == id:
                return m.name
        raise ValueError(f"ID {id} の Metric がない")

    @_operation
    def add_property(self, dim: str, prop: str, target: str, mapping: Mapping[str, str]) -> None:
        """軸 dim にプロパティ prop（dim のメンバー -> target のメンバー）を付ける。同じ名前があれば置き換える。

        置き換えると、式でそのプロパティを使う Metric（`[BY: dim.prop]`）を計算し直す。
        """
        self.dimension(dim).add_property(prop, self.dimension(target), mapping)
        self.engine.dimension_changed(self, dim)  # エンジンが持つ対応表を新しい中身にする
        for m in self.metrics.values():
            if m.written is not None and uses_property(m.written, dim, prop):
                self._redefine(m.name)

    @_operation
    def add_input(self, name: str, dims, cells: Mapping[Key, float | bool] | None = None,
                  *, kind: Kind = "number", storage: Any = None, partition: str | None = None) -> None:
        """cells は {キー: 値}。大量のデータはエンジンの格納形式で storage に渡してもよい。

        同じ名前の Metric があれば置き換える。軸と値の種類が同じなら、全セルの入力の変更として
        差分で計算し直す（計算 Metric を入力に置き換えてもよい）。
        """
        self._check_name(name)
        self._check_kind(name, kind)
        dims = tuple(dims)
        for d in dims:
            self.dimension(d)
        old = self.metrics.get(name)
        new = Metric(name, dims, kind, partition=self._check_partition(name, dims, partition),
                     id=old.id if old is not None else self._new_id())
        if storage is None:
            self.metrics[name] = new  # _check は登録した Metric の軸で検査する
            try:
                checked = {key: self._check(name, key, value) for key, value in (cells or {}).items()}
            except ValueError:
                if old is None:
                    del self.metrics[name]
                else:
                    self.metrics[name] = old
                raise
            storage = self.engine.from_cells(dims, kind, {k: v for k, v in checked.items() if v is not None},
                                             self, partition)
        if old is not None and self._plan is not None and self._same_type(old, new) and name not in self._forced:
            # 差分集計には変更前の値が要る。まだ再計算していない変更があれば、その前の値に戻して取っておく
            if name not in self._old_slices:
                before = self.engine.share(self._values[name])
                for key, value in self._old_cells.pop(name, {}).items():
                    before = self.engine.write(before, key, value, self)
                self._old_slices[name] = before
            # 変更前後で値が違うセルだけを変更範囲にする
            _, diff = self.engine.replace_diff(self.engine.share(self._old_slices[name]), {}, storage, self)
            if diff is not None:
                self._changed[name] = union_region(self._changed.get(name), diff)
        self.metrics[name] = new
        self._values[name] = storage
        self._redefine(name, old)

    @_operation
    def add_formula(self, name: str, dims, formula: Expr | str, *, kind: Kind = "number",
                    partition: str | None = None, overridable: bool = False) -> None:
        """formula は AST か式の文字列。文字列の構文エラーはここで ParseError になる。

        overridable なら、set_cell で式の結果を手入力で上書きできる。上書きした値は式より優先され、
        下流にもそのまま伝わる。set_cell で None を入れると、そのセルは式の結果に戻る。

        同じ名前の Metric があれば置き換える。軸と値の種類が同じなら、その Metric を計算し直し、
        値が変わったセルだけを下流へ伝える（入力を計算 Metric に置き換えてもよい）。
        """
        self._check_name(name)
        self._check_kind(name, kind)
        if isinstance(formula, str):
            formula = parse(formula, self_name=name)
        dims = tuple(dims)
        old = self.metrics.get(name)
        m = Metric(name, dims, kind, formula, self._check_partition(name, dims, partition), formula, overridable,
                   id=old.id if old is not None else self._new_id())
        if old is not None and old.formula is None and name in self._changed:
            self._invalidate()  # 未反映の入力の変更があった入力を式にするのは、全体で計算し直す
        self.metrics[name] = m
        if overridable and m.override_name not in self.metrics:  # 読み込みでは上書き値が先に入る
            self.add_input(m.override_name, dims, kind=kind, partition=partition)
        self._redefine(name, old)

    # ------------------------------------------------ Metric の削除と名前の変更

    @_operation
    def remove_metric(self, name: str) -> None:
        """Metric を消す。どの式からも参照されていない Metric だけを消せる（消しても他の値は変わらない）。
        上書きできる Metric なら、上書き用の隠し入力も一緒に消す。ID は再利用しない。"""
        m = self._own_metric(name)
        users = sorted(x.name for x in self.metrics.values()
                       if x.written is not None and x.name != name and references_metric(x.written, name))
        if users:
            raise ValueError(f"{name} は {', '.join(users)} の式が参照しているので消せない")
        gone = {name} | ({m.override_name} if m.overridable and m.override_name in self.metrics else set())
        for n in gone:
            for store in (self.metrics, self._values, self._counts, self._delta, self._delta_cache, self.layout,
                          self.warnings, self._edges, self._samples, self._forced, self._changed,
                          self._old_cells, self._old_slices):
                store.pop(n, None)
            for regions in self._samples.values():
                regions.pop(n, None)
        self._dirty -= gone
        if self._plan is not None:  # 誰も参照していないので、計画からその段階を外すだけで済む
            self._plan = [s for s in self._plan if s.names[0] not in gone]
            self._levels = [[s for s in level if s.names[0] not in gone] for level in self._levels]

    @_operation
    def rename_metric(self, old: str, new: str) -> None:
        """Metric の名前を変える。値は変わらないので計算し直さない。式の中の参照（Metric を使った
        BY も）はすべて新しい名前になる。上書き用の隠し入力の名前も一緒に変わる。ID は変わらない。"""
        m = self._own_metric(old)
        if not isinstance(new, str) or not new or new.startswith("__"):
            raise ValueError(f"Metric の名前は空でなく、__ で始まらない文字列: {new!r}")
        if new in self.metrics:
            raise ValueError(f"{new}: 同じ名前の Metric がある")
        self._check_name(new)
        self.recalc()  # 変更範囲などは名前で持つので、ためている変更を先に片付ける
        self.slice_log._flush()
        names = {old: new}
        if m.overridable and m.override_name in self.metrics:
            names[m.override_name] = f"__override__{new}"
        for store in (self.metrics, self._values, self._counts, self._delta, self.layout, self.warnings,
                      self._edges, self._samples):
            for o, n in names.items():
                if o in store:
                    store[n] = store.pop(o)
        for o, n in names.items():
            self.metrics[n].name = n
        for regions in self._samples.values():
            for o, n in names.items():
                if o in regions:
                    regions[n] = regions.pop(o)
        for x in self.metrics.values():
            if x.written is not None:
                x.written = rename_metrics(x.written, names)
            if x.formula is not None:
                x.formula = rename_metrics(x.formula, names)
        self._edges = {src: [dataclasses.replace(e, target=names.get(e.target, e.target)) for e in edges]
                       for src, edges in self._edges.items()}
        self._delta = {n: dataclasses.replace(dp, source=names.get(dp.source, dp.source),
                                              aux=tuple(names.get(a, a) for a in dp.aux),
                                              count=None if dp.count is None else rename_metrics(dp.count, names))
                       for n, dp in self._delta.items()}
        self._delta_cache.clear()
        if self._plan is not None:
            step = lambda s: Step(tuple(names.get(n, n) for n in s.names), s.scan_dim)
            self._plan = [step(s) for s in self._plan]
            self._levels = [[step(s) for s in level] for level in self._levels]

    def _own_metric(self, name: str) -> Metric:
        """利用者が名前で扱える Metric（上書き用の隠し入力は、持ち主と一緒にしか扱えない）。"""
        if name not in self.metrics:
            raise ValueError(f"Metric {name} がない")
        if name.startswith("__override__"):
            raise ValueError(f"{name} は上書き用の隠し入力なので、持ち主の Metric を通して扱う")
        return self.metrics[name]

    @staticmethod
    def _same_type(old: Metric, new: Metric) -> bool:
        return old.dims == new.dims and old.kind == new.kind

    def _redefine(self, name: str, old: Metric | None = None) -> None:
        """name の定義を変えたことを記録する。次の再計算では、変えた Metric だけを検査して計算計画を
        直し、その Metric を計算し直して、値が変わったセルだけを下流へ伝える。

        軸か値の種類が変わると、それを参照する式の型検査からやり直しになるので、全体で計算し直す。
        """
        if self._plan is None or (old is not None and not self._same_type(old, self.metrics[name])):
            self._invalidate()
            return
        self._dirty.add(name)

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

    @_operation
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

    @_operation
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
        d.add_member(member, self._new_id())
        for prop, value in properties.items():
            d.set_property_value(prop, member, value, self.dimension(d.properties[prop][0]))
        self._member_added(dim)
        self._added.setdefault(dim, set()).add(member)

    def _member_added(self, dim: str) -> None:
        """軸 dim にメンバーを足したことをエンジンと格納データに反映する。"""
        self.engine.dimension_changed(self, dim)
        # メンバーが増えて、格納データのキーに収まらなくなったら詰め直す
        for store in (self._values, self._counts):
            for name in store:
                store[name] = self.engine.fit(store[name], self)

    # ------------------------------------------------ メンバーの名前の変更と削除

    @_operation
    def rename_member(self, dim: str, old: str, new: str) -> None:
        """軸 dim のメンバー old の名前を new にする。

        値も計算結果も変わらない（エンジンの中ではメンバーを番号で持つ）ので、計算し直さない。
        プロパティの対応表、メンバー型の Metric、式の中の `dim."old"` もすべて新しい名前になる。
        """
        d = self.dimension(dim)
        self.recalc()  # 変更範囲はメンバー名で持つので、ためている変更を先に片付ける
        self.slice_log._flush()  # 記録を今の名前で直しておく
        self._rename_member_raw(dim, old, new)
        for name, m in self.metrics.items():
            if m.written is not None:
                m.written = rename_member(m.written, dim, old, new)
                m.formula = rename_member(m.formula, dim, old, new)
        for name, plan in self._delta.items():
            if plan.count is not None:
                self._delta[name] = dataclasses.replace(plan, count=rename_member(plan.count, dim, old, new))
        self._delta_cache.clear()

    def _rename_member_raw(self, dim: str, old: str, new: str) -> None:
        """メンバーの名前の変更を、軸、プロパティの対応表、格納データに反映する（式は書き換えない）。"""
        self.dimension(dim).rename_member(old, new)
        for other in self.dimensions.values():
            for prop, (target, mapping) in list(other.properties.items()):
                if target == dim and old in mapping.values():
                    other.properties[prop] = (target, {k: new if v == old else v for k, v in mapping.items()})
        self.engine.dimension_changed(self, dim)
        for name, m in self.metrics.items():
            if dim in m.dims and name in self._values:  # 再生の途中では計算 Metric の格納データがない
                self._values[name] = self.engine.rename_member(self._values[name], dim, old, new, self)
                if name in self._counts:
                    self._counts[name] = self.engine.rename_member(self._counts[name], dim, old, new, self)

    @_operation
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
        self._drop_member(dim, member)
        self._forced.update(todo)  # 範囲を必ず計算し直す（下流への伝え方は入力の変更と同じ）
        self.recalc()

    def _drop_member(self, dim: str, member: str) -> None:
        """メンバーを消したことを、軸、プロパティの対応表、格納データ（位置を詰める）に反映する
        （計算し直さない）。"""
        d = self.dimension(dim)
        index = d._index[member]
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

    @_operation
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
        if self._plan is not None and not self._dirty:
            return
        if self._plan is None:
            self._compile_all()
            return
        try:
            self._compile_changed()
        except Exception:
            self._invalidate()  # 計画を直せなければ、次は最初から作り直す（同じエラーならまた出る）
            raise

    def _checked(self, m: Metric) -> tuple[Expr, list[str]]:
        """m の式を評価できる形に直して型を検査し、(評価に使う式, 警告) を返す。"""
        formula = resolve(m.written, self)  # 軸の名前、Metric を使った BY を評価できる形に
        w: list[str] = []
        t = infer(formula, self, w)
        if set(t.dims) != set(m.dims):
            raise FormulaError(f"{m.name}: 式の軸 {t.dims} が宣言した軸 {m.dims} と一致しない")
        if t.kind != m.kind:
            raise FormulaError(f"{m.name}: 式の値は {t.kind} だが {m.kind} として宣言されている")
        if m.overridable:  # 検査は利用者が書いた式で済ませてから包む
            formula = Coalesce(Ref(m.override_name), formula)  # 上書きがあればそれを優先
        return formula, w

    def _compile_all(self) -> None:
        edges: dict[str, list[Edge]] = {}
        self.warnings = {}
        for m in self.metrics.values():
            if m.formula is None:
                edges[m.name] = []
                continue
            m.formula, self.warnings[m.name] = self._checked(m)
            edges[m.name] = list(collect_refs(m.formula, self))

        self._plan = [self._make_step(scc, edges) for scc in _tarjan(edges)]
        self._edges = edges
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
        self._dirty.clear()
        self._forced.clear()

    def _compile_changed(self) -> None:
        """定義を変えた Metric（_dirty）だけを検査して、計算計画を直す。

        依存グラフの順序（強連結成分と段）は全体で作り直す（Metric の数に比例する程度で速い）。
        分割軸は新しい Metric だけ選び、既存の Metric は今の格納データのまま使う。
        変えた計算 Metric は、次の再計算で全体を計算し直す（_forced）。
        """
        dirty = [n for n in self.metrics if n in self._dirty]
        checked = {n: self._checked(self.metrics[n]) for n in dirty if self.metrics[n].formula is not None}
        edges = dict(self._edges)
        for n in dirty:
            edges[n] = list(collect_refs(checked[n][0], self)) if n in checked else []
        plan = [self._make_step(scc, edges) for scc in _tarjan(edges)]

        for n, (formula, w) in checked.items():
            self.metrics[n].formula, self.warnings[n] = formula, w
        for n in dirty:
            if n not in checked:
                self.warnings.pop(n, None)
        scans = lambda steps: {n for s in steps if s.scan_dim is not None for n in s.names}
        moved = scans(self._plan) ^ scans(plan)  # scan に入った、または scan から出た Metric
        self._plan, self._edges = plan, edges
        self._levels = _levels(plan, edges)
        self._layout_changed(dirty)

        # 差分集計の計画は、変えた Metric と scan に出入りした Metric だけ作り直す
        in_scan = scans(plan)
        for n in {*dirty, *moved}:
            self._delta.pop(n, None)
            self._delta_cache.pop(n, None)
            self._counts.pop(n, None)
            m = self.metrics[n]
            if self.delta_aggregation and m.formula is not None and n not in in_scan:
                if (dp := plan_for(m.formula, self)) is not None:
                    self._delta[n] = dp
                    if dp.count is not None:
                        self._counts[n] = self.engine.empty(m.dims, "number", self.layout[n], cat=self)
            if m.formula is not None:
                self._forced[n] = {}  # 件数も含めて、全体を計算し直す
        self._dirty.clear()

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

    def _sample_point(self, name: str) -> Restrict | None:
        """分割軸の選択に使う、入力 Metric の 1 セル（各軸の先頭メンバー）。"""
        m = self.metrics[name]
        if m.formula is None and m.dims and all(self.dimensions[d].members for d in m.dims):
            return {d: frozenset([self.dimensions[d].members[0]]) for d in m.dims}
        return None

    def _choose_layout(self) -> dict[str, str | None]:
        self._samples = {}
        if self.auto_layout and getattr(self.engine, "partitions", 1) > 1:
            # 各入力 Metric の 1 セルを変えたときの影響範囲を集める
            for src in self.metrics:
                if (point := self._sample_point(src)) is not None:
                    self._samples[src] = self._propagate({src: point})
        return {name: self._pick_partition(name) for name in self.metrics}

    def _pick_partition(self, name: str) -> str | None:
        """name の分割軸。入力の 1 セルの変更で書き換えるときに、触れるパーティションの割合が
        平均で最も小さい軸を選ぶ（明示されていればそれ）。"""
        m = self.metrics[name]
        if m.partition is not None or not m.dims:
            return m.partition
        partitions = getattr(self.engine, "partitions", 1)

        def by_members(d: str) -> int:
            return len(self.dimensions[d].members)

        def touched(d: str, region: Restrict) -> float:
            """region を書き換えるときに触れるパーティションの割合。"""
            if d not in region:
                return 1.0
            dim = self.dimensions[d]
            width = -(-len(dim.members) // partitions)
            total = -(-len(dim.members) // width)
            # 記録したあとで名前を変えたり消したりしたメンバーは読み飛ばす（分割軸の選び方にしか使わない）
            return len({dim._index[x] // width for x in region[d] if x in dim._index}) / total

        samples = [regions[name] for regions in self._samples.values() if name in regions]
        if not samples:
            return max(m.dims, key=by_members)
        return min(m.dims, key=lambda d: (mean(touched(d, r) for r in samples), -by_members(d)))

    def _layout_changed(self, dirty: list[str]) -> None:
        """定義を変えた Metric の分割軸。新しい Metric は選び、既存の Metric は明示されたときだけ変える
        （格納データを持ち直さなければ、計算し直したときに値が変わったセルだけを下流へ伝えられる）。"""
        if self._samples:
            for n in dirty:  # 分割軸の選択用の影響範囲に、変えた Metric の分を足す（計画の順）
                m = self.metrics[n]
                for src, regions in self._samples.items():
                    regions.pop(n, None)
                    if m.formula is not None and (r := affected(m.formula, self, regions)) is not None:
                        regions[n] = r
                if (point := self._sample_point(n)) is not None:
                    self._samples[n] = {n: point}
        for n in dirty:
            m = self.metrics[n]
            if n not in self.layout or m.partition is not None:
                self.layout[n] = self._pick_partition(n)
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
        if not self._full and not self._changed and not self._added and not self._forced:
            return
        full, self._full = self._full, False
        added = {d: frozenset(ms) for d, ms in self._added.items()}
        self._added.clear()
        if full:
            self._changed.clear()
            self._old_cells.clear()
            self._old_slices.clear()
            self._forced.clear()
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
        # 定義を変えた計算 Metric は、影響範囲に関係なく計算し直す（差分集計は使わない）
        forced, self._forced = self._forced, {}
        fast = getattr(self.engine, "recalc_changes", None)
        if fast is not None:  # 段取りごとエンジンに任せる（Rust）。意味は以下の Python の経路と同じ
            done, named = fast(self, regions, added, olds, forced)
            self.eval_log.extend(n for n, _ in done)
            self.delta_log.extend(n for n, delta in done if delta)
            self.slice_log.extend_later(named)
            return
        for step in self._plan:
            if step.scan_dim is not None:
                seeded = regions | {n: forced[n] for n in step.names if n in forced}
                active = {n: r for n, r in self._scan_regions(step, seeded, added).items()
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
            if m.formula is None:
                continue
            region = union_region(affected(m.formula, self, regions, added), forced.get(m.name))
            if region is None:
                continue
            if m.name in sources:
                olds[m.name] = self.engine.filter(self._values[m.name], region or None, self)
            plan = self._delta.get(m.name)
            if plan is not None and m.name not in forced and self._delta_applicable(plan, regions, olds):
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
        changed = self._widened(changed)
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

    def _widened(self, changed: Restrict | None) -> Restrict | None:
        """値が変わったセルの範囲から、全メンバーにわたる軸を外す（その軸は「全体」として扱う）。
        範囲が全体になれば、下流の書き戻しは並べ直すだけで済み、差分集計より計算し直しを選べる。
        Rust のエンジンは、大きな Metric では範囲が大半を占めるときも全体にする（plan.rs）。"""
        if changed is None:
            return None
        return {d: ms for d, ms in changed.items() if len(ms) < len(self.dimensions[d].members)}

    def _replace(self, m: Metric, region: Restrict, new: Any, diff: bool = False) -> Restrict | None:
        """Metric の region 内のセルを new で置き換える。region 外のセルはそのまま残す。

        diff なら、値が実際に変わったセルを囲む範囲を返す（変化なしなら None）。
        """
        new = self.engine.reorder(new, m.dims)
        changed = None
        if diff:
            self._values[m.name], changed = self.engine.replace_diff(self._values[m.name], region, new, self)
            changed = self._widened(changed)
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
