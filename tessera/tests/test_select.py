"""特定のメンバーを指定する構文 `[SELECT: Dim."member"]`。"""
import random
import unittest

from sparse_engine import FormulaError, Model, ParseError, parse, ref, to_formula
from sparse_engine.engine import ReferenceEngine

from .test_incremental import same, snapshot

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None

MONTHS = ["Jan", "Feb", "Mar", "Apr"]


def model(engine=None) -> Model:
    m = Model(engine=engine) if engine is not None else Model()
    m.add_dimension("Version", ["予算", "実績", "見込み"])
    m.add_dimension("Product", ["A", "B", "C"])
    m.add_dimension("Category", ["X", "Y"])
    m.add_dimension("Month", MONTHS, ordered=True)
    m.add_property("Product", "Category", "Category", {"A": "X", "B": "X", "C": "Y"})
    m.add_input("Sales", ["Product", "Version", "Month"], {
        ("A", "予算", "Jan"): 100, ("A", "実績", "Jan"): 80, ("A", "見込み", "Jan"): 90,
        ("A", "予算", "Feb"): 120, ("A", "実績", "Feb"): 130,
        ("B", "予算", "Jan"): 50, ("B", "実績", "Mar"): 40,
        ("C", "実績", "Apr"): 10,
    })
    f = m.add_formula
    f("Variance", ["Product", "Month"], 'Sales[SELECT: Version."予算"] - Sales[SELECT: Version."実績"]')
    f("Achieve", ["Product", "Month"], 'Sales[SELECT: Version."実績"] / Sales[SELECT: Version."予算"]')
    f("ActualTotal", ["Month"], 'Sales[SELECT: Version."実績"][REMOVE SUM: Product]')
    f("CatActual", ["Category"], 'Sales[SELECT: Version."実績"][BY SUM: Product.Category][REMOVE SUM: Month]')
    f("LastActual", ["Product"], 'Sales[SELECT: Version."実績", Month."Apr"]')
    f("PrevActual", ["Product", "Month"], 'Sales[SELECT: Version."実績", Month - 1]')
    f("Cum", ["Product", "Month"], 'PREVIOUS(Month) + Sales[SELECT: Version."実績"]')
    m.recalc()
    m.slice_log.clear()
    m.delta_log.clear()
    return m


def cells(m: Model, name: str) -> dict:
    return {k if len(k) != 1 else k[0]: v for k, v in m.value(name).cells.items()}


class Syntax(unittest.TestCase):
    def test_round_trip(self):
        for text, expected in {
            'Sales[SELECT: Version."予算"]': 'Sales[SELECT: Version."予算"]',
            'Sales[select: Version."Budget 2026"]': 'Sales[SELECT: Version."Budget 2026"]',
            'X[SELECT: V."a""b"]': 'X[SELECT: V."a""b"]',
            'X[SELECT: Version."実績", Month - 1]': 'X[SELECT: Version."実績"][SELECT: Month - 1]',
        }.items():
            with self.subTest(text):
                self.assertEqual(to_formula(parse(text)), expected)
                self.assertEqual(to_formula(parse(expected)), expected)

    def test_escaped_quote_in_member(self):
        self.assertEqual(parse('X[SELECT: V."a""b"]').member, 'a"b')

    def test_dsl(self):
        self.assertEqual(to_formula(ref("Sales").select("Version", "実績")), 'Sales[SELECT: Version."実績"]')

    def test_member_needs_double_quotes(self):
        with self.assertRaisesRegex(ParseError, "二重引用符"):
            parse("X[SELECT: Version.実績]")

    def test_unterminated_member(self):
        with self.assertRaisesRegex(ParseError, '" が閉じていない'):
            parse('X[SELECT: Version."実績]')


class TypeChecking(unittest.TestCase):
    def reject(self, formula, dims, pattern):
        m = model()
        with self.assertRaisesRegex(FormulaError, pattern):  # an unknown member fails at the definition (bind)
            m.add_formula("Bad", dims, formula)
            m.recalc()

    def test_unknown_member(self):
        self.reject('Sales[SELECT: Version."計画"]', ["Product", "Month"], "メンバー '計画' がない")

    def test_dim_not_in_expression(self):
        self.reject('ActualTotal[SELECT: Version."実績"]', ["Month"], "Version がない")

    def test_selected_dim_is_removed(self):
        self.reject('Sales[SELECT: Version."実績"]', ["Product", "Version", "Month"], "一致しない")

    def test_select_on_scan_dim_inside_cycle(self):
        self.reject('PREVIOUS(Month) + Bad[SELECT: Month."Jan"][EXPAND: Month]', ["Product", "Month"], "循環参照")


class Values(unittest.TestCase):
    def setUp(self):
        self.m = model()

    def test_variance(self):
        self.assertEqual(cells(self.m, "Variance"), {
            ("A", "Jan"): 20, ("A", "Feb"): -10, ("B", "Jan"): 50, ("B", "Mar"): -40, ("C", "Apr"): -10})

    def test_ratio_only_where_both_exist(self):
        self.assertEqual(cells(self.m, "Achieve"), {("A", "Jan"): 0.8, ("A", "Feb"): 130 / 120})

    def test_aggregate_of_slice(self):
        self.assertEqual(cells(self.m, "ActualTotal"), {"Jan": 80, "Feb": 130, "Mar": 40, "Apr": 10})
        self.assertEqual(cells(self.m, "CatActual"), {"X": 250, "Y": 10})

    def test_two_members_at_once(self):
        self.assertEqual(cells(self.m, "LastActual"), {"C": 10})

    def test_member_and_shift(self):
        self.assertEqual(cells(self.m, "PrevActual"), {("A", "Feb"): 80, ("A", "Mar"): 130, ("B", "Apr"): 40})

    def test_inside_scan(self):
        self.assertEqual([self.m.get("Cum", Product="A", Month=t) for t in MONTHS], [80, 210, 210, 210])


class Incremental(unittest.TestCase):
    def setUp(self):
        self.m = model()

    def recomputed(self) -> dict:
        return {n: {d: set(ms) for d, ms in r.items()} for n, r in self.m.slice_log}

    def test_other_version_does_not_reach(self):
        self.m.set_cell("Sales", 95, Product="A", Version="見込み", Month="Jan")
        self.m.recalc()
        self.assertEqual(self.recomputed(), {})

    def test_selected_version_reaches(self):
        self.m.set_cell("Sales", 85, Product="A", Version="実績", Month="Jan")
        self.m.recalc()
        r = self.recomputed()
        self.assertEqual(r["Variance"], {"Product": {"A"}, "Month": {"Jan"}})
        self.assertNotIn("LastActual", r)  # Apr ではないので届かない
        self.assertEqual(self.m.get("Variance", Product="A", Month="Jan"), 15)

    def test_aggregate_of_slice_uses_delta(self):
        self.assertIn(self.m.metric("ActualTotal").id, self.m._delta)
        self.assertIn(self.m.metric("CatActual").id, self.m._delta)
        self.m.set_cell("Sales", 85, Product="A", Version="実績", Month="Jan")
        self.m.recalc()
        self.assertIn("ActualTotal", self.m.delta_log)
        self.assertEqual(self.m.get("ActualTotal", Month="Jan"), 85)


def random_round(rng: random.Random, models: list[Model], counter: list[int]) -> None:
    m0 = models[0]
    if rng.random() < 0.1:
        counter[0] += 1
        kind = rng.choice(["Product", "Month"])
        name = f"{kind[0]}{counter[0]}"
        props = {"Category": rng.choice(["X", "Y"])} if kind == "Product" else {}
        for m in models:
            m.add_member(kind, name, **props)
        return
    coords = {d: rng.choice(m0.dimension(d).members) for d in ["Product", "Version", "Month"]}
    value = None if rng.random() < 0.3 else float(rng.randint(-5, 60))
    for m in models:
        m.set_cell("Sales", value, **coords)


class MatchesFullRecalc(unittest.TestCase):
    def test_random_edits(self):
        rng = random.Random(3)
        m = model()
        counter = [0]
        for round_ in range(200):
            for _ in range(rng.randint(1, 3)):
                random_round(rng, [m], counter)
            incremental = snapshot(m)
            m._invalidate()
            full = snapshot(m)
            for name in m._metric_ids:
                with self.subTest(round=round_, metric=name):
                    self.assertTrue(same(incremental[name], full[name]),
                                    f"{name}\n差分: {incremental[name]}\n全体: {full[name]}")


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustMatchesReference(unittest.TestCase):
    def test_random_edits(self):
        rng = random.Random(9)
        ref_m, rs = model(ReferenceEngine()), model(RustEngine())
        counter = [0]
        for round_ in range(150):
            for _ in range(rng.randint(1, 3)):
                random_round(rng, [ref_m, rs], counter)
            for name in ref_m._metric_ids:
                with self.subTest(round=round_, metric=name):
                    a, b = ref_m.value(name).cells, rs.value(name).cells
                    self.assertTrue(same(a, b), f"{name}\n参照: {a}\nRust: {b}")


if __name__ == "__main__":
    unittest.main()
