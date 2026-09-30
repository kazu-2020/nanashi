"""計画ツールとしての入力機能: 計算 Metric の上書きと、合計値の按分。"""
import math
import random
import tempfile
import unittest

from sparse_engine import Model
from sparse_engine.engine import ReferenceEngine

from .test_incremental import same, snapshot

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None

try:
    import numpy  # noqa: F401  保存形式に使う
except ImportError:
    numpy = None

MONTHS = ["Jan", "Feb", "Mar"]


def model(engine=None) -> Model:
    m = Model(engine=engine) if engine is not None else Model()
    m.add_dimension("Employee", ["e1", "e2", "e3"])
    m.add_dimension("Department", ["営業", "開発"])
    m.add_dimension("Month", MONTHS, ordered=True)
    m.add_property("Employee", "Department", "Department", {"e1": "営業", "e2": "営業", "e3": "開発"})
    m.add_input("Salary", ["Employee", "Month"], {(e, t): v for e, v in [("e1", 100), ("e2", 300)] for t in MONTHS})
    m.add_input("Budget", ["Employee", "Month"], {("e1", "Jan"): 10, ("e2", "Jan"): 30})
    m.add_formula("Bonus", ["Employee", "Month"], "Salary * 0.1", overridable=True)
    m.add_formula("DeptBonus", ["Department", "Month"], "Bonus[BY SUM: Employee.Department]")
    m.add_formula("Total", [], "DeptBonus[REMOVE SUM: Department][REMOVE SUM: Month]")
    m.recalc()
    return m


class Overrides(unittest.TestCase):
    def test_override_reaches_downstream(self):
        m = model()
        m.set_cell("Bonus", 50, Employee="e1", Month="Jan")
        self.assertEqual(m.get("Bonus", Employee="e1", Month="Jan"), 50)
        self.assertEqual(m.get("DeptBonus", Department="営業", Month="Jan"), 80)
        self.assertEqual(m.get("Total"), 50 + 30 + 2 * 40)

    def test_override_where_formula_is_blank(self):
        m = model()
        m.set_cell("Bonus", 7, Employee="e3", Month="Mar")  # e3 は給与がなく、式の結果は空
        self.assertEqual(m.get("DeptBonus", Department="開発", Month="Mar"), 7)

    def test_override_wins_over_inputs_and_can_be_cleared(self):
        m = model()
        m.set_cell("Bonus", 50, Employee="e1", Month="Jan")
        m.set_cell("Salary", 1000, Employee="e1", Month="Jan")
        self.assertEqual(m.get("Bonus", Employee="e1", Month="Jan"), 50)
        m.set_cell("Bonus", None, Employee="e1", Month="Jan")  # 上書きを消すと式の結果に戻る
        self.assertEqual(m.get("Bonus", Employee="e1", Month="Jan"), 100)

    def test_not_overridable(self):
        with self.assertRaisesRegex(ValueError, "overridable=True"):
            model().set_cell("DeptBonus", 1, Department="営業", Month="Jan")

    def test_dimension_error_mentions_the_written_formula(self):
        m = model()
        m.add_formula("Bad", ["Employee", "Month"], "Salary[REMOVE SUM: Month]", overridable=True)
        with self.assertRaisesRegex(Exception, "宣言した軸"):
            m.recalc()

    def test_override_value_is_checked(self):
        with self.assertRaisesRegex(ValueError, "number"):
            model().set_cell("Bonus", True, Employee="e1", Month="Jan")

    @unittest.skipIf(numpy is None, "numpy が必要")
    def test_overrides_are_saved(self):
        m = model()
        m.set_cell("Bonus", 50, Employee="e1", Month="Jan")
        with tempfile.TemporaryDirectory() as tmp:
            m.save(tmp)
            loaded = Model.load(tmp, ReferenceEngine())
        self.assertEqual(loaded.get("Bonus", Employee="e1", Month="Jan"), 50)
        self.assertTrue(loaded.metrics["Bonus"].overridable)


class Spread(unittest.TestCase):
    def test_proportional_to_current_values(self):
        m = model()
        self.assertEqual(m.spread("Budget", 100, Month="Jan"), 2)
        self.assertEqual(m.get("Budget", Employee="e1", Month="Jan"), 25)
        self.assertEqual(m.get("Budget", Employee="e2", Month="Jan"), 75)

    def test_where_filters_by_property(self):
        m = model()
        m.spread("Budget", 60, Month="Feb", where={"Employee.Department": "営業"})  # 値がないので均等
        self.assertEqual([m.get("Budget", Employee=e, Month="Feb") for e in ["e1", "e2", "e3"]], [30, 30, None])

    def test_even(self):
        m = model()
        m.spread("Budget", 100, how="even", Month="Jan")
        self.assertEqual(m.get("Budget", Employee="e1", Month="Jan"), 50)

    def test_empty_region_gets_all_combinations(self):
        m = model()
        m.spread("Budget", 90, Employee="e3")
        self.assertEqual([m.get("Budget", Employee="e3", Month=t) for t in MONTHS], [30, 30, 30])

    def test_errors(self):
        m = model()
        with self.assertRaisesRegex(ValueError, "入力 Metric"):
            m.spread("Bonus", 1)
        with self.assertRaisesRegex(ValueError, "軸 Region がない"):
            m.spread("Budget", 1, Region="x")
        with self.assertRaisesRegex(ValueError, "軸.プロパティ"):
            m.spread("Budget", 1, where={"Employee.Color": "red"})


def random_round(rng: random.Random, m: Model) -> None:
    """入力の変更、上書き（と、その取り消し）、按分のどれかを 1 回加える。"""
    kind = rng.random()
    e, t = rng.choice(["e1", "e2", "e3"]), rng.choice(MONTHS)
    if kind < 0.4:
        m.set_cell("Salary", None if rng.random() < 0.2 else float(rng.randint(1, 500)), Employee=e, Month=t)
    elif kind < 0.8:
        m.set_cell("Bonus", None if rng.random() < 0.4 else float(rng.randint(1, 90)), Employee=e, Month=t)
    else:
        m.spread("Budget", float(rng.randint(10, 100)), Month=t)


class MatchesFullRecalc(unittest.TestCase):
    def test_random_edits(self):
        m = model()
        for round_ in range(150):
            random_round(random.Random(round_), m)
            incremental = snapshot(m)
            m._invalidate()
            full = snapshot(m)
            for name in m.metrics:
                with self.subTest(round=round_, metric=name):
                    self.assertTrue(same(incremental[name], full[name]), name)


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustMatchesReference(unittest.TestCase):
    def test_random_edits(self):
        ref_m, rs = model(ReferenceEngine()), model(RustEngine())
        for round_ in range(150):
            for m in (ref_m, rs):  # 同じ種の乱数で、同じ操作を加える
                random_round(random.Random(round_), m)
            for name in ref_m.metrics:
                with self.subTest(round=round_, metric=name):
                    self.assertTrue(same(ref_m.value(name).cells, rs.value(name).cells), name)
        self.assertTrue(math.isfinite(rs.get("Total") or 0.0))


if __name__ == "__main__":
    unittest.main()
