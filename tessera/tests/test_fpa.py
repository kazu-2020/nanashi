"""損益計画と人員計画のサンプルモデル（examples/fpa.py）の統合テスト。"""
import math
import random
import unittest

from examples.fpa import EDITS, build
from sparse_engine.engine import ReferenceEngine

from .test_incremental import same, snapshot

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None


def small(engine=None):
    return build(engine, employees=24, products=12, months=12, seed=3)


class Identities(unittest.TestCase):
    """会計上の恒等式が成り立つ。"""

    def setUp(self):
        self.m = small()
        self.m.recalc()
        self.months = self.m.dimension("Month").members
        self.cutoff = self.m.get("Cutoff")

    def oi(self, version, month):
        return self.m.get("OperatingIncome", Version=version, Month=month) or 0.0

    def test_forecast_is_actual_then_budget(self):
        for t in self.months:
            source = "実績" if self.months.index(t) <= self.months.index(self.cutoff) else "予算"
            with self.subTest(month=t):
                self.assertTrue(math.isclose(self.oi("見込み", t), self.oi(source, t)))

    def test_variance(self):
        for t in self.months:
            with self.subTest(month=t):
                v = self.m.get("Variance", Month=t) or 0.0
                self.assertTrue(math.isclose(v, self.oi("見込み", t) - self.oi("予算", t), abs_tol=1e-6))

    def test_cash_is_opening_plus_cumulative_income(self):
        opening = self.m.get("OpeningCash")
        for version in ["予算", "見込み"]:
            running = opening
            for t in self.months:
                running += self.oi(version, t)
                with self.subTest(version=version, month=t):
                    self.assertTrue(math.isclose(self.m.get("Cash", Version=version, Month=t), running))

    def test_headcount_matches_employed_people_with_department(self):
        employed = self.m.value("Employed").cells
        dept = self.m.value("DeptOf").cells
        for t in self.months:
            expected = sum(1 for e in self.m.dimension("Employee").members
                           if employed.get((e, t)) and (e, t) in dept)
            got = sum(self.m.get("Headcount", Department=d, Version="予算", Month=t) or 0
                      for d in self.m.dimension("Department").members)
            with self.subTest(month=t):
                self.assertEqual(got, expected)


def apply_random(rng: random.Random, models) -> None:
    """EDITS から 1 つ選び、同じ乱数の状態で全モデルに加える。"""
    label, edit = rng.choice(EDITS)
    state = rng.getstate()
    for m in models:
        r = random.Random()
        r.setstate(state)
        edit(m, r)
    rng.random()  # 状態を進める


class MatchesFullRecalc(unittest.TestCase):
    def test_random_edits(self):
        rng = random.Random(41)
        m = small()
        m.recalc()
        for round_ in range(60):
            for _ in range(rng.randint(1, 2)):
                apply_random(rng, [m])
            incremental = snapshot(m)
            m._invalidate()
            full = snapshot(m)
            for name in m._metric_ids:
                with self.subTest(round=round_, metric=name):
                    self.assertTrue(same(incremental[name], full[name]), name)


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustMatchesReference(unittest.TestCase):
    def test_random_edits(self):
        rng = random.Random(43)
        ref_m, rs = small(ReferenceEngine()), small(RustEngine())
        for round_ in range(60):
            for _ in range(rng.randint(1, 2)):
                apply_random(rng, [ref_m, rs])
            for name in ref_m._metric_ids:
                with self.subTest(round=round_, metric=name):
                    self.assertTrue(same(ref_m.value(name).cells, rs.value(name).cells), name)


if __name__ == "__main__":
    unittest.main()
