"""Rust エンジンの結果が参照実装と一致する。"""
import random
import unittest

from sparse_engine import Model, to_formula
from sparse_engine.engine import ReferenceEngine

from .test_incremental import model as build, same

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None


def build_with(engine) -> Model:
    m = build()
    fresh = Model(engine=engine)
    fresh.dimensions, fresh._dim_ids = m.dimensions, m._dim_ids
    formulas = [x for x in m.metrics.values() if x.formula is not None]
    for x in m.metrics.values():
        if x.formula is None:
            fresh.add_input(x.name, x.dims, m.value(x.id).cells, kind=x.kind)
    for x in formulas:  # an empty input first: the formulas can refer to each other (a scan)
        fresh.add_input(x.name, x.dims, kind=x.kind)
    for x in formulas:
        fresh.add_formula(x.name, x.dims, to_formula(x.written, m), kind=x.kind, id=fresh.metric(x.name).id)
    return fresh


class MatchesReference:
    """other() で作ったエンジンの結果が、ランダムな編集のあとも参照実装と一致する。"""

    def other(self):
        raise NotImplementedError

    def test_random_edits(self):
        rng = random.Random(7)
        ref, pol = build_with(ReferenceEngine()), build_with(self.other())
        inputs = [x.name for x in ref.metrics.values() if x.formula is None]
        for round_ in range(150):
            for _ in range(rng.randint(1, 3)):
                name = rng.choice(inputs)
                meta = ref.metric(name)
                coords = {ref.dimension(d).name: rng.choice(ref.dimension(d).members) for d in meta.dims}
                if rng.random() < 0.3:
                    value = None
                elif meta.kind == "boolean":
                    value = rng.random() < 0.5
                else:
                    value = float(rng.randint(-5, 60))
                ref.set_cell(name, value, **coords)
                pol.set_cell(name, value, **coords)
            for name in ref._metric_ids:
                with self.subTest(round=round_, metric=name):
                    a, b = ref.value(name).cells, pol.value(name).cells
                    self.assertTrue(same(a, b), f"{name}\n参照: {a}\n比較先: {b}")


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustMatchesReference(MatchesReference, unittest.TestCase):
    def other(self):
        return RustEngine()


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class WriteMany(unittest.TestCase):
    """write_many と columns が、参照実装と同じセルを書き、読む（少量は差分に入れ、大量は本体を作り直す）。"""

    def test_matches_reference(self):
        rng = random.Random(3)
        sizes = {"A": 40, "B": 30, "C": 5}
        cells = {tuple(f"{d.lower()}{rng.randrange(n)}" for d, n in sizes.items()): 1.0 for _ in range(300)}
        models = []
        for engine in (ReferenceEngine(), RustEngine()):
            m = Model(engine=engine)
            for d, n in sizes.items():
                m.add_dimension(d, [f"{d.lower()}{i}" for i in range(n)])
            m.add_input("X", list(sizes), cells)
            models.append(m)
        for count in (10, 5_000, 0, 50, 20_000):
            cols = [[rng.randrange(n) for _ in range(count)] for n in sizes.values()]
            values = [None if rng.random() < 0.2 else float(rng.randint(-9, 9)) for _ in range(count)]
            for m in models:  # 同じセルが何度も出るので、後のものが勝つことも確かめる
                m._values[m.metric("X").id] = m.engine.write_many(m._values[m.metric("X").id], cols, values, m)
            ref_m, rs = models
            with self.subTest(count=count):
                self.assertEqual(ref_m.value("X").cells, rs.value("X").cells)  # with member names: the ids differ
                read = []
                for m in models:
                    ids = lambda d, xs: frozenset(m.member_id(d, x) for x in xs)
                    region = {m.dimension_id("A"): ids("A", ["a1", "a7", "a30"]), m.dimension_id("C"): ids("C", ["c2", "c4"])}
                    cols, values = m.engine.columns(m._values[m.metric("X").id], region, m)
                    read.append(sorted(zip(*cols, values)))
                self.assertEqual(read[0], read[1])
                self.assertTrue(read[0])

    def test_rejects_bad_columns(self):
        m = Model(engine=RustEngine())
        m.add_dimension("A", ["a0", "a1"])
        m.add_input("X", ["A"], {})
        with self.assertRaisesRegex(ValueError, "メンバー番号 2"):
            m.engine.write_many(m._values[m.metric("X").id], [[0, 2]], [1.0, 2.0], m)
        with self.assertRaisesRegex(ValueError, "列の数"):
            m.engine.write_many(m._values[m.metric("X").id], [[0], [1]], [1.0], m)


if __name__ == "__main__":
    unittest.main()
