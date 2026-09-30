"""変わらない ID と、Metric の削除と名前の変更。"""
import json
import tempfile
import unittest
from pathlib import Path

from examples.fpa import build as build_fpa
from sparse_engine import Model, to_formula
from sparse_engine.engine import ReferenceEngine

from .test_engines import build_with
from .test_incremental import same, snapshot

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None

try:
    import numpy  # noqa: F401  保存形式に使う
except ImportError:
    numpy = None


def all_ids(m: Model) -> list[int]:
    ids = [d.id for d in m.dimensions.values()] + [x.id for x in m.metrics.values()]
    for d in m.dimensions.values():
        ids += d.ids
    return ids


def check_full(test, m: Model) -> None:
    incremental = snapshot(m)
    m._invalidate()
    full = snapshot(m)
    for name in m.metrics:
        test.assertTrue(same(incremental[name], full[name]), f"{name}\n差分: {incremental[name]}\n全体: {full[name]}")


class Ids(unittest.TestCase):
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        self.m = build_with(self.engine())
        self.m.recalc()

    def test_unique_in_the_model(self):
        ids = all_ids(self.m)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn(0, ids)

    def test_members_keep_their_id(self):
        d = self.m.dimensions["Product"]
        a = d.id_of("A")
        self.m.rename_member("Product", "A", "Alpha")
        self.assertEqual(d.id_of("Alpha"), a)
        self.assertEqual(d.member_of(a), "Alpha")
        c = d.id_of("C")
        self.m.remove_member("Product", "B")  # 後ろのメンバーの位置は詰まるが、ID は変わらない
        self.assertEqual(d.id_of("C"), c)
        self.assertEqual(d.member_of(c), "C")

    def test_removed_ids_are_not_reused(self):
        d = self.m.dimensions["Product"]
        b = d.id_of("B")
        self.m.remove_member("Product", "B")
        with self.assertRaisesRegex(ValueError, "ID"):
            d.member_of(b)
        self.m.add_member("Product", "B")
        self.assertNotEqual(d.id_of("B"), b)
        ids = all_ids(self.m)
        self.assertEqual(len(ids), len(set(ids)))

    def test_metrics_keep_their_id(self):
        margin = self.m.metrics["Margin"].id
        self.m.add_formula("Margin", ["Product", "Month"], "Revenue[REMOVE SUM: Region] - Cost * 2")
        self.assertEqual(self.m.metrics["Margin"].id, margin)
        price = self.m.metrics["Price"].id
        self.m.add_input("Price", ["Product"], {("A",): 1})
        self.assertEqual(self.m.metrics["Price"].id, price)
        self.m.rename_metric("Margin", "Profit")
        self.assertEqual(self.m.metrics["Profit"].id, margin)
        self.assertEqual(self.m.metric_name(margin), "Profit")

    def test_fork_keeps_ids(self):
        fork = self.m.fork()
        self.assertEqual(all_ids(fork), all_ids(self.m))


class RemoveMetric(unittest.TestCase):
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        self.m = build_with(self.engine())
        self.m.recalc()

    def test_leaf(self):
        before = snapshot(self.m)
        self.m.eval_log.clear()
        self.m.remove_metric("CatShare")
        self.m.recalc()
        self.assertEqual(self.m.eval_log, [])  # 誰も参照していないので、何も計算し直さない
        self.assertNotIn("CatShare", self.m.metrics)
        del before["CatShare"]
        self.assertEqual(snapshot(self.m), before)
        self.m.set_cell("Price", 7, Product="A")
        check_full(self, self.m)

    def test_referenced(self):
        with self.assertRaisesRegex(ValueError, "Margin は .*Adjusted.* の式が参照している"):
            self.m.remove_metric("Margin")
        self.assertIn("Margin", self.m.metrics)

    def test_mapping_of_a_dynamic_hierarchy_is_referenced(self):
        m = build_fpa(self.engine(), employees=8, products=4, months=6, seed=2)
        with self.assertRaisesRegex(ValueError, "Payroll"):
            m.remove_metric("DeptOf")

    def test_input_then_readd(self):
        old = self.m.metrics["Flag"].id
        self.m.remove_metric("Picked")
        self.m.remove_metric("Missing")
        self.m.remove_metric("Flag")
        self.m.add_input("Flag", ["Product"], {("C",): True}, kind="boolean")
        self.assertNotEqual(self.m.metrics["Flag"].id, old)
        self.m.add_formula("Picked", ["Product", "Month"], "Margin[FILTER: Flag]")
        self.assertEqual(dict(self.m.value("Picked").cells), {("C", "Jan"): 40.0})
        check_full(self, self.m)

    def test_overrides_go_with_the_metric(self):
        self.m.add_formula("Bonus", ["Product"], "Price * 0.1", overridable=True)
        self.m.set_cell("Bonus", 9, Product="A")
        with self.assertRaisesRegex(ValueError, "隠し入力"):
            self.m.remove_metric("__override__Bonus")
        self.m.remove_metric("Bonus")
        self.assertNotIn("__override__Bonus", self.m.metrics)
        check_full(self, self.m)

    def test_unknown(self):
        with self.assertRaisesRegex(ValueError, "がない"):
            self.m.remove_metric("Nope")


class RenameMetric(unittest.TestCase):
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        self.m = build_with(self.engine())
        self.m.recalc()

    def test_formulas_follow(self):
        before = snapshot(self.m)
        self.m.eval_log.clear()
        self.m.rename_metric("Margin", "Profit")
        self.m.recalc()
        self.assertEqual(self.m.eval_log, [])  # 値は変わらないので、何も計算し直さない
        self.assertEqual(to_formula(self.m.metrics["Picked"].written), "Profit[FILTER: Flag]")
        after = snapshot(self.m)
        before["Profit"] = before.pop("Margin")
        self.assertEqual(after, before)
        self.m.set_cell("Cost", 1, Product="A", Month="Jan")
        self.assertEqual(self.m.get("Picked", Product="A", Month="Jan"), 30 - 1)
        check_full(self, self.m)

    def test_delta_aggregation_keeps_working(self):
        self.m.rename_metric("Salary", "Pay")
        self.m.rename_metric("DeptSalary", "DeptPay")
        self.m.delta_log.clear()
        self.m.set_cell("Pay", 50, Employee="e2")
        self.assertEqual(self.m.get("DeptPay", Department="Sales"), 150)
        self.assertIn("DeptPay", self.m.delta_log)
        check_full(self, self.m)

    def test_scan_members(self):
        self.m.rename_metric("Stock", "Inventory")
        self.m.set_cell("Cost", 100, Product="A", Month="Feb")
        self.assertIn("Inventory", to_formula(self.m.metrics["Outflow"].written))
        check_full(self, self.m)

    def test_overrides_follow(self):
        self.m.add_formula("Bonus", ["Product"], "Price * 0.1", overridable=True)
        self.m.set_cell("Bonus", 9, Product="A")
        self.m.rename_metric("Bonus", "Reward")
        self.assertIn("__override__Reward", self.m.metrics)
        self.assertNotIn("__override__Bonus", self.m.metrics)
        self.assertEqual(self.m.get("Reward", Product="A"), 9)
        self.m.set_cell("Reward", None, Product="A")
        self.assertEqual(self.m.get("Reward", Product="A"), 1)
        check_full(self, self.m)

    def test_dynamic_hierarchy_mapping(self):
        m = build_fpa(self.engine(), employees=8, products=4, months=6, seed=2)
        m.recalc()
        m.rename_metric("DeptOf", "Assignment")
        self.assertIn("Employee.Assignment", to_formula(m.metrics["Payroll"].written))
        e, t = m.dimensions["Employee"].members[0], m.dimensions["Month"].members[2]
        m.set_cell("Assignment", "管理", Employee=e, Month=t)
        check_full(self, m)

    def test_errors(self):
        with self.assertRaisesRegex(ValueError, "同じ名前の Metric"):
            self.m.rename_metric("Margin", "Revenue")
        with self.assertRaisesRegex(ValueError, "同じ名前の軸"):
            self.m.rename_metric("Margin", "Month")
        with self.assertRaisesRegex(ValueError, "__"):
            self.m.rename_metric("Margin", "__x")


@unittest.skipIf(numpy is None, "numpy が必要")
class Storage(unittest.TestCase):
    engine = staticmethod(ReferenceEngine)

    def test_ids_survive_save_and_load(self):
        m = build_with(self.engine())
        m.remove_member("Product", "B")
        m.add_member("Product", "E")
        m.rename_metric("Margin", "Profit")
        with tempfile.TemporaryDirectory() as tmp:
            m.save(tmp)
            loaded = Model.load(tmp, self.engine())
        self.assertEqual(all_ids(loaded), all_ids(m))
        a, b = snapshot(m), snapshot(loaded)
        for name in a:
            self.assertTrue(same(a[name], b[name]), name)
        loaded.add_member("Product", "F")  # 読み込んだあとも、使った ID を振らない
        self.assertEqual(len(set(all_ids(loaded))), len(all_ids(loaded)))

    def test_reads_format_1(self):
        m = build_with(self.engine())
        with tempfile.TemporaryDirectory() as tmp:
            m.save(tmp)
            path = Path(tmp) / "model.json"
            meta = json.loads(path.read_text())
            meta["format"] = 1
            del meta["next_id"]
            for d in meta["dimensions"]:
                del d["id"], d["member_ids"]
            for x in meta["metrics"]:
                del x["id"]
            path.write_text(json.dumps(meta))
            loaded = Model.load(tmp, self.engine())
        ids = all_ids(loaded)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(snapshot(loaded).keys(), snapshot(m).keys())


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustIds(Ids):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustRemoveMetric(RemoveMetric):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustRenameMetric(RenameMetric):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


@unittest.skipIf(RustEngine is None or numpy is None, "nanashi_core と numpy が必要")
class RustStorage(Storage):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


if __name__ == "__main__":
    unittest.main()
