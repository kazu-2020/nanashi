"""必要な分だけ読む API（get、slice、rows、summarize）と、公開中の版のビュー（Version）。"""
import unittest

from sparse_engine import Model
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
        for name in m.metrics:
            dims = m.metrics[name].dims
            if not dims:
                continue
            whole = m.value(name).cells
            d = dims[0]
            members = m.dimensions[d].members[:2]
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
        order = [m.dimensions[d]._index for d in m.metrics["Volume"].dims]
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

    def test_summarize_matches_reference_aggregation(self):
        m = self.m
        for name in ["Revenue", "Margin", "Volume", "RevByCat"]:
            dims = m.metrics[name].dims
            cube = m.value(name)
            for keep in [(), dims[:1], dims[1:], dims]:
                for agg in ["sum", "avg", "min", "max", "count"]:
                    got = m.summarize(name, keep=keep, agg=agg)
                    want = aggregate_cube(cube, keep, agg)
                    self.assertEqual(got.dims, tuple(d for d in dims if d in keep))
                    self.assertTrue(same(got.cells, want.cells), (name, keep, agg, got.cells, want.cells))
        got = m.summarize("Volume", keep=["Month"], Product=["A", "C"])
        want = aggregate_cube(m.slice("Volume", Product=["A", "C"]), ["Month"], "sum")
        self.assertTrue(same(got.cells, want.cells))
        self.assertEqual(m.summarize("Flag", agg="count").cells, {(): 2.0})
        with self.assertRaisesRegex(ValueError, "集計できない"):
            m.summarize("Flag")
        with self.assertRaisesRegex(ValueError, "軸 Region がない"):
            m.summarize("Price", keep=["Region"])

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


class VersionView(unittest.TestCase):
    def test_version_exposes_reads_only(self):
        ws = Workspace(build())
        try:
            v = ws.version
            self.assertEqual(v.get("Price", Product="A"), 10)
            self.assertEqual(v.slice("Price", Product="A").cells, {("A",): 10})
            self.assertEqual(v.rows("Price", limit=1)[1], 3)
            self.assertEqual(v.summarize("Price").cells[()], 35)
            self.assertEqual(set(v.metrics) , set(build().metrics))
            self.assertEqual(v.seq, 0)
            for name in ("set_cell", "add_formula", "transaction", "refresh", "spread"):
                self.assertFalse(hasattr(v, name), name)
            what_if = v.fork()  # 手元の複製は書き換えられる
            what_if.set_cell("Price", 99, Product="A")
            self.assertEqual(what_if.get("Price", Product="A"), 99)
            self.assertEqual(v.get("Price", Product="A"), 10)
        finally:
            ws.close()


if __name__ == "__main__":
    unittest.main()
