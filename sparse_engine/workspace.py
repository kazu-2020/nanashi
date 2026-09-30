"""版の公開と単一ライター。複数の利用者が同時に読み書きするための入口。

Workspace はモデルを 1 つ預かり、確定した状態を「版」として公開する。版は作ったら変えない。
読み出しはいつでも公開中の版（version）を見るので、書き込みの途中の値や、取り消された変更は見えない。

書き込みは 1 本のスレッド（ライター）が列から順に取り出して処理する。列に溜まっている書き込みを
1 つのまとまりにして、公開中の版の複製に 1 件ずつトランザクションとして適用し、記録は 1 回の
書き出しでまとめて確定する（グループコミット）。確定したら、複製を新しい版として公開する。

    ws = Workspace(model, FileJournal("plan/"))
    seq = ws.write(lambda m: m.set_cell("Price", 12, Product="A"), user="alice")
    ws.version.get("Price", Product="A")

1 件の書き込みが失敗したら、その 1 件だけを取り消して、同じまとまりのほかの書き込みは確定する。
記録の書き出しに失敗したら、まとまり全体を捨て、公開中の版は変えない。

格納データの本体は版どうしで共有するので、版を作る費用は差分の分だけで済む。古い版は、
読んでいる人がいなくなれば捨てられる。
"""
from __future__ import annotations

import collections
import dataclasses
import queue
import threading
from concurrent.futures import Future
from typing import Any, Callable

from .journal import FileJournal


class Conflict(Exception):
    """読んだ版より後に、同じセルを他の書き込みが変えていた。seq と user はその書き込み。"""

    def __init__(self, message: str, seq: int | None = None, user: str | None = None):
        super().__init__(message)
        self.seq = seq
        self.user = user


@dataclasses.dataclass
class _Request:
    fn: Callable[[Any], Any]
    user: str | None
    reason: str | None
    client_op_id: str | None
    expect: int | None
    future: Future


def written_cells(record: dict) -> set[tuple]:
    """記録で書き換えた入力セル（Metric の ID と、座標のメンバーの ID）。"""
    return {(c["metric"], tuple(ids)) for c in record["changes"].get("cells", []) for ids, _, _ in c["rows"]}


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

    def __init__(self, model, journal: FileJournal | None = None, *, max_batch: int = 64,
                 keep_recent: int = 10_000):
        model.journal = None  # 記録はライターがまとめて書く
        model.recalc()
        if journal is not None:
            model.seq = journal.head
        model._frozen = True
        self._version = model
        self.journal = journal
        self.max_batch = max_batch
        self._queue: queue.Queue = queue.Queue()
        # 排他の確認に使う、最近の書き込み (通し番号, 利用者, 書き換えたセル)。これより古い版を
        # 読んだ書き込みは確かめられないので拒否する
        self._recent: collections.deque = collections.deque(maxlen=keep_recent)
        self._known_since = model.seq
        self._ops: dict[str, int] = dict(journal._by_client_op) if journal is not None else {}
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="nanashi-writer", daemon=True)
        self._thread.start()

    @classmethod
    def open(cls, path, engine=None, **kwargs) -> Workspace:
        """記録のディレクトリ path から復元したモデルで Workspace を作る。"""
        journal = FileJournal(path)
        return cls(journal.open(engine), journal, **kwargs)

    # ------------------------------------------------ 読み出し

    @property
    def version(self):
        """公開中の版。読み出しにだけ使う（書き換えると ValueError）。"""
        return self._version

    @property
    def seq(self) -> int:
        """公開中の版の通し番号。"""
        return self._version.seq

    # ------------------------------------------------ 書き込み

    def submit(self, fn: Callable[[Any], Any], *, user: str | None = None, reason: str | None = None,
               client_op_id: str | None = None, expect: int | None = None) -> Future:
        """書き込みを列に入れる。fn はモデルを受け取って操作する関数で、1 つのトランザクションとして
        適用する。結果は確定した通し番号の Future（失敗すれば例外）。

        expect に読んだ版の通し番号を渡すと、それより後に同じセルを変えた書き込みがあれば Conflict にする
        （比べるのは、この書き込みが実際に書き換えた入力セル）。client_op_id が確定済みなら、
        適用せずに元の通し番号を返す。
        """
        if self._closed:
            raise RuntimeError("Workspace は閉じている")
        future: Future = Future()
        self._queue.put(_Request(fn, user, reason, client_op_id, expect, future))
        return future

    def write(self, fn: Callable[[Any], Any], **kwargs) -> int:
        """submit して、確定するまで待つ。確定した通し番号を返す。"""
        return self.submit(fn, **kwargs).result()

    def checkpoint(self) -> None:
        """公開中の版のスナップショットを記録先に置く（版は変わらないので、どのスレッドからでもよい）。"""
        if self.journal is None:
            raise ValueError("記録先（journal）がない")
        self.journal.save_snapshot(self._version)

    def close(self) -> None:
        """列に入っている書き込みを処理し終えてから、ライターを止める。"""
        if not self._closed:
            self._closed = True
            self._queue.put(None)
            self._thread.join()

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
                self._process(batch)
            except BaseException as e:  # 想定外の失敗でも、待っている利用者に知らせてから続ける
                for req in batch:
                    if not req.future.done():
                        req.future.set_exception(e)

    def _process(self, batch: list[_Request]) -> None:
        working = self._version.fork()  # 公開中の版は変えない
        applied: list[tuple[_Request, dict]] = []
        aliases: list[tuple[_Request, _Request]] = []  # 同じまとまりで同じ client_op_id を送ったもの
        firsts: dict[str, _Request] = {}
        for req in batch:
            if not req.future.set_running_or_notify_cancel():
                continue
            if req.client_op_id is not None:
                if req.client_op_id in self._ops:
                    req.future.set_result(self._ops[req.client_op_id])
                    continue
                if req.client_op_id in firsts:
                    aliases.append((req, firsts[req.client_op_id]))
                    continue
                firsts[req.client_op_id] = req
            pending = [written_cells(r) for _, r in applied]
            try:
                with working.transaction(user=req.user, reason=req.reason, client_op_id=req.client_op_id,
                                         validate=lambda rec, req=req, pending=pending:
                                         self._check(req, rec, pending)) as txn:
                    req.fn(working)
                applied.append((req, txn.record))
            except BaseException as e:
                req.future.set_exception(e)

        committed = [(req, rec) for req, rec in applied if rec["ops"]]
        if not committed:  # 何も変えていない（すべて失敗したか、操作を呼ばなかった）。版はそのまま
            for req, _ in applied:
                req.future.set_result(self._version.seq)
            for req, first in aliases:
                _follow(req, first)
            return
        try:
            if self.journal is not None:
                seqs = self.journal.append_many([rec for _, rec in committed])
            else:
                seqs = list(range(working.seq + 1, working.seq + 1 + len(committed)))
        except BaseException as e:  # 記録できなければ、まとまり全体を捨てる（公開中の版は変えない）
            for req, _ in applied:
                req.future.set_exception(e)
            for req, _ in aliases:
                req.future.set_exception(e)
            return

        for (req, rec), seq in zip(committed, seqs):
            rec["seq"] = seq
            self._recent.append((seq, req.user, written_cells(rec)))
            if req.client_op_id is not None:
                self._ops[req.client_op_id] = seq
        if seqs:
            working.seq = seqs[-1]
        working._frozen = True
        self._version = working  # 公開する

        for req, rec in applied:
            req.future.set_result(rec.get("seq", working.seq))
        for req, first in aliases:
            _follow(req, first)

    def _check(self, req: _Request, record: dict, pending: list[set]) -> None:
        """読んだ版（req.expect）より後に、同じセルを変えた書き込みがあれば Conflict。"""
        if req.expect is None:
            return
        mine = written_cells(record)
        if not mine:
            return
        if req.expect < self._known_since or (self._recent and len(self._recent) == self._recent.maxlen
                                               and req.expect < self._recent[0][0] - 1):
            raise Conflict(f"通し番号 {req.expect} の版は古すぎて、その後の変更を確かめられない")
        for seq, user, cells in self._recent:
            if seq > req.expect and mine & cells:
                raise Conflict(f"読んだ版（{req.expect}）の後に、{user} が同じセルを変えた（通し番号 {seq}）",
                               seq, user)
        for cells in pending:  # 同じまとまりで先に適用した書き込み（まだ通し番号がない）
            if mine & cells:
                raise Conflict(f"読んだ版（{req.expect}）の後に、同じセルを変えた書き込みがある")
