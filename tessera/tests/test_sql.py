"""The SQL subset of POST /sql (sparse_engine/sql.py, Core.select)."""
import json
import math
import random
import unittest

from sparse_engine import Model, Named
from sparse_engine.engine import ReferenceEngine
from sparse_engine.sql import SqlError, plan, reference_select

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None


def model(engine) -> Named:
    m = Named(Model(engine=engine))
    m.add_dimension("Version", ["予算", "実績"])
    m.add_dimension("Product", ["A", "B", "C"])
    m.add_dimension("Month", ["Jan", "Feb", "Mar"], ordered=True)
    m.move_member("Product", "C", 0)  # the order is C, A, B; the numbers stay A 0, B 1, C 2
    m.add_input("Sales", ["Product", "Version", "Month"], {
        ("A", "予算", "Jan"): 100, ("A", "実績", "Jan"): 80, ("A", "実績", "Feb"): 130,
        ("B", "実績", "Feb"): 40, ("C", "予算", "Mar"): 10, ("B", "実績", "Mar"): 20})
    m.add_input("Price", ["Product", "Month"], {("A", "Jan"): 2, ("A", "Feb"): 3, ("C", "Mar"): 5})
    m.add_input("Flag", ["Product"], {("A",): True, ("B",): False}, kind="boolean")
    return m


class Behavior:
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        self.m = model(self.engine()).model

    def run_sql(self, text: str, max_rows: int = 100) -> dict:
        return json.loads(self.m.select(plan(self.m, text), "json", max_rows))

    def rows(self, text: str) -> list:
        return self.run_sql(text)["rows"]

    def test_group_by_with_a_filter(self):
        out = self.run_sql("SELECT Month, SUM(Sales) AS s, AVG(Sales) FROM model WHERE Version = '実績' "
                           "GROUP BY Month ORDER BY Month")
        self.assertEqual(out, {"seq": 0, "columns": ["Month", "s", "avg(Sales)"],
                               "rows": [["Jan", 80.0, 80.0], ["Feb", 170.0, 85.0], ["Mar", 20.0, 20.0]]})

    def test_outer_join_of_aggregates_and_the_member_order(self):
        q = "SELECT Product, SUM(Sales), MAX(Price), COUNT(Price) FROM model GROUP BY Product"
        self.assertEqual(self.rows(q), [["A", 310.0, 3.0, 2.0], ["B", 60.0, None, 0.0], ["C", 10.0, 5.0, 1.0]])
        self.assertEqual(self.rows(q + " ORDER BY Product"),
                         [["C", 10.0, 5.0, 1.0], ["A", 310.0, 3.0, 2.0], ["B", 60.0, None, 0.0]])
        self.assertEqual(self.rows(q + " ORDER BY 3 DESC NULLS FIRST"),
                         [["B", 60.0, None, 0.0], ["C", 10.0, 5.0, 1.0], ["A", 310.0, 3.0, 2.0]])

    def test_order_by_and_limit(self):
        self.assertEqual(self.rows("SELECT Product, Month, SUM(Sales) FROM model GROUP BY Product, Month "
                                   "ORDER BY 3 DESC LIMIT 2"), [["A", "Jan", 180.0], ["A", "Feb", 130.0]])
        self.assertEqual(self.rows("SELECT SUM(Sales) FROM model GROUP BY Month LIMIT 0"), [])

    def test_no_group_by_gives_one_row(self):
        self.assertEqual(self.rows("SELECT SUM(Sales), COUNT(Sales) FROM model"), [[310.0 + 60.0 + 10.0, 6.0]])
        self.assertEqual(self.rows("SELECT SUM(Sales), COUNT(Sales) FROM model WHERE Product = 'A' AND Product = 'B'"),
                         [[None, 0.0]])
        self.assertEqual(self.rows("SELECT COUNT(Flag) AS n FROM model"), [[2.0]])

    def test_names_match_exactly_and_keywords_ignore_case(self):
        out = self.run_sql("select Month, sum(Sales) from Model group by 1 order by Month desc")
        self.assertEqual(out["columns"], ["Month", "sum(Sales)"])
        self.assertEqual(out["rows"], [["Mar", 30.0], ["Feb", 170.0], ["Jan", 180.0]])
        with self.assertRaises(SqlError) as cm:
            plan(self.m, "SELECT SUM(sales) FROM model")
        self.assertEqual((cm.exception.code, str(cm.exception)), ("sql_unknown", "軸か Metric の名前 sales がない"))

    def test_errors_have_a_code_and_japanese_text(self):
        cases = [
            ("SELECT Prodct, SUM(Sales) FROM model GROUP BY Prodct", "sql_unknown", "軸か Metric の名前 Prodct がない"),
            ("SELECT SUM(Sales) FROM model HAVING SUM(Sales) > 1", "sql_unsupported", "SQL の HAVING は使えない"),
            ("SELECT SUM(Flag) FROM model", "sql_kind", "Flag は boolean なので SUM で集計できない（COUNT は使える）"),
            ("SELECT Version, SUM(Price) FROM model GROUP BY Version", "sql_missing_dim",
             "Metric Price に軸 Version がない（GROUP BY と WHERE の軸は、すべての集計の Metric に要る）"),
            ("SELECT Month, SUM(Sales) FROM model", "sql_not_grouped", "軸 Month が GROUP BY にない"),
            ("SELECT SUM(Sales) FROM model WHERE Month = 'Dec'", "sql_member", "Month にメンバー 'Dec' がない"),
            ("SELECT Sales FROM model", "sql_not_dim", "Sales は軸ではない（Metric は SUM などの集計で囲む）"),
            ("SELECT SUM(Month) FROM model", "sql_not_metric", "Month は Metric ではない（集計できるのは Metric）"),
            ("SELECT SUM(Sales) FROM sales", "sql_from", "FROM には model だけを書く（sales）"),
            ("SELECT SUM(Sales) FROM model ORDER BY x", "sql_order", "ORDER BY の x が SELECT の列にも GROUP BY の軸にもない"),
            ("SELECT SUM(Sales) AS s, MAX(Sales) AS s FROM model ORDER BY s", "sql_ambiguous",
             "ORDER BY の s に合う SELECT の列が 2 つ以上ある"),
            ("SELECT SUM(Sales) FROM model ORDER BY 2", "sql_position", "位置 2 が SELECT の列（1 個）の範囲にない"),
            ("SELECT SUM(Sales) FROM model GROUP BY 1", "sql_group_position", "GROUP BY の位置 1 は軸の列ではない"),
            ("SELECT Month FROM model GROUP BY Month", "sql_no_aggregate",
             "SELECT に集計（SUM、AVG、MIN、MAX、COUNT）が 1 つ以上要る"),
            ("SELECT 1; SELECT 2", "sql_statements", "SQL は SELECT 文 1 つだけを書く"),
            ("SELECT SUM(Sales) FROM model WHERE " + " AND ".join(["Month = 'Jan'"] * 200), "sql_too_complex",
             "SQL の演算子が 399 個あり、上限 256 個を超える"),
            ("SELECT SUM(Sales) FROM model LIMIT 1 OFFSET 1", "sql_unsupported", "SQL の OFFSET は使えない"),
        ]
        for text, code, message in cases:
            with self.subTest(text), self.assertRaises(SqlError) as cm:
                plan(self.m, text)
            self.assertEqual((cm.exception.code, str(cm.exception)), (code, message))
        with self.assertRaises(SqlError) as cm:
            plan(self.m, "SELEC 1")
        self.assertEqual(str(cm.exception), "SQL の構文が正しくない: Expected: SELECT, VALUES, or a subquery in the query body, found: SELEC at Line: 1, Column: 1")

    def test_too_many_rows(self):
        p = plan(self.m, "SELECT Product, Month, SUM(Sales) FROM model GROUP BY Product, Month")
        with self.assertRaises(SqlError) as cm:
            self.m.select(p, "json", 4)
        self.assertEqual((cm.exception.code, str(cm.exception)), ("sql_too_large", "結果が 5 行になり、上限 4 を超える（LIMIT を付ける）"))
        self.assertEqual(len(json.loads(self.m.select(p, "json", 5))["rows"]), 5)

    def test_the_plan_holds_ids(self):
        p = plan(self.m, "SELECT Month AS m, SUM(Sales) FROM model WHERE Version IN ('予算') GROUP BY Month")
        n = Named(self.m)
        self.assertEqual(p.group, (n.dimension_id("Month"),))
        self.assertEqual(p.aggs, ((n.metric("Sales").id, "sum"),))
        self.assertEqual(p.region, ((n.dimension_id("Version"), (n.member_id("Version", "予算"),)),))
        self.assertEqual(p.columns, (("m", "dim", 0), ("sum(Sales)", "agg", 0)))
        self.assertEqual(p, plan(self.m, "select  Month as m, sum(Sales) from MODEL where Version = '予算' group by 1"))


class ReferenceBehavior(Behavior, unittest.TestCase):
    pass


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustBehavior(Behavior, unittest.TestCase):
    engine = staticmethod(RustEngine)

    def test_arrow_only_on_the_rust_engine(self):
        text = "SELECT SUM(Sales) FROM model"
        self.assertEqual(self.m.select(plan(self.m, text), "arrow", 10)[:4], b"\xff\xff\xff\xff")  # an IPC message
        ref = model(ReferenceEngine()).model
        with self.assertRaises(NotImplementedError):
            ref.select(plan(ref, text), "arrow", 10)


DIMS = {"D0": 5, "D1": 3, "D2": 7, "D3": 2}
METRICS = {"M0": ["D0", "D1"], "M1": ["D0", "D1", "D2"], "M2": ["D1", "D2", "D3"], "M3": ["D0", "D1", "D2", "D3"],
           "B": ["D1", "D2"]}


def random_model(rng: random.Random, engine) -> Named:
    m = Named(Model(engine=engine))
    for d, n in DIMS.items():
        m.add_dimension(d, [f"{d}m{i}" for i in range(n)], ordered=d == "D3")
    m.move_member("D2", "D2m6", 0)
    m.move_member("D0", "D0m1", 4)
    for name, dims in METRICS.items():
        cells = {}
        for key in _keys(dims):
            if rng.random() < 0.45:
                cells[key] = rng.random() < 0.5 if name == "B" else rng.choice([0.0, -0.25, 1.5, 2.0, 1e9, rng.randint(-40, 40) / 4])
        m.add_input(name, dims, cells, kind="boolean" if name == "B" else "number")
    return m


def _keys(dims):
    keys = [()]
    for d in dims:
        keys = [k + (f"{d}m{i}",) for k in keys for i in range(DIMS[d])]
    return keys


def random_sql(rng: random.Random) -> str:
    names = rng.sample(sorted(METRICS), rng.randint(1, 3))
    common = sorted(set.intersection(*(set(METRICS[n]) for n in names)))
    group = rng.sample(common, rng.randint(0, len(common)))
    items = rng.sample(group, rng.randint(0, len(group)))
    for n in names:
        aggs = ["count"] if n == "B" else ["sum", "avg", "min", "max", "count"]
        items += [f"{a.upper()}({n})" for a in rng.sample(aggs, rng.randint(1, min(2, len(aggs))))]
    rng.shuffle(items)
    text = f"SELECT {', '.join(items)} FROM model"
    filters = []
    for d in rng.sample(common, rng.randint(0, min(2, len(common)))):
        ms = rng.sample(range(DIMS[d]), rng.randint(1, DIMS[d]))
        filters.append(f"{d} IN ({', '.join(repr(f'{d}m{i}') for i in ms)})" if len(ms) > 1 else f"{d} = '{d}m{ms[0]}'")
    if filters:
        text += " WHERE " + " AND ".join(filters)
    if group:
        text += " GROUP BY " + ", ".join(group)
    keys = [f"{rng.randint(1, len(items))} {rng.choice(['ASC', 'DESC'])}{rng.choice(['', ' NULLS FIRST', ' NULLS LAST'])}"
            for _ in range(rng.randint(0, 2))]
    if keys:
        text += " ORDER BY " + ", ".join(keys)
    if rng.random() < 0.4:
        text += f" LIMIT {rng.randint(0, 8)}"
    return text


def as_json(x):
    return x if not isinstance(x, float) or math.isfinite(x) else "NaN" if math.isnan(x) else "Infinity" if x > 0 else "-Infinity"


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustAgreesWithTheReference(unittest.TestCase):
    def test_random_queries(self):
        for seed, engine in [(1, RustEngine), (2, lambda: RustEngine(par_min=0)), (3, lambda: RustEngine(dense_always=True))]:
            ref, rust = random_model(random.Random(seed), ReferenceEngine()).model, random_model(random.Random(seed), engine()).model
            rng = random.Random(seed)
            nonempty = 0
            for _ in range(150):
                text = random_sql(rng)
                want = [[as_json(x) for x in r] for r in reference_select(ref, plan(ref, text))]
                got = json.loads(rust.select(plan(rust, text), "json", 10_000))["rows"]
                self.assertEqual(got, want, text)
                nonempty += len(want) > 1
            self.assertGreater(nonempty, 50, "most queries give several rows")


if __name__ == "__main__":
    unittest.main()
