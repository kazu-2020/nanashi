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


if __name__ == "__main__":
    unittest.main()
