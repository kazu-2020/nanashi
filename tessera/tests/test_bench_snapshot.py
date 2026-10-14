import importlib.util
import tempfile
import unittest

from examples.fpa import SIZES, build
from sparse_engine import Named

NATIVE = importlib.util.find_spec("nanashi_core") is not None
if NATIVE:  # bench_snapshot imports nanashi_core
    from bench_snapshot import write


@unittest.skipIf(not NATIVE, "nanashi_core が必要")
class SnapshotTest(unittest.TestCase):
    def test_same_model_as_build(self):
        employees, products, months = SIZES["small"]
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            write(a, employees, products, months)
            build(None, employees, products, months).save(b)
            got, want = Named.load(a), Named.load(b)
            names = [got.metrics[n].name for n in got.metrics]
            self.assertEqual(names, [want.metrics[n].name for n in want.metrics])
            for name in names:
                with self.subTest(name):
                    self.assertEqual(got.value(name).cells, want.value(name).cells)
            self.assertEqual(len(got.value("Units").cells), products * (months + months // 4))
            self.assertEqual(len(got.value("DeptOf").cells), employees * months)


if __name__ == "__main__":
    unittest.main()
