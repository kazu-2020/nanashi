"""変わらない ID と、Metric の削除と名前の変更。"""
import tempfile
import unittest
import uuid

from examples.fpa import build as build_fpa
from sparse_engine import FormulaError, Model, to_formula
from sparse_engine.engine import ReferenceEngine
from sparse_engine.model import _UNSET, DuplicateId, uuid7

from .test_engines import build_with
from .test_incremental import check_full, same, snapshot

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
        ids += [m.uuid_of(h) for h in d.ids]
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
        self.assertIs(m.metric(margin.id), margin)  # a string argument is an id first, then a name
        with self.assertRaisesRegex(ValueError, "がない"):
            m.metric("Nope")
        with self.assertRaisesRegex(ValueError, "UUID"):  # a name in the UUID form cannot collide with an id
            m.add_input(str(uuid.uuid4()), ["Product"])
        with self.assertRaisesRegex(ValueError, "UUID"):
            m.rename_metric("Margin", uuid7())


class Uuids(unittest.TestCase):
    """The external UUIDs (docs/ids.md): one for each dimension, member, property, and Metric."""
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        self.m = build_with(self.engine())
        self.m.recalc()

    def test_every_object_has_a_uuid(self):
        m = self.m
        uuids = all_ids(m) + [p for d in m.dimensions.values() for p in d.properties]
        self.assertEqual(sorted(m.ids), sorted(uuids))
        self.assertEqual({u: h for h, u in m._uuids.items()}, m.ids)
        for u in m.ids:
            self.assertEqual(uuid.UUID(u).version, 7)
        self.assertIn(m.metric("Margin").id, m.ids)  # a Metric also has a handle, for the journal and storage formats
        self.assertEqual(m.member_id("Product", "A"), m.uuid_of(m.dimension("Product").id_of("A")))
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
        self.assertEqual(m.dimension("Product").properties[m.property_id("Product", "Category")][1]["Alpha"], "Y")
        self.assertIs(m.add_dimension("Product", [], id=m.dimension_id("Product")), m.dimension("Product"))
        check_full(self, m)

    def test_same_name_with_a_different_uuid_is_an_error(self):
        m = self.m
        with self.assertRaisesRegex(ValueError, "別の ID"):
            m.add_formula("Margin", ["Product", "Month"], "Cost * 2")
        with self.assertRaisesRegex(ValueError, "別の ID"):
            m.add_input("Price", ["Product"], id="other")
        with self.assertRaisesRegex(ValueError, "すでにある"):
            m.add_member("Product", "A", id="other")
        with self.assertRaisesRegex(ValueError, "別の ID"):
            m.add_property("Product", "Category", "Category", {}, id="other")
        with self.assertRaisesRegex(ValueError, "同じ名前の軸"):
            m.add_dimension("Product", [], id="other")
        self.assertNotIn("other", m.ids)

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
        self.assertNotIn(b, m.ids)
        self.assertNotIn(share, m.ids)
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
        self.assertEqual((fork.ids, fork.tombstones), (m.ids, m.tombstones))
        fork.add_member("Product", "E", id="e")
        self.assertNotIn("e", m.ids)  # the fork and the original do not share the maps
        with self.assertRaises(DuplicateId):
            fork.add_member("Product", "B", id=next(iter(m.tombstones)))

    def test_transaction_rollback_restores_the_maps(self):
        m = self.m
        before = (dict(m.ids), set(m.tombstones))
        with self.assertRaises(ValueError):
            with m.transaction():
                m.remove_member("Product", "B")
                m.add_member("Product", "E", id="e")
                raise ValueError("abort")
        self.assertEqual((m.ids, m.tombstones), before)


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
        with self.assertRaisesRegex(ValueError, "UUID"):
            m.rename_dimension("Product", str(uuid.uuid4()))
        m.add_property("Product", "Other", "Category", {})
        with self.assertRaisesRegex(ValueError, "同じ名前のプロパティ"):
            m.rename_property("Product", "Category", "Other")
        with self.assertRaisesRegex(ValueError, "UUID"):
            m.rename_property("Product", "Category", str(uuid.uuid4()))
        with self.assertRaisesRegex(ValueError, "プロパティ Nope がない"):
            m.rename_property("Product", "Nope", "X")


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustDimensionRenames(DimensionRenames):
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
            loaded = Model.load(tmp, self.engine())
        self.assertEqual(all_ids(loaded), all_ids(m))
        self.assertEqual((loaded.ids, loaded.tombstones), (m.ids, m.tombstones))
        self.assertEqual(loaded.dimension("Area").id, m.dimension("Area").id)
        self.assertEqual(loaded.dimension("Product").property_names, m.dimension("Product").property_names)
        self.assertEqual(to_formula(loaded.metric("RevByCat").written, loaded), "Revenue[BY SUM: Product.Group]")
        a, b = snapshot(m), snapshot(loaded)
        for name in a:
            self.assertTrue(same(a[name], b[name]), name)
        loaded.add_member("Product", "F")  # 読み込んだあとも、使った ID を振らない
        self.assertEqual(len(set(all_ids(loaded))), len(all_ids(loaded)))


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
