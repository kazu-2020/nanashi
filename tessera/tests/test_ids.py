"""変わらない ID と、Metric の削除と名前の変更。"""
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from examples.fpa import build as build_fpa
from sparse_engine import FormulaError, Model, Named, to_formula
from sparse_engine.engine import ReferenceEngine
from sparse_engine.model import _UNSET, DuplicateId, uuid7

from .test_engines import build_with
from .test_incremental import check_full, mapping, same, snapshot

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None

try:
    import nanashi_core  # noqa: F401  保存形式（Parquet）の読み書きに使う
except ImportError:
    nanashi_core = None


def all_ids(m: Model) -> list[str]:
    """The UUIDs of the dimensions, the Metrics and the members."""
    ids = [d.id for d in m.dimensions.values()] + [x.id for x in m.metrics.values()]
    for d in m.dimensions.values():
        ids += list(d.ids)
    return ids


class Ids(unittest.TestCase):
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        self.m = build_with(self.engine())
        self.m.recalc()

    def test_unique_in_the_model(self):
        ids = all_ids(self.m)
        self.assertEqual(len(ids), len(set(ids)))

    def test_members_keep_their_id(self):
        d = self.m.dimension("Product")
        a = d.id_of("A")
        self.m.rename_member("Product", "A", "Alpha")
        self.assertEqual(d.id_of("Alpha"), a)
        self.assertEqual(d.member_of(a), "Alpha")
        c = d.id_of("C")
        self.m.remove_member("Product", "B")  # 後ろのメンバーの位置は詰まるが、ID は変わらない
        self.assertEqual(d.id_of("C"), c)
        self.assertEqual(d.member_of(c), "C")

    def test_removed_ids_are_not_reused(self):
        d = self.m.dimension("Product")
        b = d.id_of("B")
        self.m.remove_member("Product", "B")
        with self.assertRaisesRegex(ValueError, "ID"):
            d.member_of(b)
        self.m.add_member("Product", "B")
        self.assertNotEqual(d.id_of("B"), b)
        ids = all_ids(self.m)
        self.assertEqual(len(ids), len(set(ids)))

    def test_per_metric_state_stays_with_the_id(self):
        m = build_with(ReferenceEngine())
        m.add_formula("Double", ["Product", "Month"], "Margin * 2", overridable=True)
        m.recalc()
        fields = lambda id: {f for f in m._state[id].__slots__ if getattr(m._state[id], f) is not _UNSET}
        margin = m.metric("Margin").id
        before = fields(margin)
        m.rename_metric("Margin", "Profit")
        self.assertEqual(fields(margin), before)  # the stored data, plan, partition and estimates stay under the id
        self.assertIs(m.metric("Profit"), m.metrics[margin])
        double, hidden = m.metric("Double").id, m.metric("__override__Double").id
        m.remove_metric("Double")
        self.assertFalse({double, hidden} & set(m._state))
        self.assertEqual(set(m._state), set(m.metrics))
        check_full = snapshot(m)
        m._invalidate()
        self.assertEqual(snapshot(m), check_full)

    def test_metrics_keep_their_id(self):
        margin = self.m.metric("Margin").id
        self.m.add_formula("Margin", ["Product", "Month"], "Revenue[REMOVE SUM: Region] - Cost * 2", id=self.m.metric("Margin").id)
        self.assertEqual(self.m.metric("Margin").id, margin)
        price = self.m.metric("Price").id
        self.m.add_input("Price", ["Product"], {("A",): 1}, id=self.m.metric("Price").id)
        self.assertEqual(self.m.metric("Price").id, price)
        self.m.rename_metric("Margin", "Profit")
        self.assertEqual(self.m.metric("Profit").id, margin)
        self.assertEqual(self.m.metric(margin).name, "Profit")

    def test_fork_keeps_ids(self):
        fork = self.m.fork()
        self.assertEqual(all_ids(fork), all_ids(self.m))

    def test_metrics_are_keyed_by_uuid(self):
        m = self.m
        for key, x in m.metrics.items():
            self.assertEqual(key, x.id)
            self.assertEqual(uuid.UUID(key).version, 7)
        margin = m.metric("Margin")
        self.assertIs(m.metric(margin.id), margin)
        self.assertIs(m.metric("Margin"), margin)  # the facade takes a name first, then an id
        with self.assertRaisesRegex(ValueError, "がない"):
            m.metric("Nope")
        # a name in the UUID form is allowed: the Model API takes ids only, so it cannot collide (IdApi)


class Uuids(unittest.TestCase):
    """The external UUIDs (docs/ids.md): one for each dimension, member, property, and Metric."""
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        self.m = build_with(self.engine())
        self.m.recalc()

    def test_every_object_has_a_uuid(self):
        m = self.m
        uuids = all_ids(m) + [p for d in m.dimensions.values() for p in d.properties]
        self.assertEqual(len(set(uuids)), len(uuids))
        for u in uuids:
            self.assertEqual(uuid.UUID(u).version, 7)
        self.assertEqual(m.member_id("Product", "A"), m.dimension("Product").id_of("A"))
        self.assertEqual(m.dimension("Product").property_names[m.property_id("Product", "Category")], "Category")

    def test_uuid7_is_ordered_by_time(self):
        a, b = uuid7(), uuid7()
        self.assertEqual((uuid.UUID(a).version, uuid.UUID(a).variant), (7, uuid.RFC_4122))
        self.assertLessEqual(a[:13], b[:13])  # the time part
        self.assertNotEqual(a, b)

    def test_redefine_by_uuid_can_rename(self):
        m = self.m
        margin = m.metric("Margin").id
        m.add_formula("Profit", ["Product", "Month"], "Revenue[REMOVE SUM: Region] - Cost * 2", id=margin)
        self.assertNotIn("Margin", m._metric_ids)
        self.assertEqual(m.metric("Profit").id, margin)
        self.assertEqual(to_formula(m.metric("Picked").written, m), "Profit[FILTER: Flag]")
        check_full(self, m)
        m.add_input("Profit", ["Product", "Month"], {("A", "Jan"): 1.0}, id=margin)  # a formula becomes an input
        self.assertEqual(m.metric("Profit").id, margin)
        a = m.member_id("Product", "A")
        m.add_member("Product", "Alpha", id=a, at=2, Category="Y")
        self.assertEqual((m.dimension("Product").in_order()[2], m.member_id("Product", "Alpha")), ("Alpha", a))
        self.assertEqual(mapping(m, "Product", "Category")["Alpha"], "Y")
        self.assertIs(m.add_dimension("Product", [], id=m.dimension_id("Product")), m.dimension("Product"))
        check_full(self, m)

    def test_same_name_with_a_different_uuid_is_an_error(self):
        m = self.m
        with self.assertRaisesRegex(ValueError, "別の ID"):
            m.add_formula("Margin", ["Product", "Month"], "Cost * 2")
        with self.assertRaisesRegex(ValueError, "別の ID"):
            m.add_input("Price", ["Product"], id="other")
        with self.assertRaisesRegex(ValueError, "別の ID"):
            m.add_member("Product", "A", id="other")
        with self.assertRaisesRegex(ValueError, "別の ID"):
            m.add_property("Product", "Category", "Category", {}, id="other")
        with self.assertRaisesRegex(ValueError, "同じ名前の軸"):
            m.add_dimension("Product", [], id="other")
        self.assertNotIn("other", all_ids(m))

    def test_uuid_of_another_kind_or_a_tombstone_is_duplicate_id(self):
        m = self.m
        margin, b = m.metric("Margin").id, m.member_id("Product", "B")
        with self.assertRaises(DuplicateId):
            m.add_member("Product", "X", id=margin)
        with self.assertRaises(DuplicateId):
            m.add_dimension("X", [], id=b)
        with self.assertRaises(DuplicateId):
            m.add_formula("X", ["Product"], "Price", id=m.dimension_id("Product"))
        with self.assertRaises(DuplicateId):
            m.add_property("Product", "X", "Category", {}, id=b)
        share = m.metric("CatShare").id
        m.remove_member("Product", "B")
        m.remove_metric("CatShare")
        self.assertEqual(m.tombstones, {b, share})
        self.assertNotIn(b, all_ids(m))
        self.assertNotIn(share, all_ids(m))
        for fn in (lambda: m.add_member("Product", "B", id=b), lambda: m.add_input("B", [], id=b),
                   lambda: m.add_dimension("B", [], id=b), lambda: m.add_property("Product", "B", "Category", {}, id=b)):
            with self.assertRaises(DuplicateId):
                fn()
        m.add_member("Product", "B")  # a new UUID is fine
        self.assertNotEqual(m.member_id("Product", "B"), b)
        self.assertEqual(m.tombstones, {b, share})

    def test_removed_override_input_is_a_tombstone_too(self):
        m = self.m
        m.add_formula("Bonus", ["Product"], "Price * 0.1", overridable=True, id="bonus")
        hidden = m.metric("__override__Bonus").id
        m.remove_metric("Bonus")
        self.assertLessEqual({"bonus", hidden}, m.tombstones)

    def test_fork_keeps_uuids_and_tombstones(self):
        m = self.m
        m.remove_member("Product", "B")
        fork = m.fork()
        self.assertEqual((all_ids(fork), fork.tombstones), (all_ids(m), m.tombstones))
        fork.add_member("Product", "E", id="e")
        self.assertNotIn("e", all_ids(m))  # the fork and the original do not share the maps
        with self.assertRaises(DuplicateId):
            fork.add_member("Product", "B", id=next(iter(m.tombstones)))

    def test_transaction_rollback_restores_the_maps(self):
        m = self.m
        before = (all_ids(m), set(m.tombstones))
        with self.assertRaises(ValueError):
            with m.transaction():
                m.remove_member("Product", "B")
                m.add_member("Product", "E", id="e")
                raise ValueError("abort")
        self.assertEqual((all_ids(m), m.tombstones), before)


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
        self.assertEqual(list(self.m.eval_log), [])  # 誰も参照していないので、何も計算し直さない
        self.assertNotIn("CatShare", self.m._metric_ids)
        del before["CatShare"]
        self.assertEqual(snapshot(self.m), before)
        self.m.set_cell("Price", 7, Product="A")
        check_full(self, self.m)

    def test_referenced(self):
        with self.assertRaisesRegex(ValueError, "Margin は .*Adjusted.* の式が参照している"):
            self.m.remove_metric("Margin")
        self.assertIn("Margin", self.m._metric_ids)

    def test_mapping_of_a_dynamic_hierarchy_is_referenced(self):
        m = build_fpa(self.engine(), employees=8, products=4, months=6, seed=2)
        with self.assertRaisesRegex(ValueError, "Payroll"):
            m.remove_metric("DeptOf")

    def test_input_then_readd(self):
        old = self.m.metric("Flag").id
        self.m.remove_metric("Picked")
        self.m.remove_metric("Missing")
        self.m.remove_metric("Flag")
        self.m.add_input("Flag", ["Product"], {("C",): True}, kind="boolean")
        self.assertNotEqual(self.m.metric("Flag").id, old)
        self.m.add_formula("Picked", ["Product", "Month"], "Margin[FILTER: Flag]")
        self.assertEqual(dict(self.m.value("Picked").cells), {("C", "Jan"): 40.0})
        check_full(self, self.m)

    def test_overrides_go_with_the_metric(self):
        self.m.add_formula("Bonus", ["Product"], "Price * 0.1", overridable=True)
        self.m.set_cell("Bonus", 9, Product="A")
        with self.assertRaisesRegex(ValueError, "隠し入力"):
            self.m.remove_metric("__override__Bonus")
        self.m.remove_metric("Bonus")
        self.assertNotIn("__override__Bonus", self.m._metric_ids)
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
        self.assertEqual(list(self.m.eval_log), [])  # 値は変わらないので、何も計算し直さない
        self.assertEqual(to_formula(self.m.metric("Picked").written, self.m), "Profit[FILTER: Flag]")
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
        self.assertIn("Inventory", to_formula(self.m.metric("Outflow").written, self.m))
        check_full(self, self.m)

    def test_overrides_follow(self):
        self.m.add_formula("Bonus", ["Product"], "Price * 0.1", overridable=True)
        self.m.set_cell("Bonus", 9, Product="A")
        self.m.rename_metric("Bonus", "Reward")
        self.assertIn("__override__Reward", self.m._metric_ids)
        self.assertNotIn("__override__Bonus", self.m._metric_ids)
        self.assertEqual(self.m.get("Reward", Product="A"), 9)
        self.m.set_cell("Reward", None, Product="A")
        self.assertEqual(self.m.get("Reward", Product="A"), 1)
        check_full(self, self.m)

    def test_overrides_follow_a_redefinition(self):
        """A redefinition without overridable keeps the hidden input. The override stops, but the hidden input
        follows a rename and a remove, and overridable=True uses it again."""
        m = self.m
        bonus = m.add_formula("Bonus", ["Product"], "Price * 0.1", overridable=True)
        hidden = m.metric("__override__Bonus").id
        m.set_cell("Bonus", 9, Product="A")
        m.add_formula("Bonus", ["Product"], "Price * 0.1", id=bonus)
        self.assertEqual(m.get("Bonus", Product="A"), 1)
        m.rename_metric("Bonus", "Reward")
        self.assertEqual(m.metric(hidden).name, "__override__Reward")
        m.add_formula("Reward", ["Product"], "Price * 0.1", overridable=True, id=bonus)
        self.assertEqual(m.metric("__override__Reward").id, hidden)
        self.assertEqual(m.get("Reward", Product="A"), 9)
        m.add_input("Reward", ["Product"], id=bonus)
        m.remove_metric("Reward")
        self.assertNotIn(hidden, m.metrics)
        check_full(self, m)

    def test_dynamic_hierarchy_mapping(self):
        m = build_fpa(self.engine(), employees=8, products=4, months=6, seed=2)
        m.recalc()
        m.rename_metric("DeptOf", "Assignment")
        self.assertIn("Employee.Assignment", to_formula(m.metric("Payroll").written, m))
        e, t = m.dimension("Employee").members[0], m.dimension("Month").members[2]
        m.set_cell("Assignment", "管理", Employee=e, Month=t)
        check_full(self, m)

    def test_rename_is_an_attribute_change(self):
        """A rename calculates nothing, not even the changes that wait for the next recalc. Every formula
        shows the new name, and the next read applies the waiting change under the new name."""
        m = self.m
        m.set_cell("Cost", 1, Product="A", Month="Jan")  # a change that waits
        m.eval_log.clear()
        m.slice_log.clear()
        m.rename_metric("Margin", "Profit")
        self.assertEqual(list(m.eval_log), [])
        self.assertEqual(list(m.slice_log), [])
        formulas = {x.name: to_formula(x.written, m) for x in m.metrics.values() if x.written is not None}
        for name, text in formulas.items():
            self.assertNotRegex(text, r"\bMargin\b", name)
        self.assertEqual(formulas["Picked"], "Profit[FILTER: Flag]")
        self.assertEqual(formulas["Stock"], "Stock[SELECT: Month - 1] + Profit - Outflow")
        self.assertIn("Profit", formulas["Adjusted"])
        self.assertIn("Profit", formulas["CatShare"])
        self.assertEqual(m.get("Picked", Product="A", Month="Jan"), 30 - 1)
        self.assertIn("Profit", m.eval_log)  # the log shows the current name
        check_full(self, m)

    def test_logs_show_current_names(self):
        m = self.m
        m.set_cell("Salary", 50, Employee="e2")
        m.recalc()
        self.assertIn("DeptSalary", m.eval_log)
        self.assertIn("DeptSalary", m.delta_log)
        m.rename_metric("DeptSalary", "DeptPay")
        self.assertIn("DeptPay", m.eval_log)
        self.assertNotIn("DeptSalary", m.eval_log)
        self.assertIn("DeptPay", m.delta_log)
        self.assertIn("DeptPay", [n for n, _ in m.slice_log])
        self.assertEqual(m.eval_log.count("DeptPay"), list(m.eval_log).count("DeptPay"))

    def test_rename_of_a_metric_by_target_and_of_an_overridable_source(self):
        """The Metric of a Metric BY (`[BY: Employee.DeptOf]`) and the source of an overridable formula
        follow a rename without a recalculation, while a change waits."""
        m = build_fpa(self.engine(), employees=8, products=4, months=6, seed=2)
        m.add_formula("Bonus", ["Employee", "Version"], "Salary * 0.1", overridable=True)
        e, t = m.dimension("Employee").members[0], m.dimension("Month").members[2]
        v = m.dimension("Version").members[0]
        m.set_cell("Bonus", 9, Employee=e, Version=v)
        m.recalc()
        m.set_cell("DeptOf", "管理", Employee=e, Month=t)  # a change that waits
        m.eval_log.clear()
        m.rename_metric("DeptOf", "Assignment")
        m.rename_metric("Salary", "Pay")
        self.assertEqual(list(m.eval_log), [])
        self.assertIn("Employee.Assignment", to_formula(m.metric("Payroll").written, m))
        self.assertEqual(to_formula(m.metric("Bonus").written, m), "Pay * 0.1")
        self.assertEqual(m.get("Bonus", Employee=e, Version=v), 9)
        self.assertIn("__override__Bonus", m._metric_ids)
        m.set_cell("Bonus", None, Employee=e, Version=v)
        self.assertEqual(m.get("Bonus", Employee=e, Version=v), m.get("Pay", Employee=e, Version=v) * 0.1)
        check_full(self, m)

    def test_errors(self):
        with self.assertRaisesRegex(ValueError, "同じ名前の Metric"):
            self.m.rename_metric("Margin", "Revenue")
        with self.assertRaisesRegex(ValueError, "同じ名前の軸"):
            self.m.rename_metric("Margin", "Month")
        with self.assertRaisesRegex(ValueError, "__"):
            self.m.rename_metric("Margin", "__x")


class DimensionRenames(unittest.TestCase):
    """rename_dimension and rename_property change only the name: no recalculation, no formula rewrite."""
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        self.m = build_with(self.engine())
        self.m.recalc()

    def test_rename_dimension_is_an_attribute_change(self):
        m = self.m
        before = snapshot(m)
        product = m.dimension_id("Product")
        m.eval_log.clear()
        m.slice_log.clear()
        m.rename_dimension("Product", "Item")
        self.assertEqual(snapshot(m), before)
        self.assertEqual(list(m.eval_log), [])
        self.assertEqual(list(m.slice_log), [])
        self.assertIs(m.dimension("Item"), m.dimension(product))
        self.assertNotIn("Product", m._dim_ids)
        self.assertEqual(m.metric("Revenue").dims, (product, m.dimension_id("Region"), m.dimension_id("Month")))
        self.assertEqual(m.value("Revenue").dims, ("Item", "Region", "Month"))
        formulas = {x.name: to_formula(x.written, m) for x in m.metrics.values() if x.written is not None}
        for name, text in formulas.items():
            self.assertNotRegex(text, r"\bProduct\b", name)
        self.assertEqual(formulas["RevByCat"], "Revenue[BY SUM: Item.Category]")
        self.assertEqual(formulas["Total"], "Stock[REMOVE SUM: Item][REMOVE SUM: Month]")
        m.set_cell("Cost", 1, Item="A", Month="Jan")
        self.assertEqual(m.get("Picked", Item="A", Month="Jan"), 30 - 1)
        self.assertEqual(m.slice("Margin", Item="A").dims, ("Item", "Month"))
        with self.assertRaisesRegex(ValueError, "軸 Product"):
            m.get("Picked", Product="A", Month="Jan")
        check_full(self, m)

    def test_rename_property_is_an_attribute_change(self):
        m = self.m
        before = snapshot(m)
        category = m.property_id("Product", "Category")
        m.eval_log.clear()
        m.rename_property("Product", "Category", "Group")
        self.assertEqual(snapshot(m), before)
        self.assertEqual(list(m.eval_log), [])
        self.assertEqual(m.property_id("Product", "Group"), category)
        self.assertEqual(m.dimension("Product").property_names[category], "Group")
        self.assertEqual(to_formula(m.metric("RevByCat").written, m), "Revenue[BY SUM: Product.Group]")
        m.set_property_values("Product", "Group", {"A": "Y"})
        self.assertEqual(m.get("RevByCat", Category="Y", Region="N", Month="Jan"), 30)
        m.spread("Volume", 100, Month="Jan", where={"Product.Group": "X"})  # X is only B now: 2 empty cells
        self.assertEqual(m.summarize("Volume", Month="Jan").cells[()], 3 + 8 + 100)
        check_full(self, m)

    def test_errors_and_warnings_show_the_new_names(self):
        m = self.m
        m.add_formula("Dense", ["Product", "Month"], "IFBLANK(Cost, 0)")
        m.recalc()
        m.rename_dimension("Product", "Item")
        m.rename_dimension("Month", "Period")
        w = m.warnings[m.metric("Dense").id]
        self.assertEqual(len(w), 1)
        self.assertIn("Item", w[0])
        self.assertNotIn("Product", w[0])
        with self.assertRaises(FormulaError) as cm:  # a type check error (the Rust engine checks in Rust)
            m.add_formula("Bad", ["Item"], "Item < Item")
            m.recalc()
        self.assertEqual((cm.exception.code, cm.exception.params["dim"]), ("unordered_compare", "Item"))
        self.assertIn("Item", str(cm.exception))
        m.remove_metric("Bad")
        with self.assertRaises(FormulaError) as cm:  # a bind error shows the name that the user wrote
            m.add_formula("Bad", ["Item"], "Price[BY: Product.Category]")
        self.assertEqual((cm.exception.code, cm.exception.params), ("unknown_dim", {"name": "Product"}))
        with self.assertRaises(FormulaError) as cm:
            m.add_formula("Bad", ["Item"], "Price[BY: Item.Nope]")
        self.assertEqual(cm.exception.params, {"dim": "Item", "prop": "Nope"})
        check_full(self, m)

    def test_redefine_by_uuid_renames(self):
        m = self.m
        d, category = m.dimension("Product"), m.property_id("Product", "Category")
        self.assertIs(m.add_dimension("Item", [], id=d.id), d)
        self.assertEqual((d.name, m.dimension("Item")), ("Item", d))
        m.add_property("Item", "Group", "Category", {"A": "Y", "B": "X", "C": "X", "D": "Y"}, id=category)
        self.assertEqual(d.property_names[category], "Group")
        self.assertEqual(to_formula(m.metric("RevByCat").written, m), "Revenue[BY SUM: Item.Group]")
        self.assertEqual(m.get("RevByCat", Category="Y", Region="N", Month="Jan"), 30)
        check_full(self, m)

    def test_rename_errors(self):
        m = self.m
        with self.assertRaisesRegex(ValueError, "同じ名前の軸"):
            m.rename_dimension("Product", "Month")
        with self.assertRaisesRegex(ValueError, "同じ名前の Metric"):
            m.rename_dimension("Product", "Price")
        m.add_property("Product", "Other", "Category", {})
        with self.assertRaisesRegex(ValueError, "同じ名前のプロパティ"):
            m.rename_property("Product", "Category", "Other")
        with self.assertRaisesRegex(ValueError, "プロパティ Nope がない"):
            m.rename_property("Product", "Nope", "X")


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustDimensionRenames(DimensionRenames):
    engine = staticmethod(RustEngine)


class MemberRenames(unittest.TestCase):
    """rename_member changes only the name and the name index: no recalculation, and the property maps, the
    stored data and the formulas stay as they are (they hold the member id)."""
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        self.m = build_with(self.engine())
        self.m.add_formula("JanCost", ["Product"], 'Cost[SELECT: Month."Jan"]')
        self.m.recalc()

    def test_rename_is_an_attribute_change(self):
        m = self.m
        product, month = m.dimension("Product"), m.dimension("Month")
        jan, a = m.member_id("Month", "Jan"), m.member_id("Product", "A")
        category = m.property_id("Product", "Category")
        before = snapshot(m)
        props, values = product.properties[category], {i: m._values[i] for i in m._values}
        m.set_cell("Cost", 1, Product="A", Month="Jan")  # a change that waits
        m.eval_log.clear()
        m.slice_log.clear()
        m.rename_member("Month", "Jan", "January")
        m.rename_member("Product", a, "Alpha")  # by id
        self.assertEqual(list(m.eval_log), [])
        self.assertEqual(list(m.slice_log), [])
        self.assertIs(product.properties[category], props)  # the same map object: it holds ids
        for i, v in values.items():
            self.assertIs(m._values[i], v)  # the stored data is not written
        self.assertEqual((m.member_id("Month", "January"), m.member_id("Product", "Alpha")), (jan, a))
        self.assertEqual((month.member_of(jan), product.member_of(a)), ("January", "Alpha"))
        self.assertEqual(to_formula(m.metric("JanCost").written, m), 'Cost[SELECT: Month."January"]')
        self.assertEqual(mapping(m, "Product", "Category")["Alpha"], "X")
        self.assertEqual(m.get("JanCost", Product="Alpha"), 1)  # the waiting change applies under the new name
        self.assertEqual(m.get("Cost", Product="Alpha", Month="January"), 1)
        self.assertIsNone(m.get("Cost", Product="A", Month="Jan"))  # the old names are not members
        with self.assertRaisesRegex(ValueError, "'A' がない"):
            m.set_cell("Cost", 2, Product="A", Month="January")
        with self.assertRaisesRegex(ValueError, "'Jan' がない"):
            m.slice("Cost", Month="Jan")
        self.assertIn("JanCost", m.eval_log)
        m.rename_member("Month", "January", "Jan")
        m.rename_member("Product", "Alpha", "A")
        m.set_cell("Cost", 7, Product="A", Month="Jan")
        self.assertEqual(snapshot(m), before)  # the values are the same under the names from before
        check_full(self, m)

    def test_rename_of_a_property_target(self):
        m = self.m
        category = m.property_id("Product", "Category")
        props = m.dimension("Product").properties[category]
        m.eval_log.clear()
        m.rename_member("Category", "X", "Hard")
        self.assertEqual(list(m.eval_log), [])
        self.assertIs(m.dimension("Product").properties[category], props)
        self.assertEqual(mapping(m, "Product", "Category"), {"A": "Hard", "B": "Hard", "C": "Y", "D": "Y"})
        self.assertEqual(m.get("RevByCat", Category="Hard", Region="N", Month="Jan"),
                         m.summarize("Revenue", Product=["A", "B"], Region="N", Month="Jan").cells[()])
        m.set_cell("Rate", 0.25, Category="Hard")
        self.assertEqual(m.get("Rate", Category="Hard"), 0.25)
        m.set_property_values("Product", "Category", {"C": "Hard"})
        self.assertEqual(mapping(m, "Product", "Category")["C"], "Hard")
        check_full(self, m)

    def test_logs_show_member_names(self):
        m = self.m
        m.slice_log.clear()
        m.set_cell("Cost", 1, Product="A", Month="Jan")
        m.recalc()
        ranges = [r for n, r in m.slice_log if n == "JanCost"]
        self.assertEqual(ranges, [{"Product": frozenset(["A"])}])
        m.rename_member("Product", "A", "Alpha")
        self.assertEqual([r for n, r in m.slice_log if n == "JanCost"], [{"Product": frozenset(["Alpha"])}])

    def test_errors(self):
        m = self.m
        with self.assertRaisesRegex(ValueError, "がない"):
            m.rename_member("Month", "Dec", "December")
        with self.assertRaisesRegex(ValueError, "すでにある"):
            m.rename_member("Month", "Jan", "Feb")
        with self.assertRaisesRegex(FormulaError, "'Dec' がない"):  # a bind error shows the name that the user wrote
            m.add_formula("Bad", ["Product"], 'Cost[SELECT: Month."Dec"]')


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustMemberRenames(MemberRenames):
    engine = staticmethod(RustEngine)


@unittest.skipIf(nanashi_core is None, "nanashi_core が必要")
class Storage(unittest.TestCase):
    engine = staticmethod(ReferenceEngine)

    def test_ids_survive_save_and_load(self):
        m = build_with(self.engine())
        m.remove_member("Product", "B")
        m.add_member("Product", "E")
        m.rename_metric("Margin", "Profit")
        m.rename_dimension("Region", "Area")
        m.rename_property("Product", "Category", "Group")
        with tempfile.TemporaryDirectory() as tmp:
            m.save(tmp)
            meta = json.loads((Path(tmp) / "model.json").read_text())
            loaded = Named.load(tmp, self.engine())
        self.assertNotIn("next_id", meta)
        spec = next(x for x in meta["changes"]["metrics"] if x["name"] == "RevByCat")
        self.assertEqual(spec["formula"]["node"], "By")  # the formula is the id AST, not text
        self.assertEqual(spec["formula"]["prop"], m.property_id("Product", "Group"))
        self.assertEqual(all_ids(loaded), all_ids(m))
        self.assertEqual(loaded.tombstones, m.tombstones)
        self.assertEqual(loaded.dimension("Area").id, m.dimension("Area").id)
        self.assertEqual(loaded.dimension("Product").property_names, m.dimension("Product").property_names)
        self.assertEqual(to_formula(loaded.metric("RevByCat").written, loaded), "Revenue[BY SUM: Product.Group]")
        a, b = snapshot(m), snapshot(loaded)
        for name in a:
            self.assertTrue(same(a[name], b[name]), name)
        loaded.add_member("Product", "F")  # 読み込んだあとも、使った ID を振らない
        self.assertEqual(len(set(all_ids(loaded))), len(all_ids(loaded)))

    def test_member_renames_survive_save_and_load(self):
        m = build_with(self.engine())
        m.add_formula("JanCost", ["Product"], 'Cost[SELECT: Month."Jan"]')
        m.rename_member("Month", "Jan", "January")
        m.rename_member("Category", "X", "Hard")  # a property target
        m.rename_member("Product", "A", "Alpha")
        with tempfile.TemporaryDirectory() as tmp:
            m.save(tmp)
            loaded = Named.load(tmp, self.engine())
        self.assertEqual(all_ids(loaded), all_ids(m))
        self.assertEqual(loaded.dimension("Month").members, m.dimension("Month").members)
        self.assertEqual(to_formula(loaded.metric("JanCost").written, loaded), 'Cost[SELECT: Month."January"]')
        self.assertEqual(mapping(loaded, "Product", "Category"), mapping(m, "Product", "Category"))
        a, b = snapshot(m), snapshot(loaded)
        for name in a:
            self.assertTrue(same(a[name], b[name]), name)


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustIds(Ids):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustUuids(Uuids):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustRemoveMetric(RemoveMetric):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustRenameMetric(RenameMetric):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


@unittest.skipIf(RustEngine is None, "nanashi_core が必要")
class RustStorage(Storage):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


if __name__ == "__main__":
    unittest.main()


class IdApi(unittest.TestCase):
    """The Model API takes ids only. Named is the name facade on it."""

    def setUp(self):
        self.m = build_with(ReferenceEngine())
        self.model = self.m.model
        self.product, self.a = self.m.dimension_id("Product"), self.m.member_id("Product", "A")
        self.price = self.m.metric("Price").id

    def test_rename_to_the_current_name_changes_nothing(self):
        m = Model()
        d = m.add_dimension("Region", ["N"])
        target = m.add_dimension("Zone", ["Z"]).id
        prop = m.add_property(d.id, "In", target, {})
        x = m.add_input("Sales", [d.id])
        m.rename_dimension(d.id, "Region")
        m.rename_property(d.id, prop, "In")
        m.rename_member(d.id, d.ids[0], "N")
        m.rename_metric(x, "Sales")
        self.assertEqual((d.name, d.property_names[prop], d.members, m.metric(x).name), ("Region", "In", ["N"], "Sales"))

    def test_model_rejects_a_name_where_an_id_is_required(self):
        model = self.model
        with self.assertRaisesRegex(ValueError, "Metric Price がない"):
            model.get("Price", {self.product: self.a})
        with self.assertRaisesRegex(ValueError, "軸 Product がない"):
            model.get(self.price, {"Product": self.a})
        self.assertIsNone(model.get(self.price, {self.product: "A"}))  # a name is not a member id: blank
        with self.assertRaisesRegex(ValueError, "Price: Product に 'A' がない"):
            model.set_cell(self.price, 1, {self.product: "A"})
        with self.assertRaisesRegex(ValueError, "Product にメンバー 'A' がない"):
            model.slice(self.price, {self.product: "A"})
        with self.assertRaises(FormulaError):
            model.dimension("Product")
        with self.assertRaisesRegex(ValueError, "Metric Price がない"):
            model.rename_metric("Price", "Cost2")
        with self.assertRaisesRegex(ValueError, "number、boolean、member:<軸の ID>"):
            model.add_input("Pick", [self.product], kind="member:Product")
        with self.assertRaisesRegex(TypeError, "unexpected keyword argument"):
            model.get(self.price, Product=self.a)  # the keyword form is gone

    def test_reads_return_ids(self):
        model, month = self.model, self.m.dimension_id("Month")
        cube = model.value(self.price)
        self.assertEqual(cube.dims, (self.product,))
        self.assertEqual(cube.cells[(self.a,)], 10.0)
        self.assertEqual(model.get(self.price, {self.product: self.a}), 10.0)
        self.assertEqual(model.slice(self.price, {self.product: [self.a]}).cells, {(self.a,): 10.0})
        rows, total = model.rows(self.price, limit=1)
        self.assertEqual((rows, total), ([((self.a,), 10.0)], 3))
        total = model.summarize(self.price)
        self.assertEqual((total.dims, total.cells), ((), {(): 35.0}))
        margin = self.m.metric("Margin").id
        by_month = model.summarize(margin, {self.product: self.a}, keep=[month])
        self.assertEqual(by_month.dims, (month,))
        self.assertTrue(all(k[0] in self.m.dimension("Month").ids for k in by_month.cells))
        # a member-type value is a member id, in and out
        dept = self.m.dimension_id("Department")
        best = model.add_input("Best", [self.product], kind=f"member:{dept}")
        sales = self.m.member_id("Department", "Sales")
        model.set_cell(best, sales, {self.product: self.a})
        self.assertEqual(model.get(best, {self.product: self.a}), sales)
        self.assertEqual(model.value(best).cells, {(self.a,): sales})
        self.assertEqual(self.m.get("Best", Product="A"), "Sales")
        self.assertEqual(model.memory().keys(), model.metrics.keys())
        self.assertEqual(list(model.eval_log)[:1], [list(self.m.eval_log)[0] and self.m.metric(self.m.eval_log[0]).id])

    def test_definitions_return_the_id(self):
        m, model = self.m, self.model
        self.assertEqual(m.add_input("New", ["Product"]), m.metric("New").id)
        self.assertEqual(m.add_formula("New2", ["Product"], "New + 1"), m.metric("New2").id)
        self.assertEqual(m.add_member("Product", "Z"), m.member_id("Product", "Z"))
        self.assertEqual(m.add_property("Product", "Group", "Category", {"A": "X"}), m.property_id("Product", "Group"))
        self.assertEqual(model.add_input("Given", [self.product], id="given-1"), "given-1")

    def test_a_metric_named_like_a_uuid_does_not_collide(self):
        m, model = self.m, self.model
        looks_like_id = str(uuid.uuid4())
        by_name = m.add_input(looks_like_id, ["Product"], {("A",): 1})
        by_id = model.add_input("Other", [self.product], {(self.a,): 2}, id=looks_like_id)
        self.assertNotEqual(by_name, by_id)
        self.assertEqual(m.metric(looks_like_id).id, by_name)          # the facade looks up the name
        self.assertIs(model.metric(looks_like_id), m.metrics[by_id])  # the Model looks up the id
        self.assertEqual((m.get(looks_like_id, Product="A"), m.get("Other", Product="A")), (1.0, 2.0))
        m.add_dimension(str(uuid.uuid4()), ["x"])  # a dimension and a property can have such a name too
        m.add_property("Product", looks_like_id, "Category", {})

    def test_named_round_trips_names(self):
        m, model = self.m, self.model
        m.set_cell("Price", 11, Product="A")
        self.assertEqual(model.get(self.price, {self.product: self.a}), 11.0)
        self.assertEqual(m.get("Price", Product="A"), 11.0)
        self.assertEqual(m.value("Price").dims, ("Product",))
        self.assertEqual(m.slice("Price", Product=["A"]).cells, {("A",): 11.0})
        self.assertEqual(m.rows("Price", limit=1)[0], [(("A",), 11.0)])
        self.assertEqual(m.summarize("Margin", keep=["Month"], Product="A").dims, ("Month",))
        self.assertIn("Margin", m.eval_log)
        self.assertEqual({n for n, _ in m.slice_log}, set(m.eval_log))
        self.assertTrue(all(d in m._dim_ids for _, r in m.slice_log for d in r))  # dimension names
        self.assertIn("Price", m.memory())
        self.assertEqual(m.fork().get("Price", Product="A"), 11.0)
        m.rename_member("Product", "A", "Alpha")
        self.assertEqual(m.get("Price", Product="Alpha"), 11.0)
        self.assertEqual(model.get(self.price, {self.product: self.a}), 11.0)  # the id did not change
        with self.assertRaisesRegex(ValueError, "Metric Nope がない"):
            m.get("Nope")
        with self.assertRaisesRegex(ValueError, "Price: 軸 Nope がない"):
            m.get("Price", Nope="A")
        self.assertIsNone(m.get("Price", Product="Nope"))  # an unknown member is blank, as in the Model
        with self.assertRaisesRegex(ValueError, "Price: Product に 'Nope' がない"):
            m.set_cell("Price", 1, Product="Nope")
