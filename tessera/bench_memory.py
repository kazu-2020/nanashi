"""メモリの内訳を測る。損益計画をスナップショットから開き、全体を計算し直す間の最大と、計算し直した後に
持ち続けるメモリを、格納データ（本体、差分、索引、差分集計の件数）とそれ以外に分ける。

    python bench_memory.py                   # 損益計画（大）の 1 倍、2 倍、4 倍
    python bench_memory.py --scales 1

測るのは次の 3 つで、それぞれ別のプロセスで開き直して測る。
- Rust のヒープ（nanashi_core.track_heap と heap）: Rust の側で確保している量。格納データと計算の途中結果
- プロセスの RSS: Python のオブジェクトと、アロケータが解放後も抱えている分を含む
- 式ごとの途中結果: Metric を 1 つずつ評価したときのヒープの最大と、結果の大きさの比
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time

MB = 1e6


def rss() -> float:
    """今の RSS（バイト）。"""
    return float(subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())])) * 1024


def max_rss() -> float:
    """このプロセスの RSS の最大（バイト。macOS はバイト、Linux は KiB で返す）。"""
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return float(r if sys.platform == "darwin" else r * 1024)


def save(path: str, scale: int) -> None:
    from examples.fpa import SIZES, build
    from sparse_engine.rust_engine import RustEngine
    e, p, mo = SIZES["large"]
    m = build(RustEngine(), e * scale, p * scale, mo)
    m.recalc()
    m.save(path)


def child(path: str, per_formula: bool) -> dict:
    import nanashi_core
    from sparse_engine import Model, to_formula
    from sparse_engine.rust_engine import RustEngine

    nanashi_core.track_heap(True)  # モデルを作る前に数え始める
    out: dict = {"rss_start": rss()}
    m = Model.load(path, RustEngine())
    out["heap_loaded"], out["rss_loaded"] = nanashi_core.heap()[0], rss()
    nanashi_core.reset_heap_peak()
    t = time.perf_counter()
    m.refresh()
    out["recalc_ms"] = 1e3 * (time.perf_counter() - t)
    out["heap_now"], out["heap_peak"] = nanashi_core.heap()
    out["rss_now"], out["rss_max"] = rss(), max_rss()

    mem = m.memory()
    formulas = {x.name for x in m.metrics.values() if x.formula is not None}

    def part(key: str, names) -> int:
        return sum(mem[n].get(key, 0) for n in names)

    inputs = set(mem) - formulas
    out["cells"] = sum(m.engine.size(v) for v in m._values.values())
    out["storage"] = {
        "入力の本体": part("base", inputs),
        "計算 Metric の本体": part("base", formulas),
        "差分": part("delta", mem),
        "索引": part("index", mem),
        "差分集計の件数": part("counts", mem),
    }
    out["estimate_ratio"] = sum(m.cell_estimates.values()) / max(1, sum(m.engine.size(m._values[m.metric(n).id]) for n in formulas))

    if per_formula:  # Metric を 1 つずつ評価し、途中結果の大きさを結果の大きさと比べる
        scans = {m.metrics[n].name for s in m._plan if s.scan_dim is not None for n in s.names}
        rows = []
        for n in sorted(formulas - scans):
            before = nanashi_core.heap()[0]
            nanashi_core.reset_heap_peak()
            cube = m.engine.evaluate(m.metric(n).formula, m, {})
            peak = nanashi_core.heap()[1] - before
            cells = m.engine.core.cube_len(cube)
            del cube
            rows.append((n, cells, peak, to_formula(m.metric(n).written, m)))
        out["per_formula"] = rows
    return out


def report(scale: int, r: dict, seq: dict | None) -> None:
    cells, storage = r["cells"], sum(r["storage"].values())
    print(f"\n損益計画（大）× {scale}: {cells:,} セル、全体の再計算 {r['recalc_ms']:,.0f} ms")
    print("| 計測 | MB | 1 セルあたり B |")
    print("|---|---|---|")
    rows = [
        ("格納データ（下の内訳の合計）", storage),
        *((f"  {k}", v) for k, v in r["storage"].items()),
        ("Rust のヒープ（計算し直した後）", r["heap_now"]),
        ("  うち格納データ以外", r["heap_now"] - storage),
        ("Rust のヒープの最大（計算し直す間）", r["heap_peak"]),
        ("  うち開いた直後からの増え幅", r["heap_peak"] - r["heap_loaded"]),
    ]
    if seq is not None:
        rows.append(("  同上、1 スレッドで計算したとき", seq["heap_peak"] - seq["heap_loaded"]))
    rows += [
        ("RSS（計算し直した後）", r["rss_now"]),
        ("  うち Rust のヒープ以外", r["rss_now"] - r["heap_now"]),
        ("RSS の最大", r["rss_max"]),
    ]
    for name, v in rows:
        print(f"| {name} | {v / MB:,.0f} | {v / cells:,.1f} |")
    print(f"計算 Metric のセル数の見積もり ÷ 実際: {r['estimate_ratio']:.2f}")
    if "per_formula" in r:
        print("\n式ごとの評価（途中結果の最大 ÷ 結果の大きさ。結果は 1 セル 16 B として）")
        print("| Metric | 結果のセル数 | ヒープの最大 MB | 倍率 | 式 |")
        print("|---|---|---|---|---|")
        for n, c, peak, text in sorted(r["per_formula"], key=lambda x: -x[2])[:8]:
            print(f"| {n} | {c:,} | {peak / MB:,.0f} | {peak / max(1, c * 16):.1f} | `{text}` |")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scales", default="1,2,4")
    ap.add_argument("--sequential", action="store_true", help="1 スレッドで計算したときの最大も測る")
    ap.add_argument("--child", help=argparse.SUPPRESS)
    ap.add_argument("--save", type=int, help=argparse.SUPPRESS)
    ap.add_argument("--per-formula", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.save is not None:
        save(args.child, args.save)
        return
    if args.child:
        print(json.dumps(child(args.child, args.per_formula)))
        return

    def run(*extra: str, env=None) -> dict:
        cmd = [sys.executable, __file__, "--child", path, *extra]
        return json.loads(subprocess.check_output(cmd, env=env).decode().splitlines()[-1])

    for scale in map(int, args.scales.split(",")):
        with tempfile.TemporaryDirectory() as path:
            subprocess.check_call([sys.executable, __file__, "--child", path, "--save", str(scale)])
            r = run()
            r["per_formula"] = run("--per-formula")["per_formula"]
            seq = run(env={**os.environ, "RAYON_NUM_THREADS": "1"}) if args.sequential else None
            report(scale, r, seq)


if __name__ == "__main__":
    main()
