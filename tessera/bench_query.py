"""Compare the read query (Model.summarize) with DuckDB and Polars on the same cells.

    python bench_query.py --k 8        # the profit and loss plan (large) with k times the employees and products

DuckDB and Polars get the member numbers as u32 columns and the values as f64 (one table for each Metric).
Each round runs tessera, DuckDB and Polars one after the other, in a different order each round, and the table
shows the median and the range. Before the rounds, the script checks that the 3 give the same groups and values.
The SUM of tessera is exact (correctly rounded); DuckDB and Polars add in f64, so their sums can be different in
the last bits.
"""
from __future__ import annotations

import argparse
import math
import statistics
import sys
import time

import numpy as np

from examples.fpa import SIZES, build
from sparse_engine.rust_engine import RustEngine

try:
    import duckdb
    import polars as pl
    import pyarrow  # polars gives its tables to DuckDB as Arrow
except ImportError:
    sys.exit("bench_query.py needs duckdb, polars and pyarrow: .venv/bin/pip install duckdb polars pyarrow")

PAYROLL, REVENUE = "PayrollByEmployee", "Revenue"


def columns(m, name: str):
    """The cells of a Metric as a Polars table of member numbers (one u32 column per dimension) and v."""
    model = m.model
    mt = model.metric(m.metric(name).id)
    cols, values, _ = model.engine.core.rows(m.raw(name))
    table = {model.dimension(d).name: np.asarray(c, np.uint32) for d, c in zip(mt.dims, cols)}
    return pl.DataFrame({**table, "v": np.asarray(values)})


def cases(m, frames):
    """(label, tessera, DuckDB SQL, Polars) for each query. tessera reads through the Model API (ids), as the
    HTTP server does."""
    model = m.model
    pay, rev = m.metric(PAYROLL).id, m.metric(REVENUE).id
    dim = {n: m.dimension_id(n) for n in ["Employee", "Version", "Month"]}
    version, month = m.dimension("Version"), m.dimension("Month")
    budget = version.id_of("予算")
    bud = version._by_id[budget]
    employees = [m.dimension("Employee").ids[i] for i in range(1000)]
    months12 = [month.ids[i] for i in range(12)]
    p, r = frames[PAYROLL], frames[REVENUE]
    by = lambda *ds: [dim[d] for d in ds]
    return [
        ("sum by Month, Version=予算",
         lambda: [model.summarize(pay, {dim["Version"]: [budget]}, keep=by("Month"))],
         f"SELECT 0, Month, sum(v) FROM {PAYROLL} WHERE Version={bud} GROUP BY Month",
         lambda: [p.filter(pl.col("Version") == bud).group_by("Month").agg(pl.col("v").sum())]),
        ("sum by Employee (all)",
         lambda: [model.summarize(pay, keep=by("Employee"))],
         f"SELECT 0, Employee, sum(v) FROM {PAYROLL} GROUP BY Employee",
         lambda: [p.group_by("Employee").agg(pl.col("v").sum())]),
        ("sum by Version,Month, 1000 Employees",
         lambda: [model.summarize(pay, {dim["Employee"]: employees}, keep=by("Version", "Month"))],
         f"SELECT 0, Version, Month, sum(v) FROM {PAYROLL} WHERE Employee < 1000 GROUP BY Version, Month",
         lambda: [p.filter(pl.col("Employee") < 1000).group_by("Version", "Month").agg(pl.col("v").sum())]),
        ("total, Month in m01..m12",
         lambda: [model.summarize(pay, {dim["Month"]: months12})],
         f"SELECT 0, sum(v) FROM {PAYROLL} WHERE Month < 12",
         lambda: [p.filter(pl.col("Month") < 12).select(pl.col("v").sum())]),
        ("avg by Version",
         lambda: [model.summarize(pay, keep=by("Version"), agg="avg")],
         f"SELECT 0, Version, avg(v) FROM {PAYROLL} GROUP BY Version",
         lambda: [p.group_by("Version").agg(pl.col("v").mean())]),
        ("2 Metrics: sum by Month, Version=予算",
         lambda: [model.summarize(x, {dim["Version"]: [budget]}, keep=by("Month")) for x in (pay, rev)],
         f"SELECT 0, Month, sum(v) FROM {PAYROLL} WHERE Version={bud} GROUP BY Month UNION ALL "
         f"SELECT 1, Month, sum(v) FROM {REVENUE} WHERE Version={bud} GROUP BY Month",
         lambda: [x.filter(pl.col("Version") == bud).group_by("Month").agg(pl.col("v").sum()) for x in (p, r)]),
    ]


def groups_of_tessera(m, summaries) -> list[dict]:
    """{member numbers: value} for each Summary."""
    out = []
    for s in summaries:
        numbers = [[m.model.dimension(d)._by_id[x] for x in c] for d, c in zip(s.dims, s.coords)]
        out.append(dict(zip(zip(*numbers) if numbers else [()] * len(s.values), s.values.tolist())))
    return out


def same(a: dict, b: dict) -> bool:
    return a.keys() == b.keys() and all(math.isclose(a[k], b[k], rel_tol=1e-9, abs_tol=1e-6) for k in a)


def check(m, con, label, te, sql, po) -> int:
    """Exit if the 3 results do not have the same groups and values. Returns the number of groups."""
    want = groups_of_tessera(m, te())
    duck = [{} for _ in want]
    for row in con.execute(sql).fetchall():
        duck[row[0]][tuple(row[1:-1])] = row[-1]
    polars = [{tuple(r[:-1]): r[-1] for r in f.iter_rows()} for f in po()]
    if not all(same(w, d) and same(w, p) for w, d, p in zip(want, duck, polars, strict=True)):
        sys.exit(f"{label}: the results are different\n{want}\n{duck}\n{polars}")
    return sum(map(len, want))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=1, help="k times the employees and products of large")
    ap.add_argument("--rounds", type=int, default=7)
    ap.add_argument("--threads", type=int, default=4, help="DuckDB threads")
    args = ap.parse_args()
    e, p, mo = SIZES["large"]
    t0 = time.perf_counter()
    m = build(RustEngine(), e * args.k, p * args.k, mo)
    m.recalc()
    frames = {name: columns(m, name) for name in (PAYROLL, REVENUE)}
    con = duckdb.connect()
    con.execute(f"SET threads={args.threads}")
    for name, f in frames.items():
        con.register("t", f.to_arrow())
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM t")
        con.unregister("t")
    print(f"k={args.k}: {frames[PAYROLL].height:,} cells in {PAYROLL}, {frames[REVENUE].height:,} in {REVENUE}"
          f" (build {time.perf_counter() - t0:.0f} s), duckdb {duckdb.__version__}, polars {pl.__version__},"
          f" pyarrow {pyarrow.__version__}")
    qs = cases(m, frames)
    groups = [check(m, con, *q) for q in qs]
    times = {q[0]: ([], [], []) for q in qs}
    for r in range(args.rounds):
        for label, te, sql, po in qs:
            sides = [te, lambda: con.execute(sql).fetchnumpy(), po]
            for i in [(r + j) % 3 for j in range(3)]:
                t = time.perf_counter()
                sides[i]()
                times[label][i].append((time.perf_counter() - t) * 1e3)
    show = lambda ts: f"{statistics.median(ts):.1f} ({min(ts):.1f} to {max(ts):.1f})"
    print(f"\nmedian of {args.rounds} rounds, ms (range)\n")
    print("| query | groups | tessera | DuckDB | Polars |\n|---|---|---|---|---|")
    for (label, *_), g in zip(qs, groups):
        print(f"| {label} | {g:,} | " + " | ".join(show(ts) for ts in times[label]) + " |")


if __name__ == "__main__":
    main()
