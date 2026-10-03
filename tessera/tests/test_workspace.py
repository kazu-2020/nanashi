"""版の公開と単一ライター（Workspace）。"""
import random
import threading
import unittest

from sparse_engine import Model
from sparse_engine.engine import ReferenceEngine
from sparse_engine.workspace import Conflict, Workspace

from .journals import JournalCase, PgStore
from .test_incremental import same, snapshot
from .test_journal import check_same_state

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None

try:
    import nanashi_core  # noqa: F401  保存形式（Parquet）の読み書きに使う
except ImportError:
    nanashi_core = None

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


def workspace(test, m: Model, **kwargs) -> Workspace:
    """m の Workspace。test が記録先を持っていれば（JournalCase）、m を最初のスナップショットにして、
    その記録先に記録する（記録先の設定は test.journal_options）。"""
    journals = getattr(test, "journals", None)
    if journals is None:
        return Workspace(m, **kwargs)
    options = getattr(test, "journal_options", {})
    journals.journal(**options).start(m)
    return Workspace(m, journals.journal(**options), **kwargs)


def reopen(test, ws: Workspace, engine) -> None:
    """記録先から開き直した状態が、ws の公開中の版と同じことを確かめる（記録先がなければ何もしない）。
    開き直した側で書き込めることも確かめる（閉じた ws が書き込みの権利を手放している）。"""
    journals = getattr(test, "journals", None)
    if journals is None:
        return
    reopened = Workspace.open(journals.journal(**getattr(test, "journal_options", {})), engine)
    try:
        check_same_state(test, ws.version, reopened.version)
        test.assertEqual(reopened.seq, ws.seq)
        seq = reopened.write(lambda m: m.add_dimension("Reopened", ["x"]))
        test.assertEqual(seq, ws.seq + 1)
    finally:
        reopened.close()


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
        self.ws = workspace(self, model(self.engine()))

    def tearDown(self):
        self.ws.close()
        reopen(self, self.ws, self.engine())

    def test_write_publishes_a_new_version(self):
        v0 = self.ws.version
        seq = self.ws.write(move("p0", "p1", "Jan", 5), user="alice")
        self.assertEqual(seq, 1)
        self.assertIsNot(self.ws.version, v0)
        self.assertEqual(self.ws.version.get("Stock", Product="p1", Month="Jan"), 105)
        self.assertEqual(v0.get("Stock", Product="p1", Month="Jan"), 100)  # 古い版は変わらない

    def test_published_versions_cannot_be_changed(self):
        model = self.ws.version  # A published version is frozen. It rejects all operations
        with self.assertRaisesRegex(ValueError, "公開済み"):
            model.set_cell("Stock", 1, Product="p0", Month="Jan")
        with self.assertRaisesRegex(ValueError, "公開済み"):
            with model.transaction():
                pass
        with self.assertRaisesRegex(ValueError, "公開済み"):
            model.refresh()

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

    def test_remembered_client_op_ids_are_bounded(self):
        ws = Workspace(model(self.engine()), keep_recent=3)
        try:
            for i in range(10):
                ws.write(move("p0", "p1", "Jan", 1), client_op_id=f"op-{i}")
            self.assertEqual(list(ws._ops), ["op-7", "op-8", "op-9"])  # 増え続けない
        finally:
            ws.close()


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustBasics(Basics):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class FileBasics(JournalCase, RustBasics):
    pass


class PgBasics(JournalCase, RustBasics):
    store = PgStore


class Concurrency(unittest.TestCase):
    engine = staticmethod(ReferenceEngine)

    def test_readers_always_see_a_whole_version(self):
        ws = workspace(self, model(self.engine()))
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
        reopen(self, ws, self.engine())


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustConcurrency(Concurrency):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class FileConcurrency(JournalCase, RustConcurrency):
    pass


class PgConcurrency(JournalCase, RustConcurrency):
    store = PgStore


@unittest.skipIf(nanashi_core is None, "nanashi_core が必要")
class WithJournal(JournalCase, unittest.TestCase):
    engine = staticmethod(ReferenceEngine)

    def test_group_commit_and_reopen(self):
        ws = workspace(self, model(self.engine()))
        calls = [0]
        original = ws.journal.append_many

        def counting(records):
            calls[0] += 1
            return original(records)
        ws.journal.append_many = counting
        release = hold(ws)
        futures = [ws.submit(move(f"p{i}", f"p{i + 1}", "Mar", 1), user="u", client_op_id=f"r{i}")
                   for i in range(10)]
        release()
        self.assertEqual(sorted(f.result() for f in futures), list(range(1, 11)))
        self.assertEqual(calls[0], 1)  # 溜まった 10 件を 1 回の書き出しで確定する（止めた書き込みは何も変えない）
        self.assertEqual(ws.write(move("p0", "p1", "Jan", 1), client_op_id="r3"), futures[3].result())
        ws.checkpoint()
        ws.write(move("p0", "p9", "Apr", 3))
        ws.close()
        self.assertEqual(ws.seq, 11)
        reopen(self, ws, self.engine())
        again = Workspace.open(self.journals.journal(), self.engine())  # 開き直しても、確定済みの ID を覚えている
        self.assertEqual(again.write(move("p0", "p1", "Jan", 1), client_op_id="r3"), futures[3].result())
        again.close()

    def test_journal_failure_discards_the_batch(self):
        ws = workspace(self, model(self.engine()))
        v = ws.version

        def broken(records):
            raise OSError("disk full")
        original, ws.journal.append_many = ws.journal.append_many, broken
        with self.assertRaises(OSError):
            ws.write(move("p0", "p1", "Jan", 1))
        self.assertIs(ws.version, v)
        ws.journal.append_many = original
        self.assertEqual(ws.write(move("p0", "p1", "Jan", 1)), 1)  # 捨てたまとまりの後も続けて書ける
        ws.close()
        reopen(self, ws, self.engine())


@unittest.skipIf(RustEngine is None, "nanashi_core が必要")
class RustWithJournal(WithJournal):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


class PgWithJournal(WithJournal):
    store = PgStore


@unittest.skipIf(RustEngine is None, "nanashi_core が必要")
class PgRustWithJournal(RustWithJournal):
    store = PgStore


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class LargeWriteConflicts(unittest.TestCase):
    """多くのセルを書き換えた記録（変更の塊）とも、同じセルの書き込みを見分ける。"""
    journal_options = {"bulk_cells": 1000}  # 記録先があれば、按分（1500 セル）の変更はファイルに書く

    def setUp(self):
        from .test_journal import many_cells
        m = many_cells(RustEngine())
        m.add_input("W", ["K"], {})
        self.ws = workspace(self, m)

    def tearDown(self):
        self.ws.close()
        reopen(self, self.ws, RustEngine())

    def test_small_write_after_large_write(self):
        read = self.ws.seq
        self.ws.write(lambda m: m.spread("V", 3000.0, how="even"), user="etl")
        with self.assertRaises(Conflict) as e:
            self.ws.write(lambda m: m.set_cell("V", 9.0, K="k7", T="t0"), user="bob", expect=read)
        self.assertEqual(e.exception.user, "etl")
        self.ws.write(lambda m: m.set_cell("V", 9.0, K="k7", T="t1"), user="bob", expect=read)  # 違うセル

    def test_large_write_after_small_write(self):
        read = self.ws.seq
        self.ws.write(lambda m: m.set_cell("V", 9.0, K="k7", T="t0"), user="bob")
        with self.assertRaises(Conflict):
            self.ws.write(lambda m: m.spread("V", 3000.0, how="even"), user="etl", expect=read)

    def test_large_writes_on_both_sides(self):
        read = self.ws.seq
        self.ws.write(lambda m: m.spread("V", 3000.0, how="even"), user="etl")
        self.ws.write(lambda m: m.spread("W", 1500.0, how="even"), user="etl2", expect=read)  # 違う Metric
        with self.assertRaises(Conflict):
            self.ws.write(lambda m: m.spread("V", 4500.0, how="even"), user="etl3", expect=read)


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class FileLargeWriteConflicts(JournalCase, LargeWriteConflicts):
    pass


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class PgLargeWriteConflicts(JournalCase, LargeWriteConflicts):
    store = PgStore


class Policies(unittest.TestCase):
    """列の上限、待ち時間、スナップショットの間隔。"""

    def test_full_queue_raises_overloaded(self):
        from sparse_engine.workspace import Overloaded
        ws = Workspace(model(ReferenceEngine()), max_queue=2)
        try:
            release = hold(ws)  # ライターを止めておく
            ws.submit(move("p0", "p1", "Jan", 1))
            ws.submit(move("p1", "p2", "Jan", 1))
            with self.assertRaises(Overloaded):
                ws.submit(move("p2", "p3", "Jan", 1), timeout=0.05)
            release()
        finally:
            ws.close()

    def test_write_timeout_cancels_a_queued_write(self):
        ws = Workspace(model(ReferenceEngine()))
        try:
            release = hold(ws)
            with self.assertRaises(TimeoutError):
                ws.write(move("p0", "p1", "Jan", 1), timeout=0.05)  # 列で待っているうちに諦める
            release()
            self.assertEqual(ws.version.get("Stock", Product="p1", Month="Jan"), 100)  # 取り消されている
        finally:
            ws.close()

    def test_snapshot_policy_needs_a_journal(self):
        with self.assertRaisesRegex(ValueError, "記録先"):
            Workspace(model(ReferenceEngine()), checkpoint_every=5)


@unittest.skipIf(nanashi_core is None, "nanashi_core が必要")
class Snapshots(JournalCase, unittest.TestCase):
    def test_snapshots_are_taken_every_n_records(self):
        ws = workspace(self, model(ReferenceEngine()), checkpoint_every=5)
        for i in range(12):
            ws.write(move(f"p{i}", f"p{i + 1}", "Jan", 1))
            if ws._checkpointing is not None:
                ws._checkpointing.join()
        ws.close()
        seqs = [s for s, _ in self.journals.journal().snapshots()]
        self.assertEqual(sorted(seqs), [0, 5, 10])
        reopen(self, ws, ReferenceEngine())


class PgSnapshots(Snapshots):
    store = PgStore


if __name__ == "__main__":
    unittest.main()
