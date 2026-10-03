"""差分集計（集計元の変わった行の差分だけを足し込む）。"""
import random
import unittest

from sparse_engine import Model

from .test_incremental import model as build, same, snapshot


def model() -> Model:
    m = build()
    m.add_formula("VolCount", ["Product", "Month"], "Volume[REMOVE COUNT: Region]")
    m.add_formula("CatMonth", ["Category", "Month"], "Revenue[BY SUM: Product.Category][REMOVE SUM: Region]")
    m.add_formula("CountByCat", ["Category"],
                  "Volume[REMOVE COUNT: Region][REMOVE SUM: Month][BY SUM: Product.Category]")
    # COUNT の外側を COUNT で数えると「グループの数」になり、差分に分解できない
    m.add_formula("GroupCount", ["Product"], "Volume[REMOVE COUNT: Region, Month]")
    m.add_formula("MaxByCat", ["Category"], "Price[BY MAX: Product.Category]")
    m.recalc()
    m.delta_log.clear()
    return m


class Planning(unittest.TestCase):
    def test_sum_and_count_chains_are_delta(self):
        m = model()
        for name in ["RevByCat", "DeptSalary", "Total", "VolCount", "CatMonth", "CountByCat"]:
            with self.subTest(name):
                self.assertIn(name, m._delta)

    def test_other_formulas_are_not_delta(self):
        m = model()
        # AVG / MAX は差分に分解できない。Margin は集計のあとに引き算がある。
        # 件数の後に SUM 以外がくる式や、引き下ろしも対象外
        for name in ["AvgSalary", "MaxByCat", "Margin", "Revenue", "CatShare", "GroupCount"]:
            with self.subTest(name):
                self.assertNotIn(name, m._delta)


class Semantics(unittest.TestCase):
    def test_last_contributor_removed_makes_blank(self):
        m = model()
        m.set_cell("Salary", None, Employee="e1")
        self.assertIsNone(m.get("DeptSalary", Department="Sales"))
        self.assertIn("DeptSalary", m.delta_log)

    def test_new_group_appears(self):
        m = model()
        m.set_cell("Salary", 50, Employee="e2")
        m.set_cell("Salary", 70, Employee="e4")
        self.assertEqual(m.get("DeptSalary", Department="Sales"), 150)
        self.assertEqual(m.get("DeptSalary", Department="Eng"), 370)

    def test_sum_can_become_zero_without_becoming_blank(self):
        m = model()
        m.set_cell("Salary", -100, Employee="e2")  # e1 = 100 と打ち消し合う
        self.assertEqual(m.get("DeptSalary", Department="Sales"), 0)

    def test_several_edits_to_the_same_cell_before_recalc(self):
        m = model()
        for v in [1, 2, None, 5]:
            m.set_cell("Salary", v, Employee="e1")
        self.assertEqual(m.get("DeptSalary", Department="Sales"), 5)

    def test_disabled(self):
        m = build()
        m.delta_aggregation = False
        m._invalidate()
        m.recalc()
        m.set_cell("Salary", 1, Employee="e1")
        m.recalc()
        self.assertEqual(list(m.delta_log), [])


class MatchesFullRecalc(unittest.TestCase):
    def test_random_edits(self):
        rng = random.Random(42)
        m = model()
        inputs = [n for n, x in m.metrics.items() if x.formula is None]
        for round_ in range(300):
            for _ in range(rng.randint(1, 4)):
                name = rng.choice(inputs)
                meta = m.metrics[name]
                coords = {d: rng.choice(m.dimensions[d].members) for d in meta.dims}
                if rng.random() < 0.35:
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
        # 差分集計の経路を実際に通っていること
        for name in ["RevByCat", "DeptSalary", "Total", "VolCount", "CatMonth", "CountByCat"]:
            with self.subTest(delta_used=name):
                self.assertGreater(m.delta_log.count(name), 20)


if __name__ == "__main__":
    unittest.main()
