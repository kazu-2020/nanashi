import unittest

from sparse_engine import FormulaError, Model, Named, ParseError, if_, parse, ref, to_formula

MONTHS = ["Jan", "Feb", "Mar", "Apr"]


def model() -> Model:
    m = Named(Model())
    m.add_dimension("Product", ["A", "B", "C"])
    m.add_dimension("Month", MONTHS, ordered=True)
    m.add_dimension("Employee", ["e1", "e2", "e3"])
    m.add_dimension("Department", ["Sales", "Eng"])
    m.add_property("Employee", "Department", "Department",
                   {"e1": "Sales", "e2": "Sales", "e3": "Eng"})
    m.add_input("X", ["Product"], {("A",): 1, ("B",): 5})
    m.add_input("Y", ["Product"], {("B",): 5, ("C",): 2})
    m.add_input("Z", ["Product", "Month"], {("A", "Jan"): 3, ("B", "Feb"): 7, ("B", "Mar"): 1})
    m.add_input("Salary", ["Employee"], {("e1",): 100, ("e2",): 200, ("e3",): 300})
    m.add_input("Rate", ["Department"], {("Sales",): 0.1})
    m.add_input("P", ["Product"], {("A",): True, ("B",): False}, kind="boolean")
    return m


class SameResultAsDsl(unittest.TestCase):
    """文字列の式と Python DSL の式が同じ結果になる。"""

    CASES = [
        (["Product"], "X + Y * 2", ref("X") + ref("Y") * 2, "number"),
        (["Product"], "(X + Y) / 2", (ref("X") + ref("Y")) / 2, "number"),
        (["Product"], "-X", -ref("X"), "number"),
        (["Product"], "X * -1", ref("X") * -1, "number"),
        (["Department"], "Salary[BY SUM: Employee.Department]",
         ref("Salary").by("Employee.Department", "sum"), "number"),
        (["Department"], "Salary[by max: Employee.Department]",
         ref("Salary").by("Employee.Department", "max"), "number"),
        (["Employee"], "Salary * Rate[BY: Employee.Department]",
         ref("Salary") * ref("Rate").by("Employee.Department"), "number"),
        ([], "Z[REMOVE SUM: Product, Month]", ref("Z").remove("Product").remove("Month"), "number"),
        (["Product"], "Z[REMOVE: Month]", ref("Z").remove("Month"), "number"),
        (["Product", "Month"], "Z[SELECT: Month - 1]", ref("Z").prev("Month"), "number"),
        (["Product", "Month"], "Z[SELECT: Month + 1]", ref("Z").prev("Month", -1), "number"),
        (["Product"], "X[FILTER: Y > 3]", ref("X").filter(ref("Y") > 3), "number"),
        (["Product", "Month"], "X[EXPAND: Month] + Z", ref("X").expand("Month") + ref("Z"), "number"),
        (["Product", "Month"], "X[ON: Z] + Z", ref("X").on(ref("Z")) + ref("Z"), "number"),
        (["Product"], "IF(X > 2, 100, 0)", if_(ref("X") > 2, 100, 0), "number"),
        (["Product"], "if(X >= 1, X)", if_(ref("X") >= 1, ref("X")), "number"),
        (["Product"], "IFBLANK(X, -1)", ref("X").ifblank(-1), "number"),
        (["Product"], "ISBLANK(X)", ref("X").isblank(), "boolean"),
        (["Product"], "NOT P OR X = Y", ~ref("P") | ref("X").eq(ref("Y")), "boolean"),
        (["Product"], "P and X <> 1", ref("P") & ref("X").ne(1), "boolean"),
        (["Product"], "IFBLANK(P, FALSE)", ref("P").ifblank(False), "boolean"),
    ]

    def test_cases(self):
        for dims, text, dsl, kind in self.CASES:
            with self.subTest(text):
                m = model()
                m.add_formula("FromText", dims, text, kind=kind)
                m.add_formula("FromDsl", dims, dsl, kind=kind)
                self.assertEqual(m.value("FromText").cells, m.value("FromDsl").cells)
                self.assertEqual(to_formula(parse(text)), to_formula(dsl))


class Precedence(unittest.TestCase):
    CASES = {
        "A + B * C": "A + B * C",
        "(A + B) * C": "(A + B) * C",
        "A - B - C": "A - B - C",
        "A - (B - C)": "A - (B - C)",
        "A / (B * C)": "A / (B * C)",
        "NOT P AND Q OR R": "NOT P AND Q OR R",
        "NOT (P AND Q)": "NOT (P AND Q)",
        "P OR Q AND R": "P OR Q AND R",
        "(P OR Q) AND R": "(P OR Q) AND R",
        "A + 1 > B * 2": "A + 1 > B * 2",
        "-A[REMOVE: X]": "-A[REMOVE SUM: X]",
        "(A + B)[BY: E.D]": "(A + B)[BY: E.D]",
        "A[BY SUM: E.D, M.Q]": "A[BY SUM: E.D][BY SUM: M.Q]",
        "  if ( a>1 ,b,  c )  ": "IF(a > 1, b, c)",
        "1.50 + 2e3": "1.5 + 2000",
    }

    def test_normalized_form(self):
        for text, expected in self.CASES.items():
            with self.subTest(text):
                self.assertEqual(to_formula(parse(text)), expected)

    def test_round_trip_is_stable(self):
        for text in [*self.CASES, *(c[1] for c in SameResultAsDsl.CASES)]:
            with self.subTest(text):
                once = to_formula(parse(text))
                self.assertEqual(to_formula(parse(once)), once)


class Names(unittest.TestCase):
    def test_quoted_name(self):
        self.assertEqual(parse("'Unit Price' * 2").left.name, "Unit Price")

    def test_escaped_quote(self):
        e = parse("'Owner''s Equity'")
        self.assertEqual(e.name, "Owner's Equity")
        self.assertEqual(to_formula(e), "'Owner''s Equity'")

    def test_japanese_names(self):
        m = Named(Model())
        m.add_dimension("商品", ["りんご", "みかん"])
        m.add_input("売上", ["商品"], {("りんご",): 100, ("みかん",): 80})
        m.add_input("原価", ["商品"], {("りんご",): 60})
        m.add_formula("粗利", ["商品"], "売上 - 原価")
        self.assertEqual(m.value("粗利").cells, {("りんご",): 40, ("みかん",): 80})

    def test_keywords_as_names_must_be_quoted(self):
        self.assertEqual(to_formula(parse("'And' + 'IF'")), "'And' + 'IF'")
        with self.assertRaises(ParseError):
            parse("And + 1")

    def test_function_name_without_paren_is_a_metric(self):
        self.assertEqual(parse("If + 1").left.name, "If")


class Errors(unittest.TestCase):
    CASES = {
        "A +": (3, "値が必要"),
        "A[BY SUM Employee.Department]": (9, "':' が必要"),
        "1 < X < 2": (6, "連結できない"),
        "PREVIOUS(Month)": (0, "PREVIOUS"),
        "FOO(1)": (0, "未知の関数"),
        "A[BY MEDIAN: E.D]": (5, "集計関数"),
        "A[TOP: 3]": (2, "BY, EXPAND"),
        "'Unit Price * 2": (0, "閉じていない"),
        "A # B": (2, "使えない文字"),
        "(A + B": (6, "')' が必要"),
        "A B": (2, "余分"),
        "A[SELECT: Month]": (15, "ずらし量"),
        "A[SELECT: Month - 1.5]": (18, "整数"),
        "IFBLANK(A, B)": (11, "定数"),
        "IF(A > 1)": (8, "',' が必要"),
    }

    def test_messages_and_positions(self):
        for text, (pos, fragment) in self.CASES.items():
            with self.subTest(text):
                with self.assertRaises(ParseError) as cm:
                    parse(text)
                self.assertEqual(cm.exception.pos, pos)
                self.assertIn(fragment, str(cm.exception))

    def test_parse_error_is_a_formula_error(self):
        self.assertTrue(issubclass(ParseError, FormulaError))

    def test_caret_accounts_for_wide_characters(self):
        with self.assertRaises(ParseError) as cm:
            parse("売上 + + 1")
        line, caret = str(cm.exception).splitlines()[1:]
        self.assertEqual(caret.index("^"), 2 + 7)  # 2 はインデント。「売上 + 」は幅 4+1+1+1

    def test_syntax_error_is_raised_on_add(self):
        with self.assertRaises(ParseError):
            model().add_formula("R", ["Product"], "X +")


class Limits(unittest.TestCase):
    """The parser rejects text that would later exhaust the stack (bind, recalc, to_formula recurse over the
    tree) or overflow an integer in the Rust engine. A text in the limits parses, calculates and round-trips."""

    def test_deep_nesting_is_a_parse_error(self):
        for text in ["(" * 5000 + "X" + ")" * 5000, "-" * 5000 + "X", "NOT " * 5000 + "P",
                     "IF(P, 1, " * 5000 + "0" + ")" * 5000, "X" + "[FILTER: X" * 5000 + "]" * 5000]:
            with self.subTest(text[:16]):
                with self.assertRaisesRegex(ParseError, "深すぎる"):
                    parse(text)

    def test_long_chain_is_a_parse_error(self):
        for text in [" + ".join(["X"] * 201), " AND ".join(["P"] * 201), "X" + "[REMOVE: Month]" * 200]:
            with self.subTest(text[:16]):
                with self.assertRaisesRegex(ParseError, "長すぎる"):
                    parse(text)

    def test_text_in_the_limits_works_end_to_end(self):
        chain = " + ".join(["X"] * 200)
        nested = "(" * 50 + "X" + ")" * 50
        m = model()
        m.add_formula("Chain", ["Product"], chain)
        m.add_formula("Nested", ["Product"], nested)
        self.assertEqual(m.get("Chain", Product="A"), 200)
        self.assertEqual(m.get("Nested", Product="A"), 1)
        self.assertEqual(to_formula(parse(chain)), chain)

    def test_integer_bound(self):
        with self.assertRaisesRegex(ParseError, "以下"):
            parse("X[SELECT: Month - 2147483648]")
        with self.assertRaisesRegex(ParseError, "以下"):
            parse("PREVIOUS(Month, 99999999999999999999)", self_name="S")
        m = model()
        m.add_formula("R", ["Product", "Month"], "Z[SELECT: Month - 2147483647]")
        self.assertIsNone(m.get("R", Product="A", Month="Jan"))

    def test_infinite_number_is_a_parse_error(self):
        with self.assertRaisesRegex(ParseError, "大きすぎる"):
            parse("X * 1e400")


class ModelWithText(unittest.TestCase):
    def test_previous_refers_to_self(self):
        m = model()
        m.add_input("In", ["Product", "Month"], {("A", "Jan"): 10, ("A", "Mar"): 5})
        m.add_input("Out", ["Product", "Month"], {("A", "Feb"): 3})
        m.add_formula("Stock", ["Product", "Month"], "PREVIOUS(Month) + In - Out")
        s = m.value("Stock")
        self.assertEqual([s.get(Product="A", Month=t) for t in MONTHS], [10, 7, 12, 12])

    def test_reorder_policy(self):
        m = model()
        m.add_input("In", ["Month"], {("Jan",): 10})
        m.add_input("Demand", ["Month"], {(t,): 4 for t in MONTHS})
        m.add_input("Stock", ["Month"])  # Order and Stock refer to each other: an input first
        m.add_formula("Order", ["Month"], "IF(Stock[SELECT: Month - 1] < 5, 10, 0)")
        m.add_formula("Stock", ["Month"], "PREVIOUS(Month) + In + Order - Demand", id=m.metric("Stock").id)
        self.assertEqual([m.get("Stock", Month=t) for t in MONTHS], [6, 2, 8, 4])

    def test_type_errors_still_come_from_checker(self):
        m = model()
        m.add_formula("R", ["Product", "Month"], "X + Z")
        with self.assertRaisesRegex(FormulaError, r"\[EXPAND: Month\]"):
            m.recalc()


if __name__ == "__main__":
    unittest.main()
