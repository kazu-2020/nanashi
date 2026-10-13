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
                   RustEngine(par_min=0, dense_always=True), RustEngine(dense_always=True)]
        for engine in engines:
            m = model(engine)
            m.recalc()
            got = {n: cells(m, n) for n in ["ByMonth", "ByHome", "ByDeptOf", "Total", "HomeTotal", "NorthByMonth",
                                            "Avg"]}
            if want is None:
                want = got
                self.assertEqual(got["Total"][()], math.fsum(cells(m, "Amount").values()))
                continue
            self.assertEqual(got, want, engine)


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


if __name__ == "__main__":
    unittest.main()
