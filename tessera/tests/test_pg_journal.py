"""PostgreSQL の記録先（PgJournal）。NANASHI_PG_DSN（既定は手元の 55432 番）の PostgreSQL が必要。

ファイルの置き場所は、ローカルのディレクトリと、S3 互換のオブジェクトストレージ（NANASHI_S3_ENDPOINT、
既定は手元の 59000 番。boto3 が必要）の両方で同じテストを回す。"""
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from sparse_engine.engine import ReferenceEngine
from sparse_engine.workspace import Workspace

from .journals import DSN, PG_AVAILABLE
from .test_engines import build_with
from .test_journal import check_same_state, run_random
from .test_workspace import model as stock_model, move

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None

S3_ENDPOINT = os.environ.get("NANASHI_S3_ENDPOINT", "http://127.0.0.1:59000")
S3_BUCKET = "nanashi-test"


def s3_client():
    """テスト用のバケットを作ったクライアント。つながらなければ None。
    認証情報は compose.yaml で決めたもの（NANASHI_S3_ACCESS_KEY、NANASHI_S3_SECRET_KEY で変えられる）。"""
    try:
        import boto3
        from botocore.config import Config
        client = boto3.client("s3", endpoint_url=S3_ENDPOINT, region_name="us-east-1",
                              aws_access_key_id=os.environ.get("NANASHI_S3_ACCESS_KEY", "nanashi"),
                              aws_secret_access_key=os.environ.get("NANASHI_S3_SECRET_KEY", "nanashi-secret"),
                              config=Config(connect_timeout=2, retries={"max_attempts": 1}))
        try:
            client.head_bucket(Bucket=S3_BUCKET)
        except client.exceptions.ClientError:
            client.create_bucket(Bucket=S3_BUCKET)
        return client
    except Exception:
        return None


AVAILABLE = PG_AVAILABLE
S3 = s3_client() if AVAILABLE else None
if AVAILABLE:
    import nanashi_core

    from sparse_engine.objects import LocalObjects, S3Objects
    from sparse_engine.pg_journal import Fenced, PgJournal


@unittest.skipUnless(AVAILABLE, "PostgreSQL（NANASHI_PG_DSN）と psycopg、nanashi_core が必要")
class PgJournalTests(unittest.TestCase):
    """ファイルはローカルのディレクトリに置く。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.model_id = f"test-{uuid.uuid4().hex[:12]}"
        self.journals = []
        self.objects = self.make_objects()

    def make_objects(self):
        return LocalObjects(self.tmp.name)

    def tearDown(self):
        j = self.journal()
        j.drop()
        for x in self.journals:
            x.close()
        self.tmp.cleanup()

    def journal(self, **kwargs):
        j = PgJournal(DSN, self.model_id, self.objects, **kwargs)
        self.journals.append(j)
        return j

    def test_random_replay(self):
        engines = [ReferenceEngine] + ([RustEngine] if RustEngine is not None else [])
        run_random(self, seed=61, rounds=60, engine=engines[-1], reopen_engines=engines,
                   make=lambda tmp: self.journal())

    def test_random_replay_with_bulk_files(self):
        # ほとんどの記録で、セルの変更をファイルに置く経路を通す
        run_random(self, seed=67, rounds=60, engine=ReferenceEngine, reopen_engines=[ReferenceEngine],
                   make=lambda tmp: self.journal(bulk_cells=3))

    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_random_replay_with_blocks(self):
        # すべての変更を変更の塊で持ち、ほとんどを Parquet のファイルに置いて、まとめて書き込んで再生する
        import sparse_engine.journal as journal
        saved, journal.BLOCK_MIN = journal.BLOCK_MIN, 1
        self.addCleanup(setattr, journal, "BLOCK_MIN", saved)
        run_random(self, seed=71, rounds=60, engine=RustEngine, reopen_engines=[ReferenceEngine, RustEngine],
                   make=lambda tmp: self.journal(bulk_cells=3))

    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_large_write_goes_to_parquet(self):
        from .test_journal import many_cells
        m = many_cells(RustEngine(), n=12_000)
        j = self.journal()
        j.start(m)
        with m.transaction(user="etl"):
            m.spread("V", 24_000.0, how="even")
        (rec,) = j.conn.execute("select record from nanashi_operation where model_id = %s and cells_uri is not null",
                                (self.model_id,)).fetchone()
        (f,) = rec["cells_blob"]["files"]
        v = m.metrics["V"]
        self.assertTrue(f["uri"].startswith(f"{self.model_id}/cells/"))  # 置き場所の中の相対的なキー
        self.assertTrue(f["uri"].endswith(f"-{v.id}.parquet"))
        data = self.objects.get(f["uri"])
        meta = dict(nanashi_core.parquet_metadata(data))
        self.assertEqual(json.loads(meta["nanashi"]), {"metric": v.id})
        block = nanashi_core.CellBlock.from_parquet(data)
        self.assertEqual(len(block), 11_999)
        for e in (ReferenceEngine, RustEngine):
            check_same_state(self, m, self.journal().open(e()))
        history = self.journal().cell_history(m, "V", K="k5", T="t0")  # COPY の行を Rust で作って反映する
        self.assertEqual([(h["user"], h["old"], h["new"]) for h in history], [("etl", 5.0, 2.0)])

    def test_concurrent_indexing_does_not_duplicate_history(self):
        m = build_with(ReferenceEngine())
        j = self.journal(bulk_cells=3)
        j.start(m)
        with m.transaction(user="etl"):
            m.spread("Cost", 100, Product="C")  # 5 セル。確定の後で反映する
        others = [self.journal() for _ in range(4)]  # 別々のプロセスが、同時に反映しようとする
        barrier = threading.Barrier(len(others))

        def index(o):
            barrier.wait()
            o.index_pending()
        threads = [threading.Thread(target=index, args=(o,)) for o in others]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rows = j.conn.execute("select count(*) from nanashi_cell_change where model_id = %s",
                              (self.model_id,)).fetchone()[0]
        self.assertEqual(rows, 5)
        history = j.cell_history(m, "Cost", Product="C", Month="Feb")
        self.assertEqual([(h["user"], h["old"], h["new"]) for h in history], [("etl", None, 20.0)])

    def test_bulk_history_is_indexed_later(self):
        m = build_with(ReferenceEngine())
        j = self.journal(bulk_cells=3)
        j.start(m)
        with m.transaction(user="etl", reason="取り込み"):
            m.spread("Cost", 100, Product="C")  # C には値がないので、全月（5 セル）に均等に配る
        pending = j.conn.execute("select count(*) from nanashi_operation where model_id = %s and not indexed",
                                 (self.model_id,)).fetchone()[0]
        self.assertEqual(pending, 1)  # 確定の時点では、セルの履歴の表にはまだない
        history = j.cell_history(m, "Cost", Product="C", Month="Feb")  # 引くときに反映する
        self.assertEqual([(h["user"], h["old"], h["new"]) for h in history], [("etl", None, 20.0)])
        self.assertEqual(j.index_pending(), 0)
        check_same_state(self, m, self.journal().open(ReferenceEngine()))

    def test_workspace_group_commit_and_reopen(self):
        m = stock_model(ReferenceEngine())
        self.journal().start(m)
        ws = Workspace(m, self.journal())
        futures = [ws.submit(move(f"p{i}", f"p{i + 1}", "Jan", 1), user="u", client_op_id=f"r{i}")
                   for i in range(10)]
        self.assertEqual(sorted(f.result() for f in futures), list(range(1, 11)))
        self.assertEqual(ws.write(move("p0", "p1", "Jan", 1), client_op_id="r3"), futures[3].result())
        ws.checkpoint()
        ws.write(move("p5", "p6", "Feb", 2))
        ws.close()
        reopened = Workspace.open(self.journal(), ReferenceEngine())
        check_same_state(self, ws.version.model, reopened.version.model)
        history = reopened.journal.cell_history(reopened.version, "Stock", Product="p6", Month="Feb")
        self.assertEqual([(h["seq"], h["old"], h["new"]) for h in history], [(11, 100.0, 102.0)])
        reopened.close()

    def test_only_one_writer(self):
        m = build_with(ReferenceEngine())
        first = self.journal(lease_ttl=1.0, heartbeat=False)  # 落ちたプロセスのように、リースを延長しない
        first.start(m)
        m.set_cell("Price", 12, Product="A")  # first がリースを取る
        second = self.journal(lease_ttl=1.0, acquire_wait=0)
        other = second.open(ReferenceEngine())
        with self.assertRaises(Fenced):  # 期限内は取れない（待たない設定）
            other.set_cell("Price", 13, Product="A")
        time.sleep(1.2)
        other.set_cell("Price", 14, Product="A")  # 期限が切れたら取れる（世代番号が進む）
        with self.assertRaises(Fenced):  # 古いプロセスの確定は締め出される
            m.set_cell("Price", 15, Product="A")
        self.assertEqual(self.journal().open(ReferenceEngine()).get("Price", Product="A"), 14)

    def test_lost_lease_fences_before_the_new_writer_writes(self):
        m = build_with(ReferenceEngine())
        self.journal(lease_ttl=0.5, heartbeat=False).start(m)
        m.set_cell("Price", 12, Product="A")  # m がリースを取る
        time.sleep(0.7)
        self.journal(lease_ttl=0.5).acquire()  # 別のプロセスがリースを取っただけで、まだ書いていない
        with self.assertRaises(Fenced):  # 通し番号は合っていても、世代番号が古いので締め出される
            m.set_cell("Price", 13, Product="A")

    def test_prune_removes_old_snapshots_and_bulk_files(self):
        m = build_with(ReferenceEngine())
        j = self.journal(bulk_cells=3)
        j.start(m)
        with m.transaction(user="etl", client_op_id="bulk"):
            m.spread("Cost", 100, Product="C")  # 大量の変更のファイル
        m.checkpoint()
        m.set_cell("Price", 12, Product="A")
        m.checkpoint()
        files = lambda: sorted(self.objects.list(f"{self.model_id}/"))
        before = files()
        out = j.prune(keep=1, op_window=0)
        self.assertEqual(out, {"snapshots": 2, "cells": 1, "client_op_ids": 1})
        after = files()
        self.assertEqual(len([k for k in after if k.endswith("/manifest.json")]), 1)
        self.assertFalse(any("/cells/" in k for k in after))
        self.assertLess(len(after), len(before))
        check_same_state(self, m, self.journal().open(ReferenceEngine()))
        # 消したファイルの記録も、セルの履歴の表から再生できる
        self.assertEqual([r["seq"] for r in self.journal().records()], [1, 2])
        history = self.journal().cell_history(m, "Cost", Product="C", Month="Feb")
        self.assertEqual([(h["user"], h["old"], h["new"]) for h in history], [("etl", None, 20.0)])
        self.assertIsNone(self.journal().seq_of("bulk"))  # 覚えておく範囲の外

    def test_heartbeat_keeps_the_lease_while_idle(self):
        m = build_with(ReferenceEngine())
        first = self.journal(lease_ttl=0.6)  # 書き込みがなくても延長する
        first.start(m)
        m.set_cell("Price", 12, Product="A")
        time.sleep(1.0)  # 期限より長く何もしない
        other = self.journal(lease_ttl=0.6, acquire_wait=0).open(ReferenceEngine())
        with self.assertRaises(Fenced):  # まだ持っている
            other.set_cell("Price", 13, Product="A")
        m.set_cell("Price", 14, Product="A")  # 自分は書ける
        self.assertEqual(self.journal().open(ReferenceEngine()).get("Price", Product="A"), 14)

    def test_take_does_not_wait_and_records_the_endpoint(self):
        m = build_with(ReferenceEngine())
        self.journal(heartbeat=False).start(m)
        a = self.journal(lease_ttl=0.6, heartbeat=False, endpoint="http://a:1")
        b = self.journal(lease_ttl=0.6, heartbeat=False, endpoint="http://b:2")
        self.assertIsNone(b.leader())  # まだ誰も持っていない
        self.assertTrue(a.take())
        t = time.perf_counter()
        self.assertFalse(b.take())  # 期限内は取れず、待たない
        self.assertLess(time.perf_counter() - t, 0.3)
        self.assertEqual((a.leader(), b.leader()), ("http://a:1", "http://a:1"))
        a.release()
        self.assertIsNone(b.leader())  # 手放したら書き手はいない
        self.assertTrue(b.take())
        self.assertEqual(a.leader(), "http://b:2")
        time.sleep(0.7)
        self.assertIsNone(a.leader())  # 期限が切れたら書き手はいない
        self.assertTrue(a.take())  # 切れたリースは取れる（世代番号が進む）
        m.journal = b
        with self.assertRaises(Fenced):  # 古い持ち主の確定は締め出される
            m.set_cell("Price", 12, Product="A")

    def test_release_wakes_a_waiting_follower(self):
        m = build_with(ReferenceEngine())
        self.journal(heartbeat=False).start(m)
        a, b = self.journal(heartbeat=False), self.journal(heartbeat=False)
        a.acquire()
        b.wait(0.0)  # 通知を待つ接続をつないでおく
        threading.Timer(0.2, a.release).start()
        t = time.perf_counter()
        b.wait(5.0)  # 手放したときの通知で、間隔を待たずに戻る
        self.assertLess(time.perf_counter() - t, 2.0)

    def test_acquire_waits_for_a_dead_writers_lease(self):
        m = build_with(ReferenceEngine())
        dead = self.journal(lease_ttl=0.6, heartbeat=False)  # 落ちたプロセス（延長しない）
        dead.start(m)
        m.set_cell("Price", 12, Product="A")
        other = self.journal(lease_ttl=0.6, acquire_wait=3.0).open(ReferenceEngine())
        t = time.perf_counter()
        other.set_cell("Price", 13, Product="A")  # 期限が切れるのを待ってから取る（失敗しない）
        self.assertLess(time.perf_counter() - t, 3.0)
        self.assertEqual(self.journal().open(ReferenceEngine()).get("Price", Product="A"), 13)

    def test_workspace_reloads_when_another_process_wrote(self):
        m = stock_model(ReferenceEngine())
        self.journal().start(m)
        ws = Workspace(m, self.journal(lease_ttl=0.5, heartbeat=False))
        ws.write(move("p0", "p1", "Jan", 1))
        time.sleep(0.7)
        other = Workspace.open(self.journal(lease_ttl=0.5), ReferenceEngine())  # 別のプロセスが書く
        other.write(move("p2", "p3", "Jan", 5))
        other.close()
        with self.assertRaises(Fenced):  # 手元は締め出され、
            ws.write(move("p4", "p5", "Jan", 2))
        self.assertEqual(ws.version.get("Stock", Product="p3", Month="Jan"), 105)  # 最新の版を開き直している
        ws.close()

    def test_stale_reader_cannot_write(self):
        m = build_with(ReferenceEngine())
        first = self.journal(lease_ttl=0.5)
        first.start(m)
        stale = self.journal(lease_ttl=0.5).open(ReferenceEngine())  # 先に読み込んでおく
        m.set_cell("Price", 12, Product="A")
        time.sleep(0.7)
        with self.assertRaisesRegex(Fenced, "開き直す"):  # 読み込んだあとに書き込まれているので書けない
            stale.set_cell("Price", 13, Product="A")

    def test_corrupted_snapshot_falls_back(self):
        m = build_with(ReferenceEngine())
        j = self.journal()
        j.start(m)
        m.set_cell("Price", 12, Product="A")
        m.checkpoint()
        m.set_cell("Price", 13, Product="A")
        snap = dict(j.snapshots())[1]
        j.objects.put(f"{snap.uri}/{next(n for n in snap.files if n.startswith('inputs.'))}", b"broken")
        with self.assertLogs("sparse_engine.journal", "WARNING") as logs:  # 1 つ前（0）から開く
            check_same_state(self, m, self.journal().open(ReferenceEngine()))
        self.assertIn("スナップショット 1 が壊れている", logs.output[0])


@unittest.skipUnless(AVAILABLE and S3 is not None,
                     "PostgreSQL と、S3 互換のオブジェクトストレージ（NANASHI_S3_ENDPOINT）と boto3 が必要")
class PgJournalS3Tests(PgJournalTests):
    """同じテストを、ファイルを S3 互換のオブジェクトストレージに置いて回す。"""

    def make_objects(self):
        return S3Objects(f"s3://{S3_BUCKET}", client=S3)

    def tearDown(self):
        super().tearDown()
        keys = [o["Key"] for page in S3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET,
                                                                                  Prefix=f"{self.model_id}/")
                for o in page.get("Contents", [])]
        for i in range(0, len(keys), 1000):
            S3.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})

    def keys(self) -> list[str]:
        return sorted(o["Key"] for o in S3.list_objects_v2(Bucket=S3_BUCKET, Prefix=f"{self.model_id}/")
                      .get("Contents", []))

    def test_files_go_to_object_storage(self):
        m = build_with(ReferenceEngine())
        self.journal(bulk_cells=3).start(m)
        with m.transaction(user="etl"):
            m.spread("Cost", 100, Product="C")  # 大量の変更のファイル
        m.checkpoint()
        keys = self.keys()
        self.assertEqual({k.split("/")[1] for k in keys}, {"snapshots", "cells"})
        self.assertEqual(len({k.split("/")[2] for k in keys if "/snapshots/" in k}), 2)  # 通し番号 0 と 1
        self.assertTrue(all(uri.startswith(f"{self.model_id}/snapshots/") for _, (uri, _) in self.journal().snapshots()))
        self.assertEqual(sum(k.endswith("/manifest.json") for k in keys), 2)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])  # ローカルには何も置かない
        check_same_state(self, m, self.journal().open(ReferenceEngine()))

    def local_journal(self, m):
        """ローカルのディレクトリにファイルを置く記録先に、大量の変更とスナップショットを残す。"""
        local = PgJournal(DSN, self.model_id, LocalObjects(self.tmp.name), bulk_cells=3)
        self.journals.append(local)
        local.start(m)
        with m.transaction(user="etl"):
            m.spread("Cost", 100, Product="C")
        m.checkpoint()
        local.close()
        m.journal = None
        return local

    def test_moved_files_are_read_by_their_keys(self):
        # 表にはキーだけを保存するので、ファイルをオブジェクトストレージへ写せば、そのまま開ける
        m = build_with(ReferenceEngine())
        local = self.local_journal(m)
        for key in local.objects.list(f"{self.model_id}/"):
            self.objects.put(key, local.objects.get(key))
        self.tmp.cleanup()
        check_same_state(self, m, self.journal().open(ReferenceEngine()))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(AVAILABLE, "PostgreSQL（NANASHI_PG_DSN）と psycopg、nanashi_core が必要")
class Schema(unittest.TestCase):
    """スキーマの版。接続のたびに DDL を流さず、migrate で上げる。"""

    def setUp(self):
        import psycopg
        self.db = f"nanashi_schema_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"create database {self.db}")
        self.dsn = DSN.rsplit("/", 1)[0] + "/" + self.db
        self.addCleanup(self.drop)

    def drop(self):
        import psycopg
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"drop database if exists {self.db} with (force)")

    def test_schema_is_refused_until_migrated(self):
        from sparse_engine.pg_journal import SchemaError, migrate
        with self.assertRaisesRegex(SchemaError, "migrate"):
            PgJournal(self.dsn, "new", tempfile.mkdtemp())
        self.assertEqual(migrate(self.dsn), (0, 1))
        self.assertEqual(migrate(self.dsn), (1, 1))  # Migrate can run again
        j = PgJournal(self.dsn, "new", tempfile.mkdtemp(), heartbeat=False)
        m = build_with(ReferenceEngine())
        j.start(m)
        with m.transaction(user="alice"):
            m.set_cell("Price", 12, Product="A")
        (h,) = j.cell_history(m, "Price", Product="A")
        self.assertEqual(h["at"], m.last_record["at"])  # 時刻は記録の JSON と同じ形の文字列で返す
        j.close()
