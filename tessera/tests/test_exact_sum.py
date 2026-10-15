"""SUM gives the correctly rounded exact sum. The result does not change with the order of the rows, the aggregation
path (few groups, dense array, sort) or the thread count."""
import math
from fractions import Fraction
import random
import unittest

from sparse_engine import Model, Named
from sparse_engine.engine import ReferenceEngine
from sparse_engine.expr import AGGREGATORS

from .test_incremental import cells

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core is not built
    RustEngine = None

EMPLOYEES = [f"e{i}" for i in range(12)]
MONTHS = [f"m{i}" for i in range(6)]
REGIONS = ["N", "S", "W"]
DEPTS = ["D0", "D1", "D2"]
VALUES = [0.1, 0.7, 1e-3, 123.45, 1e9 + 0.01, -2.5]


def value(rng: random.Random) -> float:
    return rng.choice(VALUES) * rng.randint(1, 9)


def model(engine=None, seed: int = 7) -> Model:
    rng = random.Random(seed)
    m = Named(Model(engine=engine if engine is not None else ReferenceEngine()))
    m.add_dimension("Employee", EMPLOYEES)
    m.add_dimension("Department", DEPTS)
    m.add_dimension("Region", REGIONS)
    m.add_dimension("Month", MONTHS, ordered=True)
    m.add_property("Employee", "Home", "Department", {e: DEPTS[i % 3] for i, e in enumerate(EMPLOYEES)})
    m.add_input("Amount", ["Employee", "Region", "Month"],
                {(e, r, t): value(rng) for e in EMPLOYEES for r in REGIONS for t in MONTHS if rng.random() < 0.7})
    m.add_input("DeptOf", ["Employee", "Month"],
                {(e, t): rng.choice(DEPTS) for e in EMPLOYEES for t in MONTHS}, kind="member:Department")
    f = m.add_formula
    f("ByMonth", ["Region", "Month"], "Amount[REMOVE SUM: Employee]")
    f("ByHome", ["Department", "Region", "Month"], "Amount[BY SUM: Employee.Home]")
    f("ByDeptOf", ["Department", "Region", "Month"], "Amount[BY SUM: Employee.DeptOf]")
    f("Total", [], "Amount[REMOVE SUM: Employee, Region, Month]")
    f("HomeTotal", ["Department"], "Amount[BY SUM: Employee.Home][REMOVE SUM: Region, Month]")
    f("DeptOfTotal", ["Department"], "Amount[BY SUM: Employee.DeptOf][REMOVE SUM: Region, Month]")
    f("DeptOfAll", [], "Amount[BY SUM: Employee.DeptOf][REMOVE SUM: Department, Month, Region]")
    f("NorthByMonth", ["Month"], 'Amount[SELECT: Region."N"][REMOVE SUM: Employee]')
    f("Avg", ["Region"], "Amount[REMOVE AVG: Employee, Month]")
    return m


class ExactSumFunction(unittest.TestCase):
    def test_sum_is_correctly_rounded(self):
        s = AGGREGATORS["sum"]
        self.assertEqual(s([0.1] * 10), 1.0)
        self.assertEqual(s([1.0, 2.0 ** -53]), 1.0)  # a tie rounds to even
        self.assertEqual(s([1.0, 2.0 ** -53, 2.0 ** -106]), 1.0000000000000002)

    def test_special_values_do_not_depend_on_the_order(self):
        s = AGGREGATORS["sum"]
        big = 1.7976931348623157e308
        self.assertTrue(math.isnan(s([big, big, -big])))
        self.assertTrue(math.isnan(s([big, -big, big])))
        self.assertEqual(s([1.0, math.inf]), math.inf)
        self.assertTrue(math.isnan(s([math.inf, 1.0, -math.inf])))


@unittest.skipIf(RustEngine is None, "nanashi_core must be built")
class SameBitsOnEveryPath(unittest.TestCase):
    def test_paths_and_thread_counts_give_the_same_bits(self):
        want = None
        engines = [ReferenceEngine(), RustEngine(), RustEngine(par_min=0), RustEngine(par_min=0, stream_always=True),
                   RustEngine(par_min=0, dense_always=True), RustEngine(dense_always=True),
                   RustEngine(par_min=0, dense_always=True, scatter_bytes=0), RustEngine(dense_always=True, scatter_bytes=0)]
        for engine in engines:
            m = model(engine)
            m.recalc()
            got = {n: cells(m, n) for n in ["ByMonth", "ByHome", "ByDeptOf", "Total", "HomeTotal", "DeptOfTotal",
                                            "DeptOfAll", "NorthByMonth", "Avg"]}
            if want is None:
                want = got
                self.assertEqual(got["Total"][()], math.fsum(cells(m, "Amount").values()))
                continue
            self.assertEqual(got, want, engine)


class OneRoundingForRemoveRun(unittest.TestCase):
    def test_remove_sum_of_two_dimensions_rounds_one_time(self):
        for engine in [ReferenceEngine] + ([RustEngine] if RustEngine is not None else []):
            with self.subTest(engine=engine.__name__):
                m = Named(Model(engine=engine()))
                m.add_dimension("A", ["a0", "a1"])
                m.add_dimension("B", ["b0", "b1"])
                tiny = 2.0 ** -53
                m.add_input("X", ["A", "B"], {("a0", "b0"): 1.0, ("a1", "b0"): tiny, ("a0", "b1"): tiny})
                m.add_formula("T", [], "X[REMOVE SUM: A, B]")
                self.assertEqual(m.get("T"), 1.0000000000000002)  # one rounding per dimension gives 1.0
                m.delta_log.clear()
                m.set_cell("X", tiny, A="a1", B="b1")
                self.assertEqual(m.get("T"), 1.0000000000000004)
                self.assertIn("T", m.delta_log)


class MinMaxSpecialValues(unittest.TestCase):
    def test_functions_do_not_depend_on_the_order(self):
        mn, mx = AGGREGATORS["min"], AGGREGATORS["max"]
        for vs in [[1.0, math.nan, 2.0], [math.nan, 1.0, 2.0], [1.0, 2.0, math.nan]]:
            self.assertTrue(math.isnan(mn(vs)), vs)
            self.assertTrue(math.isnan(mx(vs)), vs)
        for vs in [[0.0, -0.0], [-0.0, 0.0]]:
            self.assertEqual((repr(mn(vs)), repr(mx(vs))), ("-0.0", "0.0"), vs)

    def test_engines_and_paths_give_the_same_result(self):
        engines = [ReferenceEngine] + ([] if RustEngine is None else [
            RustEngine, lambda: RustEngine(par_min=0, dense_always=True), lambda: RustEngine(stream_always=True)])
        groups = {"nan": [math.inf, math.inf], "nan1": [math.inf, 1.0], "nan2": [1.0, math.inf],
                  "zero1": [0.0, -0.0], "zero2": [-0.0, 0.0]}
        for engine in engines:
            m = Named(Model(engine=engine()))
            m.add_dimension("E", ["a", "b"])
            m.add_dimension("G", list(groups))
            m.add_input("X", ["G", "E"], {(g, e): v for g, vs in groups.items() for e, v in zip("ab", vs)})
            m.add_formula("Y", ["G", "E"], "IF(X > 1, X - X, X)")  # inf - inf is NaN
            m.add_formula("Mn", ["G"], "Y[REMOVE MIN: E]")
            m.add_formula("Mx", ["G"], "Y[REMOVE MAX: E]")
            got = {f: {g: repr(v) for (g,), v in sorted(cells(m, f).items())} for f in ["Mn", "Mx"]}
            self.assertEqual(got, {
                "Mn": {"nan": "nan", "nan1": "nan", "nan2": "nan", "zero1": "-0.0", "zero2": "-0.0"},
                "Mx": {"nan": "nan", "nan1": "nan", "nan2": "nan", "zero1": "0.0", "zero2": "0.0"}}, engine)

    def test_incremental_recalc_sees_the_sign_of_zero(self):
        engines = [ReferenceEngine] + ([] if RustEngine is None else [
            RustEngine, lambda: RustEngine(par_min=0, dense_always=True), lambda: RustEngine(stream_always=True)])
        for engine in engines:
            m = Named(Model(engine=engine()))
            m.add_dimension("E", ["a", "b"])
            m.add_dimension("G", ["g"])
            m.add_input("X", ["G", "E"], {("g", "a"): 0.0, ("g", "b"): 1.0})
            m.add_formula("Mn", ["G"], "X[REMOVE MIN: E]")
            m.add_formula("Top", [], "Mn[REMOVE MAX: G]")
            m.recalc()
            m.set_cell("X", -0.0, G="g", E="b")
            m.recalc()
            full = Named(m.fork())
            full._invalidate()
            got = [repr(cells(x, f)[k]) for x in (m, full) for f, k in [("Mn", ("g",)), ("Top", ())]]
            with self.subTest(engine=m.engine.name):
                self.assertEqual(got, ["-0.0", "-0.0", "-0.0", "-0.0"])


def lows(m: Model, name: str) -> dict:
    """The residual store of an incremental SUM, with member ids as keys."""
    mid = Named(m).metric(name).id
    return dict(m.engine.to_cube(m._lows[mid], m).cells)


class ResidualStore(unittest.TestCase):
    engine = staticmethod(ReferenceEngine)

    def test_value_and_residual_hold_the_exact_sum(self):
        m = model(self.engine())
        m.recalc()
        amount = Named(m).metric("Amount")
        cube = m.engine.to_cube(m._values[amount.id], m)
        month = amount.dims.index(Named(m).dimension("Month").id)
        region = amount.dims.index(Named(m).dimension("Region").id)
        exact: dict = {}
        for k, v in cube.cells.items():
            g = (k[region], k[month])
            exact[g] = exact.get(g, Fraction(0)) + Fraction(v)
        mid = Named(m).metric("ByMonth").id
        values = m.engine.to_cube(m._values[mid], m).cells
        low = dict(m.engine.to_cube(m._lows[mid], m).cells)
        self.assertEqual(values.keys(), exact.keys())
        for g, x in exact.items():
            self.assertEqual(Fraction(values[g]) + Fraction(low.get(g, 0.0)), x, g)
        self.assertTrue(low)  # the data has groups with a residual
        self.assertNotIn(0.0, low.values())  # the store keeps only the cells that are not 0


@unittest.skipIf(RustEngine is None, "nanashi_core must be built")
class RustResidualStore(ResidualStore):
    engine = staticmethod(lambda: RustEngine())


DELTA = ["ByMonth", "ByHome", "ByDeptOf", "Total", "HomeTotal", "DeptOfTotal", "DeptOfAll", "NorthByMonth"]


def snapshot(m: Model) -> dict:
    return {n: cells(m, n) for n in Named(m)._metric_ids if n not in ("Amount", "DeptOf")}


def edit(m: Model, rng: random.Random) -> None:
    for _ in range(rng.randint(1, 3)):
        e, t = rng.choice(EMPLOYEES), rng.choice(MONTHS)
        if rng.random() < 0.2:
            m.set_cell("DeptOf", rng.choice(DEPTS), Employee=e, Month=t)  # a transfer
        else:
            v = None if rng.random() < 0.25 else value(rng)
            m.set_cell("Amount", v, Employee=e, Region=rng.choice(REGIONS), Month=t)


class IncrementalIsExact(unittest.TestCase):
    """After random edits with non-integer values of mixed magnitude, incremental aggregation gives the same bits
    as a full recalculation and as the reference implementation."""

    def test_random_edits(self):
        rng = random.Random(20261013)
        models = [model(ReferenceEngine())] + ([model(RustEngine())] if RustEngine is not None else [])
        for m in models:
            m.recalc()
            m.delta_log.clear()
        nan_lows = 0
        for round_ in range(300):
            state = rng.getstate()
            results = []
            for m in models:
                rng.setstate(state)
                edit(m, rng)
                incremental = snapshot(m)
                nan_lows += sum(math.isnan(v) for n in DELTA for v in lows(m, n).values())
                full = Named(m.fork())
                full._invalidate()
                with self.subTest(round=round_, engine=m.engine.name):
                    self.assertEqual(incremental, snapshot(full))
                results.append(incremental)
            self.assertEqual(results[0], results[-1], f"round {round_}: Rust and reference differ")
        for m in models:
            for name in DELTA:
                with self.subTest(delta_used=name, engine=m.engine.name):
                    self.assertGreater(list(m.delta_log).count(name), 50)
        self.assertEqual(nan_lows, 0)

    def test_residual_that_is_not_one_float_calculates_again(self):
        for engine in [ReferenceEngine] + ([RustEngine] if RustEngine is not None else []):
            with self.subTest(engine=engine.__name__):
                m = model(engine())
                for i, v in enumerate([1.0, 2.0 ** -60, 2.0 ** -130]):
                    m.set_cell("Amount", v, Employee=EMPLOYEES[i], Region="N", Month="m0")
                for e in EMPLOYEES[3:]:
                    m.set_cell("Amount", None, Employee=e, Region="N", Month="m0")
                m.recalc()
                self.assertTrue(any(math.isnan(v) for v in lows(m, "ByMonth").values()))
                m.delta_log.clear()
                m.set_cell("Amount", 0.5, Employee=EMPLOYEES[5], Region="N", Month="m0")
                self.assertEqual(m.get("ByMonth", Region="N", Month="m0"), 1.5)
                self.assertNotIn("ByMonth", m.delta_log)  # the residual was NaN: no incremental aggregation
                full = Named(m.fork())
                full._invalidate()
                self.assertEqual(snapshot(m), snapshot(full))

    def test_residual_store_with_a_different_key_packing(self):
        # The redefinition makes new residual and count stores with a wider key packing than the value store
        for engine in [ReferenceEngine] + ([RustEngine] if RustEngine is not None else []):
            with self.subTest(engine=engine.__name__):
                rng = random.Random(1)
                m = Named(Model(engine=engine()))
                members = {d: [f"{d.lower()}{i}" for i in range(3)] for d in "ABC"}
                for d, ms in members.items():
                    m.add_dimension(d, ms)
                m.add_input("X", ["A", "B", "C"], {(a, b, c): rng.choice(VALUES) for a in members["A"]
                                                   for b in members["B"] for c in members["C"]})
                m.add_formula("T", ["B", "C"], "X[REMOVE SUM: A]")
                m.recalc()
                m.add_member("C", "c3")
                m.add_member("C", "c4")
                m.recalc()
                m.add_formula("T", ["B", "C"], "X[REMOVE SUM: A]", id=m.metric("T").id)
                m.recalc()
                m.delta_log.clear()
                for _ in range(30):
                    m.set_cell("X", value(rng), **{d: rng.choice(ms) for d, ms in members.items()})
                    full = Named(m.fork())
                    full._invalidate()
                    self.assertEqual(cells(m, "T"), cells(full, "T"))
                self.assertIn("T", m.delta_log)


if __name__ == "__main__":
    unittest.main()
