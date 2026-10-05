import unittest

from sparse_engine import FormulaError, Model, ref

MONTHS = ["Jan", "Feb", "Mar", "Apr"]


def base_model() -> Model:
    m = Model()
    m.add_dimension("Product", ["A", "B", "C"])
    m.add_dimension("Month", MONTHS, ordered=True)
    m.add_dimension("Employee", ["e1", "e2", "e3"])
    m.add_dimension("Department", ["Sales", "Eng"])
    m.add_property("Employee", "Department", "Department",
                   {"e1": "Sales", "e2": "Sales", "e3": "Eng"})
    return m


class BlankSemantics(unittest.TestCase):
    def setUp(self):
        self.m = base_model()
        self.m.add_input("X", ["Product"], {("A",): 1, ("B",): 2})
        self.m.add_input("Y", ["Product"], {("B",): 10, ("C",): 20})

    def test_plus_is_union_blank_as_zero(self):
        self.m.add_formula("Z", ["Product"], ref("X") + ref("Y"))
        self.assertEqual(self.m.value("Z").cells, {("A",): 1, ("B",): 12, ("C",): 20})

    def test_times_is_intersection(self):
        self.m.add_formula("Z", ["Product"], ref("X") * ref("Y"))
        self.assertEqual(self.m.value("Z").cells, {("B",): 20})

    def test_divide_by_zero_is_blank(self):
        self.m.set_cell("Y", 0, Product="B")
        self.m.add_formula("Z", ["Product"], ref("X") / ref("Y"))
        self.assertEqual(self.m.value("Z").cells, {})

    def test_scalar_multiply_keeps_sparsity(self):
        self.m.add_formula("Z", ["Product"], ref("X") * 2)
        self.assertEqual(self.m.value("Z").cells, {("A",): 2, ("B",): 4})
        self.assertEqual(self.m.warnings[self.m.metric("Z").id], [])

    def test_plus_constant_densifies_and_warns(self):
        self.m.add_formula("Z", ["Product"], ref("X") + 1)
        self.assertEqual(self.m.value("Z").cells, {("A",): 2, ("B",): 3, ("C",): 1})
        self.assertEqual(len(self.m.warnings[self.m.metric("Z").id]), 1)

    def test_ifblank(self):
        self.m.add_formula("Z", ["Product"], ref("X").ifblank(0))
        self.assertEqual(self.m.value("Z").cells, {("A",): 1, ("B",): 2, ("C",): 0})

    def test_set_none_makes_cell_blank(self):
        self.m.add_formula("Z", ["Product"], ref("X") * ref("Y"))
        self.m.set_cell("X", None, Product="B")
        self.assertEqual(self.m.value("Z").cells, {})


class Broadcasting(unittest.TestCase):
    def test_multiply_broadcasts_over_missing_dim(self):
        m = base_model()
        m.add_input("Price", ["Product"], {("A",): 100, ("B",): 200})
        m.add_input("Volume", ["Product", "Month"], {("A", "Jan"): 3, ("C", "Feb"): 5})
        m.add_formula("Revenue", ["Month", "Product"], ref("Volume") * ref("Price"))
        rev = m.value("Revenue")
        self.assertEqual(rev.dims, ("Month", "Product"))  # 宣言した軸順に揃う
        self.assertEqual(rev.cells, {("Jan", "A"): 300})



class ImplicitExpansion(unittest.TestCase):
    """定数との + は暗黙に展開し、軸の違う Metric どうしの + は明示を必須にする。"""

    def setUp(self):
        self.m = base_model()
        self.m.add_input("Fixed", ["Product"], {("A",): 100})
        self.m.add_input("Variable", ["Product", "Month"], {("A", "Jan"): 5, ("B", "Feb"): 7})

    def test_metrics_with_different_dims_are_rejected(self):
        self.m.add_formula("Z", ["Product", "Month"], ref("Fixed") + ref("Variable"))
        with self.assertRaisesRegex(FormulaError, r"\[EXPAND: Month\]"):
            self.m.recalc()

    def test_explicit_expand(self):
        self.m.add_formula("Z", ["Product", "Month"], ref("Fixed").expand("Month") + ref("Variable"))
        z = self.m.value("Z")
        self.assertEqual([z.get(Product="A", Month=t) for t in MONTHS], [105, 100, 100, 100])
        self.assertEqual(z.get(Product="B", Month="Feb"), 7)
        self.assertEqual(self.m.warnings[self.m.metric("Z").id], [])  # 明示したので警告しない

    def test_on_restricts_to_other_support(self):
        self.m.add_formula("Z", ["Product", "Month"], ref("Fixed").on(ref("Variable")) + ref("Variable"))
        self.assertEqual(self.m.value("Z").cells, {("A", "Jan"): 105, ("B", "Feb"): 7})

    def test_constant_expands_with_warning(self):
        self.m.add_formula("Z", ["Product", "Month"], ref("Variable") + 1)
        self.assertEqual(len(self.m.value("Z")), len(self.m.dimensions["Product"].members) * len(MONTHS))
        self.assertTrue(self.m.warnings[self.m.metric("Z").id])

    def test_scalar_metric_is_treated_like_constant(self):
        self.m.add_input("Offset", [], {(): 1})
        self.m.add_formula("Z", ["Product"], ref("Fixed") + ref("Offset"))
        self.assertEqual(self.m.value("Z").cells, {("A",): 101, ("B",): 1, ("C",): 1})

    def test_multiply_still_broadcasts_implicitly(self):
        self.m.add_formula("Z", ["Product", "Month"], ref("Fixed") * ref("Variable"))
        self.assertEqual(self.m.value("Z").cells, {("A", "Jan"): 500})

    def test_expand_existing_dim_is_rejected(self):
        self.m.add_formula("Z", ["Product", "Month"], ref("Variable").expand("Month"))
        with self.assertRaisesRegex(FormulaError, "すでに軸にある"):
            self.m.recalc()


class Aggregation(unittest.TestCase):
    def setUp(self):
        self.m = base_model()
        self.m.add_input("Salary", ["Employee"], {("e1",): 100, ("e2",): 200, ("e3",): 300})
        self.m.add_input("Rate", ["Department"], {("Sales",): 0.1})

    def test_by_aggregate(self):
        self.m.add_formula("DeptSalary", ["Department"], ref("Salary").by("Employee.Department"))
        self.assertEqual(self.m.value("DeptSalary").cells, {("Sales",): 300, ("Eng",): 300})

    def test_by_aggregate_with_function(self):
        self.m.add_formula("MaxSalary", ["Department"], ref("Salary").by("Employee.Department", "max"))
        self.assertEqual(self.m.value("MaxSalary").cells, {("Sales",): 200, ("Eng",): 300})

    def test_by_lookup(self):
        self.m.add_formula("Bonus", ["Employee"], ref("Salary") * ref("Rate").by("Employee.Department"))
        self.assertEqual(self.m.value("Bonus").cells, {("e1",): 10, ("e2",): 20})

    def test_remove(self):
        self.m.add_formula("Total", [], ref("Salary").remove("Employee"))
        self.assertEqual(self.m.value("Total").cells, {(): 600})


class TimeRecursion(unittest.TestCase):
    def setUp(self):
        self.m = base_model()
        self.m.add_input("In", ["Product", "Month"], {("A", "Jan"): 10, ("A", "Mar"): 5, ("B", "Feb"): 7})
        self.m.add_input("Out", ["Product", "Month"], {("A", "Feb"): 3})

    def test_prev_without_recursion(self):
        self.m.add_formula("LastIn", ["Product", "Month"], ref("In").prev("Month"))
        self.assertEqual(self.m.value("LastIn").cells,
                         {("A", "Feb"): 10, ("A", "Apr"): 5, ("B", "Mar"): 7})

    def test_stock_is_scanned(self):
        self.m.add_formula("Stock", ["Product", "Month"],
                          ref("Stock").prev("Month") + ref("In") - ref("Out"))
        s = self.m.value("Stock")
        self.assertEqual([s.get(Product="A", Month=t) for t in MONTHS], [10, 7, 12, 12])
        self.assertEqual([s.get(Product="B", Month=t) for t in MONTHS], [None, 7, 7, 7])
        self.assertIsNone(s.get(Product="C", Month="Apr"))  # 疎のまま

    def test_mutual_recursion(self):
        self.m.add_input("Closing", ["Product", "Month"])  # the two refer to each other: an input first
        self.m.add_formula("Opening", ["Product", "Month"], ref("Closing").prev("Month"))
        self.m.add_formula("Closing", ["Product", "Month"], ref("Opening") + ref("In") - ref("Out"),
                           id=self.m.metric("Closing").id)
        c = self.m.value("Closing")
        self.assertEqual([c.get(Product="A", Month=t) for t in MONTHS], [10, 7, 12, 12])
        self.assertEqual(self.m.get("Opening", Product="A", Month="Mar"), 7)

    def test_cycle_without_lag_is_rejected(self):
        self.m.add_input("Q", ["Product", "Month"])
        self.m.add_formula("P", ["Product", "Month"], ref("Q") + ref("In"))
        self.m.add_formula("Q", ["Product", "Month"], ref("P") * 2, id=self.m.metric("Q").id)
        with self.assertRaisesRegex(FormulaError, "循環参照"):
            self.m.recalc()

    def test_reading_future_is_rejected(self):
        self.m.add_formula("S", ["Product", "Month"],
                          ref("S").prev("Month") + ref("S").prev("Month", -1))
        with self.assertRaisesRegex(FormulaError, "未来"):
            self.m.recalc()

    def test_aggregating_scan_dim_in_cycle_is_rejected(self):
        self.m.add_formula("S", ["Product", "Month"],
                          ref("S").prev("Month") + ref("S").remove("Month").expand("Month"))
        with self.assertRaisesRegex(FormulaError, "集約"):
            self.m.recalc()


class TypeChecking(unittest.TestCase):
    def test_declared_dims_must_match(self):
        m = base_model()
        m.add_input("Salary", ["Employee"])
        m.add_formula("Bad", ["Department"], ref("Salary") * 2)
        with self.assertRaisesRegex(FormulaError, "一致しない"):
            m.recalc()

    def test_prev_requires_ordered_dim(self):
        m = base_model()
        m.add_input("X", ["Product"])
        m.add_formula("Bad", ["Product"], ref("X").prev("Product"))
        with self.assertRaisesRegex(FormulaError, "順序付き"):
            m.recalc()


class Incremental(unittest.TestCase):
    def test_only_downstream_is_recomputed(self):
        m = base_model()
        m.add_input("Salary", ["Employee"], {("e1",): 100})
        m.add_input("Price", ["Product"], {("A",): 1})
        m.add_formula("DeptSalary", ["Department"], ref("Salary").by("Employee.Department"))
        m.add_formula("Total", [], ref("DeptSalary").remove("Department"))
        m.add_formula("Revenue", ["Product"], ref("Price") * 10)
        m.recalc()
        m.eval_log.clear()

        m.set_cell("Salary", 150, Employee="e1")
        self.assertEqual(m.get("Total"), 150)
        self.assertEqual(list(m.eval_log), ["DeptSalary", "Total"])


if __name__ == "__main__":
    unittest.main()
