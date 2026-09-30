"""Metric ごとの分割軸の指定と自動選択。"""
import unittest

from sparse_engine import Model
from sparse_engine.engine import ReferenceEngine

from .test_engines import build_with
from .test_incremental import same

try:
    from sparse_engine.polars_engine import PolarsEngine
except ImportError:
    PolarsEngine = None


class PartitionedStub(ReferenceEngine):
    """分割数だけを持つ参照実装。分割軸の選び方はエンジンに依存しないので、これで確かめる。"""
    partitions = 256


class AutoLayout(unittest.TestCase):
    def setUp(self):
        self.m = build_with(PartitionedStub())
        self.m.recalc()

    def test_category_aggregate_is_split_by_category(self):
        # Price を 1 商品変えると RevByCat はそのカテゴリ全体（全地域×全月）が変わる。
        # Region や Month で分けると全パーティションに触れるので Category を選ぶ
        self.assertEqual(self.m.layout["RevByCat"], "Category")

    def test_detail_metrics_are_split_by_product(self):
        # Price の変更は商品単位で届くので、Month（5）より少ない Product（4）でも Product を選ぶ
        for name in ["Revenue", "Margin", "Stock", "Outflow"]:
            with self.subTest(name):
                self.assertEqual(self.m.layout[name], "Product")

    def test_input_edited_only_by_itself_uses_finest_dim(self):
        # Volume が変わるのは自分への 1 セル入力だけ。触れる割合が最小の Month（1/5）を選ぶ
        self.assertEqual(self.m.layout["Volume"], "Month")

    def test_scalar_has_no_partition(self):
        self.assertIsNone(self.m.layout["Total"])

    def test_explicit_partition_wins(self):
        self.m.add_formula("Margin2", ["Product", "Month"], "Margin * 2", partition="Month")
        self.m.recalc()
        self.assertEqual(self.m.layout["Margin2"], "Month")

    def test_explicit_partition_must_be_a_dim(self):
        with self.assertRaises(ValueError):
            self.m.add_formula("Bad", ["Product"], "Price * 2", partition="Month")

    def test_disabled_auto_layout_uses_largest_dim(self):
        m = Model(engine=PartitionedStub(), auto_layout=False)
        m.dimensions = self.m.dimensions
        m.add_input("V", ["Category", "Region", "Month"])
        m.recalc()
        self.assertEqual(m.layout["V"], "Month")  # メンバー数 5 が最多


@unittest.skipIf(PolarsEngine is None, "polars が必要")
class PolarsFollowsLayout(unittest.TestCase):
    def test_stores_are_split_by_chosen_dim(self):
        m = build_with(PolarsEngine())
        m.recalc()
        for name in m.metrics:
            with self.subTest(name):
                self.assertEqual(m.raw(name).part_dim, m.layout[name])

    def test_split_is_deferred_until_first_targeted_edit(self):
        ref, pol = build_with(ReferenceEngine()), build_with(PolarsEngine())
        ref.recalc()
        pol.recalc()
        # 全体の再計算の結果は分割を後回しにしている
        self.assertTrue(pol.raw("Revenue").pending)
        for model in (ref, pol):
            model.set_cell("Volume", 9, Product="B", Region="N", Month="Feb")
        for name in ref.metrics:
            with self.subTest(name):
                self.assertTrue(same(ref.value(name).cells, pol.value(name).cells))
        # 商品で絞った書き換えが来たので、Revenue は分割済みになる
        self.assertFalse(pol.raw("Revenue").pending)
        self.assertGreater(len(pol.raw("Revenue").parts), 1)

    def test_explicit_input_partition_keeps_values(self):
        ref = build_with(ReferenceEngine())
        pol = build_with(PolarsEngine())
        for model in (ref, pol):
            cells = model.value("Volume").cells
            model.add_input("Volume", ["Product", "Region", "Month"], cells, partition="Month")
            model.set_cell("Volume", 9, Product="D", Region="S", Month="Apr")
        self.assertEqual(pol.raw("Volume").part_dim, "Month")
        for name in ref.metrics:
            with self.subTest(name):
                self.assertTrue(same(ref.value(name).cells, pol.value(name).cells))


if __name__ == "__main__":
    unittest.main()
