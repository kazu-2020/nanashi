"""軸のメンバーを値として使う式（`Month <= Month."Feb"`、`Version = Version."見込み"`）。"""
import random
import unittest

from sparse_engine import FormulaError, Model, dim, member, parse, to_formula
from sparse_engine.engine import ReferenceEngine

from .test_incremental import cells, same, snapshot

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None

MONTHS = ["Jan", "Feb", "Mar", "Apr"]


def model(engine=None) -> Model:
    m = Model(engine=engine) if engine is not None else Model()
    m.add_dimension("Version", ["予算", "実績", "見込み"])
    m.add_dimension("Product", ["A", "B"])
    m.add_dimension("Month", MONTHS, ordered=True)
    m.add_input("Sales", ["Product", "Version", "Month"], {
        ("A", "予算", "Jan"): 100, ("A", "予算", "Feb"): 110, ("A", "予算", "Mar"): 120, ("A", "予算", "Apr"): 130,
        ("A", "実績", "Jan"): 80, ("A", "実績", "Feb"): 115, ("A", "実績", "Mar"): 999,
        ("A", "見込み", "Jan"): 90,
        ("B", "予算", "Mar"): 50, ("B", "実績", "Jan"): 40,
    })
    f = m.add_formula
    # 見込み: 締め月（Feb）までは実績、それ以降は予算
    f("Forecast", ["Product", "Month"],
      'IF(Month <= Month."Feb", Sales[SELECT: Version."実績"], Sales[SELECT: Version."予算"])')
    # 見込み版だけ式で置き換え、他の版は入力のまま
    f("Merged", ["Product", "Version", "Month"], 'IF(Version = Version."見込み", Forecast, Sales)')
    f("IsPast", ["Month"], 'Month <= Month."Feb"', kind="boolean")
    f("PastCount", [], "IsPast[FILTER: IsPast][REMOVE COUNT: Month]")
    m.recalc()
    m.slice_log.clear()
    return m


class Syntax(unittest.TestCase):
    def test_round_trip(self):
        text = 'IF(Month <= Month."Feb", A, B)'
        self.assertEqual(to_formula(parse(text)), text)
        self.assertEqual(to_formula(parse('Version = Version."見""込み"')), 'Version = Version."見""込み"')

    def test_dsl(self):
        self.assertEqual(to_formula(dim("Month") <= member("Month", "Feb")), 'Month <= Month."Feb"')


class Values(unittest.TestCase):
    def setUp(self):
        self.m = model()

    def test_switchover(self):
        self.assertEqual(cells(self.m, "Forecast"), {
            ("A", "Jan"): 80, ("A", "Feb"): 115, ("A", "Mar"): 120, ("A", "Apr"): 130,
            ("B", "Mar"): 50,  # B の Jan の実績 40 は Feb 以前なので入る
            ("B", "Jan"): 40,
        })

    def test_version_specific_formula(self):
        c = cells(self.m, "Merged")
        self.assertEqual(c[("A", "見込み", "Jan")], 80)  # 入力の 90 ではなく、見込みの式
        self.assertEqual(c[("A", "見込み", "Apr")], 130)
        self.assertEqual(c[("A", "予算", "Jan")], 100)  # 他の版は入力のまま
        self.assertEqual(c[("A", "実績", "Mar")], 999)

    def test_member_comparison_is_by_order(self):
        self.assertEqual(cells(self.m, "IsPast"), {("Jan",): True, ("Feb",): True, ("Mar",): False, ("Apr",): False})
        self.assertEqual(self.m.get("PastCount"), 2)


class TypeChecking(unittest.TestCase):
    def reject(self, formula, dims, pattern, kind="number"):
        m = model()
        m.add_formula("Bad", dims, formula, kind=kind)
        with self.assertRaisesRegex(FormulaError, pattern):
            m.recalc()

    def test_unordered_dim_cannot_be_ordered(self):
        self.reject('Version < Version."実績"', ["Version"], "順序付きの軸ではない", kind="boolean")

    def test_members_of_different_dims(self):
        self.reject('IF(Month = Version."実績", 1, 0)', ["Month"], "種類が違う")

    def test_no_arithmetic_on_members(self):
        self.reject("Month + 1", ["Month"], "number が必要")

    def test_unknown_member(self):
        self.reject('IF(Month <= Month."Dec", 1, 0)', ["Month"], "メンバー 'Dec' がない")

    def test_member_cannot_be_stored(self):
        self.reject("Month", ["Month"], "member:Month だが number")

    def test_name_collisions(self):
        m = model()
        with self.assertRaisesRegex(ValueError, "同じ名前の軸"):
            m.add_input("Month", ["Month"])
        with self.assertRaisesRegex(ValueError, "同じ名前の Metric"):
            m.add_dimension("Sales", ["x"])


class Incremental(unittest.TestCase):
    def test_edit_reaches_only_its_month(self):
        m = model()
        m.set_cell("Sales", 70, Product="A", Version="実績", Month="Jan")
        m.recalc()
        r = {n: {d: set(ms) for d, ms in reg.items()} for n, reg in m.slice_log}
        self.assertEqual(r["Forecast"], {"Product": {"A"}, "Month": {"Jan"}})
        self.assertNotIn("IsPast", r)
        self.assertEqual(m.get("Merged", Product="A", Version="見込み", Month="Jan"), 70)

    def test_new_month(self):
        m = model()
        m.add_member("Month", "May")
        m.set_cell("Sales", 140, Product="A", Version="予算", Month="May")
        m.recalc()
        self.assertEqual(m.get("IsPast", Month="May"), False)
        self.assertEqual(m.get("Forecast", Product="A", Month="May"), 140)
        incremental = snapshot(m)
        m._invalidate()
        self.assertEqual(incremental, snapshot(m))


def random_round(rng: random.Random, models: list[Model], counter: list[int]) -> None:
    m0 = models[0]
    if rng.random() < 0.1:
        counter[0] += 1
        kind = rng.choice(["Product", "Month"])
        for m in models:
            m.add_member(kind, f"{kind[0]}{counter[0]}")
        return
    coords = {d: rng.choice(m0.dimensions[d].members) for d in ["Product", "Version", "Month"]}
    value = None if rng.random() < 0.3 else float(rng.randint(-5, 60))
    for m in models:
        m.set_cell("Sales", value, **coords)


class MatchesFullRecalc(unittest.TestCase):
    def test_random_edits(self):
        rng = random.Random(21)
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
        rng = random.Random(23)
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
