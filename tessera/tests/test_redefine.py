"""定義の差分再計算。式や入力を足したり置き換えたりしても、変えた Metric とその影響だけを計算し直す。"""
import random
import unittest
from unittest import mock

from sparse_engine import FormulaError, Model
from sparse_engine.engine import ReferenceEngine

from .test_engines import build_with
from .test_incremental import check_full, model as build, same, snapshot
from .test_members import random_round as edit_or_add_member

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None


def computed(engine=None) -> Model:
    m = build_with(engine) if engine is not None else build()
    m.recalc()
    m.slice_log.clear()
    m.eval_log.clear()
    m.delta_log.clear()
    return m


class Redefine(unittest.TestCase):
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        self.m = computed(self.engine())

    def test_new_formula_computes_only_itself(self):
        self.m.add_formula("Double", ["Product", "Month"], "Margin * 2")
        self.m.recalc()
        self.assertEqual(list(self.m.eval_log), ["Double"])
        self.assertEqual(self.m.get("Double", Product="A", Month="Jan"), 2 * (30 - 7))
        check_full(self, self.m)

    def test_changed_formula_reaches_only_changed_cells(self):
        # Cost を 2 倍で引くと、Cost のあるセル（A の Jan、B の Mar、D の Feb）だけが変わる
        self.m.add_formula("Margin", ["Product", "Month"], "Revenue[REMOVE SUM: Region] - Cost * 2", id=self.m.metric("Margin").id)
        self.m.recalc()
        regions = {n: r for n, r in self.m.slice_log}
        self.assertEqual(regions["Margin"], {})
        self.assertEqual(regions["Picked"]["Month"], {"Jan", "Feb", "Mar"})  # Cost のある月だけ
        self.assertNotIn("RevByCat", regions)  # 上流は計算し直さない
        check_full(self, self.m)

    def test_same_values_stop_propagation(self):
        self.m.add_formula("Margin", ["Product", "Month"], "Revenue[REMOVE SUM: Region] - Cost * 1", id=self.m.metric("Margin").id)
        self.m.recalc()
        self.assertEqual(list(self.m.eval_log), ["Margin"])

    def test_delta_aggregate_keeps_working(self):
        self.m.add_formula("DeptSalary", ["Department"], "Salary[BY SUM: Employee.Department] * 1", id=self.m.metric("DeptSalary").id)
        self.m.add_formula("Headcount", ["Department"], "Salary[BY COUNT: Employee.Department]")
        self.m.recalc()
        self.m.delta_log.clear()
        self.m.set_cell("Salary", 50, Employee="e2")
        self.m.recalc()
        self.assertEqual(self.m.get("Headcount", Department="Sales"), 2)
        self.assertIn("Headcount", self.m.delta_log)
        check_full(self, self.m)

    def test_formula_that_becomes_a_scan(self):
        self.m.add_formula("Margin", ["Product", "Month"], "PREVIOUS(Month) * 0.5 + Revenue[REMOVE SUM: Region] - Cost", id=self.m.metric("Margin").id)
        self.m.recalc()
        check_full(self, self.m)
        self.m.add_formula("Stock", ["Product", "Month"], "Margin - Outflow", id=self.m.metric("Stock").id)  # the cycle goes away
        self.m.recalc()
        check_full(self, self.m)

    def test_error_then_fix(self):
        with self.assertRaisesRegex(FormulaError, "Missing2"):  # an unknown Metric is an error at the definition
            self.m.add_formula("Bad", ["Product"], "Missing2 + 1")
        self.m.add_formula("Bad", ["Product"], "Price * 3")
        self.assertEqual(self.m.get("Bad", Product="B"), 60)
        check_full(self, self.m)

    def test_cycle_is_an_error(self):
        self.m.add_formula("Revenue", ["Product", "Region", "Month"], "Volume * Price + Margin[EXPAND: Region]", id=self.m.metric("Revenue").id)
        with self.assertRaisesRegex(FormulaError, "循環"):
            self.m.recalc()

    def test_type_change_rechecks_dependents(self):
        self.m.add_formula("Margin", ["Product", "Month", "Region"], "Revenue - Cost", id=self.m.metric("Margin").id)
        with self.assertRaises(FormulaError):  # 下流の式の軸が合わなくなる
            self.m.recalc()

    def small(self) -> Model:
        m = Model(engine=self.engine())
        m.add_dimension("P", ["a", "b"])
        m.add_dimension("M", ["x", "y"])
        m.add_input("X", ["P"], {("a",): 1.0})
        m.add_formula("Y", ["P"], "X * 2")
        m.recalc()
        return m

    def test_input_axes_change_rechecks_unchanged_formula(self):
        # Y の式は変えていない（同じ式オブジェクトのまま検査し直す）。検査の結果を使い回さない
        m = self.small()
        m.add_input("X", ["P", "M"], {("a", "x"): 1.0}, id=m.metric("X").id)
        with self.assertRaisesRegex(FormulaError, r"^Y: 式の軸 \('P', 'M'\) が宣言した軸 \('P',\) と一致しない$"):
            m.recalc()
        m.add_input("X", ["P"], {("a",): 3.0}, id=m.metric("X").id)  # with the original dimensions, it can calculate
        m.recalc()
        self.assertEqual(m.get("Y", P="a"), 6.0)

    def test_input_kind_change_rechecks_unchanged_formula(self):
        m = self.small()
        m.add_formula("Z", ["P"], "X")
        m.recalc()
        m.add_input("X", ["P"], {("a",): True}, kind="boolean", id=m.metric("X").id)
        with self.assertRaisesRegex(FormulaError, "^'\\*' の左辺 には number が必要だが boolean が渡された$"):
            m.recalc()
        m.remove_metric("Y")
        with self.assertRaisesRegex(FormulaError, "^Z: 式の値は boolean だが number として宣言されている$"):
            m.recalc()

    def test_replaced_input_uses_delta(self):
        self.m.add_input("Salary", ["Employee"], {("e1",): 100, ("e2",): 200, ("e3",): 300}, id=self.m.metric("Salary").id)
        self.m.recalc()
        self.assertEqual(self.m.get("DeptSalary", Department="Sales"), 300)
        self.assertIn("DeptSalary", self.m.delta_log)
        check_full(self, self.m)

    def test_replaced_input_after_pending_edit(self):
        self.m.set_cell("Salary", 999, Employee="e4")
        self.m.add_input("Salary", ["Employee"], {("e1",): 1}, id=self.m.metric("Salary").id)
        self.assertEqual(self.m.get("DeptSalary", Department="Eng"), None)
        check_full(self, self.m)

    def test_replaced_input_after_pending_edit_keeps_old_value(self):
        self.m.set_cell("Salary", 999, Employee="e4")  # 再計算する前に置き換える
        self.m.add_input("Salary", ["Employee"], {("e1",): 100, ("e3",): 300, ("e4",): 50}, id=self.m.metric("Salary").id)
        self.assertEqual(self.m.get("DeptSalary", Department="Eng"), 350)
        check_full(self, self.m)

    def test_aggregate_entering_and_leaving_a_scan(self):
        # RevTotal は差分集計する。Revenue が RevTotal の前月を読むと、両者は scan になる
        self.m.add_formula("RevTotal", ["Product", "Month"], "Revenue[REMOVE SUM: Region]")
        self.m.recalc()
        self.m.add_formula("Revenue", ["Product", "Region", "Month"],
                           "Volume * Price + RevTotal[SELECT: Month - 1][EXPAND: Region] * 0", id=self.m.metric("Revenue").id)
        self.m.recalc()
        self.m.set_cell("Volume", 5, Product="A", Region="S", Month="Jan")  # scan の中で集計元が増える
        self.m.recalc()
        self.m.add_formula("Revenue", ["Product", "Region", "Month"], "Volume * Price", id=self.m.metric("Revenue").id)  # the formula leaves the scan
        self.m.recalc()
        self.m.set_cell("Volume", None, Product="A", Region="N", Month="Jan")  # 件数が正しくないと空になる
        self.assertEqual(self.m.get("RevTotal", Product="A", Month="Jan"), 50)
        check_full(self, self.m)

    def test_formula_to_input_and_back(self):
        self.m.add_input("Margin", ["Product", "Month"], {("A", "Jan"): 100.0}, id=self.m.metric("Margin").id)
        self.assertEqual(self.m.get("Picked", Product="A", Month="Jan"), 100)
        check_full(self, self.m)
        self.m.add_formula("Margin", ["Product", "Month"], "Revenue[REMOVE SUM: Region] - Cost", id=self.m.metric("Margin").id)
        self.assertEqual(self.m.get("Picked", Product="A", Month="Jan"), 23)
        check_full(self, self.m)

    def test_replaced_property(self):
        self.m.add_property("Product", "Category", "Category", {"A": "Y", "B": "X", "C": "X", "D": "Y"}, id=self.m.property_id("Product", "Category"))
        self.m.recalc()
        regions = {n: r for n, r in self.m.slice_log}
        self.assertIn("RevByCat", regions)
        self.assertNotIn("Margin", regions)
        check_full(self, self.m)

    def test_replaced_property_used_only_by_lookup(self):
        m = Model(engine=self.engine())
        m.add_dimension("Product", ["A", "B"])
        m.add_dimension("Category", ["X", "Y"])
        m.add_property("Product", "Category", "Category", {"A": "X", "B": "Y"})
        m.add_input("Rate", ["Category"], {("X",): 1, ("Y",): 2})
        m.add_formula("Looked", ["Product"], "Rate[BY: Product.Category]")
        m.recalc()
        m.add_property("Product", "Category", "Category", {"A": "Y", "B": "Y"}, id=m.property_id("Product", "Category"))
        self.assertEqual(dict(m.value("Looked").cells), {("A",): 2, ("B",): 2})

    def test_set_property_values_changes_only_given_members(self):
        m = Model(engine=self.engine())
        m.add_dimension("Product", ["A", "B", "C"])
        m.add_dimension("Category", ["X", "Y"])
        m.add_property("Product", "Category", "Category", {"A": "X", "B": "Y", "C": "Y"})
        m.add_input("Rate", ["Category"], {("X",): 1, ("Y",): 2})
        m.add_formula("Looked", ["Product"], "Rate[BY: Product.Category]")
        m.recalc()
        m.set_property_values("Product", "Category", {"A": "Y", "C": None})
        self.assertEqual(m.dimension("Product").properties[m.property_id("Product", "Category")][1], {"A": "Y", "B": "Y"})
        self.assertEqual(dict(m.value("Looked").cells), {("A",): 2, ("B",): 2})
        with self.assertRaises(ValueError):
            m.set_property_values("Product", "Category", {"A": "Z"})
        with self.assertRaises(ValueError):
            m.set_property_values("Product", "Size", {"A": "X"})
        with self.assertRaises(ValueError):
            m.set_property_values("Product", "Category", {"Missing": None})

    def test_overrides_survive_redefinition(self):
        self.m.add_formula("Plus1", ["Product"], "Price + 1", overridable=True, id=self.m.metric("Plus1").id)
        self.m.set_cell("Plus1", 50, Product="A")
        self.m.add_formula("Plus1", ["Product"], "Price + 2", overridable=True, id=self.m.metric("Plus1").id)
        self.assertEqual(self.m.get("Plus1", Product="A"), 50)
        self.assertEqual(self.m.get("Plus1", Product="B"), 22)
        check_full(self, self.m)

    def test_fork_is_independent(self):
        before = snapshot(self.m)
        fork = self.m.fork()
        fork.add_formula("Margin", ["Product", "Month"], "Cost * 3", id=fork.metric("Margin").id)
        fork.recalc()
        self.assertEqual(snapshot(self.m), before)
        check_full(self, fork)


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustRedefine(Redefine):
    engine = staticmethod(RustEngine) if RustEngine is not None else None


# ---------------------------------------------------------------- ランダムな定義の変更

def dependents(m: Model, name: str) -> set[str]:
    """name を（間接的にでも）参照する Metric。"""
    users: dict[str, set[str]] = {}
    for src, edges in m._edges.items():
        for e in edges:
            users.setdefault(e.target, set()).add(src)
    out, todo = set(), [name]
    while todo:
        for u in users.get(todo.pop(), ()):
            if u not in out:
                out.add(u)
                todo.append(u)
    return out


def template(rng: random.Random, m: Model, target: str, dims: tuple[str, ...]) -> str | None:
    """target（軸 dims の number の Metric）の式。target に依存する Metric は使わない（循環になるので）。"""
    banned = dependents(m, m.metric(target).id) | {m.metric(target).id}
    same = [x.name for x in m.metrics.values() if set(x.dims) == set(dims) and x.kind == "number"
            and x.id not in banned and not x.name.startswith("__")]
    if not same:
        return None
    a, b = rng.choice(same), rng.choice(same)
    choices = [f"{a} * 2", f"{a} + {b}", f"IF({a} > 5, {a}, 0)", f"IFBLANK({a}, 1)", f"{a}[FILTER: {b} > 3]"]
    if m.dimension_id("Month") in dims and m.dimension("Month").ordered:
        choices.append(f"PREVIOUS(Month) * 0.5 + {a}")
    return rng.choice(choices)


def redefine(rng: random.Random, models: list[Model], counter: list[int]) -> None:
    """式の追加と置き換え、入力の置き換え、計算 Metric と入力の入れ替え、プロパティの置き換え、
    Metric の名前の変更と削除のどれか。"""
    m0 = models[0]
    m0.recalc()
    kind = rng.random()
    numbers = [x.name for x in m0.metrics.values() if x.kind == "number" and not x.name.startswith("__")]
    if kind < 0.25:  # 新しい計算 Metric
        a = rng.choice(numbers)
        dims = tuple(m0.dimension(d).name for d in m0.metric(a).dims)  # names: the models have different ids
        counter[0] += 1
        name = f"N{counter[0]}"
        choices = [(f"{a} * 3", dims)]
        for d in dims:
            choices.append((f"{a}[REMOVE SUM: {d}]", tuple(x for x in dims if x != d)))
        if "Product" in dims:
            choices.append((f"{a}[BY SUM: Product.Category]",
                            tuple("Category" if x == "Product" else x for x in dims)))
        if "Month" in dims:
            choices.append((f"PREVIOUS(Month) + {a}", dims))
        formula, dims = rng.choice(choices)
        for m in models:
            m.add_formula(name, dims, formula)
    elif kind < 0.5:  # 計算 Metric の式を置き換える（入力を計算 Metric にすることもある）
        target = rng.choice(numbers)
        formula = template(rng, m0, target, m0.metric(target).dims)
        if formula is None:
            return
        for m in models:
            m.add_formula(target, m.metric(target).dims, formula, id=m.metric(target).id)
    elif kind < 0.7:  # 入力に置き換える（計算 Metric を入力にすることもある）
        target = rng.choice(numbers)
        dims = tuple(m0.dimension(d).name for d in m0.metric(target).dims)
        cells = {}
        for _ in range(rng.randint(0, 4)):
            key = tuple(rng.choice(m0.dimension(d).members) for d in dims)
            cells[key] = float(rng.randint(-5, 60))
        for m in models:
            m.add_input(target, dims, cells, id=m.metric(target).id)
    elif kind < 0.8:  # プロパティを置き換える
        mapping = {p: rng.choice(m0.dimension("Category").members) for p in m0.dimension("Product").members}
        for m in models:
            m.add_property("Product", "Category", "Category", mapping, id=m.property_id("Product", "Category"))
    elif kind < 0.9:  # Metric の名前を変える
        target = rng.choice([n for n in m0._metric_ids if not n.startswith("__")])
        counter[0] += 1
        for m in models:
            m.rename_metric(target, f"{target.split('_')[0]}_{counter[0]}")
    else:  # 誰も参照していない Metric を消す
        leaves = [x.name for x in m0.metrics.values() if not x.name.startswith("__") and dependents(m0, x.id) == set()]
        if leaves:
            target = rng.choice(leaves)
            for m in models:
                m.remove_metric(target)


def run_random(test, seed: int, rounds: int, engines) -> None:
    rng = random.Random(seed)
    models = [computed(e()) for e in engines]
    counter, members = [0], [0]
    for round_ in range(rounds):
        for _ in range(rng.randint(1, 3)):
            if rng.random() < 0.4:
                redefine(rng, models, counter)
                test.assertIsNotNone(models[0]._plan)  # 全体の計算し直しに戻っていない
            else:
                edit_or_add_member(rng, models, members)
        if len(models) == 1:
            m = models[0]
            incremental = snapshot(m)
            m._invalidate()
            full = snapshot(m)
            pairs = [(incremental, full, "差分", "全体")]
        else:
            pairs = [(snapshot(models[0]), snapshot(models[1]), "参照", "Rust")]
        for a, b, la, lb in pairs:
            for name in a:
                with test.subTest(round=round_, metric=name):
                    test.assertTrue(same(a[name], b[name]), f"{name}\n{la}: {a[name]}\n{lb}: {b[name]}")


class MatchesFullRecalc(unittest.TestCase):
    def test_random(self):
        run_random(self, seed=31, rounds=120, engines=[ReferenceEngine])


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustMatchesReference(unittest.TestCase):
    def test_random(self):
        run_random(self, seed=37, rounds=120, engines=[ReferenceEngine, RustEngine])


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class EagerHeuristics(unittest.TestCase):
    """大きなモデルでだけ働く速さのための規則（値が変わった範囲が大半を占めれば全体へ広げる、
    同じ段の Metric を並列に計算する）を、小さなモデルでも常に働かせて、結果が変わらないことを確かめる。"""

    engine = staticmethod(lambda: RustEngine(widen_min_rows=0, par_min=0))

    def test_random_rust(self):
        run_random(self, seed=43, rounds=80, engines=[ReferenceEngine, self.engine])

    def test_random_rust_alone(self):
        run_random(self, seed=47, rounds=80, engines=[self.engine])


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class StreamedAggregation(unittest.TestCase):
    """集計先が少ないときに集計元を写さずに読みながら集計する経路（Metric を使った BY は対応表を
    引きながら）を、小さなモデルでも常に使い、並列の足し込みも働かせて、結果が変わらないことを確かめる。"""

    config = {"stream_always": True, "par_min": 0}

    def test_random_rust(self):
        run_random(self, seed=53, rounds=80, engines=[ReferenceEngine, lambda: RustEngine(**self.config)])

    def test_other_suites(self):  # Metric を使った BY（異動、損益計画）を含むテストも、この設定で回す
        from .test_dynamic_hierarchy import RustMatchesReference as Dynamic
        from .test_expr_coverage import EveryNodeEverywhere
        from .test_fpa import RustMatchesReference as Fpa
        original = RustEngine.__init__

        def configured(engine, **config):  # ほかのテストが作る RustEngine にも、この設定を渡す
            original(engine, **{**self.config, **config})
        with mock.patch.object(RustEngine, "__init__", configured):
            for case, name in [(EveryNodeEverywhere, "test_rust_handles_every_node_and_agrees"),
                               (Dynamic, "test_random_edits"), (Fpa, "test_random_edits")]:
                with self.subTest(case=case.__module__, test=name):
                    getattr(case(name), name)()


if __name__ == "__main__":
    unittest.main()
