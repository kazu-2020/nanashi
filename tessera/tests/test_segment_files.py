"""The segment files of the Rust engine (``segment_dir``). A process has one segment directory, so each
test runs the engine in a child process."""
import subprocess
import sys
import unittest

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # pragma: no cover
    RustEngine = None

FAILED_WRITE = """
import os, shutil, tempfile
from sparse_engine import Model, Named
from sparse_engine.rust_engine import RustEngine
d = tempfile.mkdtemp(prefix="nanashi-seg-")
m = Named(Model(engine=RustEngine(segment_dir=d, seg_rows=16, compact_min=4, delta_max=4, cache_bytes=1 << 16, read_window=2)))
m.add_dimension("A", [f"a{i}" for i in range(64)])
m.add_input("X", ["A"], {(f"a{i}",): float(i) for i in range(64)})
shutil.rmtree(d)
try:
    for i in range(8):
        m.set_cell("X", -1.0, A=f"a{i}")
except RuntimeError as e:
    print("RuntimeError", e)
else:
    raise SystemExit("no error")
print("after", m.get("X", A="a3"), m.get("X", A="a40"))
os.makedirs(d)
m.set_cell("X", -2.0, A="a9")
print("again", m.get("X", A="a9"))
"""

MEMORY = """
import shutil, tempfile
from sparse_engine import Model, Named
from sparse_engine.rust_engine import RustEngine
d = tempfile.mkdtemp(prefix="nanashi-seg-")
m = Named(Model(engine=RustEngine(segment_dir=d, cache_bytes=1 << 16, read_window=2)))
m.add_dimension("A", [f"a{i}" for i in range(3000)])
m.add_input("X", ["A"], {(f"a{i}",): float(i) for i in range(3000)})
mem = m.memory()["X"]
print(mem["rows"], mem["base"], mem["file"])
shutil.rmtree(d)
"""


@unittest.skipIf(RustEngine is None, "nanashi_core must be built")
class FailedSegmentWrite(unittest.TestCase):
    def test_a_failed_segment_write_is_a_runtime_error_and_the_model_stays(self):
        r = subprocess.run([sys.executable, "-c", FAILED_WRITE], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertIn("RuntimeError 書き込みの内部エラー: セグメントファイルを作れない", lines[0])
        self.assertEqual(lines[1:], ["after -1.0 40.0", "again -2.0"])

    def test_memory_gives_the_ram_and_the_file_bytes_of_the_base(self):
        r = subprocess.run([sys.executable, "-c", MEMORY], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        rows, base, file = map(int, r.stdout.split())
        self.assertEqual(rows, 3000)
        # In RAM: the fences of 3 blocks. On disk: the values, the packed keys, the header and the footer
        self.assertEqual(base, 3 * 16)
        self.assertGreater(file, 3000 * 8)
