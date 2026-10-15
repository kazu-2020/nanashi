"""計画ツールとしての入力機能: 計算 Metric の上書きと、合計値の按分。"""
import math
import random
import tempfile
import unittest

from sparse_engine import Model, Named
from sparse_engine.engine import ReferenceEngine
from sparse_engine.evaluate import FormulaError

from .test_incremental import same, snapshot

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None

try:
    import nanashi_core  # noqa: F401  保存形式（Parquet）の読み書きに使う
except ImportError:
    nanashi_core = None

MONTHS = ["Jan", "Feb", "Mar"]


def model(engine=None) -> Model:
    m = Named(Model(engine=engine)) if engine is not None else Named(Model())
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


def engines() -> list:
    return [ReferenceEngine()] + ([RustEngine()] if RustEngine is not None else [])


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

    def test_formula_cannot_reference_the_hidden_input(self):
        m = model()
        with self.assertRaisesRegex(FormulaError, "未知の Metric __override__Bonus"):
            m.add_formula("Leak", ["Employee", "Month"], "'__override__Bonus' + 0")
        for name in ("Total", "DeptBonus", "Bonus"):  # nothing refers to the hidden input, so the owner can go
            m.remove_metric(name)
        m.set_cell("Salary", 1, Employee="e1", Month="Jan")
        self.assertEqual(m.get("Salary", Employee="e1", Month="Jan"), 1)

    def test_hidden_names_are_reserved(self):
        m = model()
        with self.assertRaisesRegex(ValueError, "__ で始まらない"):
            m.add_input("__override__Z", ["Employee", "Month"], {("e1", "Jan"): 99})
        with self.assertRaisesRegex(ValueError, "__ で始まらない"):
            m.add_formula("__x", ["Employee", "Month"], "Salary")
        self.assertNotIn("__override__Z", m.model._metric_ids)
        with self.assertRaisesRegex(ValueError, "__ で始まらない"):
            m.rename_metric("Budget", "__override__Bonus")
        with self.assertRaisesRegex(ValueError, "__ で始まらない"):
            m.rename_metric("Budget", ["Budget2"])
        self.assertEqual(m.metric("Budget").name, "Budget")

    def test_hidden_input_added_after_a_recalc_gets_a_layout(self):
        for engine in engines():
            with self.subTest(type(engine).__name__):
                m = model(engine)
                m.add_formula("Late", ["Employee", "Month"], "Salary * 2", overridable=True)
                m.recalc()
                hidden = m.metric("__override__Late").id
                self.assertIn(hidden, m.layout)
                m.set_cell("Late", 5, Employee="e1", Month="Jan")
                self.assertEqual(m.get("Late", Employee="e1", Month="Jan"), 5)

    def test_legacy_metric_with_the_hidden_name(self):
        # a model from before the name rule: add_input accepted "__override__Z" and "__tmp"
        m = model()
        dims = [m.dimension_id("Employee"), m.dimension_id("Month")]
        legacy = m.model._add_input("__override__Z", dims)
        m.model._add_input("__tmp", dims, {(m.member_id("Employee", "e1"), m.member_id("Month", "Jan")): 3})
        with self.assertRaisesRegex(ValueError, "__override__Z: 同じ名前の Metric がある"):
            m.add_formula("Z", ["Employee", "Month"], "Salary", overridable=True)
        self.assertEqual(m.metric("__override__Z").id, legacy)
        self.assertNotIn("Z", m.model._metric_ids)
        m.add_formula("UsesTmp", ["Employee", "Month"], "'__tmp' + 1")  # a user Metric stays usable
        m.recalc()
        self.assertEqual(m.get("UsesTmp", Employee="e1", Month="Jan"), 4)

    @unittest.skipIf(nanashi_core is None, "nanashi_core が必要")
    def test_overrides_are_saved(self):
        m = model()
        m.set_cell("Bonus", 50, Employee="e1", Month="Jan")
        with tempfile.TemporaryDirectory() as tmp:
            m.save(tmp)
            loaded = Named.load(tmp, ReferenceEngine())
        self.assertEqual(loaded.get("Bonus", Employee="e1", Month="Jan"), 50)
        self.assertTrue(loaded.metric("Bonus").overridable)


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

    def test_metric_without_dimensions(self):
        m = model()
        m.add_input("Plan", [], {})
        self.assertEqual(m.spread("Plan", 10), 1)  # 値がないので、ただ 1 つのセルへ
        m.spread("Plan", 20)
        self.assertEqual(m.get("Plan"), 20)

    def test_aggregated_input_with_edits_before_and_after(self):
        """差分集計の集計元を按分し、同じ再計算の前後で 1 セルずつも書き換える。"""
        for engine in engines():
            with self.subTest(engine=engine.name):
                m = model(engine)
                m.add_formula("DeptBudget", ["Department", "Month"], "Budget[BY SUM: Employee.Department]")
                m.recalc()
                m.set_cell("Budget", 5, Employee="e3", Month="Jan")  # 按分の前に 1 セル
                m.spread("Budget", 90, Month="Jan")  # 10 : 30 : 5 で配る
                m.set_cell("Budget", 7, Employee="e3", Month="Mar")  # 按分の範囲の外に 1 セル
                m.delta_log.clear()
                m.recalc()
                self.assertIn("DeptBudget", m.delta_log)
                self.assertAlmostEqual(m.get("DeptBudget", Department="営業", Month="Jan"), 80)
                self.assertAlmostEqual(m.get("DeptBudget", Department="開発", Month="Jan"), 10)
                self.assertEqual(m.get("DeptBudget", Department="開発", Month="Mar"), 7)

    def test_random_spreads_of_aggregated_input(self):
        ms = [model(e) for e in engines()]
        for m in ms:
            m.add_formula("DeptBudget", ["Department", "Month"], "Budget[BY SUM: Employee.Department]")
            m.recalc()
        for round_ in range(120):
            for m in ms:  # 同じ種の乱数で、同じ操作を加える
                rng = random.Random(round_)
                for _ in range(rng.randint(1, 3)):  # 1 回の再計算の前に、按分と書き換えを混ぜる
                    e, t = rng.choice(["e1", "e2", "e3"]), rng.choice(MONTHS)
                    if rng.random() < 0.5:
                        m.set_cell("Budget", None if rng.random() < 0.3 else float(rng.randint(1, 50)),
                                   Employee=e, Month=t)
                    else:
                        coords = rng.choice([{"Month": t}, {"Employee": e}, {}])
                        where = {"Employee.Department": "営業"} if rng.random() < 0.3 else None
                        try:
                            m.spread("Budget", float(rng.randint(10, 100)),
                                     how=rng.choice(["proportional", "even"]), where=where, **coords)
                        except ValueError as err:  # e3 は営業ではないので、範囲が空になりうる
                            self.assertIn("按分先のセルがない", str(err))
            for m in ms:
                incremental = snapshot(m)
                m._invalidate()
                full = snapshot(m)
                for name in m._metric_ids:
                    with self.subTest(round=round_, engine=m.engine.name, metric=name):
                        self.assertTrue(same(incremental[name], full[name]), name)
            for name in ms[0]._metric_ids:
                with self.subTest(round=round_, metric=name):
                    self.assertTrue(all(same(ms[0].value(name).cells, m.value(name).cells) for m in ms[1:]), name)

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


class BadCells(unittest.TestCase):
    def test_rejected_input_keeps_the_old_definition(self):
        for engine in engines():
            m = model(engine)
            with self.assertRaisesRegex(ValueError, "^X: Employee に 'e9' がない$"):
                m.add_input("X", ["Employee"], {("e1",): 1.0, ("e9",): 2.0})
            self.assertNotIn("X", [x.name for x in m.metrics.values()])
            with self.assertRaisesRegex(ValueError, "^Salary は number の Metric: 'a'$"):
                m.add_input("Salary", ["Employee"], {("e1",): "a"}, id=m.metric("Salary").id)
            self.assertEqual(m.metric("Salary").dims, m.metric("Budget").dims)
            self.assertEqual(m.get("Total"), 120.0)
            e1 = m.dimension("Employee").ids[0]
            with self.assertRaisesRegex(ValueError, "^同じセルが 2 回ある$"):
                m.add_input("Y", ["Employee"], {("e1",): 1.0, (e1,): 2.0})
            with self.assertRaises(TypeError):
                m.add_input("Y", ["Employee"], {1: 1.0})
            with self.assertRaises(AttributeError):
                m.add_input("Y", ["Employee"], [(("e1",), 1.0)])
            self.assertNotIn("Y", [x.name for x in m.metrics.values()])
            m.recalc()

    def test_names_must_be_non_empty_strings(self):
        for engine in engines():
            m = model(engine)
            metrics = sorted(x.name for x in m.metrics.values())
            for name in [5, None, "", True, [1]]:
                with self.subTest(name=name):
                    with self.assertRaisesRegex(ValueError, "空でない文字列"):
                        m.add_input(name, ["Employee"])
                    with self.assertRaisesRegex(ValueError, "空でない文字列"):
                        m.add_formula(name, ["Employee"], "Salary * 2")
                    with self.assertRaisesRegex(ValueError, "空でない文字列"):
                        m.add_member("Employee", name)
            self.assertEqual(sorted(x.name for x in m.metrics.values()), metrics)
            self.assertEqual(m.dimension("Employee").members, ["e1", "e2", "e3"])

    def test_values_must_be_finite(self):
        for engine in engines():
            m = model(engine)
            for value in [math.nan, math.inf, -math.inf, 10 ** 400]:
                with self.subTest(value=value):
                    with self.assertRaisesRegex(ValueError, "^Salary: 値は有限の数"):
                        m.set_cell("Salary", value, Employee="e1", Month="Jan")
                    with self.assertRaisesRegex(ValueError, "^X: 値は有限の数"):
                        m.add_input("X", ["Employee"], {("e1",): value})
                    with self.assertRaisesRegex(ValueError, "^Budget: 値は有限の数"):
                        m.spread("Budget", value, Month="Jan")
            self.assertEqual(m.get("Total"), 120.0)
            m.spread("Budget", 1e10, Month="Jan")
            with self.assertRaisesRegex(ValueError, "^Budget: 値は有限の数"):
                m.spread("Budget", 1e300, Month="Jan")  # proportional: total * value overflows
            m.set_cell("Budget", 1e308, Employee="e1", Month="Jan")
            m.set_cell("Budget", 1e308, Employee="e2", Month="Jan")
            with self.assertRaisesRegex(ValueError, "^Budget: 今の値の合計"):
                m.spread("Budget", 1, Month="Jan")  # the sum of the current values overflows


class MatchesFullRecalc(unittest.TestCase):
    def test_random_edits(self):
        m = model()
        for round_ in range(150):
            random_round(random.Random(round_), m)
            incremental = snapshot(m)
            m._invalidate()
            full = snapshot(m)
            for name in m._metric_ids:
                with self.subTest(round=round_, metric=name):
                    self.assertTrue(same(incremental[name], full[name]), name)


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustMatchesReference(unittest.TestCase):
    def test_random_edits(self):
        ref_m, rs = model(ReferenceEngine()), model(RustEngine())
        for round_ in range(150):
            for m in (ref_m, rs):  # 同じ種の乱数で、同じ操作を加える
                random_round(random.Random(round_), m)
            for name in ref_m._metric_ids:
                with self.subTest(round=round_, metric=name):
                    self.assertTrue(same(ref_m.value(name).cells, rs.value(name).cells), name)
        self.assertTrue(math.isfinite(rs.get("Total") or 0.0))


if __name__ == "__main__":
    unittest.main()
