"""Measure time and memory of the profit and loss plan at increasing sizes, under a memory limit.

    python bench_scale.py run --scales 1,2,4,8,16,32 -o before.json   # stop at the first size over the limit
    python bench_scale.py compare before.json after.json        # Markdown table of the 2 results

Scale 1 is the large profit and loss plan (examples/fpa.py, 4.9 million cells). Scale k multiplies the
employees and the products by k. Each size runs in a new process. A watcher reads the RSS of that process
and its children, and stops it when the RSS is more than --limit-gb (8 GB is the default).
The first size over the limit is the limit of 1 machine for this model.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

GB = 2**30
POLL = 0.1  # seconds between 2 reads of the RSS


def tree_rss(ps_output: str, root: int) -> int:
    """Return the total RSS in bytes of root and its descendants, from `ps -A -o pid=,ppid=,rss=`."""
    children: dict[int, list[int]] = {}
    rss: dict[int, int] = {}
    for line in ps_output.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        pid, ppid, kib = map(int, parts)
        children.setdefault(ppid, []).append(pid)
        rss[pid] = kib * 1024
    total, todo = 0, [root]
    while todo:
        pid = todo.pop()
        total += rss.get(pid, 0)
        todo += children.get(pid, [])
    return total


def compare(before: list[dict], after: list[dict]) -> str:
    """Return a Markdown table of the phases that are in both results, with the ratio after / before."""
    old = {(r["scale"], p): v for r in before if r["status"] == "ok" for p, v in r["phases"].items()}
    status = {r["scale"]: r["status"] for r in before}
    lines = ["| 規模 | 段階 | 時間 前 → 後 | ヒープの最大 前 → 後 |", "|---|---|---|---|"]
    for r in after:
        if r["status"] != "ok" or status.get(r["scale"], "ok") != "ok":
            lines.append(f"| ×{r['scale']} | {status.get(r['scale'], '-')} → {r['status']} | | |")
            continue
        for phase, v in r["phases"].items():
            o = old.get((r["scale"], phase))
            if o is None:
                continue
            lines.append(f"| ×{r['scale']} | {phase} | {_ms(o['seconds'])} → {_ms(v['seconds'])} ({_ratio(o['seconds'], v['seconds'])})"
                         f" | {o['heap_peak'] / 1e6:,.0f} → {v['heap_peak'] / 1e6:,.0f} MB ({_ratio(o['heap_peak'], v['heap_peak'])}) |")
    return "\n".join(lines)


def table(results: list[dict]) -> str:
    """Return a Markdown table of 1 result."""
    lines = ["| 規模 | セル数 | 段階 | 時間 | ヒープの最大 | 段階の後のヒープ | RSS の最大 |", "|---|---|---|---|---|---|---|"]
    for r in results:
        if r["status"] != "ok":
            lines.append(f"| ×{r['scale']} | | {r['status']} | | | | {r['rss_max'] / GB:.1f} GB |")
            continue
        for phase, v in r["phases"].items():
            lines.append(f"| ×{r['scale']} | {r['cells']:,} | {phase} | {_ms(v['seconds'])} | {v['heap_peak'] / 1e6:,.0f} MB"
                         f" | {v['heap_after'] / 1e6:,.0f} MB | {r['rss_max'] / GB:.1f} GB |")
    return "\n".join(lines)


def _ms(s: float) -> str:
    return f"{s * 1e3:,.0f} ms" if s >= 0.01 else f"{s * 1e3:.2f} ms"


def _ratio(a: float, b: float) -> str:
    return f"×{b / a:.2f}" if a else "-"


def measure(scale: int, repeats: int) -> dict:
    """Build the model at scale, and measure each phase. Run this in the child process."""
    import nanashi_core
    from examples.fpa import EDITS, SIZES, build
    from sparse_engine.rust_engine import RustEngine

    phases: dict[str, dict] = {}

    def phase(name: str, fn, n: int = 1, prepare=lambda: None):
        times, peaks = [], []
        for _ in range(n):
            prepare()
            nanashi_core.reset_heap_peak()
            t = time.perf_counter()
            result = fn()
            times.append(time.perf_counter() - t)
            peaks.append(nanashi_core.heap()[1])
        phases[name] = {"seconds": statistics.median(times), "heap_peak": max(peaks), "heap_after": nanashi_core.heap()[0]}
        return result

    employees, products, months = SIZES["large"]
    m = phase("build", lambda: build(RustEngine(), employees * scale, products * scale, months))
    phase("full recalculation", m.recalc)
    cells = sum(m.engine.size(m.raw(n)) for n in m.metrics)  # before the edits, which add employees
    rng = random.Random(1)
    for label, edit in EDITS:  # time only the recalculation, the same as examples/fpa.py
        phase(label, m.recalc, repeats, prepare=lambda: edit(m, rng))
    return {"scale": scale, "status": "ok", "cells": cells, "phases": phases,
            "rss_self_max": _max_rss()}


def run_guarded(scale: int, repeats: int, limit: int) -> dict:
    """Run measure(scale) in a child process. Stop the child if its RSS tree is more than limit bytes."""
    child = subprocess.Popen([sys.executable, __file__, "child", str(scale), str(repeats)],
                             stdout=subprocess.PIPE, text=True)  # stderr goes to the terminal, so a full pipe cannot stop the child
    peak, status = 0, None
    try:
        while child.poll() is None:
            ps = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss="], capture_output=True, text=True)
            if ps.returncode != 0 or not ps.stdout.strip():  # without the watcher, a sweep can fill the memory
                raise RuntimeError(f"ps で RSS を読めません: {ps.stderr.strip()}")
            peak = max(peak, tree_rss(ps.stdout, child.pid))
            if peak > limit:
                status = "over_limit"
                break
            time.sleep(POLL)
    finally:
        if child.poll() is None:
            child.kill()
    out, _ = child.communicate()
    if status is None and child.returncode != 0:
        status = "error"
    if status is not None:
        return {"scale": scale, "status": status, "rss_max": peak}
    r = json.loads(out.splitlines()[-1])
    return {**r, "rss_max": max(peak, r.pop("rss_self_max"))}  # the poll can miss a short peak


def _max_rss() -> int:
    """The largest RSS of this process in bytes (Linux gives KiB, macOS gives bytes)."""
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r if sys.platform == "darwin" else r * 1024


def environment() -> dict:
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "."], capture_output=True, text=True).stdout)
    return {"git": sha + ("-dirty" if dirty else ""), "machine": platform.platform(), "cpus": os.cpu_count(),
            "python": platform.python_version()}


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--scales", default="1,2,4,8,16,32")
    r.add_argument("--repeats", type=int, default=5)
    r.add_argument("--limit-gb", type=float, default=8)
    r.add_argument("-o", "--output", default="bench_scale_results.json")
    c = sub.add_parser("compare")
    c.add_argument("before")
    c.add_argument("after")
    ch = sub.add_parser("child")
    ch.add_argument("scale", type=int)
    ch.add_argument("repeats", type=int)
    args = ap.parse_args()

    if args.cmd == "child":
        print(json.dumps(measure(args.scale, args.repeats), ensure_ascii=False))
    elif args.cmd == "compare":
        load = lambda path: json.loads(Path(path).read_text())["results"]
        print(compare(load(args.before), load(args.after)))
    else:
        results = []
        for scale in map(int, args.scales.split(",")):
            print(f"×{scale} ...", file=sys.stderr, flush=True)
            results.append(run_guarded(scale, args.repeats, int(args.limit_gb * GB)))
            with open(args.output, "w") as f:  # write after each size, so that a stopped sweep keeps its results
                json.dump({"environment": environment(), "limit_gb": args.limit_gb, "results": results}, f,
                          ensure_ascii=False, indent=2)
            if results[-1]["status"] != "ok":
                break
        print(table(results))


if __name__ == "__main__":
    main()
