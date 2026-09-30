"""PostgreSQL の記録先（PgJournal）。NANASHI_PG_DSN（既定は手元の 55432 番）の PostgreSQL が必要。"""
import hashlib
import json
import tempfile
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

AVAILABLE = PG_AVAILABLE
if AVAILABLE:
    import nanashi_core
    from psycopg.types.json import Jsonb

    from sparse_engine.pg_journal import Fenced, PgJournal


@unittest.skipUnless(AVAILABLE, "PostgreSQL（NANASHI_PG_DSN）と psycopg、nanashi_core が必要")
class PgJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.model_id = f"test-{uuid.uuid4().hex[:12]}"
        self.journals = []

    def tearDown(self):
        j = self.journal()
        j.drop()
        for x in self.journals:
            x.close()
        self.tmp.cleanup()

    def journal(self, **kwargs):
        j = PgJournal(DSN, self.model_id, self.tmp.name, **kwargs)
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
        (f,) = (Path(self.tmp.name) / self.model_id / "cells").glob("*.parquet")
        v = m.metrics["V"]
        self.assertTrue(f.name.endswith(f"-{v.id}.parquet"))
        meta = dict(nanashi_core.parquet_metadata(f.read_bytes()))
        self.assertEqual(json.loads(meta["nanashi"]), {"metric": v.id})
        block = nanashi_core.CellBlock.from_parquet(f.read_bytes())
        self.assertEqual(len(block), 11_999)
        for e in (ReferenceEngine, RustEngine):
            check_same_state(self, m, self.journal().open(e()))
        history = self.journal().cell_history(m, "V", K="k5", T="t0")  # COPY の行を Rust で作って反映する
        self.assertEqual([(h["user"], h["old"], h["new"]) for h in history], [("etl", 5.0, 2.0)])

    def test_reads_npz_written_by_earlier_versions(self):
        from .legacy import npy, write_npz
        m = build_with(ReferenceEngine())
        j = self.journal(bulk_cells=3)
        j.start(m)
        with m.transaction(user="etl", reason="取り込み"):
            m.spread("Cost", 100, Product="C")
        # 記録を以前の版の形（1 つの npz、値はすべて float）に書き直す
        seq, rec = j.conn.execute("select seq, record from nanashi_operation where model_id = %s and not indexed",
                                  (self.model_id,)).fetchone()
        arrays = {}
        for i, f in enumerate(rec["cells_blob"]["files"]):
            path = Path(f["uri"])
            rows = nanashi_core.CellBlock.from_parquet(path.read_bytes()).rows()
            path.unlink()
            arrays[f"metric{i}"] = npy("<i8", [f["metric"]])
            arrays[f"coords{i}"] = npy("<i8", [x for ids, _, _ in rows for x in ids], (len(rows), len(rows[0][0])))
            for j_, name in ((1, "old"), (2, "new")):
                arrays[f"{name}{i}"] = npy("<f8", [float("nan") if r[j_] is None else float(r[j_]) for r in rows])
                arrays[f"{name}_null{i}"] = npy("|b1", [r[j_] is None for r in rows])
        path = Path(self.tmp.name) / self.model_id / "cells" / "legacy.npz"
        write_npz(path, arrays)
        rec["cells_blob"] = {"uri": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                             "cells": rec["cells_blob"]["cells"]}
        j.conn.execute("update nanashi_operation set record = %s, cells_uri = %s where model_id = %s and seq = %s",
                       (Jsonb(rec), str(path), self.model_id, seq))
        for e in [ReferenceEngine] + ([RustEngine] if RustEngine is not None else []):
            check_same_state(self, m, self.journal().open(e()))
        history = self.journal().cell_history(m, "Cost", Product="C", Month="Feb")
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
        path = dict(j.snapshots())[1]
        next(path.glob("inputs.*.parquet")).write_bytes(b"broken")
        self.assertEqual([s for s, _ in self.journal().snapshots()], [0])
        check_same_state(self, m, self.journal().open(ReferenceEngine()))


if __name__ == "__main__":
    unittest.main()
