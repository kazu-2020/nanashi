"""Model の保存と読み込み。"""
import json
import random
import tempfile
import unittest
from pathlib import Path

from examples.fpa import EDITS, build
from sparse_engine import Model, to_formula
from sparse_engine.engine import ReferenceEngine

from .test_incremental import same, snapshot

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None


def small(engine=None) -> Model:
    m = build(engine, employees=20, products=10, months=12, seed=5)
    m.recalc()
    rng = random.Random(0)
    for _, edit in EDITS:  # メンバーの追加も含めて、いくつか変更を加えておく
        edit(m, rng)
    m.recalc()
    return m


try:
    import nanashi_core  # noqa: F401  保存形式（Parquet）の読み書きに使う
except ImportError:
    nanashi_core = None


@unittest.skipIf(nanashi_core is None, "nanashi_core が必要")
class RoundTrip(unittest.TestCase):
    def check(self, saving_engine, loading_engine):
        original = small(saving_engine)
        with tempfile.TemporaryDirectory() as tmp:
            original.save(tmp)
            loaded = Model.load(tmp, loading_engine)
        self.assertEqual(loaded.dimensions["Employee"].members, original.dimensions["Employee"].members)
        a, b = snapshot(original), snapshot(loaded)
        for name in original.metrics:
            with self.subTest(metric=name):
                self.assertTrue(same(a[name], b[name]), name)
        return original, loaded

    def test_reference_to_reference(self):
        self.check(ReferenceEngine(), ReferenceEngine())

    def test_saves_parquet_per_input_metric(self):
        m = small(ReferenceEngine())
        with tempfile.TemporaryDirectory() as tmp:
            m.save(tmp)
            names = sorted(p.name for p in Path(tmp).iterdir())
        inputs = sorted(f"inputs.{x.id}.parquet" for x in m.metrics.values() if x.formula is None)
        self.assertEqual(names, sorted(inputs + ["model.json"]))

    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_reference_to_rust(self):
        self.check(ReferenceEngine(), RustEngine())

    @unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
    def test_rust_to_reference(self):
        self.check(RustEngine(), ReferenceEngine())

    def test_formulas_are_saved_as_written(self):
        original, loaded = self.check(ReferenceEngine(), ReferenceEngine())
        for name, m in original.metrics.items():
            if m.written is not None:
                with self.subTest(metric=name):
                    self.assertEqual(to_formula(loaded.metrics[name].written), to_formula(m.written))
        self.assertIn("Employee.DeptOf", to_formula(loaded.metrics["Payroll"].written))

    def test_loaded_model_keeps_working(self):
        _, loaded = self.check(ReferenceEngine(), ReferenceEngine())
        rng = random.Random(1)
        for _ in range(10):
            _, edit = rng.choice(EDITS)
            edit(loaded, rng)
            incremental = snapshot(loaded)
            loaded._invalidate()
            full = snapshot(loaded)
            for name in loaded.metrics:
                with self.subTest(metric=name):
                    self.assertTrue(same(incremental[name], full[name]), name)

    def test_unknown_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            small().save(tmp)
            meta = json.loads((Path(tmp) / "model.json").read_text())
            meta["format"] = 999
            (Path(tmp) / "model.json").write_text(json.dumps(meta))
            with self.assertRaisesRegex(ValueError, "保存形式"):
                Model.load(tmp)


if __name__ == "__main__":
    unittest.main()
