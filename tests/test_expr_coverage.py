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
