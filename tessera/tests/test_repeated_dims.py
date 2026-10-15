"""A Metric cannot have the same dimension two times (bug bash M19)."""
import unittest

from sparse_engine import Model, Named
from sparse_engine.engine import ReferenceEngine
from sparse_engine.journal import apply
from sparse_engine.server import apply_ops

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core is not built
    RustEngine = None

ENGINES = [ReferenceEngine] + ([RustEngine] if RustEngine is not None else [])
REPEATED = "X: 軸 A を 2 回以上持てない"


def small(engine) -> Named:
    m = Named(Model(engine=engine()))
    m.add_dimension("A", ["a1", "a2"])
    m.add_dimension("B", ["b1"])
    return m


class RepeatedDimension(unittest.TestCase):
    def test_add_input_rejects_repeated_dimension(self):
        for engine in ENGINES:
            with self.subTest(engine.__name__):
                m = small(engine)
                with self.assertRaisesRegex(ValueError, REPEATED):
                    m.add_input("X", ["A", "A"], {("a1", "a2"): 1.0})
                self.assertNotIn("X", m._metric_ids)

    def test_add_formula_rejects_repeated_dimension(self):
        for engine in ENGINES:
            with self.subTest(engine.__name__):
                m = small(engine)
                with self.assertRaisesRegex(ValueError, REPEATED):
                    m.add_formula("X", ["A", "B", "A"], "1")
                self.assertNotIn("X", m._metric_ids)

    def test_redefinition_keeps_the_old_definition(self):
        for engine in ENGINES:
            with self.subTest(engine.__name__):
                m = small(engine)
                m.add_input("X", ["A", "B"], {("a1", "b1"): 1.0})
                with self.assertRaisesRegex(ValueError, REPEATED):
                    m.add_input("X", ["A", "A"], id=m.metric("X").id)
                self.assertEqual(dict(m.value("X").cells), {("a1", "b1"): 1.0})

    def test_server_write_is_rejected(self):
        for engine in ENGINES:
            with self.subTest(engine.__name__):
                m = small(engine)
                a = m.dimension_id("A")
                with self.assertRaisesRegex(ValueError, REPEATED):
                    apply_ops(m.model, [{"op": "add_input", "id": "x", "name": "X", "dims": [a, a]}])
                with self.assertRaisesRegex(ValueError, REPEATED):
                    apply_ops(m.model, [{"op": "add_formula", "id": "x", "name": "X", "dims": [a, a],
                                         "formula": "1"}])
                self.assertNotIn("X", m._metric_ids)

    def test_replay_of_an_old_record_is_rejected(self):
        # Journal replay and snapshot load both use apply. A record from before the check must not load
        for engine in ENGINES:
            with self.subTest(engine.__name__):
                m = small(engine)
                a = m.dimension_id("A")
                spec = {"id": "x", "name": "X", "dims": [a, a], "kind": "number", "formula": None,
                        "partition": None, "overridable": False}
                with self.assertRaisesRegex(ValueError, REPEATED):
                    apply(m.model, {"changes": {"metrics": [spec]}})


if __name__ == "__main__":
    unittest.main()
