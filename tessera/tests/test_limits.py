"""エンジンの上限の検査、セル数の見積もりの上限、観察用の記録の上限、メモリの内訳。"""
import gc
import math
import tempfile
import unittest

from sparse_engine import FormulaError, Model, Named
from sparse_engine.engine import ReferenceEngine
from sparse_engine.model import LOG_MAX

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None


def wide(engine) -> Model:
    m = Named(Model(engine=engine))
    for i in range(5):
        m.add_dimension(f"D{i}", [f"m{j}" for j in range(1 << 13)])  # 13 ビット × 5 = 65 ビット
    return m


class KeyWidth(unittest.TestCase):
    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_rust_rejects_metrics_wider_than_64_bits_with_a_hint(self):
        m = wide(RustEngine())
        with self.assertRaisesRegex(ValueError, "64 ビット.*D0 13 ビット.*軸を減らす"):
            m.add_input("Wide", [f"D{i}" for i in range(5)])
        with self.assertRaisesRegex(ValueError, "64 ビット"):
            m.add_formula("Wide2", [f"D{i}" for i in range(5)], "1")
        m.add_input("Ok", [f"D{i}" for i in range(4)])  # 52 ビットは入る
        self.assertNotIn("Ok", m.warnings)

    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_intermediate_results_are_checked_when_the_formula_is_checked(self):
        m = wide(RustEngine())
        m.add_input("X", ["D0", "D1", "D2"], {("m1", "m2", "m3"): 1.0})
        m.add_input("Y", ["D2", "D3", "D4"], {("m3", "m4", "m5"): 2.0})
        # 結果の軸は 39 ビットだが、途中で 5 本の軸（65 ビット）を組み合わせる。評価の途中でなく、登録後の
        # 型検査で、直し方を示して拒否する
        m.add_formula("Z", ["D0", "D1", "D2"], "(X[EXPAND: D3, D4] * Y[EXPAND: D0, D1])[REMOVE SUM: D3, D4]")
        with self.assertRaisesRegex(FormulaError, "途中の結果の軸 .*64 ビット.*D0 13 ビット.*先に集計して"):
            m.recalc()
        m.add_formula("Z", ["D0", "D1", "D2"], "X * Y[REMOVE SUM: D3, D4]", id=m.metric("Z").id)
        self.assertEqual(m.get("Z", D0="m1", D1="m2", D2="m3"), 2.0)

    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_member_that_would_widen_past_64_bits_is_refused(self):
        m = Named(Model(engine=RustEngine()))
        for i in range(4):
            m.add_dimension(f"D{i}", [f"m{j}" for j in range(1 << 13)])  # 13 ビット × 4
        m.add_dimension("E", [f"e{j}" for j in range(1 << 12)])          # 12 ビット（合わせて 64 ビット）
        m.add_input("X", ["D0", "D1", "D2", "D3"], {("m1", "m1", "m1", "m1"): 1.0})
        m.add_input("Y", ["E"], {("e1",): 2.0})
        m.add_formula("Z", ["D0"], "(X[EXPAND: E] * Y)[REMOVE SUM: D1, D2, D3, E]")
        self.assertEqual(m.get("Z", D0="m1"), 2.0)
        with self.assertRaisesRegex(FormulaError, "途中の結果の軸"):  # E が 13 ビットになると収まらない
            m.add_member("E", "e_new")
        self.assertNotIn("e_new", m.dimension("E"))
        m.set_cell("Y", 3.0, E="e1")
        self.assertEqual(m.get("Z", D0="m1"), 3.0)

    @staticmethod
    def by_metric(ebits: int) -> Model:
        """Salary[E, D1, D2, D3] を所属（DeptOf[E] -> Dept）で部署別に集計するモデル。結果の軸は
        D1、D2、D3、Dept の 52 ビットだが、途中で社員と部署の両方を持つ（E が 13 ビットなら 65 ビット）。"""
        m = Named(Model(engine=RustEngine()))
        m.add_dimension("E", [f"e{j}" for j in range(1 << ebits)])
        m.add_dimension("Dept", [f"g{j}" for j in range(1 << 13)])
        for i in range(1, 4):
            m.add_dimension(f"D{i}", [f"m{j}" for j in range(1 << 13)])
        m.add_input("Salary", ["E", "D1", "D2", "D3"], {("e1", "m1", "m1", "m1"): 10.0, ("e2", "m1", "m1", "m1"): 5.0})
        m.add_input("DeptOf", ["E"], {("e1",): "g1", ("e2",): "g2"}, kind="member:Dept")
        m.add_formula("Cost", ["D1", "D2", "D3", "Dept"], "Salary[BY SUM: E.DeptOf]")
        return m

    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_by_metric_join_is_checked_when_the_formula_is_checked(self):
        # Metric を使った BY は、型検査が社員と部署の両方を持つ結合に書き換える。結果が収まっても、
        # 結合が収まらなければ、評価の途中でなく型検査で直し方を示して拒否する
        m = self.by_metric(13)
        with self.assertRaisesRegex(FormulaError, r"式の途中の結果の軸 \[.*E.*Dept.*\] が 64 ビット.*E 13 ビット"):
            m.recalc()
        m.add_formula("Cost", ["D1", "D2", "D3"], "Salary[REMOVE SUM: E]", id=m.metric("Cost").id)  # the corrected formula passes
        self.assertEqual(m.get("Cost", D1="m1", D2="m1", D3="m1"), 15.0)

    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_malformed_by_metric_reports_its_own_error_before_the_width(self):
        # 結合が収まらなくても、BY の書き方の誤り（所属が月ごとなのに式に月がない）を先に示す
        m = self.by_metric(13)
        m.add_dimension("Month", ["Jan", "Feb"])
        m.add_input("DeptOfM", ["E", "Month"], {("e1", "Jan"): "g1"}, kind="member:Dept")
        m.add_formula("Cost", ["D1", "D2", "D3", "Dept", "Month"], "Salary[BY SUM: E.DeptOfM]", id=m.metric("Cost").id)
        with self.assertRaisesRegex(FormulaError, "DeptOfM の軸 .*Month.* を持っていない"):
            m.recalc()

    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_member_that_would_widen_a_by_metric_join_past_64_bits_is_refused(self):
        m = self.by_metric(12)  # 結合は 64 ビットでちょうど収まる
        self.assertEqual(m.get("Cost", D1="m1", D2="m1", D3="m1", Dept="g1"), 10.0)
        with self.assertRaisesRegex(FormulaError, "途中の結果の軸"):  # E が 13 ビットになると結合が収まらない
            m.add_member("E", "e_new")
        self.assertNotIn("e_new", m.dimension("E"))
        m.set_cell("Salary", 20.0, E="e1", D1="m1", D2="m1", D3="m1")  # 拒否したあとも計算し直せる
        self.assertEqual(m.get("Cost", D1="m1", D2="m1", D3="m1", Dept="g1"), 20.0)
        m.set_cell("DeptOf", "g2", E="e1")
        self.assertEqual(m.get("Cost", D1="m1", D2="m1", D3="m1", Dept="g2"), 25.0)

    def test_reference_has_no_width_limit(self):
        m = wide(ReferenceEngine())
        m.add_input("Wide", [f"D{i}" for i in range(5)])


ENGINES = [ReferenceEngine] + ([RustEngine] if RustEngine is not None else [])


def big(engine) -> Model:
    """顧客 200 × 商品 100（全組み合わせで 2 万）のモデル。値は少しだけ入れ、上限を 1 万にする。
    上限の検査が壊れていても、テストが大量のメモリを使わない大きさにしてある。"""
    m = Named(Model(engine=engine, max_cells=10_000))
    m.add_dimension("Customer", [f"c{i}" for i in range(200)])
    m.add_dimension("SKU", [f"s{i}" for i in range(100)])
    m.add_dimension("Week", [f"w{i}" for i in range(52)], ordered=True)
    m.add_input("Sales", ["Customer", "SKU"], {("c1", "s1"): 1.0, ("c2", "s1"): 2.0})
    return m


class CellEstimates(unittest.TestCase):
    def test_default_limit(self):
        self.assertEqual(Named(Model(engine=ReferenceEngine())).max_cells, 1_000_000_000)

    def test_rejects_formulas_that_densify_beyond_the_limit(self):
        for engine in ENGINES:
            with self.subTest(engine=engine.__name__):
                m = big(engine())
                m.add_formula("Filled", ["Customer", "SKU"], "IFBLANK(Sales, 0)")
                with self.assertRaisesRegex(FormulaError, r"Filled: .*最大 20,000 .*上限 10,000 .*"
                                                          r"IFBLANK が \['Customer', 'SKU'\]"):
                    m.recalc()
                m.add_formula("Filled", ["Customer", "SKU"], "Sales * 2", id=m.metric("Filled").id)  # the corrected formula passes
                self.assertEqual(m.value("Filled").cells, {("c1", "s1"): 2.0, ("c2", "s1"): 4.0})
                self.assertEqual(m.cell_estimates[m.metric("Filled").id], 2.0)

    def test_rejected_definition_is_rolled_back_in_a_transaction(self):
        for engine in ENGINES:
            with self.subTest(engine=engine.__name__):
                m = big(engine())
                m.add_formula("Double", ["Customer", "SKU"], "Sales * 2")
                m.recalc()
                with self.assertRaises(FormulaError):
                    with m.transaction():
                        m.add_formula("Double", ["Customer", "SKU"], "Sales + 1", id=m.metric("Double").id)  # the addition of a constant makes it dense
                self.assertEqual(m.value("Double").cells, {("c1", "s1"): 2.0, ("c2", "s1"): 4.0})

    def test_downstream_of_a_dense_metric_is_checked_too(self):
        for engine in ENGINES:
            with self.subTest(engine=engine.__name__):
                m = big(engine())
                m.add_formula("Small", ["Customer"], "Sales[REMOVE SUM: SKU]")
                m.recalc()
                m.add_formula("Dense", ["Customer"], "IFBLANK(Small, 0)")  # 200 は上限内
                m.add_formula("Weekly", ["Customer", "Week"], "Dense[EXPAND: Week]")  # 200 × 52 = 10,400
                with self.assertRaisesRegex(FormulaError, "Weekly: .*最大 10,400 "):
                    m.recalc()

    def test_no_limit(self):
        for engine in ENGINES:
            for limit in (8, None):
                with self.subTest(engine=engine.__name__, limit=limit):
                    m = Named(Model(engine=engine(), max_cells=limit))
                    m.add_dimension("P", ["a", "b", "c"])
                    m.add_dimension("M", ["x", "y", "z"])
                    m.add_input("V", ["P", "M"], {("a", "x"): 1.0})
                    m.add_formula("Filled", ["P", "M"], "IFBLANK(V, 0)")
                    if limit is not None:
                        with self.assertRaisesRegex(FormulaError, "最大 9 と見積もられ、上限 8 を超える"):
                            m.recalc()
                    else:
                        self.assertEqual(len(m.value("Filled").cells), 9)
                        self.assertEqual(m.cell_estimates[m.metric("Filled").id], 9.0)

    def test_scan_is_estimated_as_carried_over_all_periods(self):
        for engine in ENGINES:
            with self.subTest(engine=engine.__name__):
                m = big(engine())
                m.add_input("In", ["Customer", "SKU", "Week"], {("c1", "s1", "w3"): 5.0})
                m.add_formula("Stock", ["Customer", "SKU", "Week"], "PREVIOUS(Week) + In")
                m.recalc()
                self.assertEqual(m.cell_estimates[m.metric("Stock").id], 52.0)  # 1 セルが 52 週へ持ち越されうる
                self.assertEqual(len(m.value("Stock").cells), 49)  # 実際は w3 から w51 まで

    def test_incremental_estimates_match_full(self):
        """定義を変えたときに下流だけ見積もり直した結果は、全体を見積もり直した結果と同じ。"""
        from .test_expr_coverage import model
        for engine in ENGINES:
            with self.subTest(engine=engine.__name__):
                m = model(engine())
                m.recalc()
                steps = [
                    lambda: m.add_formula("Revenue", ["Product", "Month"], "IFBLANK(Volume, 0) * Price[EXPAND: Month]", id=m.metric("Revenue").id),
                    lambda: m.rename_metric("Revenue", "Rev"),
                    lambda: m.add_formula("Rev", ["Product", "Month"], "Volume * Price", id=m.metric("Rev").id),
                    lambda: m.add_input("Volume", ["Product", "Month"], {("A", "Jan"): 1, ("B", "Jan"): 2}, id=m.metric("Volume").id),
                    lambda: m.add_formula("Leaf", ["Month"], "Total[FILTER: Actual]"),
                    lambda: m.remove_metric("Leaf"),
                    lambda: m.add_formula("Total", ["Month"], "IFBLANK(ByCat[REMOVE SUM: Category], 0)", id=m.metric("Total").id),
                    lambda: m.add_input("Total", ["Month"], {("Jan",): 1.0}, id=m.metric("Total").id),  # as an input, it reads nothing
                    lambda: m.remove_metric("ByCat"),
                    lambda: m.add_formula("Total", ["Month"], "Rev[REMOVE SUM: Product]", id=m.metric("Total").id),
                ]
                for step in steps:  # m は差分の見積もりだけを続け、全体の見積もり直しは複製で行う
                    step()
                    m.recalc()
                    full = m.fork()
                    full._invalidate()
                    full.recalc()
                    self.assertEqual(m.cell_estimates, full.cell_estimates)

    def test_empty_input_is_estimated_as_one_cell(self):
        for engine in ENGINES:
            with self.subTest(engine=engine.__name__):
                m = big(engine())
                m.add_input("Empty", ["Customer"])
                m.add_formula("Wide", ["Customer", "SKU"], "Empty[EXPAND: SKU]")
                m.recalc()
                self.assertEqual(m.cell_estimates[m.metric("Wide").id], 100.0)
                m.add_formula("Wide", ["Customer", "SKU"], "IFBLANK(Empty[EXPAND: SKU], 0)", id=m.metric("Wide").id)
                with self.assertRaisesRegex(FormulaError, "Wide: .*最大 20,000 "):
                    m.recalc()

    def test_spread_over_an_empty_range_is_checked_against_the_limit(self):
        for engine in ENGINES:
            with self.subTest(engine=engine.__name__):
                m = big(engine())
                m.add_input("Budget", ["Customer", "SKU"])
                with self.assertRaisesRegex(ValueError, "Budget: .*20,000 .*上限 10,000"):
                    m.spread("Budget", 100.0)
                self.assertEqual(m.value("Budget").cells, {})
                self.assertEqual(m.spread("Budget", 100.0, Customer="c1"), 100)

    def test_limit_is_saved(self):
        for engine in ENGINES:
            with self.subTest(engine=engine.__name__), tempfile.TemporaryDirectory() as d:
                m = Named(Model(engine=engine(), max_cells=123))
                m.add_dimension("P", ["a"])
                m.save(d)
                self.assertEqual(Named.load(d, engine()).max_cells, 123)


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class BudgetBeforeAllocation(unittest.TestCase):
    """The evaluator checks the budget before it makes a large intermediate result. If the check is late or
    wrong, one formula can stop the process. The tests use small sizes, so a failed check does not stop the test
    process."""

    def test_many_to_many_results_are_checked_before_they_are_made(self):
        import nanashi_core
        n = 1000
        m = Named(Model(engine=RustEngine(max_bytes=1 << 20)))
        m.add_dimension("X", [f"x{i}" for i in range(n)])
        m.add_dimension("Y", [f"y{i}" for i in range(n)])
        m.add_dimension("Grp", ["g0"])
        m.add_property("X", "Grp", "Grp", {f"x{i}": "g0" for i in range(n)})
        m.add_input("A", ["X"], {(f"x{i}",): 1.0 for i in range(n)})
        m.add_input("B", ["Y"], {(f"y{i}",): 1.0 for i in range(n)})
        m.add_input("R", ["Grp", "Y"], {("g0", f"y{i}"): 1.0 for i in range(n)})
        for formula in ["(A * B)[REMOVE SUM: Y]", "IF(B > 0, A)[REMOVE SUM: Y]", "R[BY: X.Grp][REMOVE SUM: Y]"]:
            with self.subTest(formula=formula):
                m.add_formula("Z", ["X"], formula)
                try:
                    nanashi_core.reset_heap_peak()
                    before = nanashi_core.heap()[0]
                    with self.assertRaisesRegex(ValueError, "メモリの予算を超える"):
                        m.recalc()
                    self.assertLess(nanashi_core.heap()[1] - before, 4 << 20, "the result was made before the check")
                finally:
                    m.remove_metric("Z")

    def test_work_inside_one_node_stays_in_the_budget(self):
        import nanashi_core
        n, k = 20_000, 20
        mib = 1 << 20
        # (formula, dimensions, a budget that refuses it, a budget that fits it)
        # A fits budget is about 1.1x the smallest budget that works, so a 1.2x over-count fails the test.
        cases = [
            ("IF(A > 0, B, C)", ["X", "Y"], 2, 3.25),
            ("A + B", ["X", "Y"], 2, 2.3),
            ("A * W", ["X", "Y", "Q"], 3, 3.3),
            ("IF(P > 0, W, -1)", ["X", "Q"], 8, 9.5),
            ("P[EXPAND: Q] + 5", ["X", "Q"], 8, 27),
            ("P[EXPAND: Q] * R[EXPAND: X]", ["X", "Q"], 24, 27),
            # A division by 0 drops all pairs, so the first join must not keep room for them.
            ("P / Z1 + P / Z2", ["X", "Q"], 7, 8.3),
        ]
        for formula, dims, refused, fits in cases:
            for budget, ok in [(int(refused * mib), False), (int(fits * mib), True)]:
                with self.subTest(formula=formula, budget=budget):
                    # A large par_min keeps the work serial. The parallel collect is a known limit.
                    m = Named(Model(engine=RustEngine(max_bytes=budget, fuse=False, par_min=1 << 40)))
                    m.add_dimension("X", [f"x{i}" for i in range(n)])
                    m.add_dimension("Y", ["y0", "y1"])
                    m.add_dimension("Q", [f"q{i}" for i in range(k)])
                    for name in "ABC":
                        m.add_input(name, ["X", "Y"], {(f"x{i}", "y0"): 1.0 if i % 2 else -1.0 for i in range(n)})
                    m.add_input("P", ["X"], {(f"x{i}",): 1.0 if i % 2 else -1.0 for i in range(n)})
                    m.add_input("R", ["Q"], {(f"q{i}",): 1.0 for i in range(k)})
                    m.add_input("W", ["X", "Q"], {(f"x{i}", "q0"): 1.0 for i in range(n)})
                    for name in ("Z1", "Z2"):
                        m.add_input(name, ["Q"], {(f"q{i}",): 0.0 for i in range(k)})
                    m.recalc()
                    gc.collect()
                    base = nanashi_core.heap()[0]
                    m.add_formula("Z", dims, formula)
                    nanashi_core.reset_heap_peak()
                    if ok:
                        m.recalc()
                    else:
                        with self.assertRaisesRegex(ValueError, "メモリの予算を超える"):
                            m.recalc()
                    self.assertLessEqual(nanashi_core.heap()[1] - base, budget, "the work went over max_bytes")

    def test_parallel_expand_stays_in_the_budget(self):
        import nanashi_core
        n, k, budget = 20_000, 100, 70 << 20
        m = Named(Model(engine=RustEngine(max_bytes=budget, par_min=0), max_cells=None))
        m.add_dimension("X", [f"x{i}" for i in range(n)])
        m.add_dimension("T", [f"t{i}" for i in range(k)], ordered=True)
        m.add_input("G", ["X"], {(f"x{i}",): 0.0 for i in range(n)})
        m.add_input("H", ["T"], {(f"t{i}",): 1.0 for i in range(k)})
        m.add_input("B", ["X", "T"], {("x0", "t0"): 1.0})
        m.recalc()
        gc.collect()
        base = nanashi_core.heap()[0]
        m.add_formula("Z", ["X", "T"], "IF(G > 0, H[EXPAND: X] + PREVIOUS(T), B)")
        nanashi_core.reset_heap_peak()
        m.recalc()
        self.assertLessEqual(nanashi_core.heap()[1] - base, budget, "the work went over max_bytes")


class Memory(unittest.TestCase):
    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_rust_reports_storage_bytes(self):
        import nanashi_core
        gc.collect()
        before = nanashi_core.heap()[0]
        m = Named(Model(engine=RustEngine()))
        m.add_dimension("P", [f"p{i}" for i in range(2000)])
        m.add_dimension("M", [f"m{i}" for i in range(12)])
        m.add_input("V", ["P", "M"], {(f"p{i}", f"m{j}"): float(i) for i in range(2000) for j in range(12)})
        m.add_property("P", "Group", "M", {f"p{i}": f"m{i % 12}" for i in range(2000)})
        m.add_formula("ByGroup", ["M"], "V[REMOVE SUM: M][BY SUM: P.Group]")
        m.recalc()
        mem = m.memory()
        self.assertEqual(mem["V"]["rows"], 24_000)
        # The values are a column with no extra capacity. The packed keys use less than 8 bytes per row
        self.assertGreater(mem["V"]["base"], 24_000 * 8)
        self.assertLess(mem["V"]["base"], 24_000 * 16)
        self.assertEqual(mem["V"]["delta_rows"], 0)
        m.set_cell("V", 1.0, P="p0", M="m0")
        m.recalc()
        self.assertEqual(m.memory()["V"]["delta_rows"], 1)
        self.assertIn("counts", m.memory()["ByGroup"])  # 差分集計の件数
        now, peak = nanashi_core.heap()
        self.assertGreater(now, before + 24_000 * 8)
        self.assertGreaterEqual(peak, now)
        nanashi_core.reset_heap_peak()
        self.assertEqual(nanashi_core.heap()[1], nanashi_core.heap()[0])
        del m
        gc.collect()
        self.assertLess(nanashi_core.heap()[0], before + 24_000 * 16)

    def test_reference_reports_rows_only(self):
        m = Named(Model(engine=ReferenceEngine()))
        m.add_dimension("P", ["a", "b"])
        m.add_input("V", ["P"], {("a",): 1.0})
        self.assertEqual(m.memory(), {"V": {"rows": 1}})


class Logs(unittest.TestCase):
    def test_observation_logs_do_not_grow_without_bound(self):
        m = Named(Model(engine=ReferenceEngine()))
        m.add_dimension("P", ["a", "b"])
        m.add_input("X", ["P"], {("a",): 1})
        m.add_formula("Y", ["P"], "X * 2")
        for i in range(LOG_MAX + 50):
            m.set_cell("X", float(i), P="a")
            m.recalc()
        self.assertEqual(len(m.eval_log), LOG_MAX)
        self.assertEqual(len(m.slice_log), LOG_MAX)
        self.assertEqual(m.eval_log[-1], "Y")


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class AggregationBudget(unittest.TestCase):
    """Each aggregation path checks the budget before it makes its buffers. The run at the fitting budget is
    accepted. The run below it is refused before it goes over max_bytes."""

    def check(self, build, fit, below, **cfg):
        import nanashi_core
        for max_bytes, refused in [(below, True), (fit, False)]:
            with self.subTest(max_bytes=max_bytes):
                m = Named(Model(engine=RustEngine(**({"max_bytes": max_bytes, "fuse": False, "par_min": 1 << 40} | cfg))))
                at, want = build(m)
                held = sum(v.get("base", 0) for v in m.memory().values())
                nanashi_core.reset_heap_peak()
                base = nanashi_core.heap()[0]
                if refused:
                    with self.assertRaisesRegex(ValueError, "メモリの予算を超える"):
                        m.recalc()
                else:
                    m.recalc()
                    self.assertEqual(m.get("Z", **at), want)
                self.assertLessEqual(held + nanashi_core.heap()[1] - base, max_bytes, "a buffer was made before the check")

    @staticmethod
    def grid(m, nx, ny, rows):
        m.add_dimension("X", [f"x{i}" for i in range(nx)])
        m.add_dimension("Y", [f"y{j}" for j in range(ny)])
        m.add_input("A", ["X", "Y"], {(f"x{i}", f"y{j}"): 1.0 for i in range(nx) for j in range(rows)})
        m.add_formula("Z", ["X"], "A[REMOVE SUM: Y]")
        return {"X": "x7"}, float(rows)

    def test_sort_path(self):
        # 200,000 groups take the sort path. Its pairs and its 2 results (SUM and COUNT) are about 9.2 MiB
        self.check(lambda m: self.grid(m, 200_000, 1, 1), fit=12 << 20, below=8 << 20)

    def test_sort_path_parallel(self):
        # With one row in each chunk, the parallel copy also holds 16 B of chunk sizes for each row
        self.check(lambda m: self.grid(m, 200_000, 1, 1), fit=12 << 20, below=6 << 20, par_min=0, chunk=1)

    def test_dense_path(self):
        # The partitioned fill checks the array (1.2 MiB) and the round buffers (1.5 MiB) first. Only the array
        # stays while the result is made
        self.check(lambda m: self.grid(m, 40_000, 10, 10), fit=7 << 20, below=6 << 20, scatter_bytes=0)

    def test_partitioned_buffers(self):
        # 32,768 slots of 32 B and 3 rounds of 65,536 rows. s0 has no department, so the first round has fewer
        # pairs. The pair buffer must not grow to 2 times its size in the second round
        def build(m):
            ns, nt, nd = 48, 4096, 8
            m.add_dimension("S", [f"s{i}" for i in range(ns)])
            m.add_dimension("T", [f"t{j}" for j in range(nt)])
            m.add_dimension("D", [f"d{i}" for i in range(nd)])
            m.add_property("S", "P", "D", {f"s{i}": f"d{i % nd}" for i in range(1, ns)})
            m.add_input("A", ["S", "T"], {(f"s{i}", f"t{j}"): 1.0 for i in range(ns) for j in range(nt)})
            m.add_formula("Z", ["D", "T"], "A[BY SUM: S.P]")
            return {"D": "d0", "T": "t5"}, float(ns // nd - 1)
        self.check(build, fit=int(4.875 * (1 << 20)), below=int(3.75 * (1 << 20)), scatter_bytes=0)

    def test_hash_merge(self):
        # 65,536 groups in the hash maps of the threads. With 2 or more threads, a merge holds the source map, the
        # destination map and its old table. With 1 thread, 16 MiB fits. Thus 16 MiB is refused or stays in it
        import nanashi_core
        nanashi_core.heap_exact(True)
        self.addCleanup(nanashi_core.heap_exact, False)
        for mib, must_fit in [(16, False), (20, True)]:
            with self.subTest(mib=mib):
                m = Named(Model(engine=RustEngine(max_bytes=mib << 20, fuse=False, par_min=0, stream_always=True)))
                at, want = self.grid(m, 65_536, 2, 1)
                held = sum(v.get("base", 0) for v in m.memory().values())
                nanashi_core.reset_heap_peak()
                base = nanashi_core.heap()[0]
                try:
                    m.recalc()
                    self.assertEqual(m.get("Z", **at), want)
                except ValueError as e:
                    self.assertFalse(must_fit, e)
                    self.assertRegex(str(e), "メモリの予算を超える")
                self.assertLessEqual(held + nanashi_core.heap()[1] - base, mib << 20, "the merge went over max_bytes")

    def test_table_path(self):
        # BY SUM with a member Metric uses the mapping table (1.1 MiB) and a hash map of 3,600 groups
        def build(m):
            ne, nm, nd = 24_000, 12, 300
            m.add_dimension("E", [f"e{i}" for i in range(ne)])
            m.add_dimension("M", [f"m{i}" for i in range(nm)])
            m.add_dimension("D", [f"d{i}" for i in range(nd)])
            m.add_input("S", ["E", "M"], {(f"e{i}", f"m{t}"): 1.0 for i in range(ne) for t in range(nm)})
            m.add_input("DeptOf", ["E", "M"], {(f"e{i}", f"m{t}"): f"d{i % nd}" for i in range(ne) for t in range(nm)}, kind="member:D")
            m.add_formula("Z", ["D", "M"], "S[BY SUM: E.DeptOf]")
            return {"D": "d7", "M": "m3"}, ne / nd
        self.check(build, fit=int(7.5 * (1 << 20)), below=7 << 20, stream_always=True)

    def test_table_falls_back_to_the_join(self):
        # The member Metric has 1,000 cells over 1,044,000 combinations. Its mapping table (4.2 MB) does not fit,
        # but the join of the 1,000 cells does
        m = Named(Model(engine=RustEngine(max_bytes=2 << 20, fuse=False, par_min=1 << 40, stream_always=True)))
        m.add_dimension("E", [f"e{i}" for i in range(87_000)])
        m.add_dimension("M", [f"m{i}" for i in range(12)])
        m.add_dimension("D", [f"d{i}" for i in range(30)])
        m.add_input("S", ["E", "M"], {(f"e{i}", "m0"): 1.0 for i in range(1000)})
        m.add_input("DeptOf", ["E", "M"], {(f"e{i}", "m0"): f"d{i % 30}" for i in range(1000)}, kind="member:D")
        m.add_formula("Z", ["D", "M"], "S[BY SUM: E.DeptOf]")
        m.recalc()
        self.assertEqual(m.get("Z", D="d7", M="m0"), 34.0)

    def test_spill(self):
        # Each group spills (3 partials) and then cancels back to 1.0, so its residual is 0. The 20,000 Spills
        # take about 1.7 MiB next to the 0.6 MiB array
        vals = [1.0, 2.0 ** -60, 2.0 ** -130, -(2.0 ** -60), -(2.0 ** -130)]

        def build(m):
            m.add_dimension("X", [f"x{i}" for i in range(20_000)])
            m.add_dimension("Y", [f"y{j}" for j in range(len(vals))])
            m.add_input("A", ["X", "Y"], {(f"x{i}", f"y{j}"): v for i in range(20_000) for j, v in enumerate(vals)})
            m.add_formula("Z", ["X"], "A[REMOVE SUM: Y]")
            return {"X": "x7"}, 1.0
        self.check(build, fit=4 << 20, below=3 << 20)

    def test_residual_cells(self):
        # Ten values of 0.1 give a residual that is not 0 in each of the 20,000 groups. The residual list grows
        # after the check of the other lists
        def build(m):
            m.add_dimension("X", [f"x{i}" for i in range(20_000)])
            m.add_dimension("Y", [f"y{j}" for j in range(10)])
            m.add_input("A", ["X", "Y"], {(f"x{i}", f"y{j}"): 0.1 for i in range(20_000) for j in range(10)})
            m.add_formula("Z", ["X"], "A[REMOVE SUM: Y]")
            return {"X": "x7"}, math.fsum([0.1] * 10)
        self.check(build, fit=4 << 20, below=int(3.5 * (1 << 20)))


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class WriteBudget(unittest.TestCase):
    """The write of a result into the store and the lookup arrays of a fused formula count against the budget."""

    def test_write_and_lookup_arrays_stay_in_the_budget(self):
        import gc

        import nanashi_core
        nanashi_core.heap_exact(True)
        self.addCleanup(nanashi_core.heap_exact, False)
        n, k = 20_000, 20
        mib = 1 << 20
        # (label, formula, dimensions, partition, fuse, edit, a budget in MiB that refuses it, a budget in MiB that
        # fits it). The fit budget is about 1.1x the smallest budget that works. The repacked case uses about 1.02x,
        # because the too large copy that it finds is only about 9% of the total. With an edit, the formula is
        # calculated first and the edit makes the work.
        wide = lambda m: m.spread("P", 1.0)  # noqa: E731
        cases = [
            ("the lookup array of A", "A + 0", ["X", "Y"], None, True, None, 2.0, 2.6),
            ("the lookup arrays of A and B", "A * B + 0", ["X", "Y"], None, True, None, 2.0, 2.6),
            ("the write of the result", "R * P", ["X", "Q"], None, False, None, 15.5, 19.8),
            ("the write of a repacked result", "C * 2", ["X", "Y"], "Y", False, None, 2.4, 2.65),
            ("the write after an edit of all members", "P * 2", ["X"], None, False, wide, 2.3, 2.7),
        ]

        def build(engine):
            m = Named(Model(engine=engine, auto_layout=False))
            m.add_dimension("X", [f"x{i}" for i in range(n)])
            m.add_dimension("Y", ["y0", "y1"])
            m.add_dimension("Q", [f"q{i}" for i in range(k)])
            for name in "AB":
                m.add_input(name, ["X", "Y"], {(f"x{i}", f"y{i % 2}"): 1.0 if i % 3 else -1.0 for i in range(n)},
                            partition="X")
            # 32,800 cells: a Vec that grows one cell at a time gets room for 65,536
            m.add_input("C", ["X", "Y"], {(f"x{i % n}", f"y{i // n}"): 1.0 for i in range(32_800)}, partition="X")
            m.add_input("P", ["X"], {(f"x{i}",): 1.0 if i % 2 else -1.0 for i in range(n)})
            m.add_input("R", ["Q"], {(f"q{i}",): 1.0 for i in range(k)})
            return m

        def held(m):
            return sum(r["base"] + r["delta"] + r["index"] + r.get("counts", 0) for r in m.memory().values())

        def work(m, max_bytes):
            """Recalculate with max_bytes. Give the error (or None) and the stored bytes plus the new Rust heap at
            the peak."""
            m.engine.core.configure(max_bytes=int(max_bytes * mib))
            gc.collect()
            start, base = held(m), nanashi_core.heap()[0]
            nanashi_core.reset_heap_peak()
            try:
                m.recalc()
                error = None
            except ValueError as e:
                error = str(e)
            return error, start + nanashi_core.heap()[1] - base

        for label, formula, dims, partition, fuse, edit, refused, fits in cases:
            with self.subTest(label):
                kw = {"partition": partition} if partition else {}
                gc.collect()
                m = build(RustEngine(max_bytes=1 << 50, fuse=fuse, par_min=1 << 40))
                ref = build(ReferenceEngine())
                for x in (m, ref):
                    x.recalc()
                    x.add_formula("Z", dims, formula, **kw)
                    if edit:
                        x.recalc()
                        edit(x)
                error, total = work(m, refused)
                self.assertRegex(error or "", "メモリの予算を超える")
                self.assertLessEqual(total, refused * mib, "the refused work went over max_bytes")
                error, total = work(m, fits)
                self.assertIsNone(error)
                self.assertLessEqual(total, fits * mib, "the work went over max_bytes")
                ref.recalc()
                self.assertEqual(sorted(m.rows("Z")[0]), sorted(ref.rows("Z")[0]))
                del m

    def test_a_larger_budget_accepts_what_a_smaller_budget_accepts(self):
        """The lookup arrays leave room for the cells that the scan needs after them."""
        n = 5000

        def build():
            m = Named(Model(engine=RustEngine(max_bytes=1 << 50, fuse=True, par_min=1 << 40)))
            m.add_dimension("X", [f"x{i}" for i in range(n)])
            m.add_dimension("Y", ["y0", "y1", "y2"])
            m.add_input("A", ["X", "Y"], {(f"x{i}", f"y{i % 3}"): (i % 13) / 7.0 - 0.9 for i in range(n)})
            m.add_input("B", ["Y", "X"], {(f"y{(i + 1) % 3}", f"x{i}"): 1.0 / (1 + i % 11) for i in range(0, n, 2)})
            m.add_input("C", ["X"], {(f"x{i}",): 0.1 * (i % 17) for i in range(n)})
            m.recalc()
            m.add_formula("Z", ["X", "Y"], "A - B * C[EXPAND: Y] + 0.3")
            return m

        accepted = []
        for i in range(17):
            m = build()
            m.engine.core.configure(max_bytes=int((1.2 + 0.05 * i) * (1 << 20)))
            try:
                m.recalc()
                accepted.append(True)
            except ValueError:
                accepted.append(False)
        self.assertIn(True, accepted)
        self.assertTrue(all(accepted[accepted.index(True):]), accepted)

    def test_writes_stay_in_the_budget_at_the_smallest_budget(self):
        """A part write into a shared delta tree copies its nodes. The writes of one level wait in sequence, with the
        changed members of a whole write. The per-period scan keeps the delta tree of the earlier periods. A part
        write into an empty store makes new columns with a block buffer, and member lists for each axis.
        The heap count is exact here, so an excess of a few KiB fails."""
        import nanashi_core
        n, mib = 40_000, 1 << 20
        nanashi_core.heap_exact(True)
        self.addCleanup(nanashi_core.heap_exact, False)

        def build(k):
            m = Named(Model(engine=RustEngine(max_bytes=1 << 50, fuse=False, par_min=1 << 40)))
            m.add_dimension("X", [f"x{i}" for i in range(n)])
            m.add_input("A", ["X"], {(f"x{i}",): float(i) for i in range(n)})
            for j in range(k):
                m.add_formula(f"Z{j}", ["X"], f"A * {j + 2}")
            m.recalc()
            return m

        def edit(m, keys):
            for i in range(n // keys // 2, n, n // keys):
                m.set_cell("A", 7.0, X=f"x{i}")

        def held(m):
            return sum(r["base"] + r["delta"] + r["index"] + r.get("counts", 0) for r in m.memory().values())

        def work(m, max_bytes):
            m.engine.core.configure(max_bytes=max_bytes)
            gc.collect()
            start, base = held(m), nanashi_core.heap()[0]
            nanashi_core.reset_heap_peak()
            try:
                m.recalc()
                error = None
            except ValueError as e:
                error = str(e)
            return error, start + nanashi_core.heap()[1] - base

        def smallest(trial):
            """The smallest budget (in steps of 4 KiB) that trial accepts, and the bytes of that work."""
            lo, hi = 0, 64 * mib
            while hi - lo > 4096:
                mid = (lo + hi) // 2
                lo, hi = (lo, mid) if trial(mid)[0] is None else (mid, hi)
            return hi, trial(hi)

        # The delta tree of each Z gets 4,000 rows. Then a fork shares it, and an edit of 60 keys changes it
        shared = build(4)
        for r in range(5):
            for i in range(r, n, n // 800):
                shared.set_cell("A", float(-i - r), X=f"x{i}")
            shared.recalc()

        def fork(max_bytes):
            f = shared.fork()
            edit(f, 60)
            return work(f, max_bytes)

        def level(max_bytes):
            m = build(8)
            edit(m, 2000)
            return work(m, max_bytes)

        def whole(max_bytes):
            # Empty destinations and 4,096 changed rows on 5 axes: the member lists are as large as the result
            dims, k = [f"D{j}" for j in range(5)], 4096
            m = Named(Model(engine=RustEngine(max_bytes=1 << 50, fuse=False, par_min=1 << 40)))
            for d in dims:
                m.add_dimension(d, [f"m{i}" for i in range(k)])
            m.add_input("A", dims, {tuple(f"m{i}" for _ in dims): float(i + 1) for i in range(k)})
            m.recalc()
            for j in range(4):
                m.add_formula(f"Z{j}", dims, f"A * {j + 2}")
            return work(m, max_bytes)

        def scan(max_bytes):
            # IF on PREVIOUS is not a tree, so the scan writes 80 periods of 50 rows into one delta tree
            m = Named(Model(engine=RustEngine(max_bytes=1 << 50, par_min=1 << 40)))
            m.add_dimension("X", [f"x{i}" for i in range(50)])
            m.add_dimension("T", [f"t{i}" for i in range(80)], ordered=True)
            m.add_input("A", ["X", "T"], {(f"x{i}", f"t{t}"): 1.0 + i % 3 for i in range(50) for t in range(80)})
            m.recalc()
            m.add_formula("Z", ["X", "T"], "IF(ISBLANK(PREVIOUS(T)), A, PREVIOUS(T) + A)")
            return work(m, max_bytes)

        def part(rows, axes, size, compact_min):
            # An empty destination, then `rows` new cells in the first 64 members of D0 (a part region)
            def trial(max_bytes):
                dims = [f"D{j}" for j in range(axes)]
                m = Named(Model(engine=RustEngine(max_bytes=1 << 50, fuse=False, par_min=1 << 40, compact_min=compact_min)))
                for d in dims:
                    m.add_dimension(d, [f"m{i}" for i in range(size)])
                m.add_input("A", dims, {})
                m.add_formula("Z", dims, "A * 2")
                m.recalc()
                for i in range(rows):
                    m.set_cell("A", float(i + 1), **{d: f"m{i % 64 if j == 0 else (i // 64 + i * j) % size}" for j, d in enumerate(dims)})
                return work(m, max_bytes)
            return trial

        for label, trial in [("a fork", fork), ("8 Metrics in one level", level), ("4 whole writes in one level", whole),
                             ("a scan by period", scan), ("member lists of a part write", part(64, 9, 128, 0)),
                             ("new columns of a part write", part(2000, 2, 256, 0))]:
            with self.subTest(label):
                budget, (error, total) = smallest(trial)
                self.assertIsNone(error)
                self.assertLessEqual(total, budget, "the work went over max_bytes")


if __name__ == "__main__":
    unittest.main()
