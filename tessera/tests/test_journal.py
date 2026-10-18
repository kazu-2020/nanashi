"""トランザクションと操作ログ（記録）、スナップショットと記録の再生による復元。"""
import contextlib
import errno
import itertools
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sparse_engine import FormulaError, Model, Named, to_formula
from sparse_engine.engine import ReferenceEngine
from sparse_engine import journal as journal_module
from sparse_engine.journal import AlreadyCommitted, Fenced, FileJournal
from sparse_engine.workspace import Conflict, Workspace

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
                     {p: (t, dict(mp), d.property_names[p]) for p, (t, mp) in d.properties.items()})
            for d in m.dimensions.values()}
    metrics = {x.name: (x.id, x.dims, x.kind, None if x.written is None else to_formula(x.written, m),
                        x.partition, x.overridable) for x in m.metrics.values()}
    return {"dims": dims, "metrics": metrics}


def check_same_state(test, a: Model, b: Model) -> None:
    test.assertEqual(definitions(a), definitions(b))
    test.assertEqual(a.tombstones, b.tombstones)
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
        cells = {c["metric"]: c["rows"] for c in rec["changes"]["cells"]}  # keyed by the Metric id
        product = self.m.dimension("Product")
        self.assertEqual(cells[self.m.metric("Price").id], [[[product.id_of("A")], 10.0, 12.0]])
        month = self.m.dimension("Month")
        self.assertEqual(cells[self.m.metric("Cost").id],
                         [[[product.id_of("B"), month.id_of("Mar")], 30.0, 60.0]])
        self.assertNotIn("metrics", rec["changes"])

    def test_nested_transactions_join_the_outer_one(self):
        with self.m.transaction() as outer:
            with self.m.transaction() as inner:
                self.m.set_cell("Price", 1, Product="A")
            self.assertIs(inner, outer)
            self.m.set_cell("Price", 2, Product="B")
        self.assertEqual(len(outer.record["ops"]), 2)

    def test_changes_of_a_fork_apply_to_the_original(self):
        """Objects added to a fork and to the original do not collide: the record of the fork applies."""
        from sparse_engine.journal import apply, changes
        from .test_ids import all_ids
        m = self.m
        base, fork = m.fork(), m.fork()
        fork.add_member("Product", "F")
        fork.add_input("Y", ["Product"], {("F",): 2})
        m.add_member("Product", "E")
        m.add_input("X", ["Product"], {("E",): 1})
        apply(m.model, {"changes": changes(base.model, fork.model)})
        self.assertEqual(m.dimension("Product").members, ["A", "B", "C", "D", "E", "F"])
        self.assertEqual((m.get("X", Product="E"), m.get("Y", Product="F")), (1, 2))
        self.assertEqual(len(set(all_ids(m))), len(all_ids(m)))


@unittest.skipIf(nanashi_core is None, "nanashi_core が必要")
class Journal(JournalCase, unittest.TestCase):
    """記録先に共通の性質。ファイルと PostgreSQL の両方の記録先で回す。"""
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        super().setUp()
        self.path = Path(self.journals.path)
        self.m = build_with(self.engine())
        self.journals.journal().start(self.m.model)

    def reopen(self, engine=None) -> Model:
        return Named(self.journals.journal().open(engine or self.engine()))

    def file_only(self) -> None:
        if self.store is not FileStore:
            self.skipTest("ファイルの記録先の形式を調べるテスト")

    def test_replay_of_an_override_shape_change(self):
        m = self.m
        plus1 = m.add_formula("Plus1", ["Product"], "Price + 1", overridable=True, id=m.metric("Plus1").id)
        m.set_cell("Plus1", 50, Product="A")
        m.add_formula("Plus1", ["Product", "Month"], "Cost + 10", overridable=True, id=plus1)
        m.set_cell("Plus1", 60, Product="A", Month="Feb")
        m.recalc()
        loaded = self.reopen()
        check_same_state(self, m, loaded)
        self.assertEqual(loaded.get("Plus1", Product="A", Month="Feb"), 60)
        self.assertEqual(loaded.get("Plus1", Product="A", Month="Jan"), 17)

    def test_replay_of_a_kind_change_and_an_override_in_one_transaction(self):
        m = self.m
        plus1 = m.add_formula("Plus1", ["Product"], "Price + 1", overridable=True, id=m.metric("Plus1").id)
        m.set_cell("Plus1", 1, Product="A")
        with m.transaction():
            m.add_formula("Plus1", ["Product"], "Price > 100", kind="boolean", overridable=True, id=plus1)
            m.set_cell("Plus1", True, Product="A")
        loaded = self.reopen()
        check_same_state(self, m, loaded)
        self.assertIs(loaded.get("Plus1", Product="A"), True)

    def test_replay_of_a_kind_change_and_an_equal_value_in_one_transaction(self):
        m = self.m
        x = m.add_input("X", ["Product"], {("A",): 1})
        with m.transaction():
            m.add_input("X", ["Product"], kind="boolean", id=x)
            m.set_cell("X", True, Product="A")
        loaded = self.reopen()
        check_same_state(self, m, loaded)
        self.assertIs(loaded.get("X", Product="A"), True)

    def test_each_call_is_recorded(self):
        self.m.set_cell("Price", 12, Product="A")
        self.m.add_member("Product", "E", Category="Y")
        self.m.set_cell("Price", 3, Product="E")
        self.m.rename_member("Product", "A", "Alpha")
        self.assertEqual(self.m.seq, 4)
        check_same_state(self, self.m, self.reopen())

    def test_a_member_with_an_empty_name_replays(self):
        """An old journal can hold a member named "" (the API accepted it before). A reopen must not refuse it."""
        d = self.m.dimension("Product")
        with self.m.transaction():
            self.m.set_cell("Price", 12, Product="A")
            d.add_member("")  # the internal path, as a replay uses it
            self.m.model._member_added(d.id)
        self.assertEqual(self.reopen().dimension("Product").members, d.members)

    def test_a_change_to_negative_zero_replays(self):
        self.m.set_cell("Price", 0.0, Product="A")
        self.m.set_cell("Price", -0.0, Product="A")
        self.assertEqual(repr(self.reopen().get("Price", Product="A")), "-0.0")

    def test_member_renames_replay(self):
        """A rename of a member in a formula, of a property target, and renames that change places."""
        m = self.m
        m.add_formula("JanCost", ["Product"], 'Cost[SELECT: Month."Jan"]')
        m.set_cell("Cost", 1, Product="A", Month="Jan")
        m.rename_member("Month", "Jan", "January")
        m.rename_member("Category", "X", "Hard")
        with m.transaction() as txn:
            m.rename_member("Product", "A", "tmp")
            m.rename_member("Product", "B", "A")
            m.rename_member("Product", "tmp", "B")
        self.assertNotIn("metrics", txn.record["changes"])  # the formulas hold ids, so no definition changes
        self.assertEqual(list(txn.record["changes"]), ["renamed"])  # a rename alone is not structural
        self.assertEqual(sorted(txn.record["changes"]["renamed"]), sorted([[m.member_id("Product", x), x] for x in "AB"]))
        reopened = self.reopen()
        check_same_state(self, m, reopened)
        self.assertEqual(to_formula(reopened.metric("JanCost").written, reopened), 'Cost[SELECT: Month."January"]')
        self.assertEqual(reopened.get("JanCost", Product="B"), 1)
        self.assertEqual(reopened.get("RevByCat", Category="Hard", Region="N", Month="January"),
                         m.get("RevByCat", Category="Hard", Region="N", Month="January"))

    def test_property_target_change_with_no_values_replays(self):
        m = self.m
        m.add_dimension("Shop", ["s1"])
        m.add_dimension("Old", ["o"])
        m.add_dimension("New", ["n"])
        pid = m.add_property("Shop", "Link", "Old", {})
        m.add_property("Shop", "Link", "New", {}, id=pid)
        self.assertEqual(self.reopen().dimension("Shop").properties[pid][0], m.dimension_id("New"))

    def test_member_order_is_recorded(self):
        product = self.m.dimension("Product")
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
            self.m.rename_dimension("Region", "Area")
            self.m.rename_property("Employee", "Department", "Dept")
        self.m.rename_dimension("Area", "Zone")
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
        j = self.m.journal = self.journals.journal(op_window=1)  # prune keeps the segments in the window
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

    def test_resend_after_prune_and_reopen_is_not_applied_twice(self):
        with self.m.transaction(client_op_id="req-1"):
            self.m.set_cell("Price", 12, Product="A")
        self.m.checkpoint()
        self.m.set_cell("Price", 13, Product="A")
        self.m.checkpoint()
        self.m.journal.prune(keep=1)
        self.m.journal.release()
        reopened = self.reopen()
        with self.assertRaises(AlreadyCommitted):
            with reopened.transaction(client_op_id="req-1"):
                reopened.set_cell("Price", 14, Product="A")
        self.assertEqual(self.reopen().get("Price", Product="A"), 13)

    def test_prune_keeps_at_least_one_snapshot(self):
        self.m.set_cell("Price", 12, Product="A")
        self.m.checkpoint()
        with self.assertRaisesRegex(ValueError, "keep"):
            self.m.journal.prune(keep=0)
        self.assertEqual(len(self.journals.journal().snapshots()), 2)
        check_same_state(self, self.m, self.reopen())

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

    def test_a_broken_line_in_an_old_segment_is_an_error(self):
        self.file_only()
        self.m.set_cell("Price", 12, Product="A")
        self.m.set_cell("Price", 13, Product="A")
        old = self.m.journal.log_path
        self.m.checkpoint()
        self.m.set_cell("Price", 14, Product="A")
        lines = old.read_text().splitlines(keepends=True)
        for broken, prev in [(0, 0), (1, 1)]:
            with self.subTest(broken_line=broken + 1):
                old.write_text("".join("{broken\n" if i == broken else line for i, line in enumerate(lines)))
                j = self.journals.journal(op_window=1)  # Open reads only the last segment
                with self.assertRaisesRegex(ValueError, f"通し番号が {prev} の次でなく"):
                    list(j.records())

    def test_a_broken_line_in_the_op_window_is_an_error_at_open(self):
        self.file_only()
        self.m.set_cell("Price", 11, Product="A")
        self.m.set_cell("Price", 12, Product="A")
        with self.m.transaction(client_op_id="req-3"):
            self.m.set_cell("Price", 13, Product="A")
        self.m.set_cell("Price", 14, Product="A")
        old = self.m.journal.log_path
        self.m.checkpoint()
        self.m.set_cell("Price", 15, Product="A")
        lines = old.read_text().splitlines(keepends=True)
        old.write_text(lines[0] + "{broken\n" + "".join(lines[2:]))
        self.m.journal.release()
        with self.assertRaisesRegex(ValueError, "通し番号が 1 の次でなく 5"):
            self.reopen()

    def test_a_broken_last_line_before_an_empty_segment_is_an_error(self):
        self.file_only()
        self.m.set_cell("Price", 11, Product="A")
        self.m.set_cell("Price", 12, Product="A")
        with self.m.transaction(client_op_id="req-3"):
            self.m.set_cell("Price", 13, Product="A")
        old = self.m.journal.log_path
        self.m.checkpoint()
        lines = old.read_text().splitlines(keepends=True)
        old.write_text(lines[0] + "{broken\n" + lines[2])
        (old.parent / f"{4:020d}.jsonl").touch()  # A failed append after the rotation leaves an empty segment
        self.m.journal.release()
        with self.assertRaisesRegex(ValueError, "通し番号が 1 の次でなく 4"):
            self.reopen()
        with self.assertRaisesRegex(ValueError, "通し番号が 1 の次でなく 4"):
            list(self.journals.journal(op_window=0).records())

    def test_cell_history(self):
        with self.m.transaction(user="alice", reason="見直し"):
            self.m.set_cell("Price", 12, Product="A")
        with self.m.transaction(user="bob"):
            self.m.spread("Price", 30, Product="A")
        history = self.m.cell_history(self.m.journal, "Price", Product="A")
        self.assertEqual([(h["user"], h["old"], h["new"]) for h in history],
                         [("alice", 10.0, 12.0), ("bob", 12.0, 30.0)])

    def test_cell_history_of_a_member_type_metric_shows_names(self):
        m = self.m
        m.add_input("Lead", ["Product"], {("A",): "e1"}, kind="member:Employee")
        m.set_cell("Lead", "e2", Product="A")
        (c,) = m.last_record["changes"]["cells"]
        self.assertEqual(c["rows"], [[[m.member_id("Product", "A")], m.member_id("Employee", "e1"),
                                      m.member_id("Employee", "e2")]])  # a member-type value is a member id
        m.rename_member("Employee", "e2", "Eve")
        history = m.cell_history(self.journals.journal(), "Lead", Product="A")
        self.assertEqual([(h["old"], h["new"]) for h in history], [(None, "e1"), ("e1", "Eve")])

    def test_record_has_only_the_changed_cells_when_a_member_is_removed(self):
        """When a member is removed, the numbers after it close up in the keys and in the member-type values. The record
        has only the cells that the transaction changed, as ids, and a new open gives the same state."""
        m = self.m
        m.add_input("Lead", ["Product"], {("A",): "e1", ("B",): "e3"}, kind="member:Employee")
        with m.transaction() as txn:
            m.remove_member("Employee", "e2")  # the number of e3 closes up
            m.set_cell("Lead", "e3", Product="C")
            m.set_cell("Salary", 70, Employee="e3")
        rows = {c["metric"]: c["rows"] for c in txn.record["changes"]["cells"]}
        self.assertEqual(rows, {m.metric("Lead").id: [[[m.member_id("Product", "C")], None, m.member_id("Employee", "e3")]],
                                m.metric("Salary").id: [[[m.member_id("Employee", "e3")], 300.0, 70.0]]})
        check_same_state(self, m, self.reopen())

    def test_a_new_member_can_take_the_number_of_the_removed_last_member(self):
        """If the last member is removed and a new member comes in one transaction, the new member gets the same
        number. The record has the cells of the new member, and the cells of the removed member are not in it."""
        m = self.m
        m.set_cell("Salary", 40, Employee="e4")
        with m.transaction() as txn:
            m.remove_member("Employee", "e4")
            m.add_member("Employee", "e5", Department="Eng")
            m.set_cell("Salary", 50, Employee="e5")
        (c,) = txn.record["changes"]["cells"]
        self.assertEqual(c["rows"], [[[m.member_id("Employee", "e5")], None, 50.0]])
        check_same_state(self, m, self.reopen())
        with m.transaction() as empty:
            m.add_member("Employee", "e6")
            m.remove_member("Employee", "e6")  # an empty last member: nothing changes
        self.assertNotIn("cells", empty.record["changes"])
        check_same_state(self, m, self.reopen())

    def caught_up(self, reader: Model) -> Model:
        """reader after an incremental catch-up on the records of this test (as a standby does)."""
        self.assertTrue(self.journals.journal().catch_up(reader.model))
        reader.recalc()
        return reader

    def test_a_removal_before_the_end_in_a_store_without_a_base_records_the_new_numbers(self):
        """A store with only a delta (cells set one by one) shares its empty base with the copy from before the
        transaction. The record still uses the new numbers of the members after the removed one."""
        m = self.m
        m.add_input("Y", ["Employee"])
        m.set_cell("Y", 5, Employee="e2")
        m.set_cell("Y", 7, Employee="e3")
        reader = self.reopen()
        with m.transaction() as txn:
            m.set_cell("Y", None, Employee="e3")
            m.remove_member("Employee", "e2")
        (c,) = txn.record["changes"]["cells"]
        self.assertEqual(c["rows"], [[[m.member_id("Employee", "e3")], 7.0, None]])
        self.assertEqual(m.value("Y").cells, {})
        check_same_state(self, m, self.caught_up(reader))
        check_same_state(self, m, self.reopen())

    def test_an_equal_value_at_the_reused_number_of_the_removed_last_member_is_a_change(self):
        """Y[e4] = 40 only in the delta. In one transaction, e4 (the last member) is removed, e5 takes its number
        and Y[e5] = 40. The old and the new store hold the same key and value, but e5 is a new member."""
        m = self.m
        m.add_input("Y", ["Employee"])
        m.set_cell("Y", 40, Employee="e4")
        reader = self.reopen()
        with m.transaction() as txn:
            m.remove_member("Employee", "e4")
            m.add_member("Employee", "e5", Department="Eng")
            m.set_cell("Y", 40, Employee="e5")
        (c,) = txn.record["changes"]["cells"]
        self.assertEqual(c["rows"], [[[m.member_id("Employee", "e5")], None, 40.0]])
        check_same_state(self, m, self.caught_up(reader))
        check_same_state(self, m, self.reopen())

    def test_a_value_that_pointed_to_the_removed_member_is_recorded_as_a_clear(self):
        """Lead[A] = e2 and e2 is removed: the record has Lead[A]: e2 -> None, because the conflict check of
        Workspace and the cell history read it. A replacement in the same transaction is recorded as e2 -> e1."""
        m = self.m
        m.add_input("Lead", ["Product"], {("A",): "e2", ("B",): "e2"}, kind="member:Employee")
        e1, e2 = m.member_id("Employee", "e1"), m.member_id("Employee", "e2")
        a, b = m.member_id("Product", "A"), m.member_id("Product", "B")
        ws = Workspace(m.model, self.journals.journal())
        self.addCleanup(ws.close)
        read = ws.seq

        def remove_and_replace(model):
            n = Named(model)
            n.remove_member("Employee", "e2")
            n.set_cell("Lead", "e1", Product="B")
        ws.write(remove_and_replace)
        (c,) = list(self.journals.journal().records())[-1]["changes"]["cells"]
        self.assertEqual(sorted(c["rows"]), sorted([[[a], e2, None], [[b], e2, e1]]))
        with self.assertRaises(Conflict):
            ws.write(lambda model: Named(model).set_cell("Lead", "e3", Product="A"), expect=read)
        current = Named(ws.version)
        history = current.cell_history(self.journals.journal(), "Lead", Product="A")
        self.assertEqual([(h["old"], h["new"]) for h in history], [(None, e2), (e2, None)])  # a removed member stays an id
        check_same_state(self, current, self.reopen())

    def test_renames_are_followed_without_recalculating(self):
        """A reader that catches up on a rename only changes the name. A new open gives the same names."""
        m = self.m
        m.add_formula("Bonus", ["Product"], "Price * 0.1", overridable=True)
        bonus, hidden = m.metric("Bonus").id, m.metric("__override__Bonus").id
        m.add_formula("Bonus", ["Product"], "Price * 0.1", id=bonus)  # the hidden input stays linked
        reader = Named(self.journals.journal().open(self.engine()))
        reader.recalc()
        steps = [("dimension", lambda: m.rename_dimension("Product", "Item")),
                 ("property", lambda: m.rename_property("Item", "Category", "Group")),
                 ("metric", lambda: m.rename_metric("Plus1", "PlusOne")),
                 ("overridable metric", lambda: m.rename_metric("Bonus", "Reward")),
                 ("member", lambda: m.rename_member("Item", "A", "Alpha"))]

        def swap():
            with m.transaction():
                m.rename_metric("Price", "tmp")
                m.rename_metric("PlusOne", "Price")
                m.rename_metric("tmp", "PlusOne")
        for label, change in steps + [("swap", swap)]:
            change()
            reader.model.eval_log.clear()
            self.assertTrue(self.journals.journal().catch_up(reader.model), label)
            reader.recalc()
            self.assertEqual(list(reader.model.eval_log), [], label)
            self.assertEqual(definitions(reader), definitions(m), label)
        self.assertEqual(reader.metric(hidden).name, "__override__Reward")
        reopened = self.reopen()
        check_same_state(self, m, reopened)
        reopened.model.journal = None  # self.m keeps the write right
        reopened.rename_metric("Reward", "Prize")
        self.assertEqual(reopened.metric(hidden).name, "__override__Prize")

    def test_structural_records_catch_up_incrementally(self):
        """A reader that catches up on a structural record calculates only the affected range, as the writer did.
        The values are the same as on the writer and after a new open (a full recalculation)."""
        m = self.m
        m.add_formula("OnlyA", [], 'Price[SELECT: Product."A"]')
        reader = Named(self.journals.journal().open(self.engine()))
        reader.recalc()

        def txn(*changes):
            def run():
                with m.transaction():
                    for change in changes:
                        change()
            return run
        product, employee = "DeptSalary", "Total"  # a Metric that a change of the other dimension does not touch
        steps = [
            ("add member", product, lambda: m.add_member("Product", "E", Category="X")),
            ("add member and cells", employee,
             txn(lambda: m.add_member("Employee", "e5", Department="Eng"), lambda: m.set_cell("Salary", 50, Employee="e5"))),
            ("property value", product, lambda: m.set_property_values("Product", "Category", {"A": "Y"})),
            ("unset property value", product, lambda: m.set_property_values("Product", "Category", {"A": None})),
            ("remove member", product, lambda: m.remove_member("Product", "B")),
            ("remove member of another dimension", employee, lambda: m.remove_member("Employee", "e2")),
            ("formula", product, lambda: m.add_formula("Plus1", ["Product"], "Price + 2", id=m.metric("Plus1").id)),
            ("new Metrics", product, txn(lambda: m.add_input("Discount", ["Product"], {("C",): 1.0}),
                                         lambda: m.add_formula("Net", ["Product"], "Price - Discount"))),
            ("remove Metric", product, lambda: m.remove_metric("Net")),
            ("new dimension", product, txn(lambda: m.add_dimension("Store", ["s1", "s2"]),
                                           lambda: m.add_input("Visits", ["Store"], {("s1",): 3.0}),
                                           lambda: m.add_formula("AllVisits", [], "Visits[REMOVE SUM: Store]"))),
            ("names move to new members", product,
             txn(lambda: m.remove_member("Product", "C"), lambda: m.rename_member("Product", "D", "C"),
                 lambda: m.add_member("Product", "D", Category="Y"), lambda: m.set_cell("Price", 7, Product="D"))),
        ]
        for label, untouched, change in steps:
            change()
            reader.model.eval_log.clear()
            self.assertTrue(self.journals.journal().catch_up(reader.model), label)
            reader.recalc()
            self.assertNotIn(untouched, list(reader.eval_log), label)
            check_same_state(self, m, reader)
            check_same_state(self, reader, self.reopen())

        # The writer changes the formula first so that the member can go: the reader calculates everything again
        txn(lambda: m.add_formula("OnlyA", [], 'Price[SELECT: Product."D"]', id=m.metric("OnlyA").id),
            lambda: m.remove_member("Product", "A"))()
        reader.model.eval_log.clear()
        self.assertTrue(self.journals.journal().catch_up(reader.model))
        reader.recalc()
        self.assertIn("DeptSalary", list(reader.eval_log))
        check_same_state(self, m, reader)

    def test_removed_metric_of_a_scan_catches_up(self):
        """A writer breaks a scan of two Metrics and removes one of them, in one record or in two. The writer and
        a reader that catches up on all of it before a recalc get the values of a full recalculation."""
        m = self.m
        reader = Named(self.journals.journal().open(self.engine()))
        for k, (in_txn, recalc) in enumerate(itertools.product([True, False], repeat=2)):
            label, a, b = f"transaction={in_txn} recalc={recalc}", f"A{k}", f"B{k}"
            m.add_input(b, ["Product", "Month"])
            m.add_formula(a, ["Product", "Month"], f"{b}[SELECT: Month - 1] + Cost")
            m.add_formula(b, ["Product", "Month"], f"{a} * 0.5", id=m.metric(b).id)
            m.recalc()
            self.journals.journal().catch_up(reader.model)
            reader.recalc()
            with m.transaction() if in_txn else contextlib.nullcontext():
                m.add_formula(a, ["Product", "Month"], "Cost", id=m.metric(a).id)
                if recalc:
                    m.recalc()
                m.remove_metric(b)
            m.recalc()
            self.assertTrue(self.journals.journal().catch_up(reader.model), label)
            reader.recalc()
            check_same_state(self, m, reader)
            check_same_state(self, reader, self.reopen())

    def test_survivor_of_a_dropped_scan_step_aggregates_incrementally(self):
        """Agg sums Prev, Prev reads the previous Month of Back, and Back reads Agg. The writer breaks the scan at
        Prev and removes Back before a recalc. Agg left the scan, so the writer and a reader that catches up
        aggregate it incrementally after a later edit."""
        m = self.m
        reader = Named(self.journals.journal().open(self.engine()))
        dims = ["Product", "Region", "Month"]
        m.add_input("Back", dims)
        m.add_input("Prev", dims)
        m.add_formula("Agg", ["Product", "Month"], "Prev[REMOVE SUM: Region]")
        m.add_formula("Prev", dims, "Back[SELECT: Month - 1] + Volume", id=m.metric("Prev").id)
        m.add_formula("Back", dims, "Agg[EXPAND: Region]", id=m.metric("Back").id)
        m.recalc()
        self.journals.journal().catch_up(reader.model)
        reader.recalc()
        with m.transaction():
            m.add_formula("Prev", dims, "Volume", id=m.metric("Prev").id)
            m.remove_metric("Back")
        m.recalc()
        self.assertTrue(self.journals.journal().catch_up(reader.model))
        reader.recalc()
        m.delta_log.clear()
        reader.delta_log.clear()
        m.set_cell("Volume", 9, Product="A", Region="N", Month="Feb")
        m.recalc()
        self.assertTrue(self.journals.journal().catch_up(reader.model))
        reader.recalc()
        self.assertIn("Agg", m.delta_log)
        self.assertIn("Agg", reader.delta_log)
        check_same_state(self, m, reader)
        check_full(self, m)
        check_full(self, reader)

    def test_renames_and_a_formula_in_one_transaction_replay(self):
        """A replay defines the objects by id. The order of the renames in a record does not matter: two Metrics
        change names with each other, and a formula uses the new names of a dimension, a property and a member."""
        m = self.m
        with m.transaction() as txn:
            m.rename_metric("Price", "tmp")
            m.rename_metric("Plus1", "Price")
            m.rename_metric("tmp", "Plus1")
            m.rename_dimension("Product", "Item")
            m.rename_property("Item", "Category", "Group")
            m.rename_member("Item", "A", "Alpha")
            m.add_formula("Twice", ["Item"], "Price * 2")
            m.add_formula("AlphaCost", ["Month"], 'Cost[SELECT: Item."Alpha"]')
            m.add_formula("CatPrice", ["Category"], "Plus1[BY SUM: Item.Group]")
        ch = txn.record["changes"]
        item = m.dimension("Item")
        self.assertEqual(sorted(ch["renamed"]), sorted(
            [[item.id, "Item"], [item.id_of("Alpha"), "Alpha"], [m.property_id("Item", "Group"), "Group"]]
            + [[m.metric(x).id, x] for x in ("Price", "Plus1")]))
        self.assertNotIn("properties", ch)
        self.assertNotIn("members", ch)
        defs = {d["name"]: d for d in ch["metrics"]}
        self.assertEqual(defs["Twice"]["formula"],  # the formula is the id AST as a JSON tree, not text
                         {"node": "BinOp", "op": "*", "left": {"node": "Ref", "name": m.metric("Price").id},
                          "right": {"node": "Const", "value": 2.0}})
        self.assertEqual(defs["CatPrice"]["formula"]["dim"], item.id)
        self.assertEqual(defs["CatPrice"]["formula"]["prop"], m.property_id("Item", "Group"))
        self.assertEqual(defs["AlphaCost"]["formula"]["member"], item.id_of("Alpha"))
        reopened = self.reopen()
        check_same_state(self, m, reopened)
        self.assertEqual(reopened.get("Twice", Item="Alpha"), 22)  # (10 + 1) * 2
        self.assertEqual(reopened.get("AlphaCost", Month="Jan"), 7)
        self.assertEqual(reopened.get("CatPrice", Category="X"), 30)
        self.assertEqual(to_formula(reopened.metric("CatPrice").written, reopened), "Plus1[BY SUM: Item.Group]")


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
        p = rng.choice(m0.dimension("Product").members)
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
        make(tmp).start(m.model)
        follower = Named(make(tmp).open(engine()))  # catches up incrementally, as a standby does
        follower.recalc()
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
                        check_same_state(test, m, Named(make(tmp).open(e())))
                with test.subTest(round=round_, follower=True):
                    make(tmp).catch_up(follower.model)
                    follower.recalc()
                    check_same_state(test, m, follower)


@unittest.skipIf(nanashi_core is None, "nanashi_core が必要")
class RandomReplay(unittest.TestCase):
    def test_reference(self):
        engines = [ReferenceEngine] + ([RustEngine] if RustEngine is not None else [])
        run_random(self, seed=51, rounds=120, engine=ReferenceEngine, reopen_engines=engines)

    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_rust(self):
        run_random(self, seed=53, rounds=120, engine=RustEngine, reopen_engines=[ReferenceEngine, RustEngine])


def many_cells(engine, n: int = 1500) -> Model:
    m = Named(Model(engine=engine))
    m.add_dimension("K", [f"k{i}" for i in range(n)])
    m.add_dimension("T", ["t0", "t1"], ordered=True)
    m.add_input("V", ["K", "T"], {(f"k{i}", "t0"): float(i) for i in range(n)})
    m.add_formula("Sum", ["T"], "V[REMOVE SUM: K]")
    m.recalc()
    return m


def check_kind_change_in_files(test, engine, make) -> None:
    """Redefine an input from number to member kind (same dims) as a large change, then reopen.
    make() gives a new journal object on the same location."""
    m = Named(Model(engine=engine()))
    m.add_dimension("K", ["k0", "k1", "k2", "k3"])
    m.add_dimension("E", ["e0", "e1"])
    m.add_input("V", ["K"], {("k0",): 1.0, ("k1",): 2.0, ("k2",): 3.0, ("k3",): 4.0})
    make().start(m.model)
    cells = {("k0",): "e0", ("k1",): "e1", ("k2",): "e0", ("k3",): "e1"}
    m.add_input("V", ["K"], cells, kind="member:E", id=m.metric("V").id)
    for reopen in [ReferenceEngine] + ([RustEngine] if RustEngine is not None else []):
        r = Named(make().open(reopen()))
        test.assertEqual(r.metric("V").kind, "member:" + r.dimension("E").id)
        test.assertEqual(r.value("V").cells, cells)
    # A number is not a value of the new kind, so the history keeps no old value
    history = m.cell_history(make(), "V", K="k1")
    test.assertEqual([(h["old"], h["new"]) for h in history], [(None, "e1")])


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class Blocks(unittest.TestCase):
    """Rust のエンジンで多くのセルを書き換えた記録は、行の列でなく変更の塊で持つ。"""

    def test_large_changes_are_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = many_cells(RustEngine())
            FileJournal(tmp, fsync=False).start(m.model)
            with m.transaction(user="etl") as txn:
                m.spread("V", 3000.0, how="even")  # 1500 セルすべてを 2 に
            (c,) = txn.record["changes"]["cells"]
            self.assertNotIsInstance(c["rows"], list)
            self.assertEqual(len(c["rows"]), 1499)  # k2 はもともと 2
            k, t = m.dimension("K"), m.dimension("T")
            self.assertEqual(c["dims"], [k.id, t.id])  # the record names a dimension and a member by its id
            rows = list(c["rows"])
            self.assertIn([[k.ids[0], t.ids[0]], 0.0, 2.0], rows)
            self.assertIn([[k.ids[1], t.ids[0]], 1.0, 2.0], rows)
            for e in (ReferenceEngine, RustEngine):  # ファイルには行として書く
                check_same_state(self, m, Named(FileJournal(tmp).open(e())))
            history = m.cell_history(FileJournal(tmp), "V", K="k5", T="t0")
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
            FileJournal(tmp).start(m.model)
            with m.transaction(user="etl"):
                m.spread("V", 24_000.0, how="even")
            m.set_cell("V", 1.0, K="k7", T="t1")  # 少ないセルは、これまでどおり行に書く
            (f,) = (Path(tmp) / "cells").glob("*.parquet")
            self.assertTrue(f.name.endswith(f"-{m.metric('V').id}.parquet"))
            lines = FileJournal(tmp).log_path.read_text().splitlines()
            self.assertLess(len(lines[0]), 1000)  # 記録の行には、ファイルの名前とハッシュだけ
            self.assertIn('"cells":[', lines[1])
            for e in (ReferenceEngine, RustEngine):
                check_same_state(self, m, Named(FileJournal(tmp).open(e())))
            history = m.cell_history(FileJournal(tmp), "V", K="k5", T="t0")  # 変更の塊を Rust で探す
            self.assertEqual([(h["user"], h["old"], h["new"]) for h in history], [("etl", 5.0, 2.0)])
            history = m.cell_history(FileJournal(tmp), "V", K="k7", T="t1")
            self.assertEqual([(h["old"], h["new"]) for h in history], [(None, 1.0)])

    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_corrupted_cell_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = many_cells(RustEngine())
            FileJournal(tmp, bulk_cells=1000).start(m.model)
            m.spread("V", 3000.0, how="even")
            (f,) = (Path(tmp) / "cells").glob("*.parquet")
            f.write_bytes(f.read_bytes()[:-10])
            with self.assertRaisesRegex(ValueError, "壊れている"):
                Named(FileJournal(tmp).open(RustEngine()))

    def test_kind_change_goes_to_parquet(self):
        for e in [ReferenceEngine] + ([RustEngine] if RustEngine is not None else []):
            with tempfile.TemporaryDirectory() as tmp:
                check_kind_change_in_files(self, e, lambda: FileJournal(tmp, fsync=False, bulk_cells=3))

    def test_moved_directory_still_opens(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = build_with(ReferenceEngine())
            FileJournal(Path(tmp) / "a", bulk_cells=3).start(m.model)
            m.spread("Cost", 100, Product="C")
            (Path(tmp) / "a").rename(Path(tmp) / "b")  # ファイルの名前は記録先のディレクトリからの相対
            check_same_state(self, m, Named(FileJournal(Path(tmp) / "b").open(ReferenceEngine())))


if __name__ == "__main__":
    unittest.main()
