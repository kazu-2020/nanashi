import unittest

from sparse_engine import FormulaError, Model, if_, ref

X, Y = ref("X"), ref("Y")


def model() -> Model:
    m = Model()
    m.add_dimension("Product", ["A", "B", "C", "D"])
    m.add_dimension("Month", ["Jan", "Feb", "Mar", "Apr"], ordered=True)
    m.add_input("X", ["Product"], {("A",): 1, ("B",): 5})
    m.add_input("Y", ["Product"], {("B",): 5, ("C",): 2})
    m.add_input("P", ["Product"], {("A",): True, ("B",): False, ("C",): True}, kind="boolean")
    m.add_input("Q", ["Product"], {("A",): True, ("B",): True, ("D",): False}, kind="boolean")
    m.add_input("Z", ["Product", "Month"], {("B", "Jan"): 7})
    return m


def cells(m: Model, name: str, dims, formula, kind="number"):
    m.add_formula(name, dims, formula, kind=kind)
    return {k[0] if len(k) == 1 else k: v for k, v in m.value(name).cells.items()}


class Comparison(unittest.TestCase):
    def test_blank_operand_gives_blank(self):
        m = model()
        self.assertEqual(cells(m, "R", ["Product"], X > 2, "boolean"), {"A": False, "B": True})

    def test_two_metrics_intersect(self):
        m = model()
        self.assertEqual(cells(m, "R", ["Product"], X.eq(Y), "boolean"), {"B": True})

    def test_reflected_comparison(self):
        m = model()
        self.assertEqual(cells(m, "R", ["Product"], 2 < X, "boolean"), {"A": False, "B": True})

    def test_chained_comparison_is_rejected(self):
        with self.assertRaises(TypeError):
            1 < X < 2


class ThreeValuedLogic(unittest.TestCase):
    def test_and(self):
        # A: T&T, B: F&T, C: T&空=空, D: 空&F=F
        m = model()
        self.assertEqual(cells(m, "R", ["Product"], ref("P") & ref("Q"), "boolean"),
                         {"A": True, "B": False, "D": False})

    def test_or(self):
        # A: T|T, B: F|T, C: T|空=T, D: 空|F=空
        m = model()
        self.assertEqual(cells(m, "R", ["Product"], ref("P") | ref("Q"), "boolean"),
                         {"A": True, "B": True, "C": True})

    def test_different_dims_are_rejected(self):
        m = model()
        m.add_formula("R", ["Product", "Month"], ref("P") & (ref("Z") > 0), kind="boolean")
        with self.assertRaisesRegex(FormulaError, "'and' の左辺"):
            m.recalc()

    def test_not_keeps_blank(self):
        m = model()
        self.assertEqual(cells(m, "R", ["Product"], ~ref("P"), "boolean"),
                         {"A": False, "B": True, "C": False})


class IfSemantics(unittest.TestCase):
    def test_blank_condition_gives_blank(self):
        m = model()
        self.assertEqual(cells(m, "R", ["Product"], if_(X > 2, 100, 0)), {"A": 0, "B": 100})
        self.assertEqual(m.warnings["R"], [])  # 条件より密にならない

    def test_without_else_false_is_blank(self):
        m = model()
        self.assertEqual(cells(m, "R", ["Product"], if_(X > 2, X)), {"B": 5})

    def test_blank_branch_value_stays_blank(self):
        m = model()
        self.assertEqual(cells(m, "R", ["Product"], if_(X > 0, Y)), {"B": 5})

    def test_treat_blank_condition_as_false_explicitly(self):
        m = model()
        r = cells(m, "R", ["Product"], if_((X > 2).ifblank(False), 1, 0))
        self.assertEqual(r, {"A": 0, "B": 1, "C": 0, "D": 0})
        self.assertIn("IFBLANK", m.warnings["R"][0])

    def test_branch_with_extra_dim(self):
        m = model()
        r = cells(m, "R", ["Product", "Month"], if_(X > 2, ref("Z"), 0))
        expected = {("B", "Jan"): 7} | {("A", t): 0 for t in ["Jan", "Feb", "Mar", "Apr"]}
        self.assertEqual(r, expected)
        self.assertEqual(len(m.warnings["R"]), 1)  # ELSE の 0 が Month 方向に展開される

    def test_metric_branch_missing_dim_is_rejected(self):
        m = model()
        m.add_formula("R", ["Product", "Month"], if_(X > 2, ref("Z"), X))
        with self.assertRaisesRegex(FormulaError, "IF の ELSE"):
            m.recalc()

    def test_boolean_result(self):
        m = model()
        self.assertEqual(cells(m, "R", ["Product"], if_(X > 2, ref("P"), ref("Q")), "boolean"),
                         {"A": True, "B": False})


class FilterSemantics(unittest.TestCase):
    def test_keeps_only_true(self):
        m = model()
        self.assertEqual(cells(m, "R", ["Product"], X.filter(Y > 3)), {"B": 5})

    def test_condition_on_fewer_dims(self):
        m = model()
        self.assertEqual(cells(m, "R", ["Product", "Month"], ref("Z").filter(X > 2)),
                         {("B", "Jan"): 7})

    def test_condition_with_extra_dim_is_rejected(self):
        m = model()
        m.add_formula("R", ["Product"], X.filter(ref("Z") > 0))
        with self.assertRaisesRegex(FormulaError, "対象にない軸"):
            m.recalc()


class IsBlank(unittest.TestCase):
    def test_isblank_is_dense(self):
        m = model()
        r = cells(m, "R", ["Product"], X.isblank(), "boolean")
        self.assertEqual(r, {"A": False, "B": False, "C": True, "D": True})
        self.assertIn("ISBLANK", m.warnings["R"][0])


class KindChecking(unittest.TestCase):
    def assertRejected(self, formula, kind="number", pattern=""):
        m = model()
        m.add_formula("R", ["Product"], formula, kind=kind)
        with self.assertRaisesRegex(FormulaError, pattern):
            m.recalc()

    def test_arithmetic_on_boolean(self):
        self.assertRejected(X + (X > 1), pattern="number が必要")

    def test_number_condition(self):
        self.assertRejected(if_(X, 1, 0), pattern="IF の条件")

    def test_branch_kinds_differ(self):
        self.assertRejected(if_(X > 1, 1, True), pattern="THEN と ELSE")

    def test_compare_different_kinds(self):
        self.assertRejected(X.eq(X > 1), kind="boolean", pattern="種類が違う")

    def test_declared_kind_mismatch(self):
        self.assertRejected(X > 1, kind="number", pattern="boolean だが number")

    def test_sum_of_boolean(self):
        m = model()
        m.add_formula("R", [], ref("P").remove("Product"))
        with self.assertRaisesRegex(FormulaError, "sum"):
            m.recalc()

    def test_count_of_boolean(self):
        m = model()
        m.add_formula("R", [], ref("P").filter(ref("P")).remove("Product", "count"))
        self.assertEqual(m.get("R"), 2)

    def test_input_kind_is_enforced(self):
        m = model()
        with self.assertRaises(ValueError):
            m.set_cell("P", 1, Product="A")
        with self.assertRaises(ValueError):
            m.set_cell("X", True, Product="A")


class IfInsideScan(unittest.TestCase):
    def test_reorder_policy(self):
        """在庫が 5 を下回ったら翌月 10 発注する。"""
        m = Model()
        m.add_dimension("Month", ["Jan", "Feb", "Mar", "Apr"], ordered=True)
        m.add_input("In", ["Month"], {("Jan",): 10})
        m.add_input("Demand", ["Month"], {(t,): 4 for t in ["Jan", "Feb", "Mar", "Apr"]})
        m.add_formula("Order", ["Month"], if_(ref("Stock").prev("Month") < 5, 10, 0))
        m.add_formula("Stock", ["Month"],
                      ref("Stock").prev("Month") + ref("In") + ref("Order") - ref("Demand"))
        stock = m.value("Stock")
        self.assertEqual([stock.get(Month=t) for t in ["Jan", "Feb", "Mar", "Apr"]], [6, 2, 8, 4])
        # Jan は前月の在庫が空なので、発注も空（0 ではない）
        self.assertEqual(m.value("Order").cells, {("Feb",): 0, ("Mar",): 10, ("Apr",): 0})


if __name__ == "__main__":
    unittest.main()
