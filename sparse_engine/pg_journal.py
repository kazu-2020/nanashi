"""PostgreSQL に記録する記録先。

表は次の 4 つ（接頭辞 nanashi_）。1 つのデータベースに複数のモデルを置ける。

    model        モデルごとの、最後の記録の通し番号（head_seq）と、書き込むプロセスのリース
                 （世代番号 writer_epoch、持ち主、期限）
    operation    1 トランザクション 1 行。意図と、セル以外の結果を JSONB で持つ
    cell_change  書き換えた入力セルごとに 1 行（Metric の ID、座標のメンバーの ID の配列、変更前後の値）。
                 セルの履歴を索引で引ける
    snapshot     スナップショットの置き場所とハッシュ（ファイルはオブジェクトストレージに置き、
                 各ファイル、manifest.json の順に置き終えてから登録する。ハッシュは読むときに確かめ、
                 合わなければ 1 つ前のものを使う）

ファイルの置き場所（objects.py）は S3 互換のオブジェクトストレージ（s3://…）か、ローカルのディレクトリ。
表には置き場所の中の相対的なキーだけを保存するので、置き場所を移しても（ディレクトリから S3 へなど）読める。

大量のセルを書き換えた記録（bulk_cells を超えるもの）は、変更前後の値を Metric ごとに Parquet の
ファイルにしてオブジェクトストレージに置いてから確定し、operation にはその置き場所とハッシュだけを
持つ。Parquet の列は、座標のメンバーの ID（d<軸の ID>、Int64）と、変更前 old と変更後 new（空は null）。
cell_change への書き込み（と索引の更新）は確定の後で行う（index_pending）。行を 1 件ずつ入れる費用が
大きく、確定の経路に入れると大量の書き込みの確定が十数倍遅くなるため。セルの履歴を引くときは、
先に未反映の分を反映する。以前の版が書いた npz のファイルも読める。

書き込むプロセスは 1 つに限る。最初に追記するときにリースを取り、世代番号を 1 つ進める。確定は
「通し番号が読んだとおりで、世代番号が自分のもの」のときだけ通る 1 回のトランザクションで行う。
リースが切れて別のプロセスが書き込みを始めていれば、古いプロセスの確定は拒否される（締め出し）。
読み込んだあとに別のプロセスが書き込んでいた場合も、手元のモデルが古いので拒否する（Stale）。

リースは書き込みのない間も別のスレッドで延長する（heartbeat）。別のプロセスが期限内のリースを
持っていれば、acquire は期限が切れるまで待ってから取る（落ちたプロセスのリースを待つ）。
"""
from __future__ import annotations

import hashlib
import io
import os
import socket
import threading
import time
import uuid
from typing import Iterator

import psycopg
from psycopg.types.json import Jsonb

from .engine import native
from .journal import (Fenced, Journal, Snapshot, _shown, as_block, cell_count, put_snapshot, read_cell_files,
                      read_snapshot, write_cell_files)
from .objects import open_objects

SCHEMA = """
create table if not exists nanashi_model (
    model_id      text primary key,
    head_seq      bigint not null default 0,
    writer_epoch  bigint not null default 0,
    lease_holder  text,
    lease_expires timestamptz
);
create table if not exists nanashi_operation (
    model_id     text not null,
    seq          bigint not null,
    at           text not null,
    user_name    text,
    reason       text,
    client_op_id text,
    record       jsonb not null,
    primary key (model_id, seq)
);
alter table nanashi_operation add column if not exists cells_uri text;
alter table nanashi_operation add column if not exists indexed boolean not null default true;
create index if not exists nanashi_operation_unindexed on nanashi_operation (model_id, seq) where not indexed;
create unique index if not exists nanashi_operation_client_op
    on nanashi_operation (model_id, client_op_id) where client_op_id is not null;
create table if not exists nanashi_cell_change (
    model_id  text not null,
    seq       bigint not null,
    metric_id bigint not null,
    coords    bigint[] not null,
    old_value double precision,
    new_value double precision
);
create index if not exists nanashi_cell_change_by_seq on nanashi_cell_change (model_id, seq);
create index if not exists nanashi_cell_change_by_cell on nanashi_cell_change (model_id, metric_id, coords, seq);
create table if not exists nanashi_snapshot (
    model_id text not null,
    seq      bigint not null,
    uri      text not null,
    meta     jsonb not null,
    primary key (model_id, seq)
);
"""


# 確定の後の反映でこの行数より多く入れたら、セルの履歴の表の統計を取り直す
ANALYZE_ROWS = 100_000


class PgJournal(Journal):
    def __init__(self, dsn: str, model_id: str, objects, *, lease_ttl: float = 30.0,
                 holder: str | None = None, bulk_cells: int = 10_000, heartbeat: bool = True,
                 acquire_wait: float | None = None):
        """objects はスナップショットと大量の変更のファイルの置き場所（s3://<バケット>/<接頭辞> か
        ディレクトリ。objects.py）。その下の <model_id>/ に置く。
        lease_ttl はリースの期限（秒）。heartbeat なら、リースを持っている間は期限の 1/3 ごとに延長する。
        acquire_wait は、別のプロセスのリースが切れるのを待つ長さ（既定は lease_ttl）。
        holder はリースの持ち主の名前（既定はホスト名、プロセス番号、乱数）。"""
        self.dsn = dsn
        self.model_id = model_id
        self.objects = open_objects(objects)
        self.bulk_cells = bulk_cells
        # 接続はスレッドをまたいで使われうる（ライターと読み出し）。トランザクションの途中に別の文が
        # 割り込まないよう、接続を使う処理はロックで順に並べる。後からの反映は別の接続で行う
        self._lock = threading.RLock()
        self._index_conn = None
        self._index_lock = threading.Lock()
        self._listen_conn = None  # 確定の通知を待つ接続（wait で初めて使うときにつなぐ）
        self._listen_lock = threading.Lock()
        self.lease_ttl = lease_ttl
        self.acquire_wait = lease_ttl if acquire_wait is None else acquire_wait
        self.holder = holder or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.epoch: int | None = None  # 取ったリースの世代番号（まだ取っていなければ None）
        self.conn = psycopg.connect(dsn, autocommit=True)  # 複数の文は transaction() で囲む
        with self.conn.transaction():
            self.conn.execute(SCHEMA)
            self.conn.execute("insert into nanashi_model (model_id) values (%s) on conflict do nothing", (model_id,))
        self.head = self._db_head()
        self._stop = threading.Event()
        self._heartbeat: threading.Thread | None = None
        if heartbeat:
            self._heartbeat = threading.Thread(target=self._beat, name="nanashi-lease", daemon=True)
            self._heartbeat.start()

    def close(self) -> None:
        """リースを手放してから閉じる（次に書くプロセスが期限を待たずに済む）。"""
        self._stop.set()
        if self._heartbeat is not None:
            self._heartbeat.join()
        try:
            self.release()
        finally:
            self.conn.close()
            for c in (self._index_conn, self._listen_conn):
                if c is not None:
                    c.close()

    def release(self) -> None:
        """持っているリースを手放す。持っていなければ何もしない。"""
        with self._lock:
            if self.epoch is None or self.conn.closed:
                return
            self.conn.execute("update nanashi_model set lease_expires = now()"
                              " where model_id = %s and writer_epoch = %s and lease_holder = %s",
                              (self.model_id, self.epoch, self.holder))
            self.epoch = None

    def open(self, engine=None):
        """記録先の最新の状態を開く（手元の通し番号が古くても、表の通し番号から開き直す）。"""
        self.head = self._db_head()
        return super().open(engine)

    def refresh(self) -> int:
        self.head = self._db_head()
        return self.head

    def wait(self, timeout: float) -> None:
        """確定のたびに書き手が送る通知（NOTIFY nanashi_head）を、専用の接続で待つ。"""
        with self._listen_lock:
            if self._listen_conn is None:
                self._listen_conn = psycopg.connect(self.dsn, autocommit=True)
                self._listen_conn.execute("listen nanashi_head")
            for n in self._listen_conn.notifies(timeout=timeout):
                if n.payload == self.model_id:
                    return

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
            with self._lock, self.conn.transaction():
                row = self.conn.execute(
                    "update nanashi_model set writer_epoch = writer_epoch + 1, lease_holder = %s,"
                    " lease_expires = now() + make_interval(secs => %s)"
                    " where model_id = %s and (lease_holder is null or lease_holder = %s or lease_expires < now())"
                    " returning writer_epoch, head_seq",
                    (self.holder, self.lease_ttl, self.model_id, self.holder)).fetchone()
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
        if head != self.head:
            raise Fenced(f"{self.model_id}: 読み込んだあとに別のプロセスが書き込んだ（{self.head} → {head}）。開き直す")
        self.epoch = epoch
        return epoch

    def _beat(self) -> None:
        """リースを持っている間、期限の 1/3 ごとに延長する。延長できなければ（締め出された）リースを手放す。"""
        while not self._stop.wait(self.lease_ttl / 3):
            if self.epoch is None:
                continue
            try:
                with self._lock:
                    if self.epoch is None:
                        continue
                    cur = self.conn.execute(
                        "update nanashi_model set lease_expires = now() + make_interval(secs => %s)"
                        " where model_id = %s and writer_epoch = %s and lease_holder = %s",
                        (self.lease_ttl, self.model_id, self.epoch, self.holder))
                    if cur.rowcount != 1:
                        self.epoch = None
            except Exception:  # 接続の一時的な失敗。次の確定で改めて確かめる
                pass

    # ------------------------------------------------ 記録

    def append_many(self, records: list[dict]) -> list[int]:
        if not records:
            return []
        with self._lock:
            if self.epoch is None:
                self.acquire()
            seqs = list(range(self.head + 1, self.head + 1 + len(records)))
            # 大量のセルは、確定の前にファイルを置いておく（確定に失敗したら参照されないファイルが残るだけ）
            blobs = [self._write_blob(r) if cell_count(r) > self.bulk_cells else None for r in records]
            with self.conn.transaction():
                cur = self.conn.execute(
                    "update nanashi_model set head_seq = %s, lease_expires = now() + make_interval(secs => %s)"
                    " where model_id = %s and head_seq = %s and writer_epoch = %s and lease_holder = %s",
                    (seqs[-1], self.lease_ttl, self.model_id, self.head, self.epoch, self.holder))
                if cur.rowcount != 1:
                    self.epoch = None
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
                with cur.copy("copy nanashi_cell_change (model_id, seq, metric_id, coords, old_value, new_value)"
                              " from stdin") as copy:
                    for rec, seq, blob in zip(records, seqs, blobs):
                        if blob is None:
                            _copy_cells(copy, self.model_id, seq, rec["changes"].get("cells", []))
                # 追従する読み手（Replica）に知らせる。確定したときに届く
                self.conn.execute("select pg_notify('nanashi_head', %s)", (self.model_id,))
            self.head = seqs[-1]
            return seqs

    # ------------------------------------------------ 大量のセル

    def _write_blob(self, record: dict) -> dict:
        """記録のセルの変更を Metric ごとの Parquet にして置き、置き場所、ハッシュ、件数を返す。"""
        prefix = f"{self.model_id}/cells/{uuid.uuid4().hex}"

        def put(name: str, data: bytes) -> str:
            self.objects.put(f"{prefix}-{name}", data)
            return f"{prefix}-{name}"
        files = write_cell_files(record, put)
        return {"format": "parquet", "prefix": prefix, "files": files, "cells": cell_count(record)}

    def _read_blob(self, blob: dict) -> list[dict]:
        """_write_blob で置いたセルの変更を読む（Metric ごとの変更の塊）。以前の版の npz も読む。"""
        if blob.get("format") != "parquet":
            return _read_npz(self._get_checked(blob))
        return read_cell_files(blob["files"], self.objects.get)

    def _get_checked(self, f: dict) -> bytes:
        data = self.objects.get(f["uri"])
        if hashlib.sha256(data).hexdigest() != f["sha256"]:
            raise ValueError(f"{f['uri']}: セルの変更のファイルが壊れている")
        return data

    def index_pending(self) -> int:
        """確定の後に回した大量のセルの変更を、cell_change に書き込む（専用の接続で行うので、
        書き込みを止めない）。書き込んだ記録の数を返す。

        複数のプロセスが同時に呼んでも、同じ記録を二重に書かない。モデルごとの advisory lock で順に並べ、
        記録ごとに「まだ反映していない」印を先に外してから書く（印を外せなければ、ほかが書いた）。"""
        with self._index_lock:
            if self._index_conn is None:
                self._index_conn = psycopg.connect(self.dsn, autocommit=True)
            conn = self._index_conn
            conn.execute("select pg_advisory_lock(hashtextextended(%s, 0))", ("nanashi_index:" + self.model_id,))
            try:
                return self._index_locked(conn)
            finally:
                conn.execute("select pg_advisory_unlock(hashtextextended(%s, 0))", ("nanashi_index:" + self.model_id,))

    def _index_locked(self, conn) -> int:
        pending = conn.execute("select seq, record->'cells_blob' from nanashi_operation"
                               " where model_id = %s and not indexed order by seq", (self.model_id,)).fetchall()
        loaded, done = 0, 0
        core = native() if pending else None
        for seq, blob in pending:
            cells = self._read_blob(blob)
            with conn.transaction():
                claimed = conn.execute("update nanashi_operation set indexed = true"
                                       " where model_id = %s and seq = %s and not indexed",
                                       (self.model_id, seq)).rowcount
                if not claimed:
                    continue  # ほかのプロセスが先に反映した
                with conn.cursor().copy("copy nanashi_cell_change (model_id, seq, metric_id, coords,"
                                        " old_value, new_value) from stdin") as copy:
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

    def seq_of_many(self, client_op_ids: list[str]) -> dict[str, int]:
        if not client_op_ids:
            return {}
        with self._lock:
            rows = self.conn.execute("select client_op_id, seq from nanashi_operation"
                                     " where model_id = %s and client_op_id = any(%s)",
                                     (self.model_id, list(client_op_ids))).fetchall()
        return dict(rows)

    def records(self, after: int = 0) -> Iterator[dict]:
        with self._lock, self.conn.transaction():
            ops = self.conn.execute("select seq, record from nanashi_operation where model_id = %s and seq > %s"
                                    " order by seq", (self.model_id, after)).fetchall()
            cells = self.conn.execute("select c.seq, c.metric_id, c.coords, c.old_value, c.new_value"
                                      " from nanashi_cell_change c join nanashi_operation o"
                                      " on o.model_id = c.model_id and o.seq = c.seq"
                                      " where c.model_id = %s and c.seq > %s and o.cells_uri is null"
                                      " order by c.seq, c.metric_id", (self.model_id, after)).fetchall()
        by_seq: dict[int, dict[int, list]] = {}
        for seq, metric, coords, old, new in cells:
            by_seq.setdefault(seq, {}).setdefault(metric, []).append([coords, old, new])
        for seq, rec in ops:
            if "cells_blob" in rec:  # 大量のセルはファイルから読む
                rec["changes"]["cells"] = self._read_blob(rec.pop("cells_blob"))
            elif seq in by_seq:
                rec["changes"]["cells"] = [{"metric": m, "rows": rows} for m, rows in by_seq[seq].items()]
            yield rec

    def cell_history(self, model, metric: str, **coords: str) -> list[dict]:
        """セルの変更の履歴を、セルの索引で引く（記録を先頭から読まない）。"""
        self.index_pending()  # 確定の後に回した分を先に反映する
        m = model.metrics[metric]
        key = [model.dimension(d).id_of(coords[d]) for d in m.dims]
        with self._lock:
            rows = self.conn.execute(
            "select c.seq, o.at, o.user_name, o.reason, c.old_value, c.new_value"
            " from nanashi_cell_change c join nanashi_operation o on o.model_id = c.model_id and o.seq = c.seq"
                " where c.model_id = %s and c.metric_id = %s and c.coords = %s::bigint[] order by c.seq",
                (self.model_id, m.id, key)).fetchall()
        return _shown(model, m, [{"seq": s, "at": a, "user": u, "reason": r, "old": o, "new": n}
                                 for s, a, u, r, o, n in rows])

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

    def load_snapshot(self, place: Snapshot, engine):
        return read_snapshot(self.objects, place, engine)

    def drop(self) -> None:
        """このモデルの記録をすべて消す（テスト用）。"""
        with self._lock, self.conn.transaction():
            for table in ("nanashi_cell_change", "nanashi_operation", "nanashi_snapshot", "nanashi_model"):
                self.conn.execute(f"delete from {table} where model_id = %s", (self.model_id,))


def _copy_cells(copy, model_id: str, seq: int, cells: list[dict]) -> None:
    for c in cells:
        rows = c["rows"]
        if not isinstance(rows, list):
            copy.write(rows.copy_text(model_id, seq, c["metric"]))
            continue
        for ids, old, new in rows:
            copy.write_row((model_id, seq, c["metric"], list(ids),
                            None if old is None else float(old), None if new is None else float(new)))


def _read_npz(raw: bytes) -> list[dict]:
    """以前の版が書いたセルの変更（1 つの npz に、Metric ごとの配列を持つ）。値は float で返す。"""
    from . import npz
    data = npz.load(io.BytesIO(raw))
    cells = []
    i = 0
    while f"metric{i}" in data:
        old = [None if n else v for v, n in zip(data[f"old{i}"], data[f"old_null{i}"])]
        new = [None if n else v for v, n in zip(data[f"new{i}"], data[f"new_null{i}"])]
        cells.append({"metric": data[f"metric{i}"][0], "rows": [list(r) for r in zip(data[f"coords{i}"], old, new)]})
        i += 1
    return cells
