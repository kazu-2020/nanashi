"""PostgreSQL に記録する記録先。

表は次の 5 つ（接頭辞 nanashi_）。1 つのデータベースに複数のモデルを置ける。

    model        モデルごとの、最後の記録の通し番号（head_seq）と、書き込むプロセスのリース
                 （世代番号 writer_epoch、持ち主、期限、持ち主が公開している番地 lease_endpoint）
    operation    1 トランザクション 1 行。意図と、セル以外の結果を JSONB で持つ
    cell_change  書き換えた入力セルごとに 1 行（Metric の ID、座標のメンバーの ID の配列、変更前後の値）。
                 セルの履歴を索引で引ける
    snapshot     スナップショットの置き場所とハッシュ（ファイルはオブジェクトストレージに置き、
                 各ファイル、manifest.json の順に置き終えてから登録する。ハッシュは読むときに確かめ、
                 合わなければ 1 つ前のものを使う）
    rejection    1 row for each rejected write (client_op_id, the HTTP status and body, and head_seq at that
                 time). A resent write gets the same answer. prune forgets the rows older than op_window

ファイルの置き場所（objects.py）は S3 互換のオブジェクトストレージ（s3://…）か、ローカルのディレクトリ。
表には置き場所の中の相対的なキーだけを保存するので、置き場所を移しても（ディレクトリから S3 へなど）読める。

大量のセルを書き換えた記録（bulk_cells を超えるもの）は、変更前後の値を Metric ごとに Parquet の
ファイルにしてオブジェクトストレージに置いてから確定し、operation にはその置き場所とハッシュだけを
持つ。Parquet の列は、座標のメンバーの ID（d<軸の ID>、Int64）と、変更前 old と変更後 new（空は null）。
cell_change への書き込み（と索引の更新）は確定の後で行う（index_pending）。行を 1 件ずつ入れる費用が
大きく、確定の経路に入れると大量の書き込みの確定が十数倍遅くなるため。セルの履歴を引くときは、
先に未反映の分を反映する。

書き込むプロセスは 1 つに限る。最初に追記するときにリースを取り、世代番号を 1 つ進める。確定は
「通し番号が読んだとおりで、世代番号が自分のもの」のときだけ通る 1 回のトランザクションで行う。
リースが切れて別のプロセスが書き込みを始めていれば、古いプロセスの確定は拒否される（締め出し）。
読み込んだあとに別のプロセスが書き込んでいた場合も、手元のモデルが古いので拒否する（Stale）。

A different thread extends the lease while there are no writes (heartbeat). If a different process has
a lease that has not expired, acquire waits until it expires and then gets it (the lease of a stopped process).
take only tries to get the lease without a wait (a standby uses it). The lease also contains the address
that the owner publishes (endpoint). leader returns it. wait reads nanashi_model at each poll_interval.
It returns when head_seq changes, or when the lease is released or expires. Thus a standby can try to
get the lease at its next poll.

The module does not use session state (LISTEN/NOTIFY, session advisory locks, prepared statements).
Thus it works through PgBouncer in transaction pooling mode.
"""
from __future__ import annotations

import datetime
import logging
import os
import socket
import threading
import time
import uuid
from typing import Iterator, Mapping

import psycopg
from psycopg.types.json import Jsonb

from .engine import native
from .journal import (Fenced, Journal, Snapshot, _key_ids, _shown, as_block, cell_count, put_snapshot, read_cell_files,
                      write_cell_files)
from .objects import open_objects

log = logging.getLogger(__name__)

# スキーマの版ごとの変更（migrate が順に流す）。版は nanashi_schema に持つ。接続のたびには流さない
# （DDL は表のロックを取るので、書き込み中の別のプロセスと競り合う）
MIGRATIONS: list[tuple[int, str]] = [
    (1, """
create table nanashi_model (
    model_id       text primary key,
    head_seq       bigint not null default 0,
    writer_epoch   bigint not null default 0,
    lease_holder   text,
    lease_expires  timestamptz,
    lease_endpoint text
);
create table nanashi_operation (
    model_id     text not null,
    seq          bigint not null,
    at           timestamptz not null,
    user_name    text,
    reason       text,
    client_op_id text,
    record       jsonb not null,
    cells_uri    text,
    indexed      boolean not null default true,
    primary key (model_id, seq)
);
create index nanashi_operation_unindexed on nanashi_operation (model_id, seq) where not indexed;
create unique index nanashi_operation_client_op
    on nanashi_operation (model_id, client_op_id) where client_op_id is not null;
create table nanashi_cell_change (
    model_id  text not null,
    seq       bigint not null,
    metric_id bigint not null,
    coords    bigint[] not null,
    old_value double precision,
    new_value double precision
);
create index nanashi_cell_change_by_seq on nanashi_cell_change (model_id, seq);
create unique index nanashi_cell_change_by_cell on nanashi_cell_change (model_id, metric_id, coords, seq);
create table nanashi_snapshot (
    model_id text not null,
    seq      bigint not null,
    uri      text not null,
    meta     jsonb not null,
    primary key (model_id, seq)
);
"""),
    (2, """
create table nanashi_rejection (
    model_id     text not null,
    client_op_id text not null,
    head_seq     bigint not null,
    at           timestamptz not null default now(),
    status       integer not null,
    body         jsonb not null,
    primary key (model_id, client_op_id)
);
"""),
    (3, """
truncate nanashi_cell_change;
alter table nanashi_cell_change
    alter column metric_id type text,
    alter column coords type text[] using coords::text[],
    add column old_member text,
    add column new_member text;
"""),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]


class SchemaError(Exception):
    """データベースのスキーマの版が、このプログラムの版と合わない（migrate を流す）。"""


def schema_version(conn) -> int:
    """データベースのスキーマの版。版の表がなければ 0。"""
    if conn.execute("select to_regclass('nanashi_schema')").fetchone()[0] is None:
        return 0
    row = conn.execute("select version from nanashi_schema").fetchone()
    return 0 if row is None else row[0]


def migrate(dsn: str) -> tuple[int, int]:
    """スキーマを最新の版にする（(前の版, 今の版) を返す）。複数のプロセスが同時に流しても 1 回だけ流す。
    書き込み中のプロセスがあれば、表のロックを待つ。"""
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as conn, conn.transaction():
        conn.execute("select pg_advisory_xact_lock(hashtextextended('nanashi_schema', 0))")
        conn.execute("create table if not exists nanashi_schema (version integer not null)")
        before = schema_version(conn)
        for version, sql in MIGRATIONS:
            if version > before:
                conn.execute(sql)
        conn.execute("delete from nanashi_schema")
        conn.execute("insert into nanashi_schema (version) values (%s)", (SCHEMA_VERSION,))
    return before, SCHEMA_VERSION


# 確定の後の反映でこの行数より多く入れたら、セルの履歴の表の統計を取り直す
ANALYZE_ROWS = 100_000


class PgJournal(Journal):
    def __init__(self, dsn: str, model_id: str, objects, *, lease_ttl: float = 30.0,
                 holder: str | None = None, bulk_cells: int = 10_000, heartbeat: bool = True,
                 acquire_wait: float | None = None, endpoint: str | None = None, poll_interval: float = 0.2):
        """objects is the location of the snapshot files and the large change files (s3://<bucket>/<prefix>
        or a directory, see objects.py). The files go below <model_id>/ in it.
        lease_ttl is the time in seconds until the lease expires. If heartbeat is true, a thread extends
        the lease at each 1/3 of lease_ttl while this process has the lease.
        acquire_wait is the time to wait until the lease of a different process expires (default lease_ttl).
        holder is the name of the lease owner (default: the host name, the process ID and a random value).
        endpoint is the address that this process publishes (the URL of its HTTP server). It goes into the
        lease with the holder, and a standby or the router uses it to find the writer.
        poll_interval is the time in seconds between 2 reads of nanashi_model in wait."""
        self.dsn = dsn
        self.model_id = model_id
        self.endpoint = endpoint
        self.objects = open_objects(objects)
        self.bulk_cells = bulk_cells
        # The writer, heartbeat, watcher and readers share one connection.
        self._lock = threading.RLock()
        self._index_conn = None
        self._index_lock = threading.Lock()
        self.poll_interval = poll_interval
        self.lease_ttl = lease_ttl
        self.acquire_wait = lease_ttl if acquire_wait is None else acquire_wait
        self.holder = holder or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.epoch: int | None = None  # 取ったリースの世代番号（まだ取っていなければ None）
        # True if the heartbeat found that another process took the lease. The next append_many raises
        # Fenced one time and does not take the lease again.
        self.lost = False
        self.lease_until: float | None = None  # リースの期限（time.monotonic の値。延長できた時点から数える）
        self.lease_error: BaseException | None = None  # 最後に延長できなかった理由（延長できたら None）
        # Put a group of statements in transaction(). prepare_threshold=None: in transaction pooling mode,
        # PgBouncer can send a statement to a server connection that does not have the prepared statement.
        self._conn = psycopg.connect(dsn, autocommit=True, prepare_threshold=None)
        version = schema_version(self.conn)
        if version != SCHEMA_VERSION:
            self._conn.close()
            raise SchemaError(f"データベースのスキーマの版が {version} で、このプログラムは {SCHEMA_VERSION} を使う。"
                              f"python -m sparse_engine.pg_journal migrate <DSN> で最新にする")
        self.conn.execute("insert into nanashi_model (model_id) values (%s) on conflict do nothing", (model_id,))
        # Read the lease state now, so that the first wait also sees a release or an expiry
        self.head, self._lease_live_at_last_poll = self._model_state()
        self._stop = threading.Event()
        self._heartbeat: threading.Thread | None = None
        if heartbeat:
            self._heartbeat = threading.Thread(target=self._beat, name="nanashi-lease", daemon=True)
            self._heartbeat.start()

    @property
    def conn(self):
        """The connection. If the server closed it, connect again."""
        if self._conn.closed:
            self._conn = psycopg.connect(self.dsn, autocommit=True, prepare_threshold=None)
        return self._conn

    def close(self) -> None:
        self._stop.set()
        if self._heartbeat is not None:
            self._heartbeat.join()
        try:
            self.release()
        finally:
            self._conn.close()
            if self._index_conn is not None:
                self._index_conn.close()

    def release(self) -> None:
        """Release the lease if this process has it. A standby in wait sees the release at its next poll,
        so it does not wait for lease_ttl."""
        with self._lock:
            epoch, self.epoch = self.epoch, None
            if epoch is None or self._conn.closed:
                return
            self._conn.execute("update nanashi_model set lease_expires = now()"
                               " where model_id = %s and writer_epoch = %s and lease_holder = %s",
                               (self.model_id, epoch, self.holder))

    def open(self, engine=None):
        """記録先の最新の状態を開く（手元の通し番号が古くても、表の通し番号から開き直す）。"""
        self.head = self._db_head()
        return super().open(engine)

    def refresh(self) -> int:
        self.head = self._db_head()
        return self.head

    def wait(self, timeout: float) -> None:
        """Read nanashi_model every poll_interval seconds. Return when head_seq is not self.head (more
        entries), when the lease state changed since the last poll (a release or an expiry), or after
        timeout. It polls instead of LISTEN (see the module docstring)."""
        deadline = time.monotonic() + timeout
        while True:
            state = self._model_state()
            if state is None:
                return  # The model row was deleted (drop)
            head, live = state
            # Keep the state between calls, so that a change between 2 calls also wakes the caller
            seen, self._lease_live_at_last_poll = self._lease_live_at_last_poll, live
            if head != self.head or seen != live:
                return
            left = deadline - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(self.poll_interval, left))

    def _model_state(self) -> tuple[int, bool] | None:
        """(head_seq, the lease is live) from nanashi_model, or None if the model row is missing."""
        with self._lock:
            return self.conn.execute(
                "select head_seq, coalesce(lease_expires > now(), false) from nanashi_model where model_id = %s",
                (self.model_id,)).fetchone()

    def _db_head(self) -> int:
        with self._lock:
            return self.conn.execute("select head_seq from nanashi_model where model_id = %s",
                                     (self.model_id,)).fetchone()[0]

    # ------------------------------------------------ リース

    def acquire(self, wait: float | None = None) -> int:
        """書き込むためのリースを取り、世代番号を返す。別のプロセスが期限内のリースを持っていれば、
        期限が切れるまで wait 秒（既定は acquire_wait）まで待ってから取り、それでも取れなければ Fenced。
        手元のモデルを読み込んだあとに別のプロセスが書き込んでいたら、手元が古いのですぐ Fenced。"""
        wait = self.acquire_wait if wait is None else wait
        deadline = time.monotonic() + wait
        while True:
            started = time.monotonic()
            with self._lock, self.conn.transaction():
                row = self._take_row()
                if row is None:
                    other = self.conn.execute(
                        "select lease_holder, head_seq, extract(epoch from lease_expires - now())"
                        " from nanashi_model where model_id = %s", (self.model_id,)).fetchone()
            if row is not None:
                break
            holder, head, left = other
            if head != self.head:
                raise Fenced(f"{self.model_id}: 読み込んだあとに別のプロセスが書き込んだ（{self.head} → {head}）。開き直す")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Fenced(f"{self.model_id}: 別のプロセス（{holder}）が書き込み中（リースの期限内）")
            time.sleep(min(remaining, max(0.05, min(float(left or 0) + 0.05, 1.0))))
        epoch, head = row
        self.lease_until = started + self.lease_ttl
        if head != self.head:
            raise Fenced(f"{self.model_id}: 読み込んだあとに別のプロセスが書き込んだ（{self.head} → {head}）。開き直す")
        self.epoch, self.lost = epoch, False
        return epoch

    def _take_row(self):
        """リースが空いていれば（誰も持っていない、自分が持っている、期限が切れている）取って世代番号を
        1 つ進め、(世代番号, 最後の記録の通し番号) を返す。取れなければ None。"""
        return self.conn.execute(
            "update nanashi_model set writer_epoch = writer_epoch + 1, lease_holder = %s, lease_endpoint = %s,"
            " lease_expires = now() + make_interval(secs => %s)"
            " where model_id = %s and (lease_holder is null or lease_holder = %s or lease_expires < now())"
            " returning writer_epoch, head_seq",
            (self.holder, self.endpoint, self.lease_ttl, self.model_id, self.holder)).fetchone()

    def take(self) -> bool:
        """今すぐリースを取れるなら取る（待たない）。別のプロセスが期限内のリースを持っていれば False。
        手元の通し番号は確かめない（呼ぶ側はリースを取ってから記録に追いつく。持っている間はほかの
        プロセスは確定できない）。"""
        with self._lock:
            started = time.monotonic()
            row = self._take_row()
            if row is None:
                return False
            self.epoch, self.lease_until, self.lease_error = row[0], started + self.lease_ttl, None
            self.lost = False
            return True

    def leader(self) -> str | None:
        """期限内のリースを持っているプロセスが公開している番地（なければ None）。"""
        with self._lock:
            row = self.conn.execute("select lease_endpoint from nanashi_model"
                                    " where model_id = %s and lease_expires > now()", (self.model_id,)).fetchone()
        return None if row is None else row[0]

    def _beat(self) -> None:
        """While this process has the lease, extend it every 1/3 of the lease time."""
        while not self._stop.wait(self.lease_ttl / 3):
            self._extend()

    def _extend(self) -> None:
        """Extend the lease one time. If another process took the lease, record the loss."""
        if self.epoch is None:
            return
        try:
            with self._lock:
                if self.epoch is None:
                    return
                started = time.monotonic()
                cur = self.conn.execute(
                    "update nanashi_model set lease_expires = now() + make_interval(secs => %s)"
                    " where model_id = %s and writer_epoch = %s and lease_holder = %s",
                    (self.lease_ttl, self.model_id, self.epoch, self.holder))
                if cur.rowcount != 1:
                    self.epoch, self.lease_until, self.lost = None, None, True
                    log.warning("%s: リースを失った（別のプロセスが書き込みを始めた）", self.model_id)
                else:
                    self.lease_until = started + self.lease_ttl
                self.lease_error = None
        except Exception as e:  # A temporary connection failure. If the lease expires, the next commit is fenced
            # Log first, so that the log is available when lease() shows the cause
            log.warning("%s: リースを延長できなかった", self.model_id, exc_info=True)
            self.lease_error = e

    def lease(self) -> dict:
        """The state of the write right: held (the lease is taken and not expired), the seconds left, and the
        last reason the heartbeat could not extend it."""
        left = None if self.lease_until is None else self.lease_until - time.monotonic()
        return {"held": self.epoch is not None and left is not None and left > 0, "expires_in": left,
                "error": None if self.lease_error is None else repr(self.lease_error)}

    # ------------------------------------------------ 記録

    def append_many(self, records: list[dict]) -> list[int]:
        if not records:
            return []
        with self._lock:
            if self.lost:
                # A batch can start before the loss and commit after it. Do not take the expired lease
                # again for it: the caller must know of the loss (a standby Workspace demotes)
                self.lost = False
                raise Fenced(f"{self.model_id}: リースを失った（別のプロセスが取った）")
            if self.epoch is None:
                self.acquire()
            seqs = list(range(self.head + 1, self.head + 1 + len(records)))
            # 大量のセルは、確定の前にファイルを置いておく（確定に失敗したら参照されないファイルが残るだけ）
            blobs = [self._write_blob(r) if cell_count(r) > self.bulk_cells else None for r in records]
            started = time.monotonic()
            with self.conn.transaction():
                cur = self.conn.execute(
                    "update nanashi_model set head_seq = %s, lease_expires = now() + make_interval(secs => %s)"
                    " where model_id = %s and head_seq = %s and writer_epoch = %s and lease_holder = %s",
                    (seqs[-1], self.lease_ttl, self.model_id, self.head, self.epoch, self.holder))
                if cur.rowcount != 1:
                    self.epoch, self.lease_until = None, None
                    raise Fenced(f"{self.model_id}: リースを失ったか、別のプロセスが先に書き込んだ")
                with cur.copy("copy nanashi_operation (model_id, seq, at, user_name, reason, client_op_id, record,"
                              " cells_uri, indexed) from stdin") as copy:
                    for rec, seq, blob in zip(records, seqs, blobs):
                        changes = {k: v for k, v in rec["changes"].items() if k != "cells"}
                        stored = {**rec, "seq": seq, "changes": changes}
                        if blob is not None:
                            stored["cells_blob"] = blob
                        copy.write_row((self.model_id, seq, rec["at"], rec["user"], rec["reason"],
                                        rec["client_op_id"], Jsonb(stored),
                                        None if blob is None else blob["prefix"], blob is None))
                with cur.copy("copy nanashi_cell_change (model_id, seq, metric_id, coords, old_value, new_value,"
                              " old_member, new_member) from stdin") as copy:
                    for rec, seq, blob in zip(records, seqs, blobs):
                        if blob is None:
                            _copy_cells(copy, self.model_id, seq, rec["changes"].get("cells", []))
            self.head, self.lease_until = seqs[-1], started + self.lease_ttl
            return seqs

    # ------------------------------------------------ 大量のセル

    def _write_blob(self, record: dict) -> dict:
        """Write the cell changes of the record as Parquet files. Return the prefix, the files, and the cell count."""
        prefix = f"{self.model_id}/cells/{uuid.uuid4().hex}"
        return {"prefix": prefix, **write_cell_files(self.objects, prefix, record)}

    def _read_blob(self, blob: dict) -> list[dict]:
        """_write_blob で置いたセルの変更を読む（Metric ごとの変更の塊）。"""
        return read_cell_files(blob["files"], self.objects.get)

    def index_pending(self) -> int:
        """Write the large cell changes that the commit left for later to cell_change. Use a separate
        connection, so that writes do not stop. Return the number of journal entries that it wrote.
        If many processes call it at the same time, each entry is written 1 time."""
        with self._index_lock:
            if self._index_conn is None or self._index_conn.closed:
                self._index_conn = psycopg.connect(self.dsn, autocommit=True, prepare_threshold=None)
            return self._index_entries(self._index_conn)

    def _index_entries(self, conn) -> int:
        pending = conn.execute("select seq, record->'cells_blob' from nanashi_operation"
                               " where model_id = %s and not indexed order by seq", (self.model_id,)).fetchall()
        loaded, done = 0, 0
        core = native() if pending else None
        for seq, blob in pending:
            with conn.transaction():
                claimed = conn.execute("update nanashi_operation set indexed = true"
                                       " where model_id = %s and seq = %s and not indexed",
                                       (self.model_id, seq)).rowcount
                if not claimed:
                    continue  # ほかのプロセスが先に反映した
                cells = self._read_blob(blob)
                with conn.cursor().copy("copy nanashi_cell_change (model_id, seq, metric_id, coords,"
                                        " old_value, new_value, old_member, new_member) from stdin") as copy:
                    for c in cells:  # COPY のテキストは Rust で作る（行ごとに Python を通さない）
                        copy.write(as_block(core, c["rows"]).copy_text(self.model_id, seq, c["metric"]))
            loaded += blob["cells"]
            done += 1
        if loaded > ANALYZE_ROWS:
            # 大量に入れた直後は表の統計が古く、セルの索引を使わない実行計画になりうる
            # （300 万行で、1 つのセルの履歴に 250 ms かかった。統計を取り直すと 0.2 ms）
            conn.execute("analyze nanashi_cell_change")
        return done

    def seq_of(self, client_op_id: str) -> int | None:
        with self._lock:
            row = self.conn.execute("select seq from nanashi_operation where model_id = %s and client_op_id = %s",
                                    (self.model_id, client_op_id)).fetchone()
        return None if row is None else row[0]

    def record_rejection(self, client_op_id: str, status: int, body: dict) -> None:
        with self._lock:
            self.conn.execute("insert into nanashi_rejection (model_id, client_op_id, head_seq, status, body)"
                              " values (%s, %s, %s, %s, %s) on conflict do nothing",
                              (self.model_id, client_op_id, self.head, status, Jsonb(body)))

    def outcomes_of_many(self, client_op_ids: list[str]) -> tuple[dict[str, int], dict[str, tuple[int, dict]]]:
        if not client_op_ids:
            return {}, {}
        ids = list(client_op_ids)
        with self._lock:
            rows = self.conn.execute("select client_op_id, seq, null::integer, null::jsonb from nanashi_operation"
                                     " where model_id = %s and client_op_id = any(%s)"
                                     " union all select client_op_id, null, status, body from nanashi_rejection"
                                     " where model_id = %s and client_op_id = any(%s)",
                                     (self.model_id, ids, self.model_id, ids)).fetchall()
        return ({i: seq for i, seq, _, _ in rows if seq is not None},
                {i: (status, body) for i, seq, status, body in rows if seq is None})

    def records(self, after: int = 0) -> Iterator[dict]:
        with self._lock, self.conn.transaction():
            ops = self.conn.execute("select seq, record from nanashi_operation where model_id = %s and seq > %s"
                                    " order by seq", (self.model_id, after)).fetchall()
            cells = self.conn.execute("select c.seq, c.metric_id, c.coords, c.old_value, c.new_value,"
                                      " c.old_member, c.new_member"
                                      " from nanashi_cell_change c join nanashi_operation o"
                                      " on o.model_id = c.model_id and o.seq = c.seq"
                                      " where c.model_id = %s and c.seq > %s and o.cells_uri is null"
                                      " order by c.seq, c.metric_id", (self.model_id, after)).fetchall()
        by_seq: dict[int, dict[str, list]] = {}
        for seq, metric, coords, old, new, old_member, new_member in cells:
            row = [coords, old if old_member is None else old_member, new if new_member is None else new_member]
            by_seq.setdefault(seq, {}).setdefault(metric, []).append(row)
        for seq, rec in ops:
            if "cells_blob" in rec:  # 大量のセルはファイルから読む
                rec["changes"]["cells"] = self._read_blob(rec.pop("cells_blob"))
            elif seq in by_seq:
                rec["changes"]["cells"] = [{"metric": m, "rows": rows} for m, rows in by_seq[seq].items()]
            yield rec

    def cell_history(self, model, metric: str, coords: Mapping[str, str]) -> list[dict]:
        """The change history of one cell, from the cell index (not from a scan of the records). metric is a
        Metric id and coords is dimension id -> member id."""
        self.index_pending()  # 確定の後に回した分を先に反映する
        m = model.metric(metric)
        key = _key_ids(model, m, coords)
        with self._lock:
            rows = self.conn.execute(
                "select c.seq, o.at, o.user_name, o.reason, c.old_value, c.new_value, c.old_member, c.new_member"
                " from nanashi_cell_change c join nanashi_operation o on o.model_id = c.model_id and o.seq = c.seq"
                " where c.model_id = %s and c.metric_id = %s and c.coords = %s::text[] order by c.seq",
                (self.model_id, m.id, key)).fetchall()
        return _shown(model, m, [{"seq": s, "at": _iso(a), "user": u, "reason": r,
                                  "old": o if om is None else om, "new": n if nm is None else nm}
                                 for s, a, u, r, o, n, om, nm in rows])

    # ------------------------------------------------ スナップショット

    def save_snapshot(self, model) -> str:
        # 同じ通し番号で取り直しても、登録済みのファイルを書き換えないよう、置き場所ごとに乱数を付ける
        prefix = f"{self.model_id}/snapshots/{model.seq:020d}-{uuid.uuid4().hex[:8]}"
        meta = put_snapshot(self.objects, prefix, model)
        with self._lock, self.conn.transaction():  # ファイルと manifest を置き終えてから登録する
            self.conn.execute("insert into nanashi_snapshot (model_id, seq, uri, meta) values (%s, %s, %s, %s)"
                              " on conflict (model_id, seq) do update set uri = excluded.uri, meta = excluded.meta",
                              (self.model_id, model.seq, prefix, Jsonb(meta)))
        return prefix

    def snapshots(self) -> list[tuple[int, Snapshot]]:
        """登録したスナップショット（新しい順）。ファイルのハッシュは読むときに確かめる
        （オブジェクトストレージから一覧のためにすべて読まない）。"""
        with self._lock:
            rows = self.conn.execute("select seq, uri, meta from nanashi_snapshot where model_id = %s and seq <= %s"
                                     " order by seq desc", (self.model_id, self.head)).fetchall()
        return [(seq, Snapshot(uri, meta["files"])) for seq, uri, meta in rows]

    def prune(self, keep: int = 2, op_window: int = 100_000) -> dict:
        """新しいほうから keep 個のスナップショットを残し、それより古いスナップショットを消す。残す一番古い
        スナップショットより前の記録の、大量の変更のファイルも消す（セルの履歴の表に反映してから消すので、
        記録の再生とセルの履歴はその表から読める）。最後の op_window 件より古い記録の client_op_id は忘れる
        （再送しても二重に確定しないと保証する範囲を決める）。消した数を返す。"""
        self.index_pending()  # 消すファイルの分は、先にセルの履歴に反映しておく
        out = {"snapshots": 0, "cells": 0, "client_op_ids": 0}
        snaps = self.snapshots()
        if len(snaps) > keep:
            oldest = snaps[keep - 1][0]
            for seq, place in snaps[keep:]:
                with self._lock:
                    self.conn.execute("delete from nanashi_snapshot where model_id = %s and seq = %s",
                                      (self.model_id, seq))
                for name in (*place.files, "manifest.json"):
                    self.objects.delete(f"{place.uri}/{name}")
                out["snapshots"] += 1
            with self._lock:
                rows = self.conn.execute("select seq, record from nanashi_operation where model_id = %s"
                                         " and seq <= %s and cells_uri is not null and indexed",
                                         (self.model_id, oldest)).fetchall()
            for seq, rec in rows:
                for f in rec.pop("cells_blob")["files"]:
                    self.objects.delete(f["uri"])
                    out["cells"] += 1
                with self._lock:
                    self.conn.execute("update nanashi_operation set record = %s, cells_uri = null"
                                      " where model_id = %s and seq = %s", (Jsonb(rec), self.model_id, seq))
        with self._lock:
            oldest = self._db_head() - op_window
            out["client_op_ids"] = self.conn.execute(
                "update nanashi_operation set client_op_id = null where model_id = %s and seq <= %s"
                " and client_op_id is not null", (self.model_id, oldest)).rowcount
            out["rejections"] = self.conn.execute("delete from nanashi_rejection where model_id = %s and head_seq <= %s",
                                                  (self.model_id, oldest)).rowcount
        return out

    def drop(self) -> None:
        """このモデルの記録をすべて消す（テスト用）。"""
        with self._lock, self.conn.transaction():
            for table in ("nanashi_cell_change", "nanashi_operation", "nanashi_snapshot", "nanashi_rejection",
                          "nanashi_model"):
                self.conn.execute(f"delete from {table} where model_id = %s", (self.model_id,))


def _copy_cells(copy, model_id: str, seq: int, cells: list[dict]) -> None:
    """Write the cell changes as rows of nanashi_cell_change. A member-type value (a member id) goes to
    old_member / new_member, a number or boolean to old_value / new_value."""
    for c in cells:
        rows = c["rows"]
        if not isinstance(rows, list):
            copy.write(rows.copy_text(model_id, seq, c["metric"]))
            continue
        for ids, old, new in rows:
            copy.write_row((model_id, seq, c["metric"], list(ids), _number(old), _number(new),
                            _member(old), _member(new)))


def _number(v):
    return float(v) if v is not None and not isinstance(v, str) else None


def _member(v):
    return v if isinstance(v, str) else None


def _iso(at) -> str:
    """記録の時刻（timestamptz）を、記録の JSON と同じ形の文字列（UTC、ミリ秒まで）にする。"""
    return at.astimezone(datetime.timezone.utc).isoformat(timespec="milliseconds")


def main(argv=None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="PostgreSQL の記録先の管理")
    sub = ap.add_subparsers(dest="command", required=True)
    m = sub.add_parser("migrate", help="スキーマを最新の版にする")
    m.add_argument("dsn")
    args = ap.parse_args(argv)
    if args.command == "migrate":
        before, after = migrate(args.dsn)
        print(f"スキーマの版: {before} → {after}")


if __name__ == "__main__":
    main()
