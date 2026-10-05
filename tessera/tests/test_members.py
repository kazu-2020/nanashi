"""軸のメンバーの追加。"""
import random
import unittest

from sparse_engine import Model, Named
from sparse_engine.engine import ReferenceEngine

from .test_engines import build_with
from .test_incremental import model as build, same, snapshot

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None


def small() -> Model:
    m = Named(Model())
    m.add_dimension("Product", ["A", "B"])
    m.add_dimension("Category", ["X", "Y"])
    m.add_dimension("Month", ["Jan", "Feb"], ordered=True)
    m.add_property("Product", "Category", "Category", {"A": "X", "B": "Y"})
    m.add_input("Price", ["Product"], {("A",): 10, ("B",): 20})
    m.add_input("Rate", ["Category"], {("X",): 0.5, ("Y",): 2})
    m.add_input("Vol", ["Product", "Month"], {("A", "Jan"): 1, ("A", "Feb"): 2, ("B", "Feb"): 3})
    m.add_formula("Plus", ["Product"], "Price + 1")
    m.add_formula("Filled", ["Product", "Month"], "IFBLANK(Vol, 0)")
    m.add_formula("Looked", ["Product"], "Rate[BY: Product.Category]")
    m.add_formula("Stock", ["Product", "Month"], "PREVIOUS(Month) + Vol")
    m.add_formula("Lag", ["Product", "Month"], "Vol[SELECT: Month - 1]")
    m.add_formula("Tot", ["Month"], "Filled[REMOVE SUM: Product]")
    m.recalc()
    m.slice_log.clear()
    return m


def regions(m: Model) -> dict:
    return {n: {d: set(ms) for d, ms in r.items()} for n, r in m.slice_log}


class Validation(unittest.TestCase):
    def test_duplicate_member(self):
        with self.assertRaisesRegex(ValueError, "すでにある"):
            small().add_member("Product", "A")

    def test_unknown_property(self):
        with self.assertRaisesRegex(ValueError, "プロパティ"):
            small().add_member("Product", "C", Color="red")

    def test_unknown_property_value(self):
        with self.assertRaisesRegex(ValueError, "Z"):
            small().add_member("Product", "C", Category="Z")

    def test_failed_add_leaves_dimension_unchanged(self):
        m = small()
        with self.assertRaises(ValueError):
            m.add_member("Product", "C", Category="Z")
        self.assertEqual(m.dimension("Product").members, ["A", "B"])

    def test_new_member_accepts_input(self):
        m = small()
        m.add_member("Product", "C", Category="Y")
        m.set_cell("Price", 30, Product="C")
        self.assertEqual(m.get("Plus", Product="C"), 31)


class NewProduct(unittest.TestCase):
    def setUp(self):
        self.m = small()
        self.m.add_member("Product", "C", Category="Y")
        self.m.recalc()

    def test_constant_expands_to_new_member(self):
        self.assertEqual(self.m.get("Plus", Product="C"), 1)

    def test_ifblank_fills_new_member(self):
        self.assertEqual([self.m.get("Filled", Product="C", Month=t) for t in ["Jan", "Feb"]], [0, 0])

    def test_lookup_reaches_new_member(self):
        self.assertEqual(self.m.get("Looked", Product="C"), 2)

    def test_only_new_member_is_recomputed(self):
        r = regions(self.m)
        self.assertEqual(r["Plus"], {"Product": {"C"}})
        self.assertEqual(r["Looked"], {"Product": {"C"}})
        self.assertNotIn("Stock", r)  # 新しい商品には Vol がないので、在庫は変わらない

    def test_aggregate_uses_delta(self):
        self.assertIn("Tot", self.m.delta_log)
        self.assertEqual(self.m.get("Tot", Month="Feb"), 5)


class NewMonth(unittest.TestCase):
    def setUp(self):
        self.m = small()
        self.m.add_member("Month", "Mar")
        self.m.recalc()

    def test_previous_carries_into_new_month(self):
        self.assertEqual(self.m.get("Stock", Product="A", Month="Mar"), 3)
        self.assertEqual(self.m.get("Stock", Product="B", Month="Mar"), 3)

    def test_select_shifts_into_new_month(self):
        self.assertEqual(self.m.get("Lag", Product="A", Month="Mar"), 2)

    def test_new_month_group_appears_in_aggregate(self):
        self.assertEqual(self.m.get("Tot", Month="Mar"), 0)  # IFBLANK の 0 の合計（空ではない）

    def test_only_new_month_is_recomputed(self):
        self.assertEqual(regions(self.m)["Stock"], {"Month": {"Mar"}})


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustRepacksWhenKeysOverflow(unittest.TestCase):
    def test_many_regions(self):
        # Region は 2 メンバー（キーは 2 ビット = 4 メンバーまで）。10 個足すと詰め直しが要る
        ref, rs = build_with(ReferenceEngine()), build_with(RustEngine())
        for model in (ref, rs):
            model.recalc()
            for i in range(10):
                model.add_member("Region", f"R{i}")
                model.set_cell("Volume", float(i + 1), Product="A", Region=f"R{i}", Month="Mar")
        for name in ref._metric_ids:
            with self.subTest(name):
                self.assertTrue(same(ref.value(name).cells, rs.value(name).cells))


def random_round(rng: random.Random, models: list[Model], counter: list[int]) -> None:
    """同じ乱数で、同じ操作（セルの変更かメンバーの追加）を全モデルに加える。"""
    m0 = models[0]
    if rng.random() < 0.2:
        counter[0] += 1
        kind = rng.choice(["Product", "Month", "Region", "Category"])
        name = f"{kind[0]}new{counter[0]}"
        props = {}
        if kind == "Product":
            props = {"Category": rng.choice(m0.dimension("Category").members)}
        for m in models:
            m.add_member(kind, name, **props)
        return
    inputs = [x.name for x in m0.metrics.values() if x.formula is None]
    name = rng.choice(inputs)
    meta = m0.metric(name)
    coords = {m0.dimension(d).name: rng.choice(m0.dimension(d).members) for d in meta.dims}
    if rng.random() < 0.3:
        value = None
    elif meta.kind == "boolean":
        value = rng.random() < 0.5
    else:
        value = float(rng.randint(-5, 60))
    for m in models:
        m.set_cell(name, value, **coords)


class MatchesFullRecalc(unittest.TestCase):
    def test_random_edits_and_additions(self):
        rng = random.Random(11)
        m = build()
        m.recalc()
        counter = [0]
        for round_ in range(150):
            for _ in range(rng.randint(1, 3)):
                random_round(rng, [m], counter)
            incremental = snapshot(m)
            m._invalidate()
            full = snapshot(m)
            for name in m._metric_ids:
                with self.subTest(round=round_, metric=name):
                    self.assertTrue(same(incremental[name], full[name]),
                                    f"{name}\n差分: {incremental[name]}\n全体: {full[name]}")
        self.assertGreater(counter[0], 20)  # メンバーの追加を実際に何度も通っていること


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustMatchesReference(unittest.TestCase):
    def test_random_edits_and_additions(self):
        rng = random.Random(5)
        ref, rs = build_with(ReferenceEngine()), build_with(RustEngine())
        counter = [0]
        for round_ in range(120):
            for _ in range(rng.randint(1, 3)):
                random_round(rng, [ref, rs], counter)
            for name in ref._metric_ids:
                with self.subTest(round=round_, metric=name):
                    a, b = ref.value(name).cells, rs.value(name).cells
                    self.assertTrue(same(a, b), f"{name}\n参照: {a}\nRust: {b}")


if __name__ == "__main__":
    unittest.main()
