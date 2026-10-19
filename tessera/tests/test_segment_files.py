"""The segment files of the Rust engine (``segment_dir``). A process has one segment directory, so each
test runs the engine in a child process."""
import subprocess
import sys
import tempfile
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
import os, shutil, tempfile
from sparse_engine import Model, Named
from sparse_engine.rust_engine import RustEngine
d = tempfile.mkdtemp(prefix="nanashi-seg-")
m = Named(Model(engine=RustEngine(segment_dir=d, cache_bytes=1 << 16, read_window=2)))
m.add_dimension("A", [f"a{i}" for i in range(3000)])
m.add_input("X", ["A"], {(f"a{i}",): float(i) for i in range(3000)})
mem = m.memory()["X"]
print(mem["rows"], mem["base"], mem["file"], sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d)))
shutil.rmtree(d)
"""

BUDGET = """
import shutil, tempfile
from sparse_engine import Model, Named
from sparse_engine.rust_engine import RustEngine
d = tempfile.mkdtemp(prefix="nanashi-seg-")
# A refused configuration installs no tier, so a later engine can use another directory
try:
    RustEngine(segment_dir=d + "-refused", cache_bytes=1 << 16, max_bytes=1 << 16)
except ValueError as e:
    print(e)
for limit in [(1 << 16) + 1024, 4 << 16]:
    m = Named(Model(engine=RustEngine(segment_dir=d, cache_bytes=1 << 16, max_bytes=limit)))
    m.add_dimension("A", [f"a{i}" for i in range(3000)])
    m.add_input("X", ["A"], {(f"a{i}",): float(i) for i in range(3000)})
    try:
        print(len(m.summarize("X", keep=["A"]).values))
    except ValueError as e:
        print(e)
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


@unittest.skipIf(RustEngine is None, "nanashi_core must be built")
class SegmentMemory(unittest.TestCase):
    def test_memory_gives_the_ram_and_the_file_bytes_of_the_base(self):
        r = subprocess.run([sys.executable, "-c", MEMORY], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        rows, base, file, disk = map(int, r.stdout.split())
        self.assertEqual(rows, 3000)
        self.assertEqual(base, 3 * 16, "the fences of 3 blocks")
        self.assertEqual(file, disk)

    def test_the_budget_counts_the_cache_bound_and_must_be_larger(self):
        r = subprocess.run([sys.executable, "-c", BUDGET], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        refused, tight, fits = r.stdout.splitlines()
        self.assertEqual(refused, "max_bytes は cache_bytes より大きくする")
        self.assertIn("メモリの予算を超える", tight)
        self.assertEqual(fits, "3000")

    def test_the_server_does_not_start_with_max_bytes_not_larger_than_cache_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            args = ["--engine", "rust", "--segment-dir", f"{d}/seg", "--cache-bytes", "65536", "--max-bytes", "65536", f"{d}/j"]
            r = subprocess.run([sys.executable, "-m", "sparse_engine.server", *args], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 2)
        self.assertIn("error: max_bytes は cache_bytes より大きくする", r.stderr)
