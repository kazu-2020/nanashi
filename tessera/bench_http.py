"""HTTP サーバー経由の読み書きの費用を測る（損益計画 大、8 人の読み手が休みなく読む中で書き込む）。

    python bench_http.py
"""
import json, sys, threading, time, urllib.parse, urllib.request
from bench import median_ms
from examples.fpa import SIZES, build
from sparse_engine.rust_engine import RustEngine
from sparse_engine.server import Server
from sparse_engine.workspace import Workspace

sys.setswitchinterval(0.0005)
m = build(RustEngine(), *SIZES["large"]); m.recalc()
emp = m.dimension("Employee").members[1]
server = Server(Workspace(m), "127.0.0.1", 0).start()
url = server.url

def get(path):
    with urllib.request.urlopen(url + path, timeout=10) as r:
        return json.loads(r.read())

def post(body):
    req = urllib.request.Request(url + "/writes", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

cell = f"/metrics/PayrollByEmployee/cell?Employee={emp}&Version={urllib.parse.quote("予算")}&Month=m01"
print(f"GET cell（単独）: {median_ms(lambda: get(cell), 20):.2f} ms")
summary = "/metrics/Revenue/summary?keep=Month&Version=" + urllib.parse.quote("予算")
print(f"GET summary 月別合計: {median_ms(lambda: get(summary), 20):.2f} ms")
print(f"POST write（単独）: {median_ms(lambda: post({'client_op_id': str(time.perf_counter_ns()), 'ops': [{'op': 'set_cell', 'args': ['Salary', 500.0], 'kwargs': {'Employee': emp, 'Version': '予算'}}]}), 20):.2f} ms")

stop = threading.Event(); reads = [0]
def reader():
    while not stop.is_set():
        get(cell); reads[0] += 1
threads = [threading.Thread(target=reader, daemon=True) for _ in range(8)]
t0 = time.perf_counter()
for t in threads: t.start()
w = median_ms(lambda: post({'client_op_id': str(time.perf_counter_ns()), 'ops': [{'op': 'set_cell', 'args': ['Salary', 501.0], 'kwargs': {'Employee': emp, 'Version': '予算'}}]}), 20)
stop.set()
for t in threads: t.join()
dt = time.perf_counter() - t0
print(f"8 人が休みなく GET cell する中での POST write: {w:.2f} ms（読み出しは毎秒 {reads[0] / dt:,.0f} 件）")
server.stop()
