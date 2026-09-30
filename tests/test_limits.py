"""エンジンの上限の検査と、観察用の記録の上限。"""
import unittest

from sparse_engine import Model
from sparse_engine.engine import ReferenceEngine
from sparse_engine.model import LOG_MAX

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None


def wide(engine) -> Model:
    m = Model(engine=engine)
    for i in range(5):
        m.add_dimension(f"D{i}", [f"m{j}" for j in range(1 << 13)])  # 13 ビット × 5 = 65 ビット
    return m


class KeyWidth(unittest.TestCase):
    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_rust_rejects_metrics_wider_than_64_bits_with_a_hint(self):
        m = wide(RustEngine())
        with self.assertRaisesRegex(ValueError, "64 ビット.*D0 13 ビット.*軸を減らす"):
            m.add_input("Wide", [f"D{i}" for i in range(5)])
        with self.assertRaisesRegex(ValueError, "64 ビット"):
            m.add_formula("Wide2", [f"D{i}" for i in range(5)], "1")
        m.add_input("Ok", [f"D{i}" for i in range(4)])  # 52 ビットは入る
        self.assertNotIn("Ok", m.warnings)

    def test_reference_has_no_width_limit(self):
        m = wide(ReferenceEngine())
        m.add_input("Wide", [f"D{i}" for i in range(5)])


class Logs(unittest.TestCase):
    def test_observation_logs_do_not_grow_without_bound(self):
        m = Model(engine=ReferenceEngine())
        m.add_dimension("P", ["a", "b"])
        m.add_input("X", ["P"], {("a",): 1})
        m.add_formula("Y", ["P"], "X * 2")
        for i in range(LOG_MAX + 50):
            m.set_cell("X", float(i), P="a")
            m.recalc()
        self.assertEqual(len(m.eval_log), LOG_MAX)
        self.assertEqual(len(m.slice_log), LOG_MAX)
        self.assertEqual(m.eval_log[-1], "Y")


if __name__ == "__main__":
    unittest.main()
