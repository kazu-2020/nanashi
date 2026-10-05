import math
import random
import unittest

from sparse_engine import Model

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May"]


def model() -> Model:
    m = Model()
    m.add_dimension("Product", ["A", "B", "C", "D"])
    m.add_dimension("Category", ["X", "Y"])
    m.add_dimension("Region", ["N", "S"])
    m.add_dimension("Month", MONTHS, ordered=True)
    m.add_dimension("Employee", ["e1", "e2", "e3", "e4"])
    m.add_dimension("Department", ["Sales", "Eng"])
    m.add_property("Product", "Category", "Category", {"A": "X", "B": "X", "C": "Y", "D": "Y"})
    m.add_property("Employee", "Department", "Department",
                   {"e1": "Sales", "e2": "Sales", "e3": "Eng", "e4": "Eng"})

    m.add_input("Price", ["Product"], {("A",): 10, ("B",): 20, ("C",): 5})
    m.add_input("Volume", ["Product", "Region", "Month"],
                {("A", "N", "Jan"): 3, ("A", "S", "Feb"): 2, ("B", "N", "Mar"): 4, ("C", "S", "Jan"): 8})
    m.add_input("Cost", ["Product", "Month"], {("A", "Jan"): 7, ("B", "Mar"): 30, ("D", "Feb"): 4})
    m.add_input("Flag", ["Product"], {("A",): True, ("B",): False}, kind="boolean")
    m.add_input("Rate", ["Category"], {("X",): 0.5})
    m.add_input("Salary", ["Employee"], {("e1",): 100, ("e3",): 300})

    f = m.add_formula
    f("Revenue", ["Product", "Region", "Month"], "Volume * Price")
    f("RevByCat", ["Category", "Region", "Month"], "Revenue[BY SUM: Product.Category]")
    f("Margin", ["Product", "Month"], "Revenue[REMOVE SUM: Region] - Cost")
    f("Adjusted", ["Product", "Month"],
      "IF(Margin > 10, Margin * Rate[BY: Product.Category], IFBLANK(Cost, 0) * -1)")
    f("Picked", ["Product", "Month"], "Margin[FILTER: Flag]")
    f("OnCost", ["Product", "Month"], "Price[ON: Cost] + Cost")
    f("Expanded", ["Product", "Month"], "Price[EXPAND: Month] + Margin[SELECT: Month - 1]")
    f("Missing", ["Product", "Month"], "ISBLANK(Cost) AND Flag[EXPAND: Month]", kind="boolean")
    f("Outflow", ["Product", "Month"], "IF(Stock[SELECT: Month - 1] > 50, 10)")
    f("Stock", ["Product", "Month"], "PREVIOUS(Month) + Margin - Outflow")
    f("Total", [], "Stock[REMOVE SUM: Product, Month]")
    f("CatShare", ["Product", "Month"],
      "Margin / RevByCat[REMOVE SUM: Region][BY: Product.Category]")
    f("DeptSalary", ["Department"], "Salary[BY SUM: Employee.Department]")
    f("AvgSalary", [], "DeptSalary[REMOVE AVG: Department]")
    f("Plus1", ["Product"], "Price + 1")
    return m


def cells(m: Model, name: str) -> dict:
    return dict(m.value(name).cells)


def snapshot(m: Model) -> dict:
    return {n: cells(m, n) for n in m.metrics}


def same(a, b) -> bool:
    if a.keys() != b.keys():
        return False
    return all(a[k] == b[k] if isinstance(a[k], (bool, str)) else math.isclose(a[k], b[k], abs_tol=1e-6)
               for k in a)


def check_full(test, m: Model) -> None:
    """Make sure that the result of the incremental recalculation is the same as a full recalculation."""
    incremental = snapshot(m)
    m._invalidate()
    full = snapshot(m)
    for name in m.metrics:
        test.assertTrue(same(incremental[name], full[name]), f"{name}\n差分: {incremental[name]}\n全体: {full[name]}")


class MatchesFullRecalc(unittest.TestCase):
    """ランダムな入力変更のあと、差分再計算の結果が全体の再計算と一致する。"""

    def test_random_edits(self):
        rng = random.Random(20260930)
        m = model()
        m.recalc()
        inputs = [n for n, x in m.metrics.items() if x.formula is None]
        for round_ in range(300):
            for _ in range(rng.randint(1, 3)):
                name = rng.choice(inputs)
                meta = m.metrics[name]
                coords = {d: rng.choice(m.dimensions[d].members) for d in meta.dims}
                if rng.random() < 0.3:
                    value = None
                elif meta.kind == "boolean":
                    value = rng.random() < 0.5
                elif name == "Rate":
                    value = rng.choice([0.25, 0.5, 1.5])
                else:
                    value = float(rng.randint(-5, 60))
                m.set_cell(name, value, **coords)
            incremental = snapshot(m)
            m._invalidate()
            full = snapshot(m)
            for name in m.metrics:
                with self.subTest(round=round_, metric=name):
                    self.assertTrue(same(incremental[name], full[name]),
                                    f"{name}\n差分: {incremental[name]}\n全体: {full[name]}")


class RecomputesOnlyTheSlice(unittest.TestCase):
    def setUp(self):
        self.m = model()
        self.m.recalc()
        self.m.slice_log.clear()

    def regions(self) -> dict:
        return {n: {d: set(ms) for d, ms in r.items()} for n, r in self.m.slice_log}

    def test_aggregation_maps_the_slice(self):
        self.m.set_cell("Salary", 150, Employee="e2")
        self.m.recalc()
        self.assertEqual(self.regions(), {"DeptSalary": {"Department": {"Sales"}}, "AvgSalary": {}})

    def test_lookup_fans_out_the_slice(self):
        self.m.set_cell("Rate", 1.5, Category="Y")
        self.m.recalc()
        self.assertEqual(self.regions(), {"Adjusted": {"Product": {"C", "D"}}})

    def test_scan_recomputes_from_changed_month(self):
        self.m.set_cell("Cost", 1, Product="B", Month="Mar")
        self.m.recalc()
        r = self.regions()
        self.assertEqual(r["Margin"], {"Product": {"B"}, "Month": {"Mar"}})
        # Stock と Outflow は Mar 以降だけ。Jan と Feb は計算し直さない
        self.assertEqual(r["Stock"], {"Product": {"B"}, "Month": {"Mar", "Apr", "May"}})
        self.assertEqual(r["Outflow"], {"Product": {"B"}, "Month": {"Apr", "May"}})
        self.assertNotIn("DeptSalary", r)

    def test_unrelated_metrics_are_untouched(self):
        self.m.set_cell("Flag", True, Product="C")
        self.m.recalc()
        self.assertEqual(set(self.regions()), {"Picked", "Missing"})

    def test_formula_change_recomputes_only_that_metric(self):
        self.m.add_formula("Plus1", ["Product"], "Price + 2", id=self.m.metric_id("Plus1"))  # no Metric refers to Plus1
        self.m.recalc()
        self.assertEqual(list(self.m.slice_log), [("Plus1", {})])
        self.assertEqual(self.m.get("Plus1", Product="A"), 12)


if __name__ == "__main__":
    unittest.main()
