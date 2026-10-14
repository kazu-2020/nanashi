"""Write a snapshot of the profit and loss plan without Python dicts, and measure a load of it.

    python bench_snapshot.py write --scale 32 /tmp/fpa32   # make the snapshot directory
    python bench_snapshot.py run /tmp/fpa32                 # load it, then time the recalculations

Scale k multiplies the employees and the products of SIZES["large"] (examples/fpa.py) by k.
build() keeps a Python tuple key for each input cell, so it uses about 73 B for each cell.
This script keeps the member numbers in array.array columns and writes the Parquet files of storage.py directly.
It uses the random stream in the same order as build(), so the snapshot holds the same model.
"""
from __future__ import annotations

import argparse
import json
import random
import resource
import statistics
import sys
import time
from array import array
from pathlib import Path

import nanashi_core

from examples.fpa import CATEGORIES, DEPARTMENTS, EDITS, FORMULAS, SIZES, VERSIONS
from sparse_engine import Model, Named
from sparse_engine.engine import parquet_columns, parquet_value
from sparse_engine.rust_engine import RustEngine
from sparse_engine.storage import FORMAT_VERSION, input_file

BIG = {  # the large inputs: dimensions and kind, as in build()
    "Units": (["Product", "Version", "Month"], "number"),
    "Price": (["Product", "Version"], "number"),
    "Salary": (["Employee", "Version"], "number"),
    "DeptOf": (["Employee", "Month"], "member:Department"),
    "HireMonth": (["Employee"], "member:Month"),
    "LeaveMonth": (["Employee"], "member:Month"),
}
REPEATS = 5  # runs of each edit, as in examples/fpa.py


# ---------------------------------------------------------------- calculations

def generate(employees: int, products: int, months: int, seed: int = 0) -> tuple[dict, dict]:
    """Return the large inputs as name -> (member number columns, values), and the OtherOpex cells.

    The calls to the random stream are in the same order as in examples.fpa.build. A member-type value is a
    member number."""
    rng = random.Random(seed)
    cutoff = months // 4
    budget, actual = VERSIONS.index("予算"), VERSIONS.index("実績")
    big = {name: ([array("I") for _ in dims], array("d")) for name, (dims, _) in BIG.items()}

    def row(name: str, key: tuple[int, ...], value: float) -> None:
        cols, values = big[name]
        for c, k in zip(cols, key):
            c.append(k)
        values.append(value)

    for p in range(products):
        base = rng.randint(10, 200)
        b = float(rng.randint(10, 100))
        row("Price", (p, budget), b)
        row("Price", (p, actual), b * rng.choice([0.9, 1.0, 1.1]))
        for t in range(months):
            row("Units", (p, budget, t), float(base + rng.randint(-5, 5)))
            if t < cutoff:
                row("Units", (p, actual, t), float(base + rng.randint(-20, 20)))

    (dept_e, dept_t), dept = big["DeptOf"]
    for i in range(employees):
        s = float(rng.randint(300, 800))
        row("Salary", (i, budget), s)
        row("Salary", (i, actual), s)
        row("HireMonth", (i,), 0 if rng.random() < 0.8 else rng.choice(range(months)))
        if rng.random() < 0.1:
            row("LeaveMonth", (i,), rng.choice(range(months)))
        home = i % 3
        move = rng.randrange(months) if rng.random() < 0.1 else None
        dept_e.extend([i] * months)
        dept_t.extend(range(months))
        dept.extend((i + 1) % 3 if move is not None and j >= move else home for j in range(months))

    month_names = _months(months)
    other_opex = {(d, v, t): float(rng.randint(1_000, 5_000)) for d in DEPARTMENTS for v in ["予算", "実績"]
                  for i, t in enumerate(month_names) if v == "予算" or i < cutoff}
    return big, other_opex


def table(r: dict) -> str:
    """Return a Markdown table of 1 result of run."""
    lines = [f"セル数 {r['cells']:,}、RSS の最大 {r['rss_max'] / 2**30:.2f} GB", "",
             "| 段階 | 時間 | ヒープの最大 | 段階の後のヒープ |", "|---|---|---|---|"]
    for phase, v in r["phases"].items():
        lines.append(f"| {phase} | {v['seconds'] * 1e3:,.1f} ms | {v['heap_peak'] / 1e6:,.0f} MB"
                     f" | {v['heap_after'] / 1e6:,.0f} MB |")
    return "\n".join(lines)


def _months(months: int) -> list[str]:
    return [f"m{i:02d}" for i in range(1, months + 1)]


# ---------------------------------------------------------------- actions

def write(path, employees: int, products: int, months: int, seed: int = 0) -> None:
    """Save the model of examples.fpa.build at these sizes to path, with the large inputs from generate."""
    big, other_opex = generate(employees, products, months, seed)
    month_names = _months(months)
    prods = [f"p{i:04d}" for i in range(products)]
    m = Named(Model(engine=RustEngine()))
    m.add_dimension("Version", VERSIONS)
    m.add_dimension("Month", month_names, ordered=True)
    m.add_dimension("Department", DEPARTMENTS)
    m.add_dimension("Category", CATEGORIES)
    m.add_dimension("Employee", [f"e{i:05d}" for i in range(employees)])
    m.add_dimension("Product", prods)
    m.add_property("Product", "Category", "Category", {p: CATEGORIES[i % 3] for i, p in enumerate(prods)})
    m.add_input("Cutoff", [], {(): month_names[months // 4 - 1]}, kind="member:Month")
    m.add_input("BenefitRate", [], {(): 0.15})
    m.add_input("OpeningCash", [], {(): 50_000.0})
    m.add_input("CogsRate", ["Category"], {("ハード",): 0.6, ("ソフト",): 0.2, ("サービス",): 0.4})
    for name, (dims, kind) in BIG.items():
        m.add_input(name, dims, {}, kind=kind)
    m.add_input("OtherOpex", ["Department", "Version", "Month"], other_opex)
    for name, dims, formula, *kind in FORMULAS:
        m.add_formula(name, dims, formula, kind=kind[0] if kind else "number")
    m.save(path)
    for name, (cols, values) in big.items():
        x = m.metric(name)
        meta = [("nanashi", json.dumps({"format": FORMAT_VERSION, "metric": x.id}))]
        data = nanashi_core.write_parquet(parquet_columns(x.dims, m.model), cols, values, parquet_value(x.kind), meta)
        (Path(path) / input_file(x.id)).write_bytes(data)


def run(path) -> dict:
    """Load the snapshot in path, then measure the full recalculation and the edits of examples/fpa.py."""
    phases: dict[str, dict] = {}

    def phase(name: str, fn, n: int = 1, prepare=lambda: None):
        times, peaks, out = [], [], None
        for _ in range(n):
            prepare()
            nanashi_core.reset_heap_peak()
            t = time.perf_counter()
            out = fn()
            times.append(time.perf_counter() - t)
            peaks.append(nanashi_core.heap()[1])
        phases[name] = {"seconds": statistics.median(times), "heap_peak": max(peaks), "heap_after": nanashi_core.heap()[0]}
        return out

    m = Named(phase("読み込み", lambda: Model.load(path, RustEngine())))
    phase("全体の再計算", m.recalc)
    cells = sum(m.engine.size(m.raw(n)) for n in m.metrics)  # before the edits, which add employees
    rng = random.Random(1)
    for label, edit in EDITS:  # time only the recalculation, as examples/fpa.py does
        phase(label, m.recalc, REPEATS, prepare=lambda: edit(m, rng))
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    return {"path": str(path), "cells": cells, "phases": phases, "rss_max": rss}


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write")
    w.add_argument("--scale", type=int, required=True)
    w.add_argument("path")
    sub.add_parser("run").add_argument("path")
    args = ap.parse_args()
    if args.cmd == "write":
        employees, products, months = SIZES["large"]
        write(args.path, employees * args.scale, products * args.scale, months)
        return
    r = run(args.path)
    print(json.dumps(r, ensure_ascii=False))
    print(table(r))


if __name__ == "__main__":
    main()
