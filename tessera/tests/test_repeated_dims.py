import tempfile
import unittest
from pathlib import Path

from sparse_engine import Model, Named
from sparse_engine.engine import ReferenceEngine
from sparse_engine.journal import FileJournal, apply
from sparse_engine.server import apply_ops

try:
    import nanashi_core
    from sparse_engine.rust_engine import RustEngine
except ImportError:
    nanashi_core = RustEngine = None

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

    def test_server_write_is_rejected(self):
        for engine in ENGINES:
            with self.subTest(engine.__name__):
                m = small(engine)
                a = m.dimension_id("A")
                with self.assertRaisesRegex(ValueError, REPEATED):
                    apply_ops(m.model, [{"op": "add_input", "id": "x", "name": "X", "dims": [a, a]}])
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

    def test_removed_member_replays_to_live_state(self):
        for engine in ENGINES:
            for dims in (("A", "A"), ("A", "B", "A")):
                for bulk in (None, 0):
                    if bulk == 0 and nanashi_core is None:
                        continue  # a bulk record needs nanashi_core
                    with self.subTest(engine.__name__, dims=dims, bulk=bulk), tempfile.TemporaryDirectory() as path:
                        kw = {} if bulk is None else {"bulk_cells": bulk}
                        m = Named(Model(engine=engine()))
                        m.add_dimension("A", ["a1", "a2", "a3"])
                        m.add_dimension("B", ["b1"])
                        j = FileJournal(path, fsync=False, **kw)
                        j.start(m.model)
                        keys = [("a1", "a3"), ("a1", "a2"), ("a3", "a1"), ("a2", "a2")]
                        if dims == ("A", "B", "A"):
                            keys = [(x, "b1", y) for x, y in keys]
                        spec = {"id": "x", "name": "X", "dims": [m.dimension_id(d) for d in dims], "kind": "number",
                                "formula": None, "partition": None, "overridable": False}
                        rows = [[[m.member_id(d, x) for d, x in zip(dims, k)], None, float(n)]
                                for n, k in enumerate(keys, 5)]
                        j.append({"changes": {"metrics": [spec], "cells": [{"metric": "x", "rows": rows}]}})
                        j.release()
                        m = Named(FileJournal(path, fsync=False, **kw).open(engine()))
                        m.remove_member("A", "a2")
                        survivors = {k: float(n) for n, k in enumerate(keys, 5) if "a2" not in k}
                        self.assertEqual(dict(m.value("X").cells), survivors)
                        m.model.journal.release()
                        self.assertEqual(dict(Named(FileJournal(path).open(engine())).value("X").cells), survivors)

    @unittest.skipIf(nanashi_core is None, "nanashi_core が必要")
    def test_old_record_survives_snapshot_and_bulk_replay(self):
        for engine in ENGINES:
            for dims, cells in ((("A", "B", "A"), {("a1", "b1", "a2"): 1.0, ("a2", "b1", "a1"): 2.0}),
                                (("A", "A"), {("a1", "a2"): 1.0, ("a2", "a1"): 2.0})):
                for snapshot in (True, False):
                    with self.subTest(engine.__name__, dims=dims, snapshot=snapshot), \
                            tempfile.TemporaryDirectory() as path:
                        m = small(engine)
                        j = FileJournal(path, fsync=False, bulk_cells=0)
                        j.start(m.model)
                        spec = {"id": "x", "name": "X", "dims": [m.dimension_id(d) for d in dims], "kind": "number",
                                "formula": None, "partition": None, "overridable": False}
                        rows = [[[m.member_id(d, x) for d, x in zip(dims, k)], None, v] for k, v in cells.items()]
                        cell = {"metric": "x", "dims": spec["dims"], "rows": rows}
                        j.append({"changes": {"metrics": [spec], "cells": [cell]}})
                        if snapshot:
                            j.save_snapshot(j.open(engine()))
                        j.release()
                        self.assertEqual(len(list((Path(path) / "cells").glob("*-x.parquet"))), 1)  # a bulk record
                        self.assertEqual(dict(Named(FileJournal(path).open(engine())).value("X").cells), cells)


if __name__ == "__main__":
    unittest.main()
