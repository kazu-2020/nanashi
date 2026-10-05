"""Rust エンジンの結果が参照実装と一致する。"""
import random
import unittest

from sparse_engine import Model
from sparse_engine.engine import ReferenceEngine

from .test_incremental import model as build, same

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None


def build_with(engine) -> Model:
    m = build()
    fresh = Model(engine=engine)
    fresh.dimensions = m.dimensions
    fresh._next_id = m._next_id  # 軸のメンバーに振った ID と重ならないように、続きから振る
    gone = {x.id for x in m.metrics.values()}  # keep the UUIDs of the dimensions, members, and properties only
    fresh.ids = {u: h for u, h in m.ids.items() if h not in gone}
    fresh._uuids = {h: u for u, h in fresh.ids.items()}
    for name, meta in m.metrics.items():
        if meta.formula is None:
            fresh.add_input(name, meta.dims, m.value(name).cells, kind=meta.kind)
        else:
            fresh.add_formula(name, meta.dims, meta.formula, kind=meta.kind)
    return fresh


class MatchesReference:
    """other() で作ったエンジンの結果が、ランダムな編集のあとも参照実装と一致する。"""

    def other(self):
        raise NotImplementedError

    def test_random_edits(self):
        rng = random.Random(7)
        ref, pol = build_with(ReferenceEngine()), build_with(self.other())
        inputs = [n for n, x in ref.metrics.items() if x.formula is None]
        for round_ in range(150):
            for _ in range(rng.randint(1, 3)):
                name = rng.choice(inputs)
                meta = ref.metrics[name]
                coords = {d: rng.choice(ref.dimensions[d].members) for d in meta.dims}
                if rng.random() < 0.3:
                    value = None
                elif meta.kind == "boolean":
                    value = rng.random() < 0.5
                else:
                    value = float(rng.randint(-5, 60))
                ref.set_cell(name, value, **coords)
                pol.set_cell(name, value, **coords)
            for name in ref.metrics:
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
                m._values["X"] = m.engine.write_many(m._values["X"], cols, values, m)
            ref_m, rs = models
            with self.subTest(count=count):
                self.assertEqual(ref_m.engine.to_cube(ref_m._values["X"], ref_m).cells,
                                 rs.engine.to_cube(rs._values["X"], rs).cells)
                region = {"A": frozenset(["a1", "a7", "a30"]), "C": frozenset(["c2", "c4"])}
                read = []
                for m in models:
                    cols, values = m.engine.columns(m._values["X"], region, m)
                    read.append(sorted(zip(*cols, values)))
                self.assertEqual(read[0], read[1])
                self.assertTrue(read[0])

    def test_rejects_bad_columns(self):
        m = Model(engine=RustEngine())
        m.add_dimension("A", ["a0", "a1"])
        m.add_input("X", ["A"], {})
        with self.assertRaisesRegex(ValueError, "メンバー番号 2"):
            m.engine.write_many(m._values["X"], [[0, 2]], [1.0, 2.0], m)
        with self.assertRaisesRegex(ValueError, "列の数"):
            m.engine.write_many(m._values["X"], [[0], [1]], [1.0], m)


if __name__ == "__main__":
    unittest.main()
