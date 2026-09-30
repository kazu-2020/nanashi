"""式の意味は、Python の参照実装と Rust の両方に、それぞれ複数の場所（構文、型推論、影響範囲、評価、
依存の収集、Rust への変換）で実装している。ノードを 1 種類足したときに、どこかの実装を忘れたまま
テストが通ってしまわないように、すべての種類のノードを含むモデルで、すべての場所を通す。

新しいノードを足したら、ここのモデルにそのノードを使う式も足すこと。
"""
import unittest

from sparse_engine import Model, parse, to_formula
from sparse_engine.engine import ReferenceEngine
from sparse_engine.evaluate import affected, collect_refs, infer
from sparse_engine.expr import Expr, _children

from .test_incremental import same

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None

MONTHS = ["Jan", "Feb", "Mar"]


def model(engine=None) -> Model:
    m = Model(engine=engine) if engine is not None else Model()
    m.add_dimension("Product", ["A", "B", "C"])
    m.add_dimension("Category", ["X", "Y"])
    m.add_dimension("Month", MONTHS, ordered=True)
    m.add_dimension("Employee", ["e1", "e2", "e3"])
    m.add_dimension("Department", ["Sales", "Eng"])
    m.add_property("Product", "Category", "Category", {"A": "X", "B": "X", "C": "Y"})
    m.add_input("Price", ["Product"], {("A",): 10, ("B",): 20})
    m.add_input("Volume", ["Product", "Month"], {("A", "Jan"): 3, ("B", "Feb"): 2, ("C", "Mar"): 4})
    m.add_input("Flag", ["Product"], {("A",): True, ("B",): False}, kind="boolean")
    m.add_input("Rate", ["Category"], {("X",): 0.5})
    m.add_input("Salary", ["Employee"], {("e1",): 100, ("e2",): 200})
    m.add_input("DeptOf", ["Employee", "Month"], {(e, t): d for e, d in [("e1", "Sales"), ("e2", "Eng")]
                                                  for t in MONTHS}, kind="member:Department")
    m.add_input("Cutoff", [], {(): "Feb"}, kind="member:Month")
    f = m.add_formula
    f("Revenue", ["Product", "Month"], "Volume * Price")                                  # BinOp Ref
    f("ByCat", ["Category", "Month"], "Revenue[BY SUM: Product.Category]")                # By（集約）
    f("Total", ["Month"], "ByCat[REMOVE SUM: Category]")                                  # Remove
    f("Adj", ["Product", "Month"], "IF(Revenue > 10, Revenue * Rate[BY: Product.Category], IFBLANK(Volume, 0) * -1)")  # If By（引き下ろし）IfBlank Const
    f("Picked", ["Product", "Month"], "Revenue[FILTER: Flag]")                            # Filter
    f("OnPrice", ["Product", "Month"], "Price[ON: Volume] + Volume")                      # On
    f("Spread", ["Product", "Month"], "Price[EXPAND: Month] - Revenue[SELECT: Month - 1]")  # Expand Shift
    f("Missing", ["Product", "Month"], "ISBLANK(Volume) AND NOT Flag[EXPAND: Month]", kind="boolean")  # IsBlank Not
    f("Actual", ["Month"], 'Month <= Cutoff', kind="boolean")                             # DimRef（軸の名前）
    f("MarchOnly", ["Product"], 'Revenue[SELECT: Month."Mar"]')                           # Select
    f("IsMar", ["Month"], 'Month = Month."Mar"', kind="boolean")                          # Member（メンバーの定数）
    f("Cash", ["Month"], "PREVIOUS(Month) + Total", overridable=True)                     # Shift（scan）Coalesce（上書き）
    f("DeptCost", ["Department", "Month"], "Salary[EXPAND: Month][BY SUM: Employee.DeptOf]")  # AsAxis（Metric を使った BY）
    m.set_cell("Cash", 999, Month="Jan")
    return m


def node_types(e: Expr) -> set:
    return {type(e)} | {t for _, c in _children(e) for t in node_types(c)}


class EveryNodeEverywhere(unittest.TestCase):
    def test_model_uses_every_kind_of_node(self):
        m = model()
        m.recalc()
        used = set()
        for x in m.metrics.values():
            if x.formula is not None:
                used |= node_types(x.formula)
        missing = {c.__name__ for c in Expr.__subclasses__()} - {t.__name__ for t in used}
        self.assertEqual(missing, set(), "このテストのモデルに、これらのノードを使う式を足すこと")

    def test_every_place_handles_every_node(self):
        m = model()
        m.recalc()
        for x in m.metrics.values():
            if x.written is None:
                continue
            text = to_formula(x.written)                     # 文字列への変換
            self.assertEqual(to_formula(parse(text, self_name=x.name)), text)  # 構文の解析（往復で安定）
            infer(x.formula, m, [])                          # 型推論
            list(collect_refs(x.formula, m))                 # 依存の収集
            changed = {n: {} for n, y in m.metrics.items() if y.formula is None}
            affected(x.formula, m, changed, {"Month": frozenset(["Apr"])}, {"Month": "Feb"})  # 影響範囲
            m.engine.evaluate(x.formula, m, {})               # 評価（参照実装）

    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_rust_handles_every_node_and_agrees(self):
        ref, rust = model(ReferenceEngine()), model(RustEngine())
        for name in ref.metrics:
            a, b = ref.value(name).cells, rust.value(name).cells
            self.assertTrue(same(a, b), f"{name}\n参照: {a}\nRust: {b}")
        ref.add_member("Month", "Apr")
        rust.add_member("Month", "Apr")
        for m in (ref, rust):
            m.set_cell("Volume", 7, Product="A", Month="Apr")
            m.set_cell("DeptOf", "Eng", Employee="e1", Month="Apr")
            m.remove_member("Month", "Feb")
        for name in ref.metrics:
            a, b = ref.value(name).cells, rust.value(name).cells
            self.assertTrue(same(a, b), f"{name}\n参照: {a}\nRust: {b}")


if __name__ == "__main__":
    unittest.main()


BAD_FORMULAS = [  # (Metric の軸, 式) 型検査で失敗するもの。文言が両方の実装で一致すること
    (["Product", "Month"], "Price + Volume"),                       # 暗黙の展開
    (["Product", "Month"], "Volume + Flag[EXPAND: Month]"),         # 種類の違い
    (["Product"], "Flag AND Price"),
    (["Product", "Month"], "IF(Price > 1, Volume, Flag[EXPAND: Month])"),
    (["Product", "Month"], "Volume[FILTER: Rate > 0]"),                   # 対象にない軸
    (["Product", "Month"], "IF(Volume, 1)"),
    (["Product", "Month"], "Volume[EXPAND: Month]"),
    (["Product", "Month"], "Price[EXPAND: Month, Month]"),
    (["Month"], "Volume[BY SUM: Product.Category][REMOVE SUM: Product]"),
    (["Category", "Month"], "Volume[BY SUM: Product.Nope]"),
    (["Category", "Month"], "Rate[BY SUM: Product.Category]"),
    (["Product", "Month"], "Rate[BY MAX: Product.Category]"),
    (["Product", "Month"], "PREVIOUS(Product) + Volume"),
    (["Product", "Month"], "Volume[SELECT: Category.\"X\"]"),
    (["Product", "Month"], 'Volume[SELECT: Month."Nope"]'),
    (["Product", "Month"], 'IF(Month = Month."Nope", 1)'),
    (["Product", "Month"], "Product < Product"),
    (["Product", "Month"], "IFBLANK(Flag, 0)[EXPAND: Month]"),
    (["Product", "Month"], "NOT Volume"),
    (["Product", "Month"], "Flag[REMOVE SUM: Product][EXPAND: Product, Month]"),
    (["Product", "Month"], "Volume = Flag[EXPAND: Month]"),
    (["Department", "Month"], "Salary[BY SUM: Employee.DeptOf]"),
    (["Employee", "Month"], "Volume[BY: Employee.DeptOf]"),
]


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustTypeCheckMatchesPython(unittest.TestCase):
    """Rust の型検査は、型、警告、エラーの文言が Python の参照実装と一致する。"""

    def test_types_and_warnings(self):
        from sparse_engine.evaluate import infer, resolve
        m = model(RustEngine())
        m.recalc()
        for x in m.metrics.values():
            if x.written is None:
                continue
            formula = resolve(x.written, m)
            w: list = []
            want = infer(formula, m, w)
            got, warnings = m.engine.check(formula, m)
            self.assertEqual((got.dims, got.kind, warnings), (want.dims, want.kind, w), x.name)

    def test_error_messages(self):
        from sparse_engine.evaluate import FormulaError as FE, infer, resolve
        for dims, text in BAD_FORMULAS:
            ref, rust = model(ReferenceEngine()), model(RustEngine())
            messages = []
            for m in (ref, rust):
                with self.subTest(formula=text, engine=m.engine.name):
                    with self.assertRaises(FE) as cm:
                        m.add_formula("Bad", dims, text)
                        m.recalc()
                    messages.append(str(cm.exception))
            self.assertEqual(messages[0], messages[1], text)
        # 参照実装の型推論そのものも、同じ式で同じ文言を出す（add_formula の経路と食い違わない）
        m = model(ReferenceEngine())
        for dims, text in BAD_FORMULAS:
            with self.assertRaises(FE):
                infer(resolve(parse(text, self_name="Bad"), m), m, [])


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustAffectedMatchesPython(unittest.TestCase):
    """Rust の影響範囲（伝搬、メンバーの削除、単一の式）は、Python の参照実装と一致する。"""

    def models(self):
        from examples.fpa import build
        yield model(RustEngine())
        yield build(RustEngine(), employees=12, products=6, months=8, seed=3)

    def test_propagate(self):
        for m in self.models():
            m.recalc()
            inputs = [n for n, x in m.metrics.items() if x.formula is None]
            cases = []
            for name in inputs:
                dims = m.metrics[name].dims
                cases.append(({name: {d: frozenset([m.dimensions[d].members[0]]) for d in dims}}, None))
                cases.append(({name: {}}, None))
            cases.append(({n: {d: frozenset(m.dimensions[d].members[:2]) for d in m.metrics[n].dims} for n in inputs[:3]},
                          {"Month": frozenset(["Zzz"])}))
            for changed, added in cases:
                if added:
                    for d, ms in added.items():
                        for x in ms:
                            if x not in m.dimensions[d]:
                                m.dimensions[d].add_member(x, m._new_id())
                                m._member_added(d)
                with self.subTest(changed=list(changed), added=added):
                    self.assertEqual(m._propagate(changed, added), m._propagate_py(changed, added))

    def test_removal_regions(self):
        for m in self.models():
            m.recalc()
            for dim in m.dimensions:
                member = m.dimensions[dim].members[1]

                def has_cells(name, dim=dim, member=member):
                    point = {dim: frozenset([member])}
                    return dim in m.metrics[name].dims and m.engine.size(m.engine.filter(m._values[name], point, m)) > 0
                with self.subTest(dim=dim, member=member):
                    self.assertEqual(m._removal_regions(dim, member, has_cells), m._removal_regions_py(dim, member, has_cells))

    def test_single_formula(self):
        from sparse_engine.evaluate import affected
        for m in self.models():
            m.recalc()
            inputs = [n for n, x in m.metrics.items() if x.formula is None]
            regions = {n: {d: frozenset(m.dimensions[d].members[:1]) for d in m.metrics[n].dims} for n in inputs}
            for x in m.metrics.values():
                if x.formula is not None:
                    with self.subTest(metric=x.name):
                        self.assertEqual(m.engine.affected(x.formula, m, regions), affected(x.formula, m, regions))


CYCLIC = [  # (名前, 軸, 式) の列。計画を作るときに失敗する循環。文言が両方の実装で一致すること
    [("A", ["Product", "Month"], "B + 1"), ("B", ["Product", "Month"], "A * 2")],
    [("A", ["Product", "Month"], "PREVIOUS(Month) + B"), ("B", ["Product", "Month"], "A[SELECT: Month + 1]")],
    [("A", ["Product", "Month"], "PREVIOUS(Month) + B[EXPAND: Month]"), ("B", ["Product"], "A[REMOVE SUM: Month]")],
    [("A", ["Product", "Month"], "PREVIOUS(Month) + B"), ("B", ["Product", "Month"], "A + C"), ("C", ["Product", "Month"], "B * 2")],
    [("A", ["Product", "Month"], "PREVIOUS(Month) + B[EXPAND: Month]"), ("B", ["Product"], "A[SELECT: Month.\"Jan\"]")],
]


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustPlanMatchesPython(unittest.TestCase):
    """Rust の計算計画（段階の順、scan、段）は Python の参照実装と一致する。"""

    def models(self):
        from examples.fpa import build
        from .test_incremental import model as incremental
        yield model(RustEngine())
        yield build(RustEngine(), employees=12, products=6, months=8, seed=3)
        m = incremental()
        fresh = Model(engine=RustEngine())
        fresh.dimensions = m.dimensions
        for name, x in m.metrics.items():
            if x.formula is None:
                fresh.add_input(name, x.dims, m.value(name).cells, kind=x.kind)
            else:
                fresh.add_formula(name, x.dims, x.formula, kind=x.kind)
        yield fresh

    def test_steps_and_levels(self):
        for m in self.models():
            m.recalc()
            formulas = {n: x.formula for n, x in m.metrics.items()}
            rust_plan, _, rust_levels = m._make_plan(formulas)
            saved, m.engine.plan = m.engine.plan, None  # 参照実装の経路を通す
            try:
                py_plan, _, py_levels = m._make_plan(formulas)
            finally:
                m.engine.plan = saved
            self.assertEqual([(s.names, s.scan_dim) for s in rust_plan], [(s.names, s.scan_dim) for s in py_plan])
            self.assertEqual([[s.names for s in level] for level in rust_levels],
                             [[s.names for s in level] for level in py_levels])

    def test_cycle_messages(self):
        from sparse_engine.evaluate import FormulaError as FE
        for defs in CYCLIC:
            messages = []
            for engine in (ReferenceEngine, RustEngine):
                m = model(engine())
                with self.subTest(defs=[d[0] + "=" + d[2] for d in defs], engine=m.engine.name):
                    with self.assertRaisesRegex(FE, "循環参照") as cm:
                        for name, dims, text in defs:
                            m.add_formula(name, dims, text)
                        m.recalc()
                    messages.append(str(cm.exception))
            self.assertEqual(messages[0], messages[1], defs)
