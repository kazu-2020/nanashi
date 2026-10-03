"""小売の計画モデルで参照実装と Rust エンジンの速度を比べる。

    python bench.py                              # 全組み合わせを別プロセスで実行して表にする
    python bench.py --engine rust --size large   # 1 組み合わせだけ（JSON を出力）
    python bench.py --report                     # 保存済みの結果を Markdown の表にするだけ
"""
from __future__ import annotations

import argparse
import json
import resource
import statistics
import subprocess
import sys
import time

import numpy as np

SIZES = {  # 商品数, 店舗数, Volume の密度
    "small": (1_000, 100, 0.01),       # 360 万の組み合わせのうち 3.6 万セル
    "medium": (10_000, 1_000, 0.001),  # 3.6 億のうち 36 万セル
    "large": (10_000, 1_000, 0.01),    # 3.6 億のうち 360 万セル
    "xlarge": (10_000, 1_000, 0.05),   # 3.6 億のうち 1800 万セル
}
MONTHS = [f"m{i:02d}" for i in range(1, 37)]
FORMULAS = [
    ("Revenue", ["Product", "Store", "Month"], "Volume * Price"),
    ("CatRev", ["Category", "Store", "Month"], "Revenue[BY SUM: Product.Category]"),
    ("StoreRev", ["Store", "Month"], "Revenue[REMOVE SUM: Product]"),
    ("ProdRev", ["Product", "Month"], "Revenue[REMOVE SUM: Store]"),
    ("Margin", ["Product", "Month"], "ProdRev - Cost"),
    ("Gap", ["Store", "Month"], "StoreRev - Target[BY: Store.Region]"),
    ("BigSale", ["Product", "Store", "Month"], "IF(Revenue > 1000, Revenue * 0.1, 0)"),
    ("ActiveRev", ["Product", "Store", "Month"], "Revenue[FILTER: Active]"),
    ("MoM", ["Product", "Month"], "Margin - Margin[SELECT: Month - 1]"),
    ("Stock", ["Product", "Month"], "PREVIOUS(Month) * 0.9 + Margin"),
    ("Total", [], "Stock[REMOVE SUM: Product, Month]"),
]
EDITS = [  # (説明, Metric, 座標)
    ("Volume 1 セル", "Volume", {"Product": "p00007", "Store": "s0003", "Month": "m05"}),
    ("Price 1 商品", "Price", {"Product": "p00007"}),
    ("Cost 1 商品の 1 月", "Cost", {"Product": "p00007", "Month": "m01"}),
    ("Target 1 地域 1 月", "Target", {"Region": "r03", "Month": "m05"}),
]


def median_ms(fn, repeats: int) -> float:
    """Run fn repeats times. Return the median time in milliseconds."""
    xs = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        xs.append(time.perf_counter() - t0)
    return 1e3 * statistics.median(xs)


def time_edits(m, edits, repeats: int) -> tuple[float, dict]:
    """Time the edits (label, Metric, coordinates) after the full recalculation.

    Return the time of the first edit, and the median time and the median number of
    recalculated Metrics for each edit. The first edit includes the deferred cost of the split.
    """
    rng = np.random.default_rng(1)
    _, name, coords = edits[0]
    m.set_cell(name, float(rng.integers(1, 100)), **coords)
    t0 = time.perf_counter()
    m.recalc()
    first = time.perf_counter() - t0
    out = {}
    for label, name, coords in edits:
        times, touched = [], []
        for _ in range(repeats):
            m.set_cell(name, float(rng.integers(1, 100)), **coords)
            m.slice_log.clear()
            t0 = time.perf_counter()
            m.recalc()
            times.append(time.perf_counter() - t0)
            touched.append(len(m.slice_log))
        out[label] = (statistics.median(times), statistics.median(touched))
    return first, out


def run_plan(script: str, plan: list[tuple[str, str, object]], timeout: int, tail: int, path: str) -> list[dict]:
    """Run each (engine, option, value) of the plan as script in a separate process.

    Write the JSON results to path and return them. If a process fails or times out,
    its result has "error" or "timeout".
    """
    results = []
    for engine, option, value in plan:
        print(f"running {engine} {value} ...", file=sys.stderr, flush=True)
        try:
            out = subprocess.run([sys.executable, script, "--engine", engine, f"--{option}", str(value)],
                                 capture_output=True, text=True, timeout=timeout, check=True)
            results.append(json.loads(out.stdout))
        except subprocess.TimeoutExpired:
            results.append({"engine": engine, option: value, "timeout": True})
        except subprocess.CalledProcessError as e:
            print(e.stderr, file=sys.stderr)
            results.append({"engine": engine, option: value, "error": e.stderr[-tail:]})
    with open(path, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    return results


def build(engine_name: str, size: str):
    from sparse_engine import Model
    from sparse_engine.engine import engine_for

    n_prod, n_store, density = SIZES[size]
    rng = np.random.default_rng(0)
    products = [f"p{i:05d}" for i in range(n_prod)]
    stores = [f"s{i:04d}" for i in range(n_store)]
    n_cat, n_reg = 50, 10

    engine = engine_for(engine_name)
    m = Model(engine=engine)
    m.add_dimension("Product", products)
    m.add_dimension("Category", [f"c{i:02d}" for i in range(n_cat)])
    m.add_dimension("Store", stores)
    m.add_dimension("Region", [f"r{i:02d}" for i in range(n_reg)])
    m.add_dimension("Month", MONTHS, ordered=True)
    m.add_property("Product", "Category", "Category", {p: f"c{i % n_cat:02d}" for i, p in enumerate(products)})
    m.add_property("Store", "Region", "Region", {s: f"r{i % n_reg:02d}" for i, s in enumerate(stores)})

    # 入力データ（列 = 各軸のメンバー番号, __v = 値）
    total = n_prod * n_store * len(MONTHS)
    lin = np.unique(rng.integers(0, total, int(total * density)))
    vol = {"Product": lin // (n_store * 36), "Store": lin // 36 % n_store, "Month": lin % 36,
           "__v": rng.integers(1, 20, lin.size).astype(float)}
    inputs = {
        "Price": ((["Product"]), {"Product": np.arange(n_prod), "__v": rng.integers(10, 200, n_prod).astype(float)}, "number"),
        "Volume": ((["Product", "Store", "Month"]), vol, "number"),
        "Cost": ((["Product", "Month"]), {"Product": np.repeat(np.arange(n_prod), 36), "Month": np.tile(np.arange(36), n_prod),
                                           "__v": rng.integers(100, 5000, n_prod * 36).astype(float)}, "number"),
        "Target": ((["Region", "Month"]), {"Region": np.repeat(np.arange(n_reg), 36), "Month": np.tile(np.arange(36), n_reg),
                                            "__v": rng.integers(1000, 9000, n_reg * 36).astype(float)}, "number"),
        "Active": ((["Product"]), {"Product": np.arange(n_prod), "__v": rng.random(n_prod) < 0.8}, "boolean"),
    }
    for name, (dims, cols, kind) in inputs.items():
        if engine_name == "rust":
            storage = engine.from_arrays(tuple(dims), kind, cols, m)
            m.add_input(name, dims, kind=kind, storage=storage)
        else:
            members = [m.dimensions[d].members for d in dims]
            codes = [cols[d].tolist() for d in dims]
            values = cols["__v"].tolist()
            cells = {tuple(members[j][codes[j][i]] for j in range(len(dims))): values[i]
                     for i in range(len(values))}
            m.add_input(name, dims, cells, kind=kind)
    for name, dims, formula in FORMULAS:
        m.add_formula(name, dims, formula)
    return m


def run(engine_name: str, size: str, repeats: int) -> dict:
    t0 = time.perf_counter()
    m = build(engine_name, size)
    load = time.perf_counter() - t0

    t0 = time.perf_counter()
    m.recalc()
    full = time.perf_counter() - t0

    cells = {n: m.engine.size(m.raw(n)) for n in m.metrics}
    total_value = m.value("Total").cells.get(())

    first_edit, timings = time_edits(m, EDITS, repeats)
    edits = {label: t for label, (t, _) in timings.items()}

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # macOS はバイト単位
    return {"engine": engine_name, "size": size, "load": load, "full": full, "edits": edits,
            "first_edit": first_edit,
            "cells": cells, "total": total_value, "peak_mb": peak / 2**20,
            "layout": dict(m.layout), "delta_metrics": sorted(m._delta)}


def driver(skip_reference: bool) -> None:
    """Run each combination of engine and size in a separate process."""
    plan = []
    for size in SIZES:
        if not skip_reference and size != "xlarge":  # 参照実装は遅いので 1756 万セルは省く
            plan.append(("reference", "size", size))
        plan.append(("rust", "size", size))
    results = run_plan(__file__, plan, 1800, 500, "bench_results.json")
    print(json.dumps(results, ensure_ascii=False, indent=2))


def ms(x: float) -> str:
    return f"{x * 1000:,.0f} ms" if x >= 0.01 else f"{x * 1000:.1f} ms"


def report(rows: list[dict]) -> None:
    """Print the results as a Markdown table, and the relative difference of Total between the engines."""
    edits = next(r["edits"] for r in rows if "edits" in r)
    print("| エンジン | 規模 | Volume セル数 | 全体再計算 | 直後の最初の変更 | " + " | ".join(edits) + " | ピークメモリ |")
    print("|---" * (6 + len(edits)) + "|")
    for r in rows:
        if "edits" not in r:
            print(f"| {r['engine']} | {r['size']} | | {'タイムアウト' if r.get('timeout') else 'エラー'} |")
            continue
        cells = f"{r['cells']['Volume']:,}"
        first = ms(r["first_edit"]) if "first_edit" in r else "-"
        print(f"| {r['engine']} | {r['size']} | {cells} | {ms(r['full'])} | {first} | "
              + " | ".join(ms(v) for v in r["edits"].values()) + f" | {r['peak_mb']:,.0f} MB |")

    totals = {}
    for r in rows:
        if "total" in r:
            totals.setdefault(r["size"], []).append((r["engine"], r["total"]))
    print()
    for size, ts in totals.items():
        vals = [t for _, t in ts]
        rel = (max(vals) - min(vals)) / abs(vals[0])
        print(f"{size}: Total の相対差 {rel:.1e}（{', '.join(e for e, _ in ts)}）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["reference", "rust"])
    ap.add_argument("--size", choices=list(SIZES))
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--skip-reference", action="store_true", help="参照実装を省く（参照実装が変わっていないとき）")
    ap.add_argument("--report", action="store_true", help="保存済みの結果（bench_results.json）を表にするだけ")
    args = ap.parse_args()
    if args.report:
        report(json.load(open("bench_results.json")))
    elif args.engine:
        print(json.dumps(run(args.engine, args.size, args.repeats), ensure_ascii=False))
    else:
        driver(args.skip_reference)
