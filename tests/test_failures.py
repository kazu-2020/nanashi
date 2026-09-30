"""計算や入力の途中で失敗しても、モデルが壊れた状態で残らない。"""
import unittest

from sparse_engine.engine import ReferenceEngine

from .test_engines import build_with
from .test_incremental import same, snapshot

try:
    import nanashi_core
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    nanashi_core = RustEngine = None


@unittest.skipIf(RustEngine is None, "nanashi_core が必要")
class FailedRecalc(unittest.TestCase):
    def failing(self, metric: str | None, panic: bool = False) -> None:
        """metric を書き戻すところで Rust の再計算を失敗させる（None なら戻す）。"""
        _, names = self.m.engine.planner._plan_for(self.m.compiled(), self.m)
        self.m.engine.core.configure(fail_at=None if metric is None else (names.index(metric), panic))

    def check(self, panic: bool) -> None:
        self.m = build_with(RustEngine())
        ref = build_with(ReferenceEngine())
        self.m.recalc()
        inputs = {n: self.m.value(n).cells for n, x in self.m.metrics.items() if x.formula is None}
        for m in (self.m, ref):
            m.set_cell("Volume", 50, Product="B", Region="S", Month="Feb")
            m.set_cell("Price", 7, Product="A")
        self.failing("Margin", panic)
        error = RuntimeError if panic else ValueError
        with self.assertRaises(error):
            self.m.value("Revenue")
        with self.assertRaises(error):  # 失敗したままなら何度読んでも失敗する（途中の値を見せない）
            self.m.get("Margin", Product="A", Month="Jan")
        for n, cells in inputs.items():  # 入力の格納データは失われない（計算し直さずに読む）
            if n not in ("Volume", "Price"):
                self.assertEqual(self.m.engine.to_cube(self.m._values[n], self.m).cells, cells)
        self.failing(None)
        expected, actual = snapshot(ref), snapshot(self.m)
        for n in expected:
            self.assertTrue(same(expected[n], actual[n]), f"{n}\n参照: {expected[n]}\nRust: {actual[n]}")

    def test_error_in_the_middle(self):
        self.check(panic=False)

    def test_panic_in_the_middle(self):
        self.check(panic=True)

    def test_failed_recalc_in_a_transaction_rolls_back(self):
        self.m = build_with(RustEngine())
        before = snapshot(self.m)
        self.failing("Margin", panic=True)
        with self.assertRaises(RuntimeError):
            with self.m.transaction():
                self.m.set_cell("Price", 7, Product="A")
        self.failing(None)
        self.assertEqual(snapshot(self.m), before)



@unittest.skipIf(RustEngine is None, "nanashi_core が必要")
class RustBoundary(unittest.TestCase):
    """Python から Rust に渡す番号と長さは、境界で検査する（黙って別のセルを書き換えない）。"""

    def setUp(self):
        self.core = nanashi_core.Core()
        self.a = self.core.add_dim(4, False, "A")
        self.b = self.core.add_dim(4, False, "B")
        self.store = self.core.from_rows([self.a, self.b], self.a, False, [[0, 1], [2, 3]], [1.0, 2.0])

    def cells(self):
        cols, values, _ = self.core.rows(self.store)
        return sorted(zip(zip(*cols), values))

    def test_member_out_of_range_is_rejected(self):
        before = self.cells()
        with self.assertRaisesRegex(ValueError, "メンバー番号 4"):
            self.core.write(self.store, [0, 4], 9.0)  # 4 ビット目に溢れると隣の軸のビットを書き換えていた
        with self.assertRaisesRegex(ValueError, "メンバー番号"):
            self.core.write(self.store, [1 << 20, 0], 9.0)
        with self.assertRaisesRegex(ValueError, "メンバー番号"):
            self.core.write_many(self.store, [[0], [7]], [9.0])
        with self.assertRaisesRegex(ValueError, "メンバー番号"):
            self.core.from_rows([self.a, self.b], self.a, False, [[0], [9]], [1.0])
        with self.assertRaisesRegex(ValueError, "軸の番号 9"):
            self.core.remove_member(self.store, 9, 0, False)
        with self.assertRaisesRegex(ValueError, "メンバー番号"):
            self.core.filter(self.store, [(self.b, [0, 5])])
        self.assertEqual(self.cells(), before)

    def test_lengths_must_match(self):
        with self.assertRaisesRegex(ValueError, "キーの長さ"):
            self.core.write(self.store, [0], 9.0)  # 以前は zip で切り詰めて (0, 0) に書いていた
        with self.assertRaisesRegex(ValueError, "キーの長さ"):
            self.core.get(self.store, [0, 1, 2])
        with self.assertRaisesRegex(ValueError, "列の数"):
            self.core.from_rows([self.a, self.b], self.a, False, [[0, 1], [2]], [1.0, 2.0])
        self.assertEqual(self.core.get(self.store, [0, 2]), 1.0)

    def test_unknown_dimension_and_mapping(self):
        with self.assertRaisesRegex(ValueError, "軸の番号 9"):
            self.core.empty([self.a, 9], None, False)
        with self.assertRaisesRegex(ValueError, "軸の番号 9"):
            self.core.resize_dim(9, 3)
        with self.assertRaisesRegex(ValueError, "対応先のメンバー番号 5"):
            self.core.add_mapping(4, [0, 5])
        with self.assertRaisesRegex(ValueError, "対応表の番号 3"):
            self.core.set_mapping(3, 4, [0])

    def test_store_in_use_by_another_call_is_an_error(self):
        m = build_with(RustEngine())
        m.recalc()
        engine, compiled = m.engine, m.compiled()
        rplan, names = engine.planner._plan_for(compiled, m)
        stores = [m._values[n] for n in names]
        stores[1] = stores[0]  # 同じハンドルを 2 回渡す（2 つ目は書き換えの途中で借りられない）
        with self.assertRaisesRegex(RuntimeError, "使用中"):
            engine.core.recalc_changes(rplan, stores, [None] * len(names), [], [], [], [])
        check_values(self, m)


def check_values(test, m) -> None:
    """m の値が、同じ入力から計算し直した参照実装と一致する。"""
    ref = build_with(ReferenceEngine())
    expected, actual = snapshot(ref), snapshot(m)
    for n in expected:
        test.assertTrue(same(expected[n], actual[n]), f"{n}\n参照: {expected[n]}\n比較先: {actual[n]}")


if __name__ == "__main__":
    unittest.main()
