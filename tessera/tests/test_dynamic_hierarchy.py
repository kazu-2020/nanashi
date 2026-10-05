"""メンバー型の Metric と、それを使った BY（時間で変わる階層）。"""
import random
import unittest

from sparse_engine import FormulaError, Model
from sparse_engine.engine import ReferenceEngine

from .test_incremental import same, snapshot

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None

MONTHS = ["Jan", "Feb", "Mar", "Apr"]


def model(engine=None) -> Model:
    m = Model(engine=engine) if engine is not None else Model()
    m.add_dimension("Employee", ["e1", "e2", "e3"])
    m.add_dimension("Department", ["営業", "開発"])
    m.add_dimension("Month", MONTHS, ordered=True)
    m.add_input("Salary", ["Employee", "Month"], {(e, t): v for e, v in [("e1", 100), ("e2", 200), ("e3", 300)]
                                                 for t in MONTHS})
    # e2 は 3 月に営業から開発へ異動。e3 は 4 月の所属が未定
    dept = {("e1", t): "営業" for t in MONTHS} | {("e2", "Jan"): "営業", ("e2", "Feb"): "営業",
                                                  ("e2", "Mar"): "開発", ("e2", "Apr"): "開発"}
    dept |= {("e3", t): "開発" for t in MONTHS[:3]}
    m.add_input("DeptOf", ["Employee", "Month"], dept, kind="member:Department")
    m.add_input("HireMonth", ["Employee"], {("e1",): "Jan", ("e2",): "Jan", ("e3",): "Feb"}, kind="member:Month")
    m.add_input("Budget", ["Department", "Month"], {("営業", t): 1000 for t in MONTHS} | {("開発", t): 2000 for t in MONTHS})
    f = m.add_formula
    f("DeptCost", ["Department", "Month"], "Salary[BY SUM: Employee.DeptOf]")
    f("Headcount", ["Department", "Month"], "Salary[BY COUNT: Employee.DeptOf]")
    f("Share", ["Employee", "Month"], "Salary / DeptCost[BY: Employee.DeptOf]")
    f("BudgetOf", ["Employee", "Month"], "Budget[BY: Employee.DeptOf]")
    f("Active", ["Employee", "Month"], "Month >= HireMonth", kind="boolean")
    f("Paid", ["Employee", "Month"], "Salary[FILTER: Active]")
    f("PaidByDept", ["Department"], "Paid[BY SUM: Employee.DeptOf][REMOVE SUM: Month]")
    m.recalc()
    m.slice_log.clear()
    return m


class Values(unittest.TestCase):
    def setUp(self):
        self.m = model()

    def test_aggregate_follows_monthly_membership(self):
        c = self.m.value("DeptCost").cells
        self.assertEqual([c[("営業", t)] for t in MONTHS], [300, 300, 100, 100])
        self.assertEqual([c[("開発", t)] for t in MONTHS], [300, 300, 500, 200])  # e3 は 4 月に所属なし

    def test_count(self):
        c = self.m.value("Headcount").cells
        self.assertEqual([c[("営業", t)] for t in MONTHS], [2, 2, 1, 1])

    def test_lookup_through_metric(self):
        self.assertEqual(self.m.get("Share", Employee="e2", Month="Feb"), 200 / 300)
        self.assertEqual(self.m.get("Share", Employee="e2", Month="Mar"), 200 / 500)
        self.assertEqual(self.m.get("BudgetOf", Employee="e2", Month="Mar"), 2000)
        self.assertIsNone(self.m.get("BudgetOf", Employee="e3", Month="Apr"))

    def test_member_valued_comparison(self):
        self.assertEqual(self.m.get("Active", Employee="e3", Month="Jan"), False)
        self.assertEqual(self.m.get("Active", Employee="e3", Month="Feb"), True)
        # 開発 = e2 の 3・4 月（400）+ e3 の 2・3 月（600）。e3 の 4 月は所属がないので入らない
        self.assertEqual(self.m.value("PaidByDept").cells, {("営業",): 800, ("開発",): 1000})

    def test_member_values_are_names(self):
        self.assertEqual(self.m.get("DeptOf", Employee="e2", Month="Mar"), "開発")
        self.assertEqual(self.m.get("HireMonth", Employee="e3"), "Feb")


class Transfer(unittest.TestCase):
    def test_transfer_is_incremental(self):
        m = model()
        m.set_cell("DeptOf", "開発", Employee="e1", Month="Apr")
        m.recalc()
        c = m.value("DeptCost").cells
        self.assertNotIn(("営業", "Apr"), c)  # 営業に誰もいなくなる
        self.assertEqual(c[("開発", "Apr")], 300)
        r = {n: {d: set(ms) for d, ms in reg.items()} for n, reg in m.slice_log}
        self.assertEqual(r["DeptCost"], {"Month": {"Apr"}})  # 4 月だけ（両部署）を計算し直す
        self.assertNotIn("Active", r)

    def test_salary_change_and_transfer_use_delta(self):
        m = model()
        self.assertEqual(m._delta[m.metric("DeptCost").id].aux, (m.metric("DeptOf").id,))
        m.set_cell("Salary", 150, Employee="e1", Month="Jan")
        m.recalc()
        self.assertIn("DeptCost", m.delta_log)  # 対応表は変わっていないので差分を足し込む
        self.assertEqual(m.get("DeptCost", Department="営業", Month="Jan"), 350)
        m.delta_log.clear()
        m.set_cell("DeptOf", "開発", Employee="e1", Month="Jan")
        m.recalc()
        # 対応表が変わっても、変わった社員の古い所属での寄与を引き、新しい所属での寄与を足す
        self.assertIn("DeptCost", m.delta_log)
        self.assertEqual(m.get("DeptCost", Department="開発", Month="Jan"), 450)
        self.assertEqual(m.get("DeptCost", Department="営業", Month="Jan"), 200)

    def test_hire_month_change(self):
        m = model()
        m.set_cell("HireMonth", "Mar", Employee="e1")
        m.recalc()
        self.assertEqual(m.value("PaidByDept").cells[("営業",)], 600)


class Validation(unittest.TestCase):
    def test_member_value_must_exist(self):
        m = model()
        with self.assertRaisesRegex(ValueError, "Department のメンバー"):
            m.set_cell("DeptOf", "人事", Employee="e1", Month="Jan")
        with self.assertRaisesRegex(ValueError, "Department のメンバー"):
            m.set_cell("DeptOf", 1, Employee="e1", Month="Jan")

    def test_unknown_kind(self):
        with self.assertRaisesRegex(ValueError, "値の種類"):
            model().add_input("X", ["Employee"], kind="member:Nope")

    def reject(self, formula, dims, pattern):
        m = model()
        m.add_formula("Bad", dims, formula)
        with self.assertRaisesRegex(FormulaError, pattern):
            m.recalc()

    def test_unknown_property_or_metric(self):
        self.reject("Salary[BY SUM: Employee.Nope]", ["Department", "Month"], "同じ名前の Metric もない")

    def test_mapping_must_be_member_typed(self):
        self.reject("Salary[BY SUM: Employee.Salary]", ["Department", "Month"], "メンバー型の Metric ではない")

    def test_expression_must_have_mapping_dims(self):
        self.reject("Salary[REMOVE SUM: Month][BY SUM: Employee.DeptOf]", ["Department"], "持っていない")


def random_round(rng: random.Random, models: list[Model], counter: list[int]) -> None:
    m0 = models[0]
    r = rng.random()
    if r < 0.08:
        counter[0] += 1
        kind = rng.choice(["Employee", "Month", "Department"])
        for m in models:
            m.add_member(kind, f"{kind[0]}{counter[0]}")
        return
    name = rng.choice(["Salary", "DeptOf", "DeptOf", "HireMonth", "Budget"])
    meta = m0.metric(name)
    coords = {d: rng.choice(m0.dimensions[d].members) for d in meta.dims}
    if rng.random() < 0.25:
        value = None
    elif meta.kind.startswith("member:"):
        value = rng.choice(m0.dimensions[meta.kind.removeprefix("member:")].members)
    else:
        value = float(rng.randint(1, 60))
    for m in models:
        m.set_cell(name, value, **coords)


class MatchesFullRecalc(unittest.TestCase):
    def test_random_edits(self):
        rng = random.Random(31)
        m = model()
        counter = [0]
        for round_ in range(200):
            for _ in range(rng.randint(1, 3)):
                random_round(rng, [m], counter)
            incremental = snapshot(m)
            m._invalidate()
            full = snapshot(m)
            for name in m._metric_ids:
                with self.subTest(round=round_, metric=name):
                    self.assertTrue(same(incremental[name], full[name]),
                                    f"{name}\n差分: {incremental[name]}\n全体: {full[name]}")


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustMatchesReference(unittest.TestCase):
    def test_random_edits(self):
        rng = random.Random(37)
        ref_m, rs = model(ReferenceEngine()), model(RustEngine())
        counter = [0]
        for round_ in range(150):
            for _ in range(rng.randint(1, 3)):
                random_round(rng, [ref_m, rs], counter)
            for name in ref_m._metric_ids:
                with self.subTest(round=round_, metric=name):
                    a, b = ref_m.value(name).cells, rs.value(name).cells
                    self.assertTrue(same(a, b), f"{name}\n参照: {a}\nRust: {b}")


if __name__ == "__main__":
    unittest.main()
