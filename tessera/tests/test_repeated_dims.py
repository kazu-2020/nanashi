"""A new Metric cannot have the same dimension two times. A Metric from an old journal still loads."""
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
REPEATED = "X: 軸 A を 2 回以上持てない。同じメンバーの軸をもう 1 つ作る"


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

    def test_old_record_loads_and_can_be_removed(self):
        for engine in ENGINES:
            for partition in (None, "A"):
                with self.subTest(engine.__name__, partition=partition):
                    m = small(engine)
                    a, b = m.dimension_id("A"), m.dimension_id("B")
                    spec = {"id": "x", "name": "X", "dims": [a, b, a], "kind": "number", "formula": None,
                            "partition": partition and a, "overridable": False}
                    a1, a2 = (m.member_id("A", n) for n in ("a1", "a2"))
                    rows = [[[a1, m.member_id("B", "b1"), a2], None, 1.0],
                            [[a2, m.member_id("B", "b1"), a1], None, 2.0]]
                    apply(m.model, {"changes": {"metrics": [spec], "cells": [{"metric": "x", "rows": rows}]}})
                    self.assertEqual(dict(m.value("X").cells), {("a1", "b1", "a2"): 1.0, ("a2", "b1", "a1"): 2.0})
                    m.remove_member("A", "a1")
                    self.assertEqual(dict(m.value("X").cells), {})
                    m.remove_metric("X")
                    self.assertNotIn("X", m._metric_ids)


if __name__ == "__main__":
    unittest.main()
