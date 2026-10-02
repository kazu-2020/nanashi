"""Model の保存と読み込み。"""
import json
import random
import tempfile
import unittest
from pathlib import Path

from examples.fpa import EDITS, build
from sparse_engine import Model, to_formula
from sparse_engine.engine import ReferenceEngine
from sparse_engine.storage import dump, read

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
    def check(self, saving_engine, loading_engine, legacy=False):
        original = small(saving_engine)
        with tempfile.TemporaryDirectory() as tmp:
            original.save(tmp)
            if legacy:
                from .legacy import to_format2
                to_format2(tmp, original)
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

    def test_reads_format_2(self):
        """以前の版の保存形式（inputs.npz）を、numpy なしで読める。"""
        engines = [ReferenceEngine] + ([RustEngine] if RustEngine is not None else [])
        for saving in engines:
            for loading in engines:
                with self.subTest(saving=saving.name, loading=loading.name):
                    self.check(saving(), loading(), legacy=True)

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


def engines() -> list:
    return [ReferenceEngine] + ([RustEngine] if RustEngine is not None else [])


@unittest.skipIf(nanashi_core is None, "nanashi_core が必要")
class SnapshotFormat(unittest.TestCase):
    """記録先のスナップショットの形式（版 5）。入力は平らな形式で、計算 Metric の値も持つ。"""

    def check_values(self, original: Model, loaded: Model) -> None:
        a, b = snapshot(original), snapshot(loaded)
        for name in original.metrics:
            with self.subTest(metric=name):
                self.assertTrue(same(a[name], b[name]), name)

    def test_computed_values_are_saved_and_restored(self):
        for engine in engines():
            with self.subTest(engine=engine.name):
                m = small(engine())
                files = dump(m, snapshot=True)
                meta = json.loads(files["model.json"])
                self.assertEqual(meta["format"], 5)
                self.assertEqual(meta["computed"]["engine"], engine.name)
                formulas = {x.id for x in m.metrics.values() if x.formula is not None}
                inputs = {x.id for x in m.metrics.values() if x.formula is None}
                self.assertEqual({n for n in files if n.startswith("inputs.")}, {f"inputs.{i}.cells" for i in inputs})
                self.assertEqual({n for n in files if n.startswith("values.")}, {f"values.{i}.cells" for i in formulas})
                self.assertEqual({n for n in files if n.startswith("counts.")}, {f"counts.{m.metrics[n].id}.cells" for n in m._counts})
                loaded = read(lambda n: files[n], engine())
                self.assertFalse(loaded._pending.full)  # 計算し直さずに始められる
                self.assertIsNotNone(loaded._plan)
                self.check_values(m, loaded)
                # 値を読み込んだモデルでも、以後の差分再計算が全体の再計算と一致する
                rng = random.Random(2)
                for _ in range(8):
                    _, edit = rng.choice(EDITS)
                    edit(loaded, rng)
                    incremental = snapshot(loaded)
                    loaded._invalidate()
                    full = snapshot(loaded)
                    for name in loaded.metrics:
                        self.assertTrue(same(incremental[name], full[name]), name)

    def test_model_with_pending_changes_saves_inputs_only(self):
        m = small()
        name = next(n for n, x in m.metrics.items() if x.formula is None and x.kind == "number" and x.dims)
        key = {d: m.dimensions[d].members[0] for d in m.metrics[name].dims}
        m.set_cell(name, 123.0, **key)  # 再計算していない変更がある
        files = dump(m, snapshot=True)
        meta = json.loads(files["model.json"])
        self.assertNotIn("computed", meta)
        self.assertFalse([n for n in files if n.startswith("values.")])
        loaded = read(lambda n: files[n])
        self.assertTrue(loaded._pending.full)
        m.recalc()
        self.check_values(m, loaded)
        # 定義を変えて、まだ計算し直していないときも、古い値を保存しない
        formula = next(n for n, x in m.metrics.items() if x.formula is not None and x.kind == "number")
        x = m.metrics[formula]
        m.add_formula(formula, x.dims, f"({to_formula(x.written)}) * 2", kind=x.kind, partition=x.partition,
                      overridable=x.overridable)
        self.assertNotIn("computed", json.loads(dump(m, snapshot=True)["model.json"]))
        m.recalc()
        self.assertIn("computed", json.loads(dump(m, snapshot=True)["model.json"]))

    def test_values_from_another_engine_or_version_are_recomputed(self):
        if RustEngine is None:
            self.skipTest("nanashi_core のビルドが必要")
        m = small(RustEngine())
        files = dump(m, snapshot=True)
        loaded = read(lambda n: files[n], ReferenceEngine())  # 別のエンジン
        self.assertTrue(loaded._pending.full)
        self.check_values(m, loaded)
        meta = json.loads(files["model.json"])
        meta["computed"]["compute"] += 1  # 計算の版が違う
        files["model.json"] = json.dumps(meta).encode()
        loaded = read(lambda n: files[n], RustEngine())
        self.assertTrue(loaded._pending.full)
        self.check_values(m, loaded)

    def test_saved_layout_differing_from_the_plan_is_repacked(self):
        """保存した分割軸と読むときの分割軸が違っても（詰め方が違う）、詰め直して同じ値になる。"""
        for engine in engines():
            with self.subTest(engine=engine.name):
                m = small(engine())
                files = dump(m, snapshot=True)
                meta = json.loads(files["model.json"])
                for spec in meta["metrics"]:
                    if len(spec["dims"]) > 1:
                        spec["layout"] = None if spec["layout"] is not None else spec["dims"][-1]
                files["model.json"] = json.dumps(meta).encode()
                loaded = read(lambda n: files[n], engine())
                self.check_values(m, loaded)

    def test_save_keeps_parquet_and_reads_both(self):
        """Model.save は交換用の Parquet のまま（版 4）で、版 5 のディレクトリも Model.load で読める。"""
        m = small()
        with tempfile.TemporaryDirectory() as tmp:
            m.save(tmp)
            self.assertEqual(json.loads((Path(tmp) / "model.json").read_text())["format"], 4)
            self.assertTrue(any(p.suffix == ".parquet" for p in Path(tmp).iterdir()))
            for name, data in dump(m, snapshot=True).items():
                (Path(tmp) / name).write_bytes(data)
            loaded = Model.load(tmp)
            self.assertFalse(loaded._pending.full)
            self.check_values(m, loaded)


if __name__ == "__main__":
    unittest.main()
