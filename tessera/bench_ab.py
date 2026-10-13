"""Compare a benchmark between a git ref (base) and a tessera checkout (head).

    python bench_ab.py --base origin/main -- bench.py --engine rust --size large
    python bench_ab.py --self-test

The script builds both sides first, then runs base and head alternately.
It prints the environment, the error count, and a table of median (min-max) per metric.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


# Calculations

def flatten(obj, prefix=""):
    """Return the numeric leaves of a JSON value as {"a.b.0": number}. Bools are not numbers here."""
    if isinstance(obj, dict):
        items = obj.items()
    elif isinstance(obj, list):
        items = enumerate(obj)
    else:
        ok = isinstance(obj, (int, float)) and not isinstance(obj, bool)
        return {prefix: obj} if ok else {}
    return {k: v for key, val in items for k, v in flatten(val, f"{prefix}.{key}" if prefix else str(key)).items()}


def parse_metrics(stdout):
    """Return the numeric leaves of the last non-empty stdout line, or {} if it is not JSON."""
    lines = [s for s in stdout.splitlines() if s.strip()]
    try:
        return flatten(json.loads(lines[-1])) if lines else {}
    except json.JSONDecodeError:
        return {}


def summarize(runs):
    """Return {side: {metric: (median, min, max)}} from the runs without an error."""
    values = {}
    for r in runs:
        if r["rc"] == 0:
            for k, v in r["metrics"].items():
                values.setdefault(r["side"], {}).setdefault(k, []).append(v)
    return {side: {k: (statistics.median(v), min(v), max(v)) for k, v in m.items()} for side, m in values.items()}


def change_pct(base, head):
    return None if base == 0 else (head - base) / base * 100


def render(summary):
    base, head = summary.get("base", {}), summary.get("head", {})
    cell = lambda s: f"{s[0]:.4g} ({s[1]:.4g}–{s[2]:.4g})"
    rows = ["| metric | base median (min–max) | head median (min–max) | change % | ranges overlap |",
            "|---|---|---|---|---|"]
    for k in sorted(set(base) & set(head)):
        d = change_pct(base[k][0], head[k][0])
        # If the ranges overlap, the change is not larger than the run-to-run noise.
        overlap = "yes" if base[k][1] <= head[k][2] and head[k][1] <= base[k][2] else "no"
        rows.append(f"| {k} | {cell(base[k])} | {cell(head[k])} | {'-' if d is None else f'{d:+.1f}'} | {overlap} |")
    # Keys such as ids change in each run, thus only the keys of both sides are compared.
    only = len(set(base) ^ set(head))
    return "\n".join(rows) + (f"\n\n{only} metrics are only on one side and are not shown." if only else "")


def self_test():
    assert flatten({"a": {"b": [1, 2.5]}, "c": True, "d": "x", "e": 3}) == {"a.b.0": 1, "a.b.1": 2.5, "e": 3}
    assert parse_metrics("log\n{\"x\": 1}\n\n") == {"x": 1} and parse_metrics("not json") == {}
    runs = [{"side": "base", "rc": 0, "metrics": {"t": t}} for t in (3, 1, 2)]
    runs.append({"side": "base", "rc": 1, "metrics": {"t": 100}})
    assert summarize(runs) == {"base": {"t": (2, 1, 3)}}
    assert change_pct(2, 3) == 50.0 and change_pct(0, 1) is None
    table = render({"base": {"t": (2, 1, 3), "id1": (1, 1, 1)}, "head": {"t": (3, 3, 3), "id2": (1, 1, 1)}})
    assert "| t | 2 (1–3) | 3 (3–3) | +50.0 | yes |" in table and "2 metrics are only on one side" in table
    print("self-test ok")


# Actions

def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def base_dir(ref):
    """Make (or reuse) a detached worktree of ref at <repo root>/.bench/<short sha>."""
    root = Path(sh("git", "rev-parse", "--show-toplevel", cwd=HERE))
    sha = sh("git", "rev-parse", "--short", f"{ref}^{{commit}}", cwd=HERE)
    wt = root / ".bench" / sha
    if not wt.exists():
        sh("git", "worktree", "prune", cwd=root)  # forget a worktree whose directory was deleted
        sh("git", "worktree", "add", "--detach", str(wt), sha, cwd=root)
    return sha, wt / HERE.relative_to(root)


def build(t):
    venv = t / ".venv"
    if not venv.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        subprocess.run([venv / "bin/pip", "install", "-q", "-e", f"{t}[dev]"], check=True)
    start = time.perf_counter()
    subprocess.run([venv / "bin/maturin", "develop", "--release", "-m", t / "native/Cargo.toml"],
                   check=True, stdout=sys.stderr,
                   # One target dir per side. If the sides share one, cargo can take a stale build as fresh.
                   env={**os.environ, "VIRTUAL_ENV": str(venv), "CARGO_TARGET_DIR": str(t / "native/target")})
    print(f"built {t} in {time.perf_counter() - start:.1f} s", file=sys.stderr)


def run_once(t, args):
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        start = time.perf_counter()
        p = subprocess.Popen([t / ".venv/bin/python", *args], cwd=t, stdout=out, stderr=err)
        _, status, usage = os.wait4(p.pid, 0)
        wall = time.perf_counter() - start
        out.seek(0)
        err.seek(0)
        # ru_maxrss is in bytes on macOS and in KiB on Linux.
        rss_mb = usage.ru_maxrss / (2**20 if sys.platform == "darwin" else 2**10)
        return os.waitstatus_to_exitcode(status), wall, rss_mb, out.read(), err.read()


def environment(args, sha, head):
    cpu = (sh("sysctl", "-n", "machdep.cpu.brand_string") if sys.platform == "darwin" else
           next((ln.split(":", 1)[1].strip() for ln in open("/proc/cpuinfo") if ln.startswith("model name")), "?"))
    return [f"base: {args.base} ({sha})", f"head: {head} ({sh('git', 'describe', '--always', '--dirty', cwd=head)})",
            f"cpu: {cpu}, nproc {os.cpu_count()}", f"python {platform.python_version()}, {sh('rustc', '--version')}",
            f"command: python {' '.join(args.bench)}, {args.runs} runs per side, alternating"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", help="git ref of the base side")
    ap.add_argument("--head", type=Path, default=HERE, help="tessera dir of the head side (default: this checkout)")
    ap.add_argument("--runs", type=int, default=5, help="runs per side")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("bench", nargs="*", help="benchmark command after --, for example bench.py --size small")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.base or not args.bench:
        ap.error("give --base REF and a benchmark command after --")
    if args.runs < 1:
        ap.error("--runs must be 1 or more")
    sha, base = base_dir(args.base)
    head = args.head.resolve()
    sides = {"base": base, "head": head}
    for t in sides.values():
        build(t)
    env = environment(args, sha, head)  # before the runs, so that a failure here does not lose them
    load_before = os.getloadavg()
    runs = []
    for i in range(2 * args.runs):
        side = ("base", "head")[i % 2]
        rc, wall, rss, out, err = run_once(sides[side], args.bench)
        metrics = parse_metrics(out)
        if rc == 0 and not metrics:
            print(f"{side}: the last line of the output is not JSON, thus only wall_s and max_rss_mb",
                  file=sys.stderr)
        runs.append({"side": side, "dir": str(sides[side]), "run": i // 2, "rc": rc,
                     "metrics": {**metrics, "wall_s": wall, "max_rss_mb": rss},  # the harness values win
                     **({"stderr_tail": err[-2000:]} if rc else {})})
        print(f"{side} run {i // 2}: rc {rc}, {wall:.2f} s", file=sys.stderr, flush=True)
    load_after = os.getloadavg()
    with open(head / "bench_ab_results.jsonl", "a") as f:
        f.writelines(json.dumps({"base": sha, **r}) + "\n" for r in runs)
    errors = {s: sum(r["rc"] != 0 for r in runs if r["side"] == s) for s in sides}
    print("\n".join(env))
    print(f"load average: before {load_before[0]:.2f}, after {load_after[0]:.2f}")
    print(f"errors: base {errors['base']}, head {errors['head']}")
    for r in runs:
        if r["rc"]:
            print(f"--- {r['side']} run {r['run']} stderr ---\n{r['stderr_tail']}")
    print()
    print(render(summarize(runs)))
    sys.exit(1 if any(errors.values()) else 0)


if __name__ == "__main__":
    main()
