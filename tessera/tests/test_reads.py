"""The read API that reads only as necessary (get, slice, rows, summarize), and the published version."""
import unittest

from sparse_engine import Model, Named
from sparse_engine.engine import ReferenceEngine, aggregate_cube
from sparse_engine.workspace import Workspace

from .test_incremental import model as build, same

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None


def with_engine(engine) -> Model:
    from .test_engines import build_with
    m = build_with(engine)
    m.add_input("Cutoff", [], {(): "Feb"}, kind="member:Month")
    m.add_input("Home", ["Employee"], {("e1",): "Sales", ("e4",): "Eng"}, kind="member:Department")
    m.recalc()
    return m


class Reads:
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        self.m = with_engine(self.engine())

    def test_get_reads_one_cell_without_the_whole_metric(self):
        m = self.m
        for name, x in m.metrics.items():
            cube = m.value(name)
            for key, value in list(cube.cells.items())[:5]:
                self.assertEqual(m.get(name, **dict(zip(x.dims, key))), value, (name, key))
        self.assertIsNone(m.get("Volume", Product="D", Region="N", Month="Jan"))  # 空
        self.assertEqual(m.get("Cutoff"), "Feb")  # メンバー型は名前
        self.assertEqual(m.get("Home", Employee="e4"), "Eng")
        self.assertIs(m.get("Flag", Product="B"), False)  # 真偽値は bool
        self.assertIsNone(m.get("Price", Product="Z"))  # ないメンバーのセルは空
        with self.assertRaisesRegex(ValueError, "指定していない"):
            m.get("Price")
        with self.assertRaisesRegex(ValueError, "軸 Region がない"):
            m.slice("Price", Region="N")

    def test_slice_matches_filtering_the_whole_metric(self):
        m = self.m
        for name in m._metric_ids:
            dims = m.metric(name).dims
            if not dims:
                continue
            whole = m.value(name).cells
            d = dims[0]
            members = m.dimension(d).members[:2]
            got = m.slice(name, **{d: members}).cells
            want = {k: v for k, v in whole.items() if k[dims.index(d)] in members}
            self.assertTrue(same(got, want), (name, got, want))
            one = m.slice(name, **{d: members[0]}).cells  # 1 つのメンバーは文字列でもよい
            self.assertTrue(same(one, {k: v for k, v in whole.items() if k[dims.index(d)] == members[0]}))
        self.assertEqual(m.slice("Home", Employee=["e4"]).cells, {("e4",): "Eng"})

    def test_rows_are_ordered_and_paged(self):
        m = self.m
        rows, total = m.rows("Volume")
        whole = m.value("Volume").cells
        self.assertEqual(total, len(whole))
        self.assertEqual(dict(rows), whole)
        order = [m.dimension(d)._index for d in m.metric("Volume").dims]
        keys = [k for k, _ in rows]
        self.assertEqual(keys, sorted(keys, key=lambda k: tuple(ix[x] for ix, x in zip(order, k))))
        page1, total1 = m.rows("Volume", offset=0, limit=3)
        page2, _ = m.rows("Volume", offset=3, limit=3)
        self.assertEqual(total1, total)
        self.assertEqual(page1 + page2, rows[:6])
        only, n = m.rows("Volume", Region="S", limit=1)
        self.assertEqual(n, sum(1 for k in whole if k[1] == "S"))
        self.assertEqual(len(only), 1)
        self.assertEqual(m.rows("Home", Employee="e4"), ([(("e4",), "Eng")], 1))
        self.assertEqual(m.rows("Flag", Product=["B"]), ([(("B",), False)], 1))

    def test_pages_follow_a_custom_member_order(self):
        m = Named(Model(engine=self.engine()))
        m.add_dimension("P", ["p0", "p1", "p2", "p3"])
        m.add_dimension("Q", ["q0", "q1", "q2"])
        m.add_input("X", ["Q", "P"], {(q, p): float(10 * i + j) for i, q in enumerate(["q0", "q1", "q2"])
                                      for j, p in enumerate(["p0", "p1", "p2", "p3"]) if (i + j) % 3})
        m.move_member("P", "p3", 0)
        m.move_member("Q", "q2", 0)
        whole = [(("q2", "p3"), 23.0), (("q2", "p0"), 20.0), (("q2", "p2"), 22.0),
                 (("q0", "p1"), 1.0), (("q0", "p2"), 2.0),
                 (("q1", "p3"), 13.0), (("q1", "p0"), 10.0), (("q1", "p1"), 11.0)]
        self.assertEqual(m.rows("X"), (whole, 8))
        for offset in range(10):
            for limit in range(10):
                self.assertEqual(m.rows("X", offset=offset, limit=limit), (whole[offset:offset + limit], 8),
                                 (offset, limit))
        self.assertEqual(m.rows("X", offset=1, limit=2, P=["p0", "p3"]), ([(("q2", "p0"), 20.0), (("q1", "p3"), 13.0)], 4))

    def test_summarize_matches_reference_aggregation(self):
        m = self.m
        for name in ["Revenue", "Margin", "Volume", "RevByCat"]:
            dims = m.metric(name).dims
            cube = m.value(name)
            for keep in [(), dims[:1], dims[1:], dims]:
                for agg in ["sum", "avg", "min", "max", "count"]:
                    got = m.summarize(name, keep=keep, agg=agg)
                    want = aggregate_cube(cube, tuple(m.dimension(d).name for d in keep), agg)
                    self.assertEqual(got.dims, tuple(m.dimension(d).name for d in dims if d in keep))
                    self.assertTrue(same(got.cells, want.cells), (name, keep, agg, got.cells, want.cells))
        got = m.summarize("Volume", keep=["Month"], Product=["A", "C"])
        want = aggregate_cube(m.slice("Volume", Product=["A", "C"]), ["Month"], "sum")
        self.assertTrue(same(got.cells, want.cells))
        self.assertEqual(m.summarize("Flag", agg="count").cells, {(): 2.0})
        with self.assertRaisesRegex(ValueError, "集計できない"):
            m.summarize("Flag")
        with self.assertRaisesRegex(ValueError, "軸 Region がない"):
            m.summarize("Price", keep=["Region"])

    def test_summarize_gives_columns_of_member_names(self):
        s = self.m.summarize("Home", keep=["Employee"], agg="count")  # a count of a member-type Metric is a number
        self.assertEqual(s.dims, ("Employee",))
        self.assertEqual(sorted(zip(s.coords[0].tolist(), s.values.tolist())), [("e1", 1.0), ("e4", 1.0)])
        self.assertEqual(str(s.values.dtype), "float64")
        self.assertEqual(self.m.summarize("Home", agg="count").cells, {(): 2.0})

    def test_reads_see_pending_changes(self):
        m = self.m
        m.set_cell("Price", 100, Product="A")
        self.assertEqual(m.get("Revenue", Product="A", Region="N", Month="Jan"), 300)
        self.assertEqual(m.summarize("Revenue", Product="A").cells[()], 500)


class ReferenceReads(Reads, unittest.TestCase):
    pass


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustReads(Reads, unittest.TestCase):
    engine = staticmethod(RustEngine)

    def test_a_short_page_does_not_copy_the_range(self):
        import nanashi_core
        m = Named(Model(engine=self.engine()))
        m.add_dimension("A", [f"a{i}" for i in range(400)])
        m.add_dimension("B", [f"b{i}" for i in range(500)])
        m.add_input("X", ["A", "B"], {(f"a{i}", f"b{j}"): 1.0 for i in range(400) for j in range(500)})
        m.move_member("B", "b499", 0)
        m.recalc()
        budget = 64 << 10  # the range has 200,000 cells (3.2 MB as key and value)
        for offset in [0, 100]:
            nanashi_core.reset_heap_peak()
            before = nanashi_core.heap()[0]
            self.assertEqual(m.rows("X", offset=offset, limit=1), ([(("a0", f"b{offset - 1 if offset else 499}"), 1.0)], 200_000))
            self.assertLess(nanashi_core.heap()[1] - before, budget)
        nanashi_core.reset_heap_peak()
        before = nanashi_core.heap()[0]
        self.assertEqual(m.summarize("X").cells, {(): 200_000.0})
        self.assertLess(nanashi_core.heap()[1] - before, budget)

    def test_a_summary_after_an_edit_stays_in_the_budget(self):
        m = Named(Model(engine=self.engine(max_bytes=3 << 20)))
        m.add_dimension("A", [f"a{i}" for i in range(400)])
        m.add_dimension("B", [f"b{i}" for i in range(500)])
        m.add_input("X", ["A", "B"], {(f"a{i}", f"b{j}"): 1.0 for i in range(400) for j in range(500)})
        m.set_cell("X", 2.0, A="a0", B="b0")
        m.recalc()
        self.assertEqual(m.summarize("X").cells, {(): 200_001.0})


class VersionView(unittest.TestCase):
    def test_version_reads_and_rejects_writes(self):
        ws = Workspace(build().model)
        try:
            v = Named(ws.version)
            self.assertEqual(v.get("Price", Product="A"), 10)
            self.assertEqual(v.slice("Price", Product="A").cells, {("A",): 10})
            self.assertEqual(v.rows("Price", limit=1)[1], 3)
            self.assertEqual(v.summarize("Price").cells[()], 35)
            self.assertEqual(set(v._metric_ids), set(build()._metric_ids))
            self.assertEqual(v.seq, 0)
            with self.assertRaisesRegex(ValueError, "公開済み"):
                v.set_cell("Price", 99, Product="A")
            what_if = v.fork()  # 手元の複製は書き換えられる
            what_if.set_cell("Price", 99, Product="A")
            self.assertEqual(what_if.get("Price", Product="A"), 99)
            self.assertEqual(v.get("Price", Product="A"), 10)
        finally:
            ws.close()


if __name__ == "__main__":
    unittest.main()
