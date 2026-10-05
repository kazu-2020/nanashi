"""モデルの複製（ホワットイフ分析）。複製と元の変更が互いに影響しない。"""
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
    m = build(engine, employees=16, products=8, months=12, seed=7)
    m.add_formula("PlanOI", ["Version", "Month"], "OperatingIncome", overridable=True)
    m.recalc()
    return m


def what_if(m, seed: int) -> None:
    """値上げ、社員の追加、上書きなど、ランダムな変更を加える。"""
    rng = random.Random(seed)
    for _ in range(6):
        _, edit = rng.choice(EDITS)
        edit(m, rng)
    m.add_member("Employee", f"new{seed}")  # 社員の追加を必ず含める
    m.set_cell("Salary", 500.0, Employee=f"new{seed}", Version="予算")
    m.set_cell("HireMonth", m.dimensions["Month"].members[0], Employee=f"new{seed}")
    m.set_cell("PlanOI", 1.0, Version="予算", Month=m.dimensions["Month"].members[0])
    m.spread("OtherOpex", 9_000.0, Version="予算", Month=m.dimensions["Month"].members[-1])


def check_equal(test, a: dict, b: dict) -> None:
    test.assertEqual(set(a), set(b))
    for name in a:
        with test.subTest(metric=name):
            test.assertTrue(same(a[name], b[name]), name)


class Fork(unittest.TestCase):
    engine = staticmethod(ReferenceEngine)

    def test_changes_in_fork_do_not_touch_original(self):
        original = small(self.engine())
        before = snapshot(original)
        fork = original.fork()
        what_if(fork, 1)
        check_equal(self, snapshot(original), before)
        self.assertNotEqual(len(fork.dimensions["Employee"].members), len(original.dimensions["Employee"].members))

    def test_changes_in_original_do_not_touch_fork(self):
        original = small(self.engine())
        fork = original.fork()
        before = snapshot(fork)
        what_if(original, 2)
        check_equal(self, snapshot(fork), before)

    def test_fork_recalculates_correctly(self):
        fork = small(self.engine()).fork()
        what_if(fork, 3)
        incremental = snapshot(fork)
        fork._invalidate()
        check_equal(self, incremental, snapshot(fork))

    def test_fork_matches_same_edits_on_original(self):
        a = small(self.engine())
        b = a.fork()
        c = small(self.engine())
        what_if(b, 4)
        what_if(c, 4)
        check_equal(self, snapshot(b), snapshot(c))

    def test_fork_of_fork(self):
        original = small(self.engine())
        first = original.fork()
        what_if(first, 5)
        before = snapshot(first)
        second = first.fork()
        what_if(second, 6)
        check_equal(self, snapshot(first), before)


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustFork(Fork):
    engine = staticmethod(RustEngine) if RustEngine is not None else None

    def test_renaming_in_fork_keeps_original_layout_choices(self):
        """分割軸の選択に使う影響範囲（入力ごと。分割軸を選ぶ Rust のエンジンだけが持つ）も、複製と元で別々に持つ。"""
        original = small(self.engine())
        before = {src: set(regions) for src, regions in original._samples.items()}
        plan_oi = original.metric("PlanOI").id
        self.assertTrue(any(plan_oi in r for r in before.values()))
        fork = original.fork()
        fork.rename_metric("PlanOI", "PlanOI2")
        fork.remove_metric("PlanOI2")
        self.assertEqual({src: set(regions) for src, regions in original._samples.items()}, before)
        self.assertFalse(any(plan_oi in r for r in fork._samples.values()))


if __name__ == "__main__":
    unittest.main()
