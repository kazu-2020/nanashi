"""値の変化による影響範囲の絞り込み（値が変わらなければ下流を計算し直さない）。"""
import unittest

from examples.fpa import build
from sparse_engine import Model


def regions(m: Model) -> dict:
    return {n: {d: set(ms) for d, ms in r.items()} for n, r in m.slice_log}


class Pruning(unittest.TestCase):
    def test_unchanged_value_stops_propagation(self):
        m = Model()
        m.add_dimension("Product", ["A", "B"])
        m.add_input("X", ["Product"], {("A",): 20, ("B",): 5})
        m.add_formula("Big", ["Product"], "IF(X > 10, 1, 0)")
        m.add_formula("Scaled", ["Product"], "Big * 100")
        m.recalc()
        m.slice_log.clear()
        m.set_cell("X", 30, Product="A")  # 10 を超えたままなので Big は変わらない
        m.recalc()
        self.assertIn("Big", regions(m))
        self.assertNotIn("Scaled", regions(m))
        m.slice_log.clear()
        m.set_cell("X", 1, Product="A")  # 10 以下になるので Big が変わる
        m.recalc()
        self.assertEqual(regions(m)["Scaled"], {"Product": {"A"}})
        self.assertEqual(m.get("Scaled", Product="A"), 0)

    def test_moving_cutoff_recomputes_only_the_crossed_month(self):
        m = build(None, employees=12, products=6, months=12, seed=1)
        m.recalc()
        months = m.dimensions["Month"].members
        cutoff = m.get("Cutoff")
        nxt = months[months.index(cutoff) + 1]
        m.slice_log.clear()
        m.set_cell("Cutoff", nxt)
        m.recalc()
        r = regions(m)
        self.assertEqual(r["IsActual"], {})  # 締め月そのものは全月に効きうる
        self.assertEqual(r["InPeriod"]["Month"], {nxt})  # 実際に変わったのは 1 か月だけ
        self.assertEqual(r["Revenue"]["Month"], {nxt})


if __name__ == "__main__":
    unittest.main()
