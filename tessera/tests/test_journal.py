"""トランザクションと操作ログ（記録）、スナップショットと記録の再生による復元。"""
import errno
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sparse_engine import FormulaError, Model, to_formula
from sparse_engine.engine import ReferenceEngine
from sparse_engine import journal as journal_module
from sparse_engine.journal import AlreadyCommitted, Fenced, FileJournal

from .journals import FileStore, JournalCase, PgStore
from .test_engines import build_with
from .test_incremental import check_full, same, snapshot
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
    dims = {d.name: (d.id, d.ordered, list(zip(d.ids, d.members)), d.in_order(),
                     {p: (t, dict(mp)) for p, (t, mp) in d.properties.items()})
            for d in m.dimensions.values()}
    metrics = {x.name: (x.id, x.dims, x.kind, None if x.written is None else to_formula(x.written, m),
                        x.partition, x.overridable) for x in m.metrics.values()}
    return {"dims": dims, "metrics": metrics, "next_id": m._next_id}


def check_same_state(test, a: Model, b: Model) -> None:
    test.assertEqual(definitions(a), definitions(b))
    test.assertEqual((a.ids, a.tombstones), (b.ids, b.tombstones))
    test.assertEqual({d.name: d.property_ids for d in a.dimensions.values()},
                     {d.name: d.property_ids for d in b.dimensions.values()})
    sa, sb = snapshot(a), snapshot(b)
    for name in sa:
        test.assertTrue(same(sa[name], sb[name]), f"{name}\n{sa[name]}\n{sb[name]}")


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
        self.assertNotIn("Bad", self.m._metric_ids)
        self.assertEqual(snapshot(self.m), before)

    def test_record(self):
        with self.m.transaction(user="alice", reason="値上げ") as txn:
            self.m.set_cell("Price", 12, Product="A")
            self.m.spread("Cost", 60, Product="B")  # B の Cost は Mar だけなので、Mar が 30 から 60 になる
        rec = txn.record
        self.assertEqual((rec["user"], rec["reason"]), ("alice", "値上げ"))
        self.assertEqual([op["op"] for op in rec["ops"]], ["set_cell", "spread"])  # 按分の中の set_cell は記録しない
        cells = {c["metric"]: c["rows"] for c in rec["changes"]["cells"]}  # keyed by the Metric handle
        product = self.m.dimensions["Product"]
        self.assertEqual(cells[self.m.ids[self.m.metric("Price").id]], [[[product.id_of("A")], 10.0, 12.0]])
        month = self.m.dimensions["Month"]
        self.assertEqual(cells[self.m.ids[self.m.metric("Cost").id]], [[[product.id_of("B"), month.id_of("Mar")], 30.0, 60.0]])
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

    def test_member_order_is_recorded(self):
        product = self.m.dimensions["Product"]
        with self.m.transaction() as moved:
            self.m.move_member("Product", "D", 0)
        # 並び替えだけなら、メンバーの変更（構造の変更）でなく並び順として記録する
        self.assertEqual(moved.record["changes"]["member_order"],
                         [{"dim": product.id, "order": [product.id_of(x) for x in "DABC"]}])
        self.assertNotIn("members", moved.record["changes"])
        with self.m.transaction() as txn:
            self.m.add_member("Product", "E", at=1, Category="Y")
            self.m.remove_member("Product", "B")
            self.m.set_cell("Price", 3, Product="E")
        self.assertEqual(txn.record["changes"]["member_order"],
                         [{"dim": product.id, "order": [product.id_of(x) for x in "DEAC"]}])
        with self.m.transaction() as appended:
            self.m.add_member("Product", "F")  # 最後に足すだけなら、並び順は記録しない
        self.assertNotIn("member_order", appended.record["changes"])
        self.assertEqual(product.in_order(), ["D", "E", "A", "C", "F"])
        reopened = self.reopen()
        check_same_state(self, self.m, reopened)
        self.assertEqual(reopened.rows("Price")[0], self.m.rows("Price")[0])

    def test_definitions_and_removals(self):
        with self.m.transaction(user="bob"):
            self.m.add_formula("Double", ["Product", "Month"], "Margin * 2", overridable=True)
            self.m.set_cell("Double", 5, Product="A", Month="Jan")
            self.m.rename_metric("Margin", "Profit")
            self.m.remove_member("Month", "Feb")
            self.m.remove_member("Product", "B")
            self.m.add_property("Product", "Category", "Category", {"A": "Y", "C": "X"}, id=self.m.property_id("Product", "Category"))  # D loses its mapping
            self.m.set_property_values("Product", "Category", {"A": "X", "C": None, "D": "Y"})
            self.m.remove_metric("CatShare")
        self.m.add_input("Salary", ["Employee"], {("e2",): 250}, id=self.m.metric("Salary").id)  # replace an input
        self.m.add_input("Stock", ["Product", "Month"], {("A", "Jan"): 1}, id=self.m.metric("Stock").id)  # a formula Metric becomes an input
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
        # 新しいスナップショットが壊れていたら、読むときにハッシュが合わないので、古いスナップショットから
        # 記録を多く再生する
        place = dict(snapshots)[1]
        name = next(n for n in place.files if n.startswith("inputs."))
        self.journals.journal().objects.put(f"{place.uri}/{name}", b"broken")
        check_same_state(self, self.m, self.reopen())

    def test_torn_last_line_is_dropped(self):
        self.file_only()
        self.m.set_cell("Price", 12, Product="A")
        with open(self.m.journal.log_path, "a") as f:
            f.write('{"seq": 2, "changes"')  # 書いている途中で落ちた
        self.m.journal.release()
        reopened = self.reopen()
        self.assertEqual(reopened.seq, 1)
        reopened.set_cell("Price", 14, Product="A")
        self.assertEqual(FileJournal(self.path).head, 2)
        self.assertEqual(self.reopen().get("Price", Product="A"), 14)

    def test_failed_append_is_undone(self):
        self.file_only()
        self.m.set_cell("Price", 12, Product="A")
        size = (self.m.journal.log_path).stat().st_size

        def disk_full(fd, data):
            os.write(fd, data[:len(data) // 2])  # 行の途中まで書けたところで満杯になった
            raise OSError(errno.ENOSPC, "No space left on device")

        failed = []

        def fsync_fails(fd):  # Linux の fsync は、失敗を 1 回だけ知らせる
            if not failed:
                failed.append(fd)
                raise OSError(errno.EIO, "Input/output error")
        for name, broken in (("_write_all", disk_full), ("_sync", fsync_fails)):
            with self.subTest(name), mock.patch.object(journal_module, name, broken):
                with self.assertRaises(OSError):
                    self.m.set_cell("Price", 99, Product="A")
            self.assertEqual((self.m.journal.log_path).stat().st_size, size)
            self.assertEqual(self.m.get("Price", Product="A"), 12)
        self.m.set_cell("Price", 13, Product="A")  # 同じ通し番号で書き直せる
        self.assertEqual(self.m.seq, 2)
        self.m.journal.release()
        check_same_state(self, self.m, self.reopen())

    def test_append_that_cannot_be_undone_stops_writes(self):
        self.file_only()
        self.m.set_cell("Price", 12, Product="A")

        def disk_full(fd, data):
            os.write(fd, data[:5])
            raise OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(journal_module, "_write_all", disk_full), \
                mock.patch.object(journal_module.os, "ftruncate", side_effect=OSError(errno.EIO, "EIO")):
            with self.assertRaises(OSError):
                self.m.set_cell("Price", 99, Product="A")
        with self.assertRaisesRegex(OSError, "取り消せなかった"):
            self.m.set_cell("Price", 13, Product="A")
        self.assertEqual(self.m.get("Price", Product="A"), 12)
        self.m.journal.release()
        self.assertEqual(self.reopen().get("Price", Product="A"), 12)  # 書きかけの行は捨てて開ける

    def test_second_writer_is_fenced(self):
        self.file_only()
        other = self.reopen()
        self.m.set_cell("Price", 12, Product="A")  # self.m の記録先が書き込みの権利を持つ
        with self.assertRaisesRegex(Fenced, "書き込み中"):
            other.set_cell("Price", 20, Product="B")
        self.m.journal.release()
        with self.assertRaisesRegex(Fenced, "開き直す"):  # 権利は取れても、手元が古い
            other.set_cell("Price", 20, Product="B")
        other = self.reopen()
        other.set_cell("Price", 20, Product="B")
        self.assertEqual(self.reopen().get("Price", Product="A"), 12)
        self.assertEqual(self.reopen().get("Price", Product="B"), 20)

    def test_reader_leaves_the_writers_partial_line(self):
        self.file_only()
        self.m.set_cell("Price", 12, Product="A")
        with open(self.m.journal.log_path, "a") as f:
            f.write('{"seq": 2, "changes"')  # 書き手がまだ書いている途中
        size = (self.m.journal.log_path).stat().st_size
        self.assertEqual(self.reopen().get("Price", Product="A"), 12)
        self.assertEqual((self.m.journal.log_path).stat().st_size, size)

    def test_snapshot_without_manifest_is_not_used(self):
        self.file_only()
        self.m.set_cell("Price", 12, Product="A")
        self.m.checkpoint()
        manifest = next(k for k in self.m.journal.objects.list("snapshots/") if k.endswith("manifest.json")
                        and "/00000000000000000001-" in k)
        self.m.journal.objects.delete(manifest)  # ファイルを置いている途中で落ちた
        self.assertEqual([s for s, _ in self.journals.journal().snapshots()], [0])
        check_same_state(self, self.m, self.reopen())

    def test_snapshots_split_the_log_and_prune_removes_the_old_part(self):
        self.file_only()
        j = self.m.journal
        for i in range(3):
            self.m.set_cell("Price", 10 + i, Product="A")
            self.m.checkpoint()
        self.m.set_cell("Price", 20, Product="B")
        segments = sorted(p.name for p in (self.path / "log").glob("*.jsonl"))
        self.assertEqual(segments, [f"{q:020d}.jsonl" for q in (1, 2, 3, 4)])  # スナップショットごとに区切る
        reopened = self.journals.journal()
        self.assertEqual([r["seq"] for r in reopened.records(after=3)], [4])
        check_same_state(self, self.m, self.reopen())
        self.assertEqual(j.prune(keep=1), {"snapshots": 3, "segments": 3, "cells": 0})  # 通し番号 0、1、2 の分
        self.assertEqual([s for s, _ in self.journals.journal().snapshots()], [3])
        check_same_state(self, self.m, self.reopen())
        with self.assertRaisesRegex(ValueError, "prune で消した"):
            list(self.journals.journal().records(after=0))

    def test_client_op_ids_are_remembered_within_the_window(self):
        self.file_only()
        self.m.journal = self.journals.journal(op_window=2)
        for i, op in enumerate("abc"):
            with self.m.transaction(client_op_id=op):
                self.m.set_cell("Price", i, Product="A")
        for j in (self.m.journal, self.journals.journal(op_window=2)):
            self.assertEqual((j.seq_of("a"), j.seq_of("b"), j.seq_of("c")), (None, 2, 3))

    def test_rejections_survive_a_reopen(self):
        body = {"error": "bad_request", "message": "bad"}
        self.m.journal.record_rejection("rj-1", 400, body)
        self.m.set_cell("Price", 12, Product="A")
        self.assertEqual(self.journals.journal().outcomes_of_many(["rj-1", "x"]), ({}, {"rj-1": (400, body)}))

    def test_rejections_are_remembered_within_the_window(self):
        self.file_only()
        j = self.journals.journal(op_window=2)
        j.record_rejection("old", 400, {})
        self.m.journal = j
        for i in range(3):
            self.m.set_cell("Price", i, Product="A")
        j.record_rejection("new", 400, {})
        self.assertEqual(self.journals.journal(op_window=2).outcomes_of_many(["old", "new"])[1], {"new": (400, {})})

    def test_a_rejection_after_a_cut_rejection_line_is_read(self):
        self.file_only()
        (self.path / "rejections.jsonl").write_text('{"client_op_id":"cut","head')  # A crash cut the line
        j = self.journals.journal()
        j.record_rejection("rj-1", 400, {})
        self.assertEqual(self.journals.journal().outcomes_of_many(["rj-1"])[1], {"rj-1": (400, {})})

    def test_corruption_in_the_middle_is_an_error(self):
        self.file_only()
        self.m.set_cell("Price", 12, Product="A")
        self.m.set_cell("Price", 13, Product="A")
        lines = (self.m.journal.log_path).read_text().splitlines(keepends=True)
        (self.m.journal.log_path).write_text("{broken\n" + lines[1])
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
        if "Volume" in m0._metric_ids and m0.metric("Volume").formula is None:
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


@unittest.skipIf(nanashi_core is None, "nanashi_core が必要")
class CellFiles(unittest.TestCase):
    """bulk_cells を超えるセルを書き換えた記録は、セルの変更を JSON の行にせず Parquet のファイルに書く。"""

    def test_random_replay_with_files(self):
        # ほとんどの記録で、セルの変更をファイルに置く経路を通す（参照実装の行の列も、変更の塊にして書く）
        run_random(self, seed=61, rounds=60, engine=ReferenceEngine, reopen_engines=[ReferenceEngine],
                   make=lambda tmp: FileJournal(tmp, fsync=False, bulk_cells=3))

    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_random_replay_with_blocks_in_files(self):
        import sparse_engine.journal as journal
        saved, journal.BLOCK_MIN = journal.BLOCK_MIN, 1  # すべての変更を変更の塊で持つ
        self.addCleanup(setattr, journal, "BLOCK_MIN", saved)
        run_random(self, seed=63, rounds=60, engine=RustEngine, reopen_engines=[ReferenceEngine, RustEngine],
                   make=lambda tmp: FileJournal(tmp, bulk_cells=3))

    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_large_write_goes_to_parquet(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = many_cells(RustEngine(), n=12_000)
            FileJournal(tmp).start(m)
            with m.transaction(user="etl"):
                m.spread("V", 24_000.0, how="even")
            m.set_cell("V", 1.0, K="k7", T="t1")  # 少ないセルは、これまでどおり行に書く
            (f,) = (Path(tmp) / "cells").glob("*.parquet")
            self.assertTrue(f.name.endswith(f"-{m.ids[m.metric('V').id]}.parquet"))
            lines = FileJournal(tmp).log_path.read_text().splitlines()
            self.assertLess(len(lines[0]), 1000)  # 記録の行には、ファイルの名前とハッシュだけ
            self.assertIn('"cells":[', lines[1])
            for e in (ReferenceEngine, RustEngine):
                check_same_state(self, m, FileJournal(tmp).open(e()))
            history = FileJournal(tmp).cell_history(m, "V", K="k5", T="t0")  # 変更の塊を Rust で探す
            self.assertEqual([(h["user"], h["old"], h["new"]) for h in history], [("etl", 5.0, 2.0)])
            history = FileJournal(tmp).cell_history(m, "V", K="k7", T="t1")
            self.assertEqual([(h["old"], h["new"]) for h in history], [(None, 1.0)])

    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_corrupted_cell_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = many_cells(RustEngine())
            FileJournal(tmp, bulk_cells=1000).start(m)
            m.spread("V", 3000.0, how="even")
            (f,) = (Path(tmp) / "cells").glob("*.parquet")
            f.write_bytes(f.read_bytes()[:-10])
            with self.assertRaisesRegex(ValueError, "壊れている"):
                FileJournal(tmp).open(RustEngine())

    def test_moved_directory_still_opens(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = build_with(ReferenceEngine())
            FileJournal(Path(tmp) / "a", bulk_cells=3).start(m)
            m.spread("Cost", 100, Product="C")
            (Path(tmp) / "a").rename(Path(tmp) / "b")  # ファイルの名前は記録先のディレクトリからの相対
            check_same_state(self, m, FileJournal(Path(tmp) / "b").open(ReferenceEngine()))


if __name__ == "__main__":
    unittest.main()
