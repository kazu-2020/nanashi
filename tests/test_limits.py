"""エンジンの上限の検査、セル数の見積もりの上限、観察用の記録の上限、メモリの内訳。"""
import tempfile
import unittest

from sparse_engine import FormulaError, Model
from sparse_engine.engine import ReferenceEngine
from sparse_engine.model import LOG_MAX

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None


def wide(engine) -> Model:
    m = Model(engine=engine)
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
        m.add_formula("Z", ["D0", "D1", "D2"], "X * Y[REMOVE SUM: D3, D4]")
        self.assertEqual(m.get("Z", D0="m1", D1="m2", D2="m3"), 2.0)

    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_member_that_would_widen_past_64_bits_is_refused(self):
        m = Model(engine=RustEngine())
        for i in range(4):
            m.add_dimension(f"D{i}", [f"m{j}" for j in range(1 << 13)])  # 13 ビット × 4
        m.add_dimension("E", [f"e{j}" for j in range(1 << 12)])          # 12 ビット（合わせて 64 ビット）
        m.add_input("X", ["D0", "D1", "D2", "D3"], {("m1", "m1", "m1", "m1"): 1.0})
        m.add_input("Y", ["E"], {("e1",): 2.0})
        m.add_formula("Z", ["D0"], "(X[EXPAND: E] * Y)[REMOVE SUM: D1, D2, D3, E]")
        self.assertEqual(m.get("Z", D0="m1"), 2.0)
        with self.assertRaisesRegex(FormulaError, "途中の結果の軸"):  # E が 13 ビットになると収まらない
            m.add_member("E", "e_new")
        self.assertNotIn("e_new", m.dimensions["E"])
        m.set_cell("Y", 3.0, E="e1")
        self.assertEqual(m.get("Z", D0="m1"), 3.0)

    @staticmethod
    def by_metric(ebits: int) -> Model:
        """Salary[E, D1, D2, D3] を所属（DeptOf[E] -> Dept）で部署別に集計するモデル。結果の軸は
        D1、D2、D3、Dept の 52 ビットだが、途中で社員と部署の両方を持つ（E が 13 ビットなら 65 ビット）。"""
        m = Model(engine=RustEngine())
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
        m.add_formula("Cost", ["D1", "D2", "D3"], "Salary[REMOVE SUM: E]")  # 直せば通る
        self.assertEqual(m.get("Cost", D1="m1", D2="m1", D3="m1"), 15.0)

    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_member_that_would_widen_a_by_metric_join_past_64_bits_is_refused(self):
        m = self.by_metric(12)  # 結合は 64 ビットでちょうど収まる
        self.assertEqual(m.get("Cost", D1="m1", D2="m1", D3="m1", Dept="g1"), 10.0)
        with self.assertRaisesRegex(FormulaError, "途中の結果の軸"):  # E が 13 ビットになると結合が収まらない
            m.add_member("E", "e_new")
        self.assertNotIn("e_new", m.dimensions["E"])
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
    m = Model(engine=engine, max_cells=10_000)
    m.add_dimension("Customer", [f"c{i}" for i in range(200)])
    m.add_dimension("SKU", [f"s{i}" for i in range(100)])
    m.add_dimension("Week", [f"w{i}" for i in range(52)], ordered=True)
    m.add_input("Sales", ["Customer", "SKU"], {("c1", "s1"): 1.0, ("c2", "s1"): 2.0})
    return m


class CellEstimates(unittest.TestCase):
    def test_default_limit(self):
        self.assertEqual(Model(engine=ReferenceEngine()).max_cells, 1_000_000_000)

    def test_rejects_formulas_that_densify_beyond_the_limit(self):
        for engine in ENGINES:
            with self.subTest(engine=engine.__name__):
                m = big(engine())
                m.add_formula("Filled", ["Customer", "SKU"], "IFBLANK(Sales, 0)")
                with self.assertRaisesRegex(FormulaError, r"Filled: .*最大 20,000 .*上限 10,000 .*"
                                                          r"IFBLANK が \['Customer', 'SKU'\]"):
                    m.recalc()
                m.add_formula("Filled", ["Customer", "SKU"], "Sales * 2")  # 直せば通る
                self.assertEqual(m.value("Filled").cells, {("c1", "s1"): 2.0, ("c2", "s1"): 4.0})
                self.assertEqual(m.cell_estimates["Filled"], 2.0)

    def test_rejected_definition_is_rolled_back_in_a_transaction(self):
        for engine in ENGINES:
            with self.subTest(engine=engine.__name__):
                m = big(engine())
                m.add_formula("Double", ["Customer", "SKU"], "Sales * 2")
                m.recalc()
                with self.assertRaises(FormulaError):
                    with m.transaction():
                        m.add_formula("Double", ["Customer", "SKU"], "Sales + 1")  # 定数との足し算で密になる
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
                    m = Model(engine=engine(), max_cells=limit)
                    m.add_dimension("P", ["a", "b", "c"])
                    m.add_dimension("M", ["x", "y", "z"])
                    m.add_input("V", ["P", "M"], {("a", "x"): 1.0})
                    m.add_formula("Filled", ["P", "M"], "IFBLANK(V, 0)")
                    if limit is not None:
                        with self.assertRaisesRegex(FormulaError, "最大 9 と見積もられ、上限 8 を超える"):
                            m.recalc()
                    else:
                        self.assertEqual(len(m.value("Filled").cells), 9)
                        self.assertEqual(m.cell_estimates["Filled"], 9.0)

    def test_scan_is_estimated_as_carried_over_all_periods(self):
        for engine in ENGINES:
            with self.subTest(engine=engine.__name__):
                m = big(engine())
                m.add_input("In", ["Customer", "SKU", "Week"], {("c1", "s1", "w3"): 5.0})
                m.add_formula("Stock", ["Customer", "SKU", "Week"], "PREVIOUS(Week) + In")
                m.recalc()
                self.assertEqual(m.cell_estimates["Stock"], 52.0)  # 1 セルが 52 週へ持ち越されうる
                self.assertEqual(len(m.value("Stock").cells), 49)  # 実際は w3 から w51 まで

    def test_incremental_estimates_match_full(self):
        """定義を変えたときに下流だけ見積もり直した結果は、全体を見積もり直した結果と同じ。"""
        from .test_expr_coverage import model
        for engine in ENGINES:
            with self.subTest(engine=engine.__name__):
                m = model(engine())
                m.recalc()
                steps = [
                    lambda: m.add_formula("Revenue", ["Product", "Month"], "IFBLANK(Volume, 0) * Price[EXPAND: Month]"),
                    lambda: m.rename_metric("Revenue", "Rev"),
                    lambda: m.add_formula("Rev", ["Product", "Month"], "Volume * Price"),
                    lambda: m.add_input("Volume", ["Product", "Month"], {("A", "Jan"): 1, ("B", "Jan"): 2}),
                    lambda: m.add_formula("Leaf", ["Month"], "Total[FILTER: Actual]"),
                    lambda: m.remove_metric("Leaf"),
                    lambda: m.add_formula("Total", ["Month"], "IFBLANK(ByCat[REMOVE SUM: Category], 0)"),
                    lambda: m.add_input("Total", ["Month"], {("Jan",): 1.0}),  # 式を入力にすると何も読まない
                    lambda: m.remove_metric("ByCat"),
                    lambda: m.add_formula("Total", ["Month"], "Rev[REMOVE SUM: Product]"),
                ]
                for step in steps:  # m は差分の見積もりだけを続け、全体の見積もり直しは複製で行う
                    step()
                    m.recalc()
                    full = m.fork()
                    full._invalidate()
                    full.recalc()
                    self.assertEqual(m.cell_estimates, full.cell_estimates)

    def test_limit_is_saved(self):
        for engine in ENGINES:
            with self.subTest(engine=engine.__name__), tempfile.TemporaryDirectory() as d:
                m = Model(engine=engine(), max_cells=123)
                m.add_dimension("P", ["a"])
                m.save(d)
                self.assertEqual(Model.load(d, engine()).max_cells, 123)


class Memory(unittest.TestCase):
    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_rust_reports_storage_bytes(self):
        import gc

        import nanashi_core
        # heap() は数え始めてからの確保と解放の差なので、前のテストが残した循環参照のゴミ（Rust の格納データを
        # 持つもの）が数えている途中で回収されると減る。数え始める前に回収しておく
        gc.collect()
        nanashi_core.track_heap(True)
        self.addCleanup(nanashi_core.track_heap, False)
        before = nanashi_core.heap()[0]
        m = Model(engine=RustEngine())
        m.add_dimension("P", [f"p{i}" for i in range(2000)])
        m.add_dimension("M", [f"m{i}" for i in range(12)])
        m.add_input("V", ["P", "M"], {(f"p{i}", f"m{j}"): float(i) for i in range(2000) for j in range(12)})
        m.add_property("P", "Group", "M", {f"p{i}": f"m{i % 12}" for i in range(2000)})
        m.add_formula("ByGroup", ["M"], "V[REMOVE SUM: M][BY SUM: P.Group]")
        m.recalc()
        mem = m.memory()
        self.assertEqual(mem["V"]["rows"], 24_000)
        self.assertGreaterEqual(mem["V"]["base"], 24_000 * 16)
        self.assertEqual(mem["V"]["delta_rows"], 0)
        m.set_cell("V", 1.0, P="p0", M="m0")
        m.recalc()
        self.assertEqual(m.memory()["V"]["delta_rows"], 1)
        self.assertIn("counts", m.memory()["ByGroup"])  # 差分集計の件数
        now, peak = nanashi_core.heap()
        self.assertGreater(now, before + 24_000 * 16)
        self.assertGreaterEqual(peak, now)
        nanashi_core.reset_heap_peak()
        self.assertEqual(nanashi_core.heap()[1], nanashi_core.heap()[0])
        nanashi_core.track_heap(False)  # 止めている間は数えない
        m.add_input("W", ["P", "M"], {(f"p{i}", "m0"): 1.0 for i in range(2000)})
        m.recalc()
        self.assertEqual(nanashi_core.heap()[0], now)

    def test_reference_reports_rows_only(self):
        m = Model(engine=ReferenceEngine())
        m.add_dimension("P", ["a", "b"])
        m.add_input("V", ["P"], {("a",): 1.0})
        self.assertEqual(m.memory(), {"V": {"rows": 1}})


class Logs(unittest.TestCase):
    def test_observation_logs_do_not_grow_without_bound(self):
        m = Model(engine=ReferenceEngine())
        m.add_dimension("P", ["a", "b"])
        m.add_input("X", ["P"], {("a",): 1})
        m.add_formula("Y", ["P"], "X * 2")
        for i in range(LOG_MAX + 50):
            m.set_cell("X", float(i), P="a")
            m.recalc()
        self.assertEqual(len(m.eval_log), LOG_MAX)
        self.assertEqual(len(m.slice_log), LOG_MAX)
        self.assertEqual(m.eval_log[-1], "Y")


if __name__ == "__main__":
    unittest.main()
