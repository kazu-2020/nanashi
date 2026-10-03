"""小売の計画モデルで参照実装と Rust エンジンの速度を比べる。

    python bench.py                              # 全組み合わせを別プロセスで実行して表にする
    python bench.py --engine rust --size large   # 1 組み合わせだけ（JSON を出力）
"""
from __future__ import annotations

import argparse
import json
import os
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


def build(engine_name: str, size: str):
    from sparse_engine import Model
    from sparse_engine.engine import ReferenceEngine

    n_prod, n_store, density = SIZES[size]
    rng = np.random.default_rng(0)
    products = [f"p{i:05d}" for i in range(n_prod)]
    stores = [f"s{i:04d}" for i in range(n_store)]
    n_cat, n_reg = 50, 10

    if engine_name == "rust":
        from sparse_engine.rust_engine import RustEngine
        engine = RustEngine()
    else:
        engine = ReferenceEngine()
    m = Model(engine=engine, auto_layout=os.environ.get("BENCH_LAYOUT", "auto") == "auto",
              delta_aggregation=os.environ.get("BENCH_DELTA", "on") == "on")
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

    rng = np.random.default_rng(1)
    # 全体の再計算の直後の最初の変更（後回しにした分割のコストを含む）
    label, name, coords = EDITS[0]
    m.set_cell(name, float(rng.integers(1, 100)), **coords)
    t0 = time.perf_counter()
    m.recalc()
    first_edit = time.perf_counter() - t0
    edits = {}
    for label, name, coords in EDITS:
        times = []
        for _ in range(repeats):
            m.set_cell(name, float(rng.integers(1, 100)), **coords)
            t0 = time.perf_counter()
            m.recalc()
            times.append(time.perf_counter() - t0)
        edits[label] = statistics.median(times)

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # macOS はバイト単位
    return {"engine": engine_name, "size": size, "load": load, "full": full, "edits": edits,
            "first_edit": first_edit,
            "cells": cells, "total": total_value, "peak_mb": peak / 2**20,
            "layout": dict(m.layout), "layout_mode": os.environ.get("BENCH_LAYOUT", "auto"),
            "delta": os.environ.get("BENCH_DELTA", "on"), "delta_metrics": sorted(m._delta)}


def driver(skip_reference: bool) -> None:
    """(表示名, エンジン, 規模, 環境変数) の組み合わせを別プロセスで実行する。"""
    plan = []
    for size in SIZES:
        if not skip_reference and size != "xlarge":  # 参照実装は遅いので 1756 万セルは省く
            plan.append(("reference", "reference", size, {}))
        plan.append(("rust", "rust", size, {}))
    results = []
    for label, engine, size, extra in plan:
        print(f"running {label} {size} ...", file=sys.stderr, flush=True)
        try:
            out = subprocess.run([sys.executable, __file__, "--engine", engine, "--size", size],
                                 env=os.environ | extra, capture_output=True, text=True,
                                 timeout=1800, check=True)
            r = json.loads(out.stdout)
            r["engine"] = label
            results.append(r)
        except subprocess.TimeoutExpired:
            results.append({"engine": label, "size": size, "timeout": True})
        except subprocess.CalledProcessError as e:
            print(e.stderr, file=sys.stderr)
            results.append({"engine": label, "size": size, "error": e.stderr[-500:]})
    with open("bench_results.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["reference", "rust"])
    ap.add_argument("--size", choices=list(SIZES))
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--skip-reference", action="store_true", help="参照実装を省く（参照実装が変わっていないとき）")
    args = ap.parse_args()
    if args.engine:
        print(json.dumps(run(args.engine, args.size, args.repeats), ensure_ascii=False))
    else:
        driver(args.skip_reference)
