import tempfile
import unittest

try:
    from bench_snapshot import write
except ImportError:  # nanashi_core is not built
    write = None
from examples.fpa import SIZES, build
from sparse_engine import Named


@unittest.skipIf(write is None, "nanashi_core が必要")
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
            self.assertEqual(len(got.value("Units").cells), 30 * (24 + 6))
            self.assertEqual(len(got.value("DeptOf").cells), 60 * 24)


if __name__ == "__main__":
    unittest.main()
