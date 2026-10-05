"""版の公開と単一ライター。複数の利用者が同時に読み書きするための入口。

Workspace はモデルを 1 つ預かり、確定した状態を「版」として公開する。版は作ったら変えない。
読み出しはいつでも公開中の版（version）を見るので、書き込みの途中の値や、取り消された変更は見えない。
A version is a frozen Model. Read one cell, a range, a page, or a summary only as necessary.
An operation on a version causes a ValueError. To try changes, make a copy with fork.

書き込みは 1 本のスレッド（ライター）が列から順に取り出して処理する。列に溜まっている書き込みを
1 つのまとまりにして、公開中の版の複製に 1 件ずつトランザクションとして適用し、記録は 1 回の
書き出しでまとめて確定する（グループコミット）。確定したら、複製を新しい版として公開する。

    ws = Workspace(model, FileJournal("plan/"), checkpoint_every=1000)
    seq = ws.write(lambda m: m.set_cell("Price", 12, Product="A"), user="alice")
    ws.version.get("Price", Product="A")

1 件の書き込みが失敗したら、その 1 件だけを取り消して、同じまとまりのほかの書き込みは確定する。
記録の書き出しに失敗したら、まとまり全体を捨て、公開中の版は変えない。記録先が「手元の版が古い」
と言えば（別のプロセスが書き込んだ）、記録先から開き直して最新の版を公開する。

列の長さ（max_queue）を決めると、溢れたときは submit が Overloaded を投げる（過負荷を上流に伝える）。
checkpoint_every か checkpoint_interval を決めると、その間隔でスナップショットを別のスレッドで取る。

standby=True にすると、同じモデルを開いた複数のプロセスのうち 1 つだけが書き込みを受ける。各プロセスは
2 つの役割（Role）のどちらかにいる。書き手（LEADER）は記録先の書き込みの権利（リース）を持ち、書き込みを
受ける。待機系（STANDBY）は見張りのスレッド（nanashi-standby）で記録先に追従して読み出しだけを受け、
書き込みは NotLeader で拒む（leader に書き手の番地）。権利が空けば（書き手が close で手放した、落ちて
期限が切れた）取り、記録に追いついてから書き手になる。書き手は権利を失えば（確定が締め出された、
延長できなかった）待機系に戻る。役割を変えるのは _promote と _demote だけで、ライターはまとまりごとに
同じロック（_role_lock）を持つので、まとまりの途中で役割は変わらない。

格納データの本体は版どうしで共有するので、版を作る費用は差分の分だけで済む。古い版は、
読んでいる人がいなくなれば捨てられる。
"""
from __future__ import annotations

import collections
import dataclasses
import enum
import logging
import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable

from .evaluate import FormulaError
from .journal import FileJournal, Journal, Stale
from .model import DuplicateId, Model

log = logging.getLogger(__name__)


class Conflict(Exception):
    """読んだ版より後に、同じセルを他の書き込みが変えていた。seq と user はその書き込み。"""

    def __init__(self, message: str, seq: int | None = None, user: str | None = None):
        super().__init__(message)
        self.seq = seq
        self.user = user


class Overloaded(Exception):
    """書き込みの列が満杯（max_queue）で、timeout の間に空かなかった。"""


class Rejected(Exception):
    """A write with this client_op_id was rejected before. The HTTP server returns the same status and body again."""

    def __init__(self, status: int, body: dict):
        super().__init__(f"この操作は拒否済み（{status}）")
        self.status, self.body = status, body


def rejection_of(e: Exception) -> tuple[int, dict] | None:
    """The HTTP status and body of a write that the model refused, or None if the refusal is not terminal.
    If the refusal is terminal, a resend with the same client_op_id gets the same answer."""
    if isinstance(e, DuplicateId):
        return 409, {"error": "duplicate_id", "message": str(e)}
    if isinstance(e, FormulaError):
        return 400, {"error": "formula", "message": str(e), "code": e.code}
    if isinstance(e, ValueError):
        return 400, {"error": "bad_request", "message": str(e)}
    return None


class Role(enum.Enum):
    """書き手のプロセスの役割。LEADER は書き込みの権利を持ち、書き込みを受ける。STANDBY は記録先に追従して
    読み出しだけを受け、権利が空いたら取って LEADER になる。"""
    LEADER = "leader"
    STANDBY = "standby"


class NotLeader(Exception):
    """この Workspace は待機系で、書き込みを受けない。leader は書き手が公開している番地（分からなければ None）。"""

    def __init__(self, leader: str | None):
        super().__init__("この書き手は待機系（書き込みは書き手へ送る）" if leader is None
                         else f"この書き手は待機系（書き込みは {leader} へ送る）")
        self.leader = leader


@dataclasses.dataclass
class _Request:
    fn: Callable[[Any], Any]
    user: str | None
    reason: str | None
    client_op_id: str | None
    expect: int | None
    future: Future


class Written:
    """記録で書き換えた入力セル。行の列で持つものは (Metric の ID, 座標のメンバーの ID の組) の集合に、
    変更の塊で持つものは塊のまま（Python のオブジェクトにせずに）持つ。"""

    def __init__(self, record: dict):
        self.cells: set[tuple] = set()
        self.blocks: list[tuple[str, Any]] = []
        for c in record["changes"].get("cells", []):
            if isinstance(c["rows"], list):
                self.cells.update((c["metric"], tuple(ids)) for ids, _, _ in c["rows"])
            else:
                self.blocks.append((c["metric"], c["rows"]))

    def __bool__(self) -> bool:
        return bool(self.cells or self.blocks)

    def overlaps(self, other: Written) -> bool:
        """同じセルを書き換えているか。"""
        if self.cells & other.cells:
            return True
        for a, b in ((self, other), (other, self)):
            for metric, block in a.blocks:
                keys = [list(k) for m, k in b.cells if m == metric]
                if keys and block.contains_any(keys):
                    return True
        return any(m == n and x.overlaps(y) for m, x in self.blocks for n, y in other.blocks)


class Stats:
    """観察用の数（HTTP サーバーの /stats が出す）。時間は秒で、時刻は time.monotonic の値。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.commits = 0              # 確定した書き込み
        self.batches = 0              # 確定したまとまり（1 回の記録の書き出し）
        self.commit_seconds = 0.0     # 記録の書き出しと確定にかかった時間の合計
        self.commit_seconds_max = 0.0
        self.rejected = 0             # 失敗して取り消した書き込み（Conflict、式の誤りなど）
        self.journal_errors = 0       # 記録の書き出しに失敗したまとまり
        self.catch_ups = 0            # ほかのプロセスの書き込みに追いついた回数
        self.reopen_failures = 0      # 追いつけず、開き直しにも失敗した回数
        self.snapshots = 0
        self.snapshot_failures = 0
        self.snapshot_at: float | None = None  # 最後にスナップショットを置き終えた時刻
        self.snapshot_seq: int | None = None
        self.last_error: str | None = None

    def add(self, **counts) -> None:
        with self._lock:
            for k, v in counts.items():
                setattr(self, k, getattr(self, k) + v)

    def set(self, **values) -> None:
        with self._lock:
            for k, v in values.items():
                setattr(self, k, v)

    def commit(self, n: int, seconds: float) -> None:
        with self._lock:
            self.commits += n
            self.batches += 1
            self.commit_seconds += seconds
            self.commit_seconds_max = max(self.commit_seconds_max, seconds)


def _operation(client_op_id: str, known: dict[str, int], rejected: dict[str, tuple[int, dict]]) -> dict | None:
    """The result of Workspace.operation and Replica.operation, from the lookup of _outcomes."""
    if (seq := known.get(client_op_id)) is not None:
        return {"state": "committed", "seq": seq}
    if (r := rejected.get(client_op_id)) is not None:
        return {"state": "rejected", "status": r[0], "body": r[1]}
    return None


def _follow(req: _Request, first: _Request) -> None:
    """同じまとまりで同じ client_op_id を送った書き込みに、最初の書き込みと同じ結果を返す。"""
    if first.future.exception() is not None:
        req.future.set_exception(first.future.exception())
    else:
        req.future.set_result(first.future.result())


class Workspace:
    """モデルを預かり、版の公開と単一ライターで、同時の読み書きを受け付ける。

    渡したモデルは Workspace のものになる（最初の版として公開する）。以後は直接書き換えず、
    write で書き込み、version で読む。journal を渡すと、確定したトランザクションを記録する。
    """

    def __init__(self, model, journal: Journal | None = None, *, max_batch: int = 64,
                 keep_recent: int = 10_000, max_queue: int = 0, checkpoint_every: int | None = None,
                 checkpoint_interval: float | None = None, standby: bool = False, interval: float = 1.0):
        """standby なら、記録先の書き込みの権利を取れたときだけ書き手になり、取れなければ待機系として追従する
        （モジュールの説明）。interval は待機系が権利を試す間隔と、書き手が権利を確かめる間隔（秒）。"""
        self.journal = journal
        self.max_batch = max_batch
        self.standby = standby
        self.interval = interval
        self._publish(model)
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        # 排他の確認に使う、最近の書き込み (通し番号, 利用者, 書き換えたセル)。これより古い版を
        # 読んだ書き込みは確かめられないので拒否する
        self._recent: collections.deque = collections.deque(maxlen=keep_recent)
        # このライターが最近確定した client_op_id（keep_recent 件まで。記録先にあるものは seq_of で引く）
        self._ops: collections.OrderedDict[str, int] = collections.OrderedDict()
        # The rejections that the writer recorded (client_op_id -> (status, body)), keep_recent at most.
        # Only the writer thread writes it. The journal keeps them too, so a restarted server gives the same answer
        self._rejected: collections.OrderedDict[str, tuple[int, dict]] = collections.OrderedDict()
        self._closed = False
        if (checkpoint_every is not None or checkpoint_interval is not None) and journal is None:
            raise ValueError("スナップショットを取るには記録先（journal）が要る")
        self.checkpoint_every = checkpoint_every
        self.checkpoint_interval = checkpoint_interval
        self._checkpoint_seq = self._version.seq  # 最後にスナップショットを取った（または取り始めた）版
        self._checkpoint_at = time.monotonic()
        self._checkpointing: threading.Thread | None = None
        self.stats = Stats()
        self._degraded: str | None = None  # 記録先に追いつけず、開き直しにも失敗した（古い版を公開している）
        self._role = Role.LEADER
        self._role_lock = threading.RLock()  # 役割の変更と、まとまりの処理を順に並べる
        self._stop = threading.Event()
        self._watch_error: BaseException | None = None  # 見張りのスレッドが最後に失敗した理由（成功したら None）
        self._watcher: threading.Thread | None = None
        if standby:
            if journal is None:
                raise ValueError("待機系にするには記録先（journal）が要る")
            if hasattr(journal, "acquire_wait"):
                journal.acquire_wait = 0  # 権利を失った書き手は、別のプロセスのリースを待たずに待機系に戻る
            self._role = Role.STANDBY
            if journal.take():
                self._promote()
            self._watcher = threading.Thread(target=self._watch, name="nanashi-standby", daemon=True)
            self._watcher.start()
        self._thread = threading.Thread(target=self._run, name="nanashi-writer", daemon=True)
        self._thread.start()

    @classmethod
    def open(cls, journal, engine=None, **kwargs) -> Workspace:
        """記録先 journal（FileJournal など。ディレクトリのパスでもよい）から復元したモデルで Workspace を作る。"""
        if not isinstance(journal, Journal):
            journal = FileJournal(journal)
        return cls(journal.open(engine), journal, **kwargs)

    def _publish(self, model) -> None:
        """model を公開中の版にする（記録はライターがまとめて書くので、モデル自身には記録させない）。"""
        model.journal = None
        model.recalc()
        if self.journal is not None:
            model.seq = self.journal.head
        model._frozen = True
        self._version = model
        self._known_since = model.seq

    # ------------------------------------------------ 読み出し

    @property
    def version(self) -> Model:
        """The published version. It is frozen: an operation on it causes a ValueError."""
        return self._version

    @property
    def seq(self) -> int:
        """公開中の版の通し番号。"""
        return self._version.seq

    @property
    def role(self) -> Role:
        """書き手（LEADER）か待機系（STANDBY）か。standby でなければいつも LEADER。"""
        return self._role

    # ------------------------------------------------ 書き込み

    def submit(self, fn: Callable[[Any], Any], *, user: str | None = None, reason: str | None = None,
               client_op_id: str | None = None, expect: int | None = None,
               timeout: float | None = None) -> Future:
        """書き込みを列に入れる。fn はモデルを受け取って操作する関数で、1 つのトランザクションとして
        適用する。結果は確定した通し番号の Future（失敗すれば例外）。

        expect に読んだ版の通し番号を渡すと、それより後に同じセルを変えた書き込みがあれば Conflict にする
        （比べるのは、この書き込みが実際に書き換えた入力セル）。client_op_id が確定済みなら、
        適用せずに元の通し番号を返す。列が満杯（max_queue）なら timeout の間だけ待ち、Overloaded を投げる。
        待機系なら NotLeader（leader に書き手の番地）。
        """
        if self._closed:
            raise RuntimeError("Workspace は閉じている")
        if self._role is Role.STANDBY:
            raise self._not_leader()
        future: Future = Future()
        try:
            self._queue.put(_Request(fn, user, reason, client_op_id, expect, future), timeout=timeout)
        except queue.Full:
            raise Overloaded(f"書き込みの列が満杯（{self._queue.maxsize} 件）") from None
        return future

    def write(self, fn: Callable[[Any], Any], *, timeout: float | None = None, **kwargs) -> int:
        """submit して、確定するまで待つ。確定した通し番号を返す。timeout を過ぎたら TimeoutError
        （まだ列にいれば取り消し、適用が始まっていれば結果を待たずに返る）。"""
        future = self.submit(fn, timeout=timeout, **kwargs)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    def operation(self, client_op_id: str) -> dict | None:
        """The result of the write with client_op_id: {"state": "committed", "seq"} or
        {"state": "rejected", "status", "body"}. None if the Workspace does not know it."""
        known, rejected = self._outcomes([client_op_id])
        return _operation(client_op_id, known, rejected)

    def checkpoint(self) -> None:
        """公開中の版のスナップショットを記録先に置く（版は変わらないので、どのスレッドからでもよい）。"""
        if self.journal is None:
            raise ValueError("記録先（journal）がない")
        model = self._version
        self._checkpoint_seq, self._checkpoint_at = model.seq, time.monotonic()
        self.journal.save_snapshot(model)
        self.stats.add(snapshots=1)
        self.stats.set(snapshot_at=time.monotonic(), snapshot_seq=model.seq)

    def ready(self) -> list[str]:
        """要求を受けられない理由の列（受けられるなら空）。HTTP サーバーの /ready が使う。"""
        reasons = []
        if self._closed:
            reasons.append("閉じている")
        elif not self._thread.is_alive():
            reasons.append("ライターのスレッドが止まっている")
        elif self._watcher is not None and not self._watcher.is_alive():
            reasons.append("待機系の見張りのスレッドが止まっている")
        if self._watch_error is not None:
            reasons.append(f"待機系の見張りに失敗した: {type(self._watch_error).__name__}: {self._watch_error}")
        if self._degraded is not None:
            reasons.append(self._degraded)
        if self._role is Role.LEADER and self.journal is not None and (err := self.journal.lease()["error"]) is not None:
            reasons.append(f"書き込みの権利を延長できない: {err}")
        return reasons

    def queued(self) -> int:
        return self._queue.qsize()

    def close(self) -> None:
        """見張りを止め、列に入っている書き込みを処理し終えてから、ライターを止める。取りかけのスナップショットも待つ。
        記録先の書き込みの権利（PgJournal のリース）も手放すので、次に開く書き手は期限を待たずに書ける。"""
        if not self._closed:
            self._closed = True
            self._stop.set()
            if self._watcher is not None:
                self._watcher.join()
            self._queue.put(None)
            self._thread.join()
            if self._checkpointing is not None:
                self._checkpointing.join()
            if self.journal is not None:
                self.journal.release()

    def __enter__(self) -> Workspace:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------ ライター

    def _run(self) -> None:
        stop = False
        while not stop:
            first = self._queue.get()
            if first is None:
                return
            batch = [first]
            while len(batch) < self.max_batch:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    stop = True
                    break
                batch.append(nxt)
            try:
                with self._role_lock:  # まとまりの途中で役割が変わらないようにする
                    self._process(batch)
            except Exception as e:  # 想定外の失敗でも、待っている利用者に知らせてから続ける
                log.exception("書き込みのまとまりの処理に失敗した")
                for req in batch:
                    if not req.future.done():
                        req.future.set_exception(e)
            except BaseException as e:  # KeyboardInterrupt などは知らせてから止まる
                for req in batch:
                    if not req.future.done():
                        req.future.set_exception(e)
                raise

    def _process(self, batch: list[_Request]) -> None:
        if self._role is Role.STANDBY:  # 待機系に戻る直前に列に入った書き込み
            err = self._not_leader()
            for req in batch:
                if req.future.set_running_or_notify_cancel():
                    req.future.set_exception(err)
            return
        working = self._version.fork()  # 公開中の版は変えない
        applied: list[tuple[_Request, dict]] = []
        refused: list[tuple[_Request, Exception]] = []
        aliases: list[tuple[_Request, _Request]] = []  # 同じまとまりで同じ client_op_id を送ったもの
        firsts: dict[str, _Request] = {}
        ids = [r.client_op_id for r in batch if r.client_op_id is not None]
        known, rejected = self._outcomes(ids)  # Find the known client_op_ids with one lookup for each batch
        for req in batch:
            if not req.future.set_running_or_notify_cancel():
                continue
            if req.client_op_id is not None:
                if req.client_op_id in known:
                    req.future.set_result(known[req.client_op_id])
                    continue
                if req.client_op_id in rejected:
                    req.future.set_exception(Rejected(*rejected[req.client_op_id]))
                    continue
                if req.client_op_id in firsts:
                    aliases.append((req, firsts[req.client_op_id]))
                    continue
                firsts[req.client_op_id] = req
            pending = [Written(r) for _, r in applied]
            try:
                with working.transaction(user=req.user, reason=req.reason, client_op_id=req.client_op_id,
                                         validate=lambda rec, req=req, pending=pending:
                                         self._check(req, rec, pending)) as txn:
                    req.fn(working)
                applied.append((req, txn.record))
            except Exception as e:
                self.stats.add(rejected=1)
                refused.append((req, e))

        committed = [(req, rec) for req, rec in applied if rec["ops"]]
        if not committed:  # 何も変えていない（すべて失敗したか、操作を呼ばなかった）。版はそのまま
            for req, _ in applied:
                req.future.set_result(self._version.seq)
            self._refuse(refused)
            for req, first in aliases:
                _follow(req, first)
            return
        started = time.monotonic()
        try:
            if self.journal is not None:
                seqs = self.journal.append_many([rec for _, rec in committed])
            else:
                seqs = list(range(working.seq + 1, working.seq + 1 + len(committed)))
        except Exception as e:  # 記録できなければ、まとまり全体を捨てる（公開中の版は変えない）
            self.stats.add(journal_errors=1)
            self.stats.set(last_error=f"{type(e).__name__}: {e}")
            if isinstance(e, Stale) and self.standby:  # 権利を失った。追いつくのは見張りに任せる
                e = self._not_leader()
                self._demote()
            elif isinstance(e, Stale):
                self._reload()  # 別のプロセスが書き込んでいた。知らせる前に、記録先から最新の版を開き直す
            # An earlier request of the batch can cause a refusal. Thus do not record the refusals.
            for req, _ in applied + refused:
                req.future.set_exception(e)
            for req, _ in aliases:
                req.future.set_exception(e)
            return

        self.stats.commit(len(committed), time.monotonic() - started)
        for (req, rec), seq in zip(committed, seqs):
            rec["seq"] = seq
            self._recent.append((seq, req.user, Written(rec)))
            if req.client_op_id is not None:
                self._ops[req.client_op_id] = seq
                if len(self._ops) > self._recent.maxlen:
                    self._ops.popitem(last=False)
        if seqs:
            working.seq = seqs[-1]
        working._frozen = True
        self._version = working  # 公開する

        for req, rec in applied:
            req.future.set_result(rec.get("seq", working.seq))
        self._refuse(refused)
        for req, first in aliases:
            _follow(req, first)
        self._maybe_checkpoint()

    def _refuse(self, refused: list[tuple[_Request, Exception]]) -> None:
        """Give each refused request its error. Record a terminal rejection first, so a resend cannot apply the
        write. Call this only after the batch is committed or has nothing to commit.
        An earlier request of the batch can cause a refusal, so do not record it if the append fails."""
        for req, e in refused:
            if req.client_op_id is not None and (r := rejection_of(e)) is not None:
                self._reject(req.client_op_id, *r)
            req.future.set_exception(e)

    def _outcomes(self, ids: list[str]) -> tuple[dict[str, int], dict[str, tuple[int, dict]]]:
        """The committed client_op_id -> seq and the rejected client_op_id -> (status, body).
        Look in memory first, then look up the other IDs in the journal with one query."""
        known = {i: s for i in ids if (s := self._ops.get(i)) is not None}
        rejected = {i: r for i in ids if (r := self._rejected.get(i)) is not None}
        unknown = [i for i in ids if i not in known and i not in rejected]
        if unknown and self.journal is not None:
            more_known, more_rejected = self.journal.outcomes_of_many(unknown)
            known.update(more_known)
            rejected.update(more_rejected)
        return known, rejected

    def _reject(self, client_op_id: str, status: int, body: dict) -> None:
        """Record that the write with client_op_id was rejected with this HTTP status and body (writer thread only).
        If the same client_op_id comes again, the writer raises Rejected with them instead of applying the write."""
        if self.journal is not None:
            self.journal.record_rejection(client_op_id, status, body)
        self._rejected[client_op_id] = (status, body)
        while len(self._rejected) > self._recent.maxlen:
            self._rejected.popitem(last=False)

    def _reload(self) -> None:
        """記録先の最新の版に追いつく（手元の版が古いと言われたとき）。公開中の版の複製に、ほかのプロセスが
        確定した記録を書き込み、影響範囲だけを計算し直す。追いつけなければ、記録先から開き直す。"""
        try:
            model = self._version.fork()
            if not self.journal.catch_up(model):
                return
        except Exception:
            log.warning("記録先の記録に追いつけなかったので、開き直す", exc_info=True)
            try:
                model = self.journal.open(self._version.engine)
            except Exception as e:
                log.exception("記録先からの開き直しに失敗した")
                self.stats.add(reopen_failures=1)
                self._degraded = f"記録先からの開き直しに失敗した: {type(e).__name__}: {e}"
                return
        self.stats.add(catch_ups=1)
        self._degraded = None
        self._publish(model)
        self._recent.clear()
        self._ops.clear()

    def _check(self, req: _Request, record: dict, pending: list[Written]) -> None:
        """読んだ版（req.expect）より後に、同じセルを変えた書き込みがあれば Conflict。"""
        if req.expect is None:
            return
        mine = Written(record)
        if not mine:
            return
        if req.expect < self._known_since or (self._recent and len(self._recent) == self._recent.maxlen
                                               and req.expect < self._recent[0][0] - 1):
            raise Conflict(f"通し番号 {req.expect} の版は古すぎて、その後の変更を確かめられない")
        for seq, user, cells in self._recent:
            if seq > req.expect and mine.overlaps(cells):
                raise Conflict(f"読んだ版（{req.expect}）の後に、{user} が同じセルを変えた（通し番号 {seq}）",
                               seq, user)
        for cells in pending:  # 同じまとまりで先に適用した書き込み（まだ通し番号がない）
            if mine.overlaps(cells):
                raise Conflict(f"読んだ版（{req.expect}）の後に、同じセルを変えた書き込みがある")

    # ------------------------------------------------ 待機系

    def _not_leader(self) -> NotLeader:
        try:
            leader = self.journal.leader()
        except Exception:  # 記録先が使えなくても、待機系であることは伝える
            leader = None
        return NotLeader(leader)

    def _promote(self) -> None:
        """リースを取ったあとに呼ぶ（_role_lock の中で）。ほかのプロセスの記録に追いついてから書き手になる。
        書き手になれなければリースを手放す（持ったまま待機系でいると、どのプロセスも書き手になれない）。"""
        try:
            self._reload()
            if self._degraded is None:
                self._publish(self._version)  # 版が同じでも、expect を確かめる基準は今の版にする
        except BaseException:
            self.journal.release()
            raise
        if self._degraded is not None:  # 追いつけない版で書くと、ほかの書き手の記録を上書きしてしまう
            self.journal.release()
            return
        self._recent.clear()
        self._ops.clear()
        self._role = Role.LEADER
        log.info("書き手になった（版 %d）", self.seq)

    def _demote(self) -> None:
        """書き込みの権利を失ったときに呼ぶ（_role_lock の中で）。待機系に戻り、見張りが追いつき直す。"""
        if self._role is Role.LEADER:
            self._role = Role.STANDBY
            log.warning("書き込みの権利を失ったので、待機系に戻る")

    def _watch(self) -> None:
        """待機系なら、記録が増えるたびに追いつき、間隔ごとに権利を取れるか試す。書き手なら、間隔ごとに
        権利を持ち続けているか確かめ、失っていれば（延長できなかった）待機系に戻る。"""
        while not self._stop.is_set():
            try:
                if self._role is Role.LEADER:
                    self._stop.wait(self.interval)
                    with self._role_lock:
                        if self._role is Role.LEADER and not self.journal.lease()["held"]:
                            self._demote()
                else:
                    self.journal.wait(self.interval)
                    with self._role_lock:
                        if not self._stop.is_set() and self._role is Role.STANDBY:
                            self._reload()
                            if self._degraded is None and self.journal.take():
                                self._promote()
                self._watch_error = None
            except Exception as e:  # 記録先が一時的に使えない。次の間隔で改めて試す
                self._watch_error = e
                self.stats.set(last_error=f"{type(e).__name__}: {e}")
                log.warning("待機系の見張りに失敗した（次の間隔で改めて試す）", exc_info=True)
                self._stop.wait(self.interval)

    # ------------------------------------------------ スナップショット

    def _maybe_checkpoint(self) -> None:
        """決めた間隔を過ぎていれば、公開したばかりの版のスナップショットを別のスレッドで取り始める
        （取っている最中なら、次の公開で改めて確かめる）。"""
        if self.journal is None or (self.checkpoint_every is None and self.checkpoint_interval is None):
            return
        if self._checkpointing is not None and self._checkpointing.is_alive():
            return
        due = (self.checkpoint_every is not None and self._version.seq - self._checkpoint_seq >= self.checkpoint_every) \
            or (self.checkpoint_interval is not None and time.monotonic() - self._checkpoint_at >= self.checkpoint_interval)
        if not due:
            return
        model = self._version
        self._checkpoint_seq, self._checkpoint_at = model.seq, time.monotonic()

        def run():
            try:
                self.journal.save_snapshot(model)
                self.stats.add(snapshots=1)
                self.stats.set(snapshot_at=time.monotonic(), snapshot_seq=model.seq)
            except Exception:
                self.stats.add(snapshot_failures=1)
                log.exception("スナップショットの保存に失敗した（次の間隔で取り直す）")
        thread = threading.Thread(target=run, name="nanashi-checkpoint", daemon=True)
        thread.start()
        self._checkpointing = thread


class Replica:
    """記録先に追従する読み出し専用の版。書き込むプロセス（Workspace）とは別のプロセスで、読み手を増やすのに使う。

        replica = Replica(PgJournal(dsn, "plan", "s3://nanashi/plans", heartbeat=False), RustEngine())
        replica.version.get("Price", Product="A")

    別のスレッドで記録先を見張り（PgJournal は確定の通知、FileJournal はファイルの長さ）、ほかのプロセスが
    確定した記録を、公開中の版の複製に入力の変更として書き込んで影響範囲だけを計算し直し、新しい版として
    公開する。読み出しはいつでも公開中の版を見る。書き込めない（書くのは Workspace を持つ 1 つのプロセス）。
    """

    def __init__(self, journal: Journal, engine=None, *, interval: float = 1.0):
        self.journal = journal
        self.interval = interval
        self._lock = threading.Lock()  # 追いつく処理を順に並べる（見張りのスレッドと refresh）
        self._publish(journal.open(engine))
        self._stop = threading.Event()
        self.error: BaseException | None = None  # 最後に追いつけなかった理由（追いつけたら None）
        self.stats = Stats()
        self._thread = threading.Thread(target=self._run, name="nanashi-replica", daemon=True)
        self._thread.start()

    def _publish(self, model) -> None:
        model.journal = None
        model.recalc()
        model._frozen = True
        self._version = model

    @property
    def version(self) -> Model:
        return self._version

    @property
    def seq(self) -> int:
        return self._version.seq

    def refresh(self) -> int:
        """今すぐ記録先に追いつく。公開中の版の通し番号を返す。"""
        with self._lock:
            model = self._version.fork()
            if self.journal.catch_up(model):
                self._publish(model)
                self.stats.add(catch_ups=1)
            self.error = None
            return self.seq

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.journal.wait(self.interval)
                if not self._stop.is_set():
                    self.refresh()
            except Exception as e:  # 記録先が一時的に使えない。次の間隔で改めて追いつく
                self.error = e
                self.stats.set(last_error=f"{type(e).__name__}: {e}")
                log.warning("記録先に追いつけなかった（次の間隔で改めて試す）", exc_info=True)
                self._stop.wait(self.interval)

    def ready(self) -> list[str]:
        """要求を受けられない理由の列（受けられるなら空）。"""
        reasons = []
        if not self._thread.is_alive():
            reasons.append("追従のスレッドが止まっている")
        if self.error is not None:
            reasons.append(f"記録先に追いつけない: {type(self.error).__name__}: {self.error}")
        return reasons

    def lag(self) -> int:
        """記録先の最後の記録から、公開中の版がいくつ遅れているか（最後に記録先を読んだ時点で）。"""
        return max(0, self.journal.head - self.seq)

    def operation(self, client_op_id: str) -> dict | None:
        """The result of the write with client_op_id, from the journal (the same as Workspace.operation)."""
        return _operation(client_op_id, *self.journal.outcomes_of_many([client_op_id]))

    def close(self) -> None:
        self._stop.set()
        self._thread.join()

    def __enter__(self) -> Replica:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
