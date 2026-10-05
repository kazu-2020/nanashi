"""Metric ごとの分割軸の指定と自動選択。"""
import unittest

from sparse_engine import Model, Named
from sparse_engine.engine import ReferenceEngine

from .test_engines import build_with
from .test_incremental import same

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None


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
        self.assertEqual(self.m.layout[self.m.metric("RevByCat").id], self.m.dimension_id("Category"))

    def test_detail_metrics_are_split_by_product(self):
        # Price の変更は商品単位で届くので、Month（5）より少ない Product（4）でも Product を選ぶ
        for name in ["Revenue", "Margin", "Stock", "Outflow"]:
            with self.subTest(name):
                self.assertEqual(self.m.layout[self.m.metric(name).id], self.m.dimension_id("Product"))

    def test_input_edited_only_by_itself_uses_finest_dim(self):
        # Volume が変わるのは自分への 1 セル入力だけ。触れる割合が最小の Month（1/5）を選ぶ
        self.assertEqual(self.m.layout[self.m.metric("Volume").id], self.m.dimension_id("Month"))

    def test_scalar_has_no_partition(self):
        self.assertIsNone(self.m.layout[self.m.metric("Total").id])

    def test_explicit_partition_wins(self):
        self.m.add_formula("Margin2", ["Product", "Month"], "Margin * 2", partition="Month")
        self.m.recalc()
        self.assertEqual(self.m.layout[self.m.metric("Margin2").id], self.m.dimension_id("Month"))

    def test_explicit_partition_must_be_a_dim(self):
        with self.assertRaises(ValueError):
            self.m.add_formula("Bad", ["Product"], "Price * 2", partition="Month")

    def test_disabled_auto_layout_uses_largest_dim(self):
        m = Named(Model(engine=PartitionedStub(), auto_layout=False))
        m.dimensions, m._dim_ids = self.m.dimensions, self.m._dim_ids
        m.add_input("V", ["Category", "Region", "Month"])
        m.recalc()
        self.assertEqual(m.layout[m.metric("V").id], m.dimension_id("Month"))  # メンバー数 5 が最多


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustFollowsLayout(unittest.TestCase):
    def test_stores_are_split_by_chosen_dim(self):
        m = build_with(RustEngine())
        m.recalc()
        for name in m._metric_ids:
            with self.subTest(name):
                self.assertEqual(m.engine.partition_of(m.raw(name)), m.layout[m.metric(name).id])

    def test_explicit_input_partition_keeps_values(self):
        ref = build_with(ReferenceEngine())
        rs = build_with(RustEngine())
        for model in (ref, rs):
            cells = model.value("Volume").cells
            model.add_input("Volume", ["Product", "Region", "Month"], cells, partition="Month", id=model.metric("Volume").id)
            model.set_cell("Volume", 9, Product="D", Region="S", Month="Apr")
        self.assertEqual(rs.engine.partition_of(rs.raw("Volume")), rs.dimension_id("Month"))
        for name in ref._metric_ids:
            with self.subTest(name):
                self.assertTrue(same(ref.value(name).cells, rs.value(name).cells))


if __name__ == "__main__":
    unittest.main()
