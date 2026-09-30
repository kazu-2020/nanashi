"""トランザクションと操作ログ（記録）、スナップショットと記録の再生による復元。"""
import random
import tempfile
import unittest
from pathlib import Path

from sparse_engine import FormulaError, Model, to_formula
from sparse_engine.engine import ReferenceEngine
from sparse_engine.journal import AlreadyCommitted, FileJournal

from .journals import FileStore, JournalCase, PgStore
from .test_engines import build_with
from .test_incremental import same, snapshot
from .test_member_edit import structural
from .test_members import random_round as edit_or_add_member
from .test_redefine import redefine

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None

try:
    import nanashi_core  # noqa: F401  保存形式（Parquet）の読み書きに使う
except ImportError:
    nanashi_core = None


def definitions(m: Model) -> dict:
    """値以外の状態（軸、メンバー、ID、プロパティ、Metric の定義）。"""
    dims = {d.name: (d.id, d.ordered, list(zip(d.ids, d.members)),
                     {p: (t, dict(mp)) for p, (t, mp) in d.properties.items()})
            for d in m.dimensions.values()}
    metrics = {x.name: (x.id, x.dims, x.kind, None if x.written is None else to_formula(x.written),
                        x.partition, x.overridable) for x in m.metrics.values()}
    return {"dims": dims, "metrics": metrics, "next_id": m._next_id}


def check_same_state(test, a: Model, b: Model) -> None:
    test.assertEqual(definitions(a), definitions(b))
    sa, sb = snapshot(a), snapshot(b)
    for name in sa:
        test.assertTrue(same(sa[name], sb[name]), f"{name}\n{sa[name]}\n{sb[name]}")


def check_full(test, m: Model) -> None:
    incremental = snapshot(m)
    m._invalidate()
    full = snapshot(m)
    for name in m.metrics:
        test.assertTrue(same(incremental[name], full[name]), f"{name}\n差分: {incremental[name]}\n全体: {full[name]}")


class Abort(Exception):
    pass


class Transactions(unittest.TestCase):
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        self.m = build_with(self.engine())
        self.m.recalc()

    def test_exception_rolls_back_everything(self):
        before, defs = snapshot(self.m), definitions(self.m)
        with self.assertRaises(Abort):
            with self.m.transaction(user="alice"):
                self.m.set_cell("Price", 99, Product="A")
                self.m.add_member("Product", "E")
                self.m.remove_member("Month", "Feb")
                self.m.add_formula("Double", ["Product"], "Price * 2")
                self.m.rename_metric("Margin", "Profit")
                raise Abort
        self.assertEqual(definitions(self.m), defs)
        self.assertEqual(snapshot(self.m), before)
        self.m.set_cell("Price", 7, Product="A")
        check_full(self, self.m)

    def test_formula_error_at_commit_rolls_back(self):
        before = snapshot(self.m)
        with self.assertRaises(FormulaError):
            with self.m.transaction():
                self.m.set_cell("Price", 99, Product="A")
                self.m.add_formula("Bad", ["Product"], "Nope + 1")
        self.assertNotIn("Bad", self.m.metrics)
        self.assertEqual(snapshot(self.m), before)

    def test_record(self):
        with self.m.transaction(user="alice", reason="値上げ") as txn:
            self.m.set_cell("Price", 12, Product="A")
            self.m.spread("Cost", 60, Product="B")  # B の Cost は Mar だけなので、Mar が 30 から 60 になる
        rec = txn.record
        self.assertEqual((rec["user"], rec["reason"]), ("alice", "値上げ"))
        self.assertEqual([op["op"] for op in rec["ops"]], ["set_cell", "spread"])  # 按分の中の set_cell は記録しない
        cells = {c["metric"]: c["rows"] for c in rec["changes"]["cells"]}
        product = self.m.dimensions["Product"]
        self.assertEqual(cells[self.m.metrics["Price"].id], [[[product.id_of("A")], 10.0, 12.0]])
        month = self.m.dimensions["Month"]
        self.assertEqual(cells[self.m.metrics["Cost"].id], [[[product.id_of("B"), month.id_of("Mar")], 30.0, 60.0]])
        self.assertNotIn("metrics", rec["changes"])

    def test_nested_transactions_join_the_outer_one(self):
        with self.m.transaction() as outer:
            with self.m.transaction() as inner:
                self.m.set_cell("Price", 1, Product="A")
            self.assertIs(inner, outer)
            self.m.set_cell("Price", 2, Product="B")
        self.assertEqual(len(outer.record["ops"]), 2)


@unittest.skipIf(nanashi_core is None, "nanashi_core が必要")
class Journal(JournalCase, unittest.TestCase):
    """記録先に共通の性質。ファイルと PostgreSQL の両方の記録先で回す。"""
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        super().setUp()
        self.path = Path(self.journals.path)
        self.m = build_with(self.engine())
        self.journals.journal().start(self.m)

    def reopen(self, engine=None) -> Model:
        return self.journals.journal().open(engine or self.engine())

    def file_only(self) -> None:
        if self.store is not FileStore:
            self.skipTest("ファイルの記録先の形式を調べるテスト")

    def test_each_call_is_recorded(self):
        self.m.set_cell("Price", 12, Product="A")
        self.m.add_member("Product", "E", Category="Y")
        self.m.set_cell("Price", 3, Product="E")
        self.m.rename_member("Product", "A", "Alpha")
        self.assertEqual(self.m.seq, 4)
        check_same_state(self, self.m, self.reopen())

    def test_definitions_and_removals(self):
        with self.m.transaction(user="bob"):
            self.m.add_formula("Double", ["Product", "Month"], "Margin * 2", overridable=True)
            self.m.set_cell("Double", 5, Product="A", Month="Jan")
            self.m.rename_metric("Margin", "Profit")
            self.m.remove_member("Month", "Feb")
            self.m.remove_member("Product", "B")
            self.m.add_property("Product", "Category", "Category", {"A": "Y", "C": "X"})  # D は対応を外す
            self.m.remove_metric("CatShare")
        self.m.add_input("Salary", ["Employee"], {("e2",): 250})  # 入力の置き換え
        self.m.add_input("Stock", ["Product", "Month"], {("A", "Jan"): 1})  # 計算 Metric を入力に
        check_same_state(self, self.m, self.reopen())

    def test_rolled_back_transactions_are_not_recorded(self):
        with self.assertRaises(Abort):
            with self.m.transaction():
                self.m.set_cell("Price", 99, Product="A")
                raise Abort
        self.assertEqual(self.journals.journal().head, 0)
        check_same_state(self, self.m, self.reopen())

    def test_log_failure_rolls_back(self):
        before = snapshot(self.m)
        journal = self.m.journal
        original = journal.append

        def broken(record):
            raise OSError("disk full")
        journal.append = broken
        with self.assertRaises(OSError):
            self.m.set_cell("Price", 99, Product="A")
        journal.append = original
        self.assertEqual(snapshot(self.m), before)
        self.m.set_cell("Price", 98, Product="A")
        check_same_state(self, self.m, self.reopen())

    def test_resent_operation_is_not_applied_twice(self):
        with self.m.transaction(client_op_id="req-1"):
            self.m.set_cell("Price", 12, Product="A")
        with self.assertRaises(AlreadyCommitted) as e:
            with self.m.transaction(client_op_id="req-1"):
                self.m.set_cell("Price", 13, Product="A")
        self.assertEqual(e.exception.seq, 1)
        self.assertEqual(self.m.get("Price", Product="A"), 12)
        reopened = self.reopen()
        with self.assertRaises(AlreadyCommitted):  # 開き直しても覚えている
            with reopened.transaction(client_op_id="req-1"):
                pass

    def test_checkpoint_and_corrupted_snapshot(self):
        self.m.set_cell("Price", 12, Product="A")
        self.m.checkpoint()
        self.m.set_cell("Price", 13, Product="A")
        snapshots = self.journals.journal().snapshots()
        self.assertEqual([s for s, _ in snapshots], [1, 0])
        check_same_state(self, self.m, self.reopen())
        # 新しいスナップショットが壊れていたら、古いスナップショットから記録を多く再生する
        broken = next(Path(dict(snapshots)[1]).glob("inputs.*.parquet"))
        broken.write_bytes(b"broken")
        self.assertEqual([s for s, _ in self.journals.journal().snapshots()], [0])
        check_same_state(self, self.m, self.reopen())

    def test_torn_last_line_is_dropped(self):
        self.file_only()
        self.m.set_cell("Price", 12, Product="A")
        with open(self.path / "log.jsonl", "a") as f:
            f.write('{"seq": 2, "changes"')  # 書いている途中で落ちた
        reopened = self.reopen()
        self.assertEqual(reopened.seq, 1)
        reopened.set_cell("Price", 14, Product="A")
        self.assertEqual(FileJournal(self.path).head, 2)
        self.assertEqual(self.reopen().get("Price", Product="A"), 14)

    def test_corruption_in_the_middle_is_an_error(self):
        self.file_only()
        self.m.set_cell("Price", 12, Product="A")
        self.m.set_cell("Price", 13, Product="A")
        lines = (self.path / "log.jsonl").read_text().splitlines(keepends=True)
        (self.path / "log.jsonl").write_text("{broken\n" + lines[1])
        with self.assertRaisesRegex(ValueError, "壊れている"):
            FileJournal(self.path)

    def test_cell_history(self):
        with self.m.transaction(user="alice", reason="見直し"):
            self.m.set_cell("Price", 12, Product="A")
        with self.m.transaction(user="bob"):
            self.m.spread("Price", 30, Product="A")
        history = self.m.journal.cell_history(self.m, "Price", Product="A")
        self.assertEqual([(h["user"], h["old"], h["new"]) for h in history],
                         [("alice", 10.0, 12.0), ("bob", 12.0, 30.0)])


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustTransactions(Transactions):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


@unittest.skipIf(RustEngine is None, "nanashi_core が必要")
class RustJournal(Journal):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


class PgJournalContract(Journal):
    store = PgStore


@unittest.skipIf(RustEngine is None, "nanashi_core が必要")
class PgRustJournal(RustJournal):
    store = PgStore


# ---------------------------------------------------------------- ランダムな操作

def random_operation(rng: random.Random, models: list[Model], counters: list[list[int]]) -> None:
    kind = rng.random()
    if kind < 0.4:
        edit_or_add_member(rng, models, counters[0])
    elif kind < 0.55:
        m0 = models[0]
        p = rng.choice(m0.dimensions["Product"].members)
        total = float(rng.randint(10, 200))
        if "Volume" in m0.metrics and m0.metrics["Volume"].formula is None:
            for m in models:
                m.spread("Volume", total, Product=p)
    elif kind < 0.7:
        structural(rng, models, ["Product", "Month", "Region", "Category"], counters[1])
    else:
        redefine(rng, models, counters[2])


def run_random(test, seed: int, rounds: int, engine, reopen_engines, make=None) -> None:
    """make(tmp) は記録先を作る関数（省けばファイル）。同じ記録を指す記録先を、開き直すたびに作り直す。"""
    rng = random.Random(seed)
    make = make or (lambda tmp: FileJournal(tmp, fsync=False))
    with tempfile.TemporaryDirectory() as tmp:
        m = build_with(engine())
        make(tmp).start(m)
        counters = [[0], [0], [0]]
        for round_ in range(rounds):
            if rng.random() < 0.3:  # いくつかの操作を 1 つのトランザクションにまとめる（ときどき取り消す）
                before = (snapshot(m), definitions(m))
                abort = rng.random() < 0.3
                try:
                    with m.transaction(user=f"u{round_ % 3}"):
                        for _ in range(rng.randint(1, 3)):
                            random_operation(rng, [m], counters)
                        if abort:
                            raise Abort
                except Abort:
                    test.assertEqual((snapshot(m), definitions(m)), before)
            else:
                random_operation(rng, [m], counters)
            if rng.random() < 0.1:
                m.checkpoint()
            if rng.random() < 0.25:
                for e in reopen_engines:
                    with test.subTest(round=round_, engine=e.__name__):
                        check_same_state(test, m, make(tmp).open(e()))


@unittest.skipIf(nanashi_core is None, "nanashi_core が必要")
class RandomReplay(unittest.TestCase):
    def test_reference(self):
        engines = [ReferenceEngine] + ([RustEngine] if RustEngine is not None else [])
        run_random(self, seed=51, rounds=120, engine=ReferenceEngine, reopen_engines=engines)

    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_rust(self):
        run_random(self, seed=53, rounds=120, engine=RustEngine, reopen_engines=[ReferenceEngine, RustEngine])


def many_cells(engine, n: int = 1500) -> Model:
    m = Model(engine=engine)
    m.add_dimension("K", [f"k{i}" for i in range(n)])
    m.add_dimension("T", ["t0", "t1"], ordered=True)
    m.add_input("V", ["K", "T"], {(f"k{i}", "t0"): float(i) for i in range(n)})
    m.add_formula("Sum", ["T"], "V[REMOVE SUM: K]")
    m.recalc()
    return m


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class Blocks(unittest.TestCase):
    """Rust のエンジンで多くのセルを書き換えた記録は、行の列でなく変更の塊で持つ。"""

    def test_large_changes_are_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = many_cells(RustEngine())
            FileJournal(tmp, fsync=False).start(m)
            with m.transaction(user="etl") as txn:
                m.spread("V", 3000.0, how="even")  # 1500 セルすべてを 2 に
            (c,) = txn.record["changes"]["cells"]
            self.assertNotIsInstance(c["rows"], list)
            self.assertEqual(len(c["rows"]), 1499)  # k2 はもともと 2
            k, t = m.dimensions["K"], m.dimensions["T"]
            self.assertEqual(c["dims"], [k.id, t.id])
            self.assertEqual(sorted(c["rows"])[:2], [[[k.ids[0], t.ids[0]], 0.0, 2.0], [[k.ids[1], t.ids[0]], 1.0, 2.0]])
            for e in (ReferenceEngine, RustEngine):  # ファイルには行として書く
                check_same_state(self, m, FileJournal(tmp).open(e()))
            history = FileJournal(tmp).cell_history(m, "V", K="k5", T="t0")
            self.assertEqual([(h["user"], h["old"], h["new"]) for h in history], [("etl", 5.0, 2.0)])

    def test_random_replay_through_blocks(self):
        import sparse_engine.journal as journal
        saved, journal.BLOCK_MIN = journal.BLOCK_MIN, 1  # すべての変更を変更の塊で持つ
        self.addCleanup(setattr, journal, "BLOCK_MIN", saved)
        run_random(self, seed=57, rounds=80, engine=RustEngine, reopen_engines=[ReferenceEngine, RustEngine])


if __name__ == "__main__":
    unittest.main()
