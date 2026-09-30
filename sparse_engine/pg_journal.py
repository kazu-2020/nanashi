"""PostgreSQL に記録する記録先。

表は次の 4 つ（接頭辞 nanashi_）。1 つのデータベースに複数のモデルを置ける。

    model        モデルごとの、最後の記録の通し番号（head_seq）と、書き込むプロセスのリース
                 （世代番号 writer_epoch、持ち主、期限）
    operation    1 トランザクション 1 行。意図と、セル以外の結果を JSONB で持つ
    cell_change  書き換えた入力セルごとに 1 行（Metric の ID、座標のメンバーの ID の配列、変更前後の値）。
                 セルの履歴を索引で引ける
    snapshot     スナップショットの置き場所とハッシュ（ファイルはオブジェクトストレージの代わりに
                 ローカルのディレクトリに置き、置き終えてから登録する）

大量のセルを書き換えた記録（bulk_cells を超えるもの）は、変更前後の値をファイル（オブジェクト
ストレージの代わり）に書いて確定し、operation にはその置き場所とハッシュだけを持つ。cell_change への
書き込み（と索引の更新）は確定の後で行う（index_pending）。セルの索引の更新は 1 行あたりの費用が
大きく、確定の経路に入れると大量の書き込みの確定が何倍も遅くなるため。セルの履歴を引くときは、
先に未反映の分を反映する。

書き込むプロセスは 1 つに限る。最初に追記するときにリースを取り、世代番号を 1 つ進める。確定は
「通し番号が読んだとおりで、世代番号が自分のもの」のときだけ通る 1 回のトランザクションで行う。
リースが切れて別のプロセスが書き込みを始めていれば、古いプロセスの確定は拒否される（締め出し）。
読み込んだあとに別のプロセスが書き込んでいた場合も、手元のモデルが古いので拒否する（Stale）。

リースは書き込みのない間も別のスレッドで延長する（heartbeat）。別のプロセスが期限内のリースを
持っていれば、acquire は期限が切れるまで待ってから取る（落ちたプロセスのリースを待つ）。
"""
from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Iterator

import numpy as np
import psycopg
from psycopg.types.json import Jsonb

from .journal import Journal, Stale, _sha256, _shown, _sync, snapshot_ok, write_snapshot

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


class Fenced(Stale):
    """書き込むためのリースを持っていないか、別のプロセスが先に書き込んでいた。"""


class PgJournal(Journal):
    def __init__(self, dsn: str, model_id: str, snapshot_dir, *, lease_ttl: float = 30.0,
                 holder: str | None = None, bulk_cells: int = 10_000, heartbeat: bool = True,
                 acquire_wait: float | None = None):
        """lease_ttl はリースの期限（秒）。heartbeat なら、リースを持っている間は期限の 1/3 ごとに延長する。
        acquire_wait は、別のプロセスのリースが切れるのを待つ長さ（既定は lease_ttl）。
        holder はリースの持ち主の名前（既定はホスト名、プロセス番号、乱数）。"""
        self.dsn = dsn
        self.model_id = model_id
        self.snapshot_dir = Path(snapshot_dir) / model_id
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.blob_dir = self.snapshot_dir / "cells"
        self.blob_dir.mkdir(exist_ok=True)
        self.bulk_cells = bulk_cells
        # 接続はスレッドをまたいで使われうる（ライターと読み出し）。トランザクションの途中に別の文が
        # 割り込まないよう、接続を使う処理はロックで順に並べる。後からの反映は別の接続で行う
        self._lock = threading.RLock()
        self._index_conn = None
        self._index_lock = threading.Lock()
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
            if self._index_conn is not None:
                self._index_conn.close()

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
            # 大量のセルは、確定の前にファイルへ書いておく（確定に失敗したら参照されないファイルが残るだけ）
            blobs = [self._write_blob(r) if _cell_count(r) > self.bulk_cells else None for r in records]
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
                                        None if blob is None else blob["uri"], blob is None))
                with cur.copy("copy nanashi_cell_change (model_id, seq, metric_id, coords, old_value, new_value)"
                              " from stdin") as copy:
                    for rec, seq, blob in zip(records, seqs, blobs):
                        if blob is None:
                            _copy_cells(copy, self.model_id, seq, rec["changes"].get("cells", []))
            self.head = seqs[-1]
            return seqs

    # ------------------------------------------------ 大量のセル

    def _write_blob(self, record: dict) -> dict:
        """記録のセルの変更をファイルに書き、置き場所、ハッシュ、件数を返す。"""
        arrays = {}
        for i, c in enumerate(record["changes"].get("cells", [])):
            rows = c["rows"]
            width = len(rows[0][0]) if rows else 0
            arrays[f"metric{i}"] = np.array([c["metric"]], dtype=np.int64)
            arrays[f"coords{i}"] = np.array([ids for ids, _, _ in rows], dtype=np.int64).reshape(len(rows), width)
            for j, name in ((1, "old"), (2, "new")):
                vals = [r[j] for r in rows]
                arrays[f"{name}{i}"] = np.array([np.nan if v is None else float(v) for v in vals], dtype=np.float64)
                arrays[f"{name}_null{i}"] = np.array([v is None for v in vals], dtype=bool)
        final = self.blob_dir / f"{uuid.uuid4().hex}.npz"
        tmp = final.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            np.savez(f, **arrays)
            f.flush()
            _sync(f.fileno())
        os.rename(tmp, final)
        return {"uri": str(final), "sha256": _sha256(final), "cells": _cell_count(record)}

    @staticmethod
    def _read_blob(blob: dict) -> list[dict]:
        path = Path(blob["uri"])
        if _sha256(path) != blob["sha256"]:
            raise ValueError(f"{path}: セルの変更のファイルが壊れている")
        cells = []
        with np.load(path) as data:
            i = 0
            while f"metric{i}" in data:
                coords = data[f"coords{i}"].tolist()
                old, new = data[f"old{i}"].tolist(), data[f"new{i}"].tolist()
                old_null, new_null = data[f"old_null{i}"].tolist(), data[f"new_null{i}"].tolist()
                rows = [[ids, None if on else o, None if nn else n]
                        for ids, o, on, n, nn in zip(coords, old, old_null, new, new_null)]
                cells.append({"metric": int(data[f"metric{i}"][0]), "rows": rows})
                i += 1
        return cells

    def index_pending(self) -> int:
        """確定の後に回した大量のセルの変更を、cell_change に書き込む（専用の接続で行うので、
        書き込みを止めない）。書き込んだ記録の数を返す。"""
        with self._index_lock:
            if self._index_conn is None:
                self._index_conn = psycopg.connect(self.dsn, autocommit=True)
            conn = self._index_conn
            pending = conn.execute("select seq, record->'cells_blob' from nanashi_operation"
                                   " where model_id = %s and not indexed order by seq", (self.model_id,)).fetchall()
            loaded = 0
            for seq, blob in pending:
                cells = self._read_blob(blob)
                with conn.transaction():
                    with conn.cursor().copy("copy nanashi_cell_change (model_id, seq, metric_id, coords,"
                                            " old_value, new_value) from stdin") as copy:
                        _copy_cells(copy, self.model_id, seq, cells)
                    conn.execute("update nanashi_operation set indexed = true where model_id = %s and seq = %s",
                                 (self.model_id, seq))
                loaded += blob["cells"]
            if loaded > ANALYZE_ROWS:
                # 大量に入れた直後は表の統計が古く、セルの索引を使わない実行計画になりうる
                # （300 万行で、1 つのセルの履歴に 250 ms かかった。統計を取り直すと 0.2 ms）
                conn.execute("analyze nanashi_cell_change")
            return len(pending)

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

    def save_snapshot(self, model) -> Path:
        final = self.snapshot_dir / f"{model.seq:020d}"
        meta = write_snapshot(model, final, fsync=True)
        with self._lock, self.conn.transaction():  # ファイルを置き終えてから登録する
            self.conn.execute("insert into nanashi_snapshot (model_id, seq, uri, meta) values (%s, %s, %s, %s)"
                              " on conflict (model_id, seq) do update set uri = excluded.uri, meta = excluded.meta",
                              (self.model_id, model.seq, str(final), Jsonb(meta)))
        return final

    def snapshots(self) -> list[tuple[int, Path]]:
        with self._lock:
            rows = self.conn.execute("select seq, uri, meta from nanashi_snapshot where model_id = %s and seq <= %s"
                                     " order by seq desc", (self.model_id, self.head)).fetchall()
        return [(seq, Path(uri)) for seq, uri, meta in rows if snapshot_ok(Path(uri), meta)]

    def drop(self) -> None:
        """このモデルの記録をすべて消す（テスト用）。"""
        with self._lock, self.conn.transaction():
            for table in ("nanashi_cell_change", "nanashi_operation", "nanashi_snapshot", "nanashi_model"):
                self.conn.execute(f"delete from {table} where model_id = %s", (self.model_id,))


def _cell_count(record: dict) -> int:
    return sum(len(c["rows"]) for c in record["changes"].get("cells", []))


def _copy_cells(copy, model_id: str, seq: int, cells: list[dict]) -> None:
    for c in cells:
        for ids, old, new in c["rows"]:
            copy.write_row((model_id, seq, c["metric"], list(ids),
                            None if old is None else float(old), None if new is None else float(new)))
