"""A scan with a tree of +, -, *, /, IF and COALESCE over PREVIOUS gives the same values as the reference implementation, bit for bit."""
import random
import unittest

from sparse_engine import Model, Named
from sparse_engine.engine import ReferenceEngine

from .test_failures import stored_bytes

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core is not built
    RustEngine = None

MONTHS = [f"m{i:02d}" for i in range(1, 15)]
SCANS = [
    ("Stock", "PREVIOUS(Month) * 0.9 + A"),
    ("Decay", "A - PREVIOUS(Month) / 3"),  # PREVIOUS on the right side of - and /
    ("Ratio", "100 / (PREVIOUS(Month) + A)"),  # a division by 0 is blank
    ("Spread", "PREVIOUS(Month) + Price[EXPAND: Month]"),  # an operand expanded along the time dimension
    ("Scaled", "PREVIOUS(Month) * Rate + A"),  # a lookup in an operand with fewer dimensions
    ("Cash", "PREVIOUS(Month) + A - B"),  # overridable: COALESCE around the tree
    ("Lagged", "PREVIOUS(Month, 2) * 0.5 + PREVIOUS(Month, 3) - A"),  # more than one PREVIOUS, with n > 1
    ("Turn3", "IF(IsActual, RecvActual, Revenue * (PREVIOUS(Month) + PREVIOUS(Month, 2) + PREVIOUS(Month, 3)) / (Revenue[SELECT: Month - 1] + Revenue[SELECT: Month - 2] + Revenue[SELECT: Month - 3]))"),  # IF at the root: the months of receivables over 3 months
    ("Turn6", "IF(IsActual, RecvActual, Revenue * (PREVIOUS(Month) + PREVIOUS(Month, 2) + PREVIOUS(Month, 3) + PREVIOUS(Month, 4) + PREVIOUS(Month, 5) + PREVIOUS(Month, 6)) / (Revenue[SELECT: Month - 1] + Revenue[SELECT: Month - 2] + Revenue[SELECT: Month - 3] + Revenue[SELECT: Month - 4] + Revenue[SELECT: Month - 5] + Revenue[SELECT: Month - 6]))"),
]


def model(engine) -> Named:
    m = Named(Model(engine=engine))
    m.add_dimension("Product", ["p1", "p2", "p3"])
    m.add_dimension("Month", MONTHS, ordered=True)
    m.add_input("A", ["Product", "Month"], {("p1", "m01"): 1.5, ("p2", "m03"): -2.25})
    m.add_input("B", ["Product", "Month"], {("p1", "m02"): 0.1})
    m.add_input("Price", ["Product"], {("p2",): 0.3})
    m.add_input("Rate", ["Product"], {("p1",): 1.1, ("p3",): 0.7})
    m.add_input("IsActual", ["Month"], {(mo,): mo in MONTHS[:4] for mo in MONTHS}, kind="boolean")
    m.add_input("RecvActual", ["Product", "Month"], {(p, mo): 2.0 + i for i, (p, mo) in enumerate((p, mo) for p in ["p1", "p2"] for mo in MONTHS[:4])})
    m.add_input("Revenue", ["Product", "Month"], {(p, mo): 1.0 + (i % 5) / 4 for i, (p, mo) in enumerate((p, mo) for p in ["p1", "p2", "p3"] for mo in MONTHS)})
    for name, formula in SCANS:
        m.add_formula(name, ["Product", "Month"], formula, overridable=name == "Cash")
    return m


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class MatchesReference(unittest.TestCase):
    def test_random_edits(self):
        rng = random.Random(20261013)
        # par_min=0 splits the rows of the scan into parallel parts also in this small model
        ref, rs, par = model(ReferenceEngine()), model(RustEngine()), model(RustEngine(par_min=0))
        inputs = {"A": ["Product", "Month"], "B": ["Product", "Month"], "Price": ["Product"], "Rate": ["Product"],
                  "Cash": ["Product", "Month"],  # set_cell on Cash writes its override
                  "IsActual": ["Month"], "RecvActual": ["Product", "Month"], "Revenue": ["Product", "Month"]}
        for round_ in range(200):
            name = rng.choice(list(inputs))
            coords = {d: rng.choice(ref.dimension(d).members) for d in inputs[name]}
            values = [True, False] if name == "IsActual" else [0.0, -0.0, 0.1, 1 / 3, -2.5, 7.0]
            value = None if rng.random() < 0.3 else rng.choice(values)
            for m in (ref, rs, par):
                m.set_cell(name, value, **coords)
            for scan, _ in SCANS:
                with self.subTest(round=round_, metric=scan):
                    self.assertEqual(rs.value(scan).cells, ref.value(scan).cells)
                    self.assertEqual(par.value(scan).cells, ref.value(scan).cells)

    def test_edit_in_a_middle_period(self):
        """After an edit in a middle period, the values agree with the reference. PREVIOUS reads the stored values before the edit."""
        ref, rs = model(ReferenceEngine()), model(RustEngine())
        for m in (ref, rs):
            m.value("Turn6")  # the first read calculates all periods. The edits below then recalculate from m03 or m07
            m.set_cell("RecvActual", 9.0, Product="p1", Month="m03")
            m.set_cell("Revenue", 3.5, Product="p1", Month="m07")
        for scan, _ in SCANS:
            with self.subTest(metric=scan):
                self.assertEqual(rs.value(scan).cells, ref.value(scan).cells)
        late = [k for k in ref.value("Turn6").cells if k[1] in MONTHS[6:]]
        self.assertTrue(late, "the turnover has values in the forecast months")

    def test_small_budget_uses_the_per_period_path(self):
        """If the operands of all periods do not fit in max_bytes, the engine evaluates one period at a time."""
        products = [f"p{i:03d}" for i in range(200)]
        values = {(p, mo): float(i % 7) / 3 for i, (p, mo) in enumerate((p, mo) for p in products for mo in MONTHS[:8])}
        results = []
        for engine in (ReferenceEngine(), RustEngine()):
            m = Named(Model(engine=engine))
            m.add_dimension("Product", products)
            m.add_dimension("Month", MONTHS[:8], ordered=True)
            m.add_input("A", ["Product", "Month"], values)
            m.add_formula("Stock", ["Product", "Month"], "PREVIOUS(Month) * 0.9 + A + A")
            if engine.name == "rust":
                engine.core.configure(max_bytes=stored_bytes(m) + (128 << 10))
            results.append(m.value("Stock").cells)
        self.assertEqual(results[1], results[0])


if __name__ == "__main__":
    unittest.main()
