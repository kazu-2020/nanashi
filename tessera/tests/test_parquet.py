"""入力 Metric の値を Parquet のバイト列で出し入れする口（to_parquet / from_parquet）。"""
import random
import unittest

from sparse_engine import Model
from sparse_engine.engine import ReferenceEngine

try:
    import nanashi_core
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    nanashi_core = RustEngine = None


def model(engine, like: Model | None = None) -> Model:
    """A model with 4 input Metrics. With like, the dimensions get the ids of that model (a Parquet file names
    its columns by dimension id, so a reader must have the same ids)."""
    rng = random.Random(3)
    m = Model(engine=engine)
    dim_id = lambda name: None if like is None else like.dimension_id(name)
    m.add_dimension("Employee", [f"e{i}" for i in range(40)], id=dim_id("Employee"))
    m.add_dimension("Department", ["営業", "開発", "管理"], id=dim_id("Department"))
    m.add_dimension("Month", [f"m{i:02d}" for i in range(1, 13)], ordered=True, id=dim_id("Month"))
    emps, months = m.dimension("Employee").members, m.dimension("Month").members
    m.add_input("Salary", ["Employee", "Month"],
                {(e, t): round(rng.uniform(-100, 900), 3) for e in emps for t in months if rng.random() < 0.7},
                partition="Month")
    m.add_input("Active", ["Employee"], {(e,): rng.random() < 0.5 for e in emps if rng.random() < 0.8},
                kind="boolean")
    m.add_input("DeptOf", ["Employee", "Month"],
                {(e, t): rng.choice(["営業", "開発", "管理"]) for e in emps for t in months if rng.random() < 0.5},
                kind="member:Department")
    m.add_input("Empty", ["Department"], {})
    m.recalc()
    return m


def cells(m: Model, name: str) -> dict:
    return shown(m, m._values[m.metric(name).id])


def shown(m: Model, store) -> dict:
    """The cells of a store with member names (the engine holds member ids)."""
    cube = m.engine.to_cube(store, m)
    return {tuple(m.dimension(d).member_of(x) for d, x in zip(cube.dims, k)): v for k, v in cube.cells.items()}


@unittest.skipIf(nanashi_core is None, "nanashi_core が必要")
class ParquetRoundTrip(unittest.TestCase):
    def engines(self):
        return [ReferenceEngine, RustEngine]

    def test_round_trip_within_and_across_engines(self):
        for writer in self.engines():
            src = model(writer())
            for reader in self.engines():
                dst = model(reader(), like=src)
                for name in ("Salary", "Active", "DeptOf", "Empty"):
                    m = src.metric(name)
                    dims = tuple(src.dimension(d).name for d in m.dims)  # names: dst has other member ids
                    kind = src._name_of(m.kind)
                    partition = None if m.partition is None else src.dimension(m.partition).name
                    with self.subTest(writer=writer.name, reader=reader.name, metric=name):
                        data = src.engine.to_parquet(src._values[src.metric(name).id], dims, kind, src, {"metric": str(m.id)})
                        self.assertIsInstance(data, bytes)
                        store = dst.engine.from_parquet(data, dims, kind, dst, partition)
                        self.assertEqual(shown(dst, store), cells(src, name))
                        self.assertIn(("metric", str(m.id)), nanashi_core.parquet_metadata(data))

    def test_values_keep_their_types(self):
        for engine in self.engines():
            m = model(engine())
            with self.subTest(engine=engine.name):
                meta = m.metric("Active")
                data = m.engine.to_parquet(m._values[m.metric("Active").id], meta.dims, meta.kind, m, {})
                store = m.engine.from_parquet(data, meta.dims, meta.kind, m)
                self.assertTrue(all(isinstance(v, bool) for v in m.engine.to_cube(store, m).cells.values()))

    def test_columns_follow_dimension_ids(self):
        """軸の名前を変えても、同じバイト列を読める。"""
        for engine in self.engines():
            m = model(engine())
            with self.subTest(engine=engine.name):
                data = m.engine.to_parquet(m._values[m.metric("Salary").id], ("Employee", "Month"), "number", m, {})
                before = cells(m, "Salary")
                m.rename_member("Employee", "e3", "Eve")
                store = m.engine.from_parquet(data, ("Employee", "Month"), "number", m)
                after = {tuple("Eve" if x == "e3" else x for x in k): v for k, v in before.items()}
                self.assertEqual(shown(m, store), after)

    def test_rejects_mismatch(self):
        for engine in self.engines():
            m = model(engine())
            data = m.engine.to_parquet(m._values[m.metric("Salary").id], ("Employee", "Month"), "number", m, {})
            with self.subTest(engine=engine.name, case="値の種類"):
                with self.assertRaisesRegex(ValueError, "列が合わない"):
                    m.engine.from_parquet(data, ("Employee", "Month"), "boolean", m)
            with self.subTest(engine=engine.name, case="軸"):
                with self.assertRaisesRegex(ValueError, "列が合わない"):
                    m.engine.from_parquet(data, ("Department", "Month"), "number", m)
            with self.subTest(engine=engine.name, case="軸の並びが違う"):  # 列は名前で選ぶ
                store = m.engine.from_parquet(data, ("Month", "Employee"), "number", m)
                want = {(t, e): v for (e, t), v in cells(m, "Salary").items()}
                self.assertEqual(shown(m, store), want)
            with self.subTest(engine=engine.name, case="壊れたバイト列"):
                with self.assertRaisesRegex(ValueError, "Parquet"):
                    m.engine.from_parquet(data[:-20], ("Employee", "Month"), "number", m)
            with self.subTest(engine=engine.name, case="メンバー番号が軸の外"):
                m.remove_member("Month", "m12")
                with self.assertRaisesRegex(ValueError, "メンバー番号 11"):
                    m.engine.from_parquet(data, ("Employee", "Month"), "number", m)


@unittest.skipIf(nanashi_core is None, "nanashi_core が必要")
class CellBlocks(unittest.TestCase):
    """記録の入力セルの変更の塊（nanashi_core.CellBlock）。"""

    def test_behaves_like_rows_and_round_trips(self):
        for rows in ([[["a", "x"], 1.5, None], [["b", "y"], None, -2.0], [["b", "z"], 0.0, 1e300]],  # number
                     [[["a"], True, False], [["b"], None, True]],  # boolean
                     [[["a"], "p", "q"], [["b"], None, "p"]]):  # a member-type value is a member id
            with self.subTest(rows=rows):
                b = nanashi_core.CellBlock.from_rows(rows)
                self.assertEqual((len(b), b.width, b.rows(), list(b)), (len(rows), len(rows[0][0]), rows, rows))
                self.assertEqual(b, rows)
                back = nanashi_core.CellBlock.from_parquet(b.to_parquet([f"d{j}" for j in range(len(rows[0][0]))], []))
                self.assertEqual(back, b)
                self.assertEqual([type(v) for _, _, v in back.rows()], [type(v) for _, _, v in rows])

    def test_rejects_other_parquet(self):
        m = model(RustEngine())
        data = m.engine.to_parquet(m._values[m.metric("Salary").id], ("Employee", "Month"), "number", m, {})
        with self.assertRaisesRegex(ValueError, "変更の形でない"):
            nanashi_core.CellBlock.from_parquet(data)

    def test_copy_text(self):
        b = nanashi_core.CellBlock.from_rows([[["a", "x"], 1.5, None], [["b", "y"], None, 1e300],
                                              [['q"t', "z"], float("-inf"), 0.1]])
        self.assertEqual(b.copy_text("m\t1", 5, "metric-9"),  # the ids are quoted in the array, so any character is fine
                         b'm\\t1\t5\tmetric-9\t{"a","x"}\t1.5\t\\N\t\\N\t\\N\n'
                         b'm\\t1\t5\tmetric-9\t{"b","y"}\t\\N\t1e300\t\\N\t\\N\n'
                         b'm\\t1\t5\tmetric-9\t{"q\\\\"t","z"}\t-Infinity\t0.1\t\\N\t\\N\n')
        b = nanashi_core.CellBlock.from_rows([[["a"], "p", None], [["b"], None, "q"]])
        self.assertEqual(b.copy_text("m", 1, "x"),  # a member-type value goes to old_member / new_member
                         b'm\t1\tx\t{"a"}\t\\N\t\\N\tp\t\\N\n'
                         b'm\t1\tx\t{"b"}\t\\N\t\\N\t\\N\tq\n')

    def test_overlaps(self):
        a = nanashi_core.CellBlock.from_rows([[[f"k{i}", "t0"], 1.0, 2.0] for i in range(100)])
        b = nanashi_core.CellBlock.from_rows([[[f"k{i}", "t1"], 1.0, 2.0] for i in range(100)] + [[["k5", "t0"], None, 1.0]])
        c = nanashi_core.CellBlock.from_rows([[[f"k{i}", "t2"], 1.0, 2.0] for i in range(10)])
        self.assertTrue(a.overlaps(b) and b.overlaps(a))
        self.assertFalse(a.overlaps(c))
        self.assertTrue(a.contains_any([["k1000", "t0"], ["k7", "t0"]]))
        self.assertFalse(a.contains_any([["k7", "t3"], ["k1000", "t0"]]))


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class ApplyBlock(unittest.TestCase):
    """記録の再生で、変更の塊をまとめて書き込む（Rust）。1 セルずつ書いた結果と同じになる。"""

    def model(self):
        m = Model(engine=RustEngine())
        m.add_dimension("K", [f"k{i}" for i in range(300)])
        m.add_dimension("T", [f"t{i}" for i in range(12)], ordered=True)
        m.add_dimension("D", ["a", "b", "c"])
        rng = random.Random(5)
        m.add_input("V", ["K", "T"], {(f"k{i}", f"t{j}"): float(i + j) for i in range(300) for j in range(12)
                                      if rng.random() < 0.6})
        m.add_input("M", ["K"], {(f"k{i}",): "abc"[i % 3] for i in range(300) if i % 4}, kind="member:D")
        m.recalc()
        return m

    def test_matches_writing_cell_by_cell(self):
        rng = random.Random(9)
        for n in (50, 3000):  # 1024 件未満は差分に入れ、それ以上は本体を作り直す
            for name in ("V", "M"):
                a = self.model()
                b = a.fork()  # the same member ids
                x = a.metric(name)
                dims = [a.dimension(d) for d in x.dims]
                vdim = a.dimension("D") if name == "M" else None
                rows = []
                for _ in range(n):
                    ids = [d.ids[rng.randrange(len(d.ids))] for d in dims]
                    if rng.random() < 0.05:
                        ids[0] = "gone"  # a member that is not in the dimension (a removed member) is skipped
                    new = None if rng.random() < 0.2 else (
                        vdim.ids[rng.randrange(3)] if vdim else round(rng.uniform(-9, 9), 2))
                    rows.append([ids, None, new])
                with self.subTest(n=n, metric=name):
                    block = nanashi_core.CellBlock.from_rows(rows)
                    a._values[a.metric(name).id] = a.engine.apply_block(a._values[a.metric(name).id], block,
                                                                        [d.ids for d in dims],
                                                                        None if vdim is None else vdim.ids)
                    store = b._values[b.metric(name).id]
                    for ids, _, new in rows:  # journal.apply の 1 セルずつの経路と同じ
                        key = tuple(ids)
                        if not all(u in d._by_id for d, u in zip(dims, key)):
                            continue
                        value = new if new is None or vdim is None else float(vdim._by_id[new])
                        store = b.engine.write(store, key, value, b)
                    self.assertEqual(cells(a, name), cells(b, name))


if __name__ == "__main__":
    unittest.main()
