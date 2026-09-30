"""PostgreSQL の記録先（PgJournal）。NANASHI_PG_DSN（既定は手元の 55432 番）の PostgreSQL が必要。"""
import os
import tempfile
import time
import unittest
import uuid

from sparse_engine.engine import ReferenceEngine
from sparse_engine.workspace import Workspace

from .test_engines import build_with
from .test_journal import check_same_state, run_random
from .test_workspace import model as stock_model, move

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None

DSN = os.environ.get("NANASHI_PG_DSN", "postgresql://postgres@127.0.0.1:55432/nanashi")


def available() -> bool:
    try:
        import numpy  # noqa: F401  スナップショットの保存形式に使う
        import psycopg
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


AVAILABLE = available()
if AVAILABLE:
    from sparse_engine.pg_journal import Fenced, PgJournal


@unittest.skipUnless(AVAILABLE, "PostgreSQL（NANASHI_PG_DSN）と psycopg、numpy が必要")
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
        check_same_state(self, ws.version, reopened.version)
        history = reopened.journal.cell_history(reopened.version, "Stock", Product="p6", Month="Feb")
        self.assertEqual([(h["seq"], h["old"], h["new"]) for h in history], [(11, 100.0, 102.0)])
        reopened.close()

    def test_only_one_writer(self):
        m = build_with(ReferenceEngine())
        first = self.journal(lease_ttl=1.0)
        first.start(m)
        m.set_cell("Price", 12, Product="A")  # first がリースを取る
        second = self.journal(lease_ttl=1.0)
        other = second.open(ReferenceEngine())
        with self.assertRaises(Fenced):  # 期限内は取れない
            other.set_cell("Price", 13, Product="A")
        time.sleep(1.2)
        other.set_cell("Price", 14, Product="A")  # 期限が切れたら取れる（世代番号が進む）
        with self.assertRaises(Fenced):  # 古いプロセスの確定は締め出される
            m.set_cell("Price", 15, Product="A")
        self.assertEqual(self.journal().open(ReferenceEngine()).get("Price", Product="A"), 14)

    def test_lost_lease_fences_before_the_new_writer_writes(self):
        m = build_with(ReferenceEngine())
        self.journal(lease_ttl=0.5).start(m)
        m.set_cell("Price", 12, Product="A")  # m がリースを取る
        time.sleep(0.7)
        self.journal(lease_ttl=0.5).acquire()  # 別のプロセスがリースを取っただけで、まだ書いていない
        with self.assertRaises(Fenced):  # 通し番号は合っていても、世代番号が古いので締め出される
            m.set_cell("Price", 13, Product="A")

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
        (path / "inputs.npz").write_bytes(b"broken")
        self.assertEqual([s for s, _ in self.journal().snapshots()], [0])
        check_same_state(self, m, self.journal().open(ReferenceEngine()))


if __name__ == "__main__":
    unittest.main()
