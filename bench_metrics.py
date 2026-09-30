"""計算 Metric の数を増やしたときの速度を測る。セル数はほぼ固定し、Metric の数だけを変える。

    python bench_metrics.py                         # 全組み合わせを別プロセスで実行して表にする
    python bench_metrics.py --engine polars --metrics 300   # 1 組み合わせだけ（JSON を出力）

式は乱数で生成する。計画モデルでよく出る形（定数倍、同じ軸どうしの足し算、率の引き下ろし、
集計、IF、前月参照、scan）を混ぜ、直前に作った Metric から派生させる「連鎖」と、
全体から選ぶ「分岐」を半々にする。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import resource
import statistics
import subprocess
import sys
import time

import numpy as np

N_PRODUCT, N_CATEGORY, N_REGION = 2_000, 20, 10
MONTHS = [f"m{i:02d}" for i in range(1, 37)]
DENSITY = 0.05  # Volume の密度（2000 x 10 x 36 = 72 万のうち約 3.6 万セル）
EDITS = [  # (説明, Metric, 座標)
    ("Volume 1 セル", "Volume", {"Product": "p0007", "Region": "r03", "Month": "m05"}),
    ("Price 1 商品", "Price", {"Product": "p0007"}),
    ("Cost 1 商品の 1 月", "Cost", {"Product": "p0007", "Month": "m01"}),
]


def generate(n: int, seed: int = 0) -> list[tuple[str, tuple[str, ...], str]]:
    """(名前, 軸, 式) を n 個作る。"""
    rng = random.Random(seed)
    pool: list[tuple[str, tuple[str, ...]]] = [
        ("Volume", ("Product", "Region", "Month")), ("Price", ("Product",)),
        ("Cost", ("Product", "Month")),
    ]
    out = []

    def same_dims(dims, exclude):
        return [name for name, d in pool if set(d) == set(dims) and name != exclude]

    templates = [  # (重み, 関数)。関数は使えないとき None を返す
        (3, lambda a, d: (f"{a} * 1.01", d)),
        (3, lambda a, d: (f"{a} + {rng.choice(c)}", d) if (c := same_dims(d, a)) else None),
        (2, lambda a, d: (f"{a} * Rate[BY: Product.Category]", d) if "Product" in d else None),
        (1, lambda a, d: (f"{a}[REMOVE SUM: Region]", tuple(x for x in d if x != "Region"))
            if "Region" in d else None),
        (1, lambda a, d: (f"{a}[BY SUM: Product.Category]",
                          tuple("Category" if x == "Product" else x for x in d)) if "Product" in d else None),
        (1, lambda a, d: (f"IF({a} > 100, {a}, 0)", d)),
        (1, lambda a, d: (f"{a}[SELECT: Month - 1]", d) if "Month" in d else None),
        (0.5, lambda a, d: (f"{a}[REMOVE SUM: Month]", tuple(x for x in d if x != "Month"))
            if "Month" in d and len(d) > 1 else None),
        (1, lambda a, d: ("PREVIOUS(Month) * 0.9 + " + a, d) if "Month" in d else None),
    ]
    weights = [w for w, _ in templates]
    while len(out) < n:
        recent = pool[-20:]
        base_name, base_dims = rng.choice(recent if rng.random() < 0.5 else pool)
        _, make = rng.choices(templates, weights)[0]
        made = make(base_name, base_dims)
        if made is None:
            continue
        formula, dims = made
        name = f"M{len(out):04d}"
        out.append((name, dims, formula))
        pool.append((name, dims))
    return out


def build(engine_name: str, n: int):
    from sparse_engine import Model
    from sparse_engine.engine import ReferenceEngine

    if engine_name == "rust":
        from sparse_engine.rust_engine import RustEngine
        engine = RustEngine()
    elif engine_name == "polars":
        from sparse_engine.polars_engine import PolarsEngine
        engine = PolarsEngine()
    else:
        engine = ReferenceEngine()
    m = Model(engine=engine)
    products = [f"p{i:04d}" for i in range(N_PRODUCT)]
    m.add_dimension("Product", products)
    m.add_dimension("Category", [f"c{i:02d}" for i in range(N_CATEGORY)])
    m.add_dimension("Region", [f"r{i:02d}" for i in range(N_REGION)])
    m.add_dimension("Month", MONTHS, ordered=True)
    per_cat = N_PRODUCT // N_CATEGORY  # 同じカテゴリの商品は連続した番号にする
    m.add_property("Product", "Category", "Category",
                   {p: f"c{i // per_cat:02d}" for i, p in enumerate(products)})

    rng = np.random.default_rng(0)
    total = N_PRODUCT * N_REGION * len(MONTHS)
    lin = np.unique(rng.integers(0, total, int(total * DENSITY)))
    vol = {"Product": lin // (N_REGION * 36), "Region": lin // 36 % N_REGION, "Month": lin % 36,
           "__v": rng.integers(1, 20, lin.size).astype(float)}
    inputs = {
        "Volume": (["Product", "Region", "Month"], vol),
        "Price": (["Product"], {"Product": np.arange(N_PRODUCT), "__v": rng.integers(10, 200, N_PRODUCT).astype(float)}),
        "Cost": (["Product", "Month"], {"Product": np.repeat(np.arange(N_PRODUCT), 36),
                                        "Month": np.tile(np.arange(36), N_PRODUCT),
                                        "__v": rng.integers(100, 5000, N_PRODUCT * 36).astype(float)}),
        "Rate": (["Category"], {"Category": np.arange(N_CATEGORY), "__v": rng.random(N_CATEGORY)}),
    }
    for name, (dims, cols) in inputs.items():
        if engine_name == "rust":
            m.add_input(name, dims, storage=engine.from_arrays(tuple(dims), "number", cols, m))
        elif engine_name == "polars":
            import polars as pl
            m.add_input(name, dims, storage=engine.from_frame(tuple(dims), "number", pl.DataFrame(cols), m))
        else:
            members = [m.dimensions[d].members for d in dims]
            codes = [cols[d].tolist() for d in dims]
            values = cols["__v"].tolist()
            m.add_input(name, dims, {tuple(members[j][codes[j][i]] for j in range(len(dims))): values[i]
                                     for i in range(len(values))})
    for name, dims, formula in generate(n):
        m.add_formula(name, dims, formula)
    return m


def run(engine_name: str, n: int, repeats: int) -> dict:
    t0 = time.perf_counter()
    m = build(engine_name, n)
    build_time = time.perf_counter() - t0

    t0 = time.perf_counter()
    m._compile()
    compile_time = time.perf_counter() - t0

    t0 = time.perf_counter()
    m.recalc()
    full = time.perf_counter() - t0

    rng = np.random.default_rng(1)
    # 全体の再計算の直後の最初の変更（後回しにした分割のコストを含む）
    label, name, coords = EDITS[0]
    m.set_cell(name, float(rng.integers(1, 100)), **coords)
    t0 = time.perf_counter()
    m.recalc()
    first_edit = time.perf_counter() - t0

    edits = {}
    for label, name, coords in EDITS:
        times, touched = [], []
        for _ in range(repeats):
            m.set_cell(name, float(rng.integers(1, 100)), **coords)
            m.slice_log.clear()
            t0 = time.perf_counter()
            m.recalc()
            times.append(time.perf_counter() - t0)
            touched.append(len(m.slice_log))
        t = statistics.median(times)
        k = statistics.median(touched)
        edits[label] = {"ms": t * 1000, "metrics": k, "ms_per_metric": t * 1000 / k if k else None}

    formulas = [x.formula for x in m.metrics.values() if x.formula is not None]
    scans = sum(len(s.names) for s in m._plan if s.scan_dim is not None)
    cells = sum(m.engine.size(m.raw(n_)) for n_ in m.metrics)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {"engine": engine_name, "metrics": n, "build": build_time, "compile": compile_time,
            "full": full, "first_edit": first_edit, "edits": edits, "formulas": len(formulas),
            "scan_metrics": scans, "delta_metrics": len(m._delta), "levels": len(m._levels),
            "cells": cells, "peak_mb": peak / 2**20}


def driver(sizes: list[int], reference_up_to: int) -> None:
    results = []
    for n in sizes:
        for engine in (["reference"] if n <= reference_up_to else []) + ["polars", "rust"]:
            print(f"running {engine} {n} ...", file=sys.stderr, flush=True)
            try:
                out = subprocess.run([sys.executable, __file__, "--engine", engine, "--metrics", str(n)],
                                     capture_output=True, text=True, timeout=3600, check=True)
                results.append(json.loads(out.stdout))
            except subprocess.TimeoutExpired:
                results.append({"engine": engine, "metrics": n, "timeout": True})
            except subprocess.CalledProcessError as e:
                print(e.stderr, file=sys.stderr)
                results.append({"engine": engine, "metrics": n, "error": e.stderr[-800:]})
    with open("bench_metrics_results.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    report(results)


def report(results: list[dict]) -> None:
    labels = [label for label, _, _ in EDITS]
    print("| エンジン | 計算 Metric 数 | 全セル数 | 計画作成 | 全体の再計算 | 直後の最初の変更 | "
          + " | ".join(f"{l}（影響数 / 時間 / 1 Metric あたり）" for l in labels) + " | ピークメモリ |")
    print("|---" * (7 + len(labels)) + "|")
    for r in results:
        if "edits" not in r:
            print(f"| {r['engine']} | {r['metrics']} | {'タイムアウト' if r.get('timeout') else 'エラー'} |")
            continue
        cols = []
        for l in labels:
            e = r["edits"][l]
            per = f"{e['ms_per_metric']:.2f} ms" if e["ms_per_metric"] is not None else "-"
            cols.append(f"{e['metrics']:.0f} / {e['ms']:,.1f} ms / {per}")
        print(f"| {r['engine']} | {r['metrics']:,} | {r['cells']:,} | {r['compile'] * 1000:,.0f} ms | "
              f"{r['full'] * 1000:,.0f} ms | {r['first_edit'] * 1000:,.0f} ms | " + " | ".join(cols)
              + f" | {r['peak_mb']:,.0f} MB |")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["reference", "polars", "rust"])
    ap.add_argument("--metrics", type=int)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--sizes", default="30,100,300,1000")
    ap.add_argument("--reference-up-to", type=int, default=100, help="参照実装はこの数までにする（遅いため）")
    ap.add_argument("--report", action="store_true", help="保存済みの結果を表にするだけ")
    args = ap.parse_args()
    if args.report:
        report(json.load(open("bench_metrics_results.json")))
    elif args.engine:
        print(json.dumps(run(args.engine, args.metrics, args.repeats), ensure_ascii=False))
    else:
        driver([int(x) for x in args.sizes.split(",")], args.reference_up_to)
