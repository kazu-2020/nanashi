"""版の公開と単一ライター（Workspace）。"""
import random
import tempfile
import threading
import time
import unittest

from sparse_engine import Model
from sparse_engine.engine import ReferenceEngine
from sparse_engine.journal import FileJournal
from sparse_engine.workspace import Conflict, Workspace

from .test_incremental import same, snapshot
from .test_journal import check_same_state

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None

try:
    import numpy  # noqa: F401  スナップショットの保存形式に使う
except ImportError:
    numpy = None

PRODUCTS = [f"p{i}" for i in range(20)]
MONTHS = ["Jan", "Feb", "Mar", "Apr"]


def model(engine) -> Model:
    """在庫を商品と月で持ち、合計を集計するモデル。書き込みは商品の間で在庫を移すだけにして、
    合計が変わらないようにする。"""
    m = Model(engine=engine)
    m.add_dimension("Product", PRODUCTS)
    m.add_dimension("Month", MONTHS, ordered=True)
    m.add_input("Stock", ["Product", "Month"], {(p, t): 100.0 for p in PRODUCTS for t in MONTHS})
    m.add_formula("ByMonth", ["Month"], "Stock[REMOVE SUM: Product]")
    m.add_formula("Total", [], "ByMonth[REMOVE SUM: Month]")
    m.add_formula("Double", ["Product", "Month"], "Stock * 2")
    return m


def hold(ws: Workspace):
    """ライターを止めておく書き込みを入れる。止まったのを確かめてから返すので、後から入れた書き込みは
    1 つのまとまりに溜まる。返す関数を呼ぶとライターが動き出す。"""
    started, gate = threading.Event(), threading.Event()

    def fn(m):
        started.set()
        gate.wait()
    future = ws.submit(fn)
    started.wait()

    def release():
        gate.set()
        future.result()
    return release


def move(src: str, dst: str, month: str, amount: float):
    def fn(m):
        m.set_cell("Stock", m.get("Stock", Product=src, Month=month) - amount, Product=src, Month=month)
        m.set_cell("Stock", m.get("Stock", Product=dst, Month=month) + amount, Product=dst, Month=month)
    return fn


class Basics(unittest.TestCase):
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        self.ws = Workspace(model(self.engine()))

    def tearDown(self):
        self.ws.close()

    def test_write_publishes_a_new_version(self):
        v0 = self.ws.version
        seq = self.ws.write(move("p0", "p1", "Jan", 5), user="alice")
        self.assertEqual(seq, 1)
        self.assertIsNot(self.ws.version, v0)
        self.assertEqual(self.ws.version.get("Stock", Product="p1", Month="Jan"), 105)
        self.assertEqual(v0.get("Stock", Product="p1", Month="Jan"), 100)  # 古い版は変わらない

    def test_published_versions_cannot_be_changed(self):
        with self.assertRaisesRegex(ValueError, "公開済み"):
            self.ws.version.set_cell("Stock", 1, Product="p0", Month="Jan")
        with self.assertRaisesRegex(ValueError, "公開済み"):
            with self.ws.version.transaction():
                pass
        with self.assertRaisesRegex(ValueError, "公開済み"):
            self.ws.version.refresh()

    def test_failed_write_changes_nothing(self):
        v = self.ws.version

        def bad(m):
            m.set_cell("Stock", 0, Product="p0", Month="Jan")
            raise KeyError("oops")
        with self.assertRaises(KeyError):
            self.ws.write(bad)
        self.assertIs(self.ws.version, v)
        self.assertEqual(self.ws.write(move("p0", "p1", "Jan", 1)), 1)

    def test_one_failure_does_not_fail_the_batch(self):
        release = hold(self.ws)  # 後ろの書き込みを 1 つのまとまりに溜める
        good = self.ws.submit(move("p0", "p1", "Jan", 1))
        bad = self.ws.submit(lambda m: m.add_formula("Bad", ["Product"], "Nope + 1"))
        good2 = self.ws.submit(move("p2", "p3", "Jan", 1))
        release()
        self.assertEqual((good.result(), good2.result()), (1, 2))
        with self.assertRaises(Exception):
            bad.result()
        self.assertNotIn("Bad", self.ws.version.metrics)
        self.assertEqual(self.ws.version.get("Stock", Product="p3", Month="Jan"), 101)

    def test_conflict_on_the_same_cells(self):
        read = self.ws.seq
        self.ws.write(move("p0", "p1", "Jan", 1), user="alice")
        with self.assertRaises(Conflict) as e:
            self.ws.write(move("p1", "p2", "Jan", 1), user="bob", expect=read)
        self.assertEqual((e.exception.user, e.exception.seq), ("alice", 1))
        # 違うセルなら、古い版を読んでいても書ける
        self.assertEqual(self.ws.write(move("p5", "p6", "Feb", 1), user="bob", expect=read), 2)

    def test_resent_writes_apply_once(self):
        first = self.ws.write(move("p0", "p1", "Jan", 1), client_op_id="req-1")
        again = self.ws.write(move("p0", "p1", "Jan", 1), client_op_id="req-1")
        self.assertEqual(first, again)
        self.assertEqual(self.ws.version.get("Stock", Product="p1", Month="Jan"), 101)


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustBasics(Basics):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


class Concurrency(unittest.TestCase):
    engine = staticmethod(ReferenceEngine)

    def test_readers_always_see_a_whole_version(self):
        ws = Workspace(model(self.engine()))
        expected = 100.0 * len(PRODUCTS) * len(MONTHS)
        stop, errors, reads = threading.Event(), [], [0]

        def reader():
            while not stop.is_set():
                v = ws.version
                total = v.get("Total")
                by_month = sum(v.value("ByMonth").cells.values())
                cells = sum(v.value("Stock").cells.values())
                if not (total == by_month == cells == expected):
                    errors.append((v.seq, total, by_month, cells))
                reads[0] += 1

        def writer(seed):
            rng = random.Random(seed)
            for _ in range(30):
                a, b = rng.sample(PRODUCTS, 2)
                ws.write(move(a, b, rng.choice(MONTHS), float(rng.randint(1, 5))), user=f"w{seed}")

        readers = [threading.Thread(target=reader) for _ in range(3)]
        writers = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in readers + writers:
            t.start()
        for t in writers:
            t.join()
        stop.set()
        for t in readers:
            t.join()
        ws.close()
        self.assertEqual(errors, [])
        self.assertGreater(reads[0], 10)
        self.assertEqual(ws.seq, 120)
        full = model(self.engine())  # 同じ書き込みを全体の計算で求め直しても合う
        v = ws.version
        incremental = snapshot(v)
        v2 = v.fork()
        v2._invalidate()
        for name, cells in snapshot(v2).items():
            self.assertTrue(same(incremental[name], cells), name)
        self.assertEqual(full.get("Total"), v.get("Total"))


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustConcurrency(Concurrency):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


@unittest.skipIf(numpy is None, "numpy が必要")
class WithJournal(unittest.TestCase):
    engine = staticmethod(ReferenceEngine)

    def test_group_commit_and_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = model(self.engine())
            FileJournal(tmp).start(m)
            ws = Workspace(m, FileJournal(tmp))
            calls = [0]
            original = ws.journal.append_many

            def counting(records):
                calls[0] += 1
                return original(records)
            ws.journal.append_many = counting
            release = hold(ws)
            futures = [ws.submit(move(f"p{i}", f"p{i + 1}", "Mar", 1), user="u") for i in range(10)]
            release()
            self.assertEqual(sorted(f.result() for f in futures), list(range(1, 11)))
            self.assertEqual(calls[0], 1)  # 溜まった 10 件を 1 回の書き出しで確定する（止めた書き込みは何も変えない）
            ws.checkpoint()
            ws.write(move("p0", "p9", "Apr", 3))
            ws.close()
            reopened = Workspace.open(tmp, self.engine())
            check_same_state(self, ws.version, reopened.version)
            self.assertEqual(reopened.seq, 11)
            reopened.close()

    def test_journal_failure_discards_the_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = model(self.engine())
            FileJournal(tmp).start(m)
            ws = Workspace(m, FileJournal(tmp))
            v = ws.version

            def broken(records):
                raise OSError("disk full")
            ws.journal.append_many = broken
            with self.assertRaises(OSError):
                ws.write(move("p0", "p1", "Jan", 1))
            self.assertIs(ws.version, v)
            ws.close()


@unittest.skipIf(RustEngine is None or numpy is None, "nanashi_core と numpy が必要")
class RustWithJournal(WithJournal):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


if __name__ == "__main__":
    unittest.main()
