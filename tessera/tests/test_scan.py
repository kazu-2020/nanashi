"""A scan with a chain of +, -, *, / and COALESCE around PREVIOUS gives the same values as the reference implementation, bit for bit."""
import random
import unittest

from sparse_engine import Model, Named
from sparse_engine.engine import ReferenceEngine

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core is not built
    RustEngine = None

MONTHS = [f"m{i:02d}" for i in range(1, 9)]
SCANS = [
    ("Stock", "PREVIOUS(Month) * 0.9 + A"),
    ("Decay", "A - PREVIOUS(Month) / 3"),  # PREVIOUS on the right side of - and /
    ("Ratio", "100 / (PREVIOUS(Month) + A)"),  # a division by 0 is blank
    ("Spread", "PREVIOUS(Month) + Price[EXPAND: Month]"),  # an operand expanded along the time dimension
    ("Scaled", "PREVIOUS(Month) * Rate + A"),  # a lookup in an operand with fewer dimensions
    ("Cash", "PREVIOUS(Month) + A - B"),  # overridable: COALESCE around the chain
]


def model(engine) -> Named:
    m = Named(Model(engine=engine))
    m.add_dimension("Product", ["p1", "p2", "p3"])
    m.add_dimension("Month", MONTHS, ordered=True)
    m.add_input("A", ["Product", "Month"], {("p1", "m01"): 1.5, ("p2", "m03"): -2.25})
    m.add_input("B", ["Product", "Month"], {("p1", "m02"): 0.1})
    m.add_input("Price", ["Product"], {("p2",): 0.3})
    m.add_input("Rate", ["Product"], {("p1",): 1.1, ("p3",): 0.7})
    for name, formula in SCANS:
        m.add_formula(name, ["Product", "Month"], formula, overridable=name == "Cash")
    return m


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class MatchesReference(unittest.TestCase):
    def test_random_edits(self):
        rng = random.Random(20261013)
        ref, rs = model(ReferenceEngine()), model(RustEngine())
        inputs = {"A": ["Product", "Month"], "B": ["Product", "Month"], "Price": ["Product"], "Rate": ["Product"],
                  "Cash": ["Product", "Month"]}  # set_cell on Cash writes its override
        for round_ in range(200):
            name = rng.choice(list(inputs))
            coords = {d: rng.choice(ref.dimension(d).members) for d in inputs[name]}
            value = None if rng.random() < 0.3 else rng.choice([0.0, -0.0, 0.1, 1 / 3, -2.5, 7.0])
            for m in (ref, rs):
                m.set_cell(name, value, **coords)
            for scan, _ in SCANS:
                with self.subTest(round=round_, metric=scan):
                    self.assertEqual(rs.value(scan).cells, ref.value(scan).cells)


if __name__ == "__main__":
    unittest.main()
