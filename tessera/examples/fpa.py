"""予算・実績・見込みを持つ損益計画と人員計画のモデル（サンプル兼ベンチマーク）。

    python -m examples.fpa                      # 規模ごとに全体の再計算と典型的な変更の時間を測る
    python -m examples.fpa --size small --show  # 小さい規模で主要な Metric を表示する

使っている機能:
- 版の軸（予算 / 実績 / 見込み）と、版ごとの式（IF(Version = Version."見込み", ...)）
- 締め月（メンバー型の入力 Cutoff）で、見込みを「締め月までは実績、それ以降は予算」にする
- 入社月・退職月（メンバー型）による在籍判定と、月ごとの所属（異動）による部署別の集計
- 年初からの累計と資金残高（PREVIOUS による scan）、予実差（SELECT）
"""
from __future__ import annotations

import argparse
import random
import statistics
import time

from sparse_engine import Model, Named
from sparse_engine.engine import engine_for

VERSIONS = ["予算", "実績", "見込み"]
DEPARTMENTS = ["営業", "開発", "管理"]
CATEGORIES = ["ハード", "ソフト", "サービス"]
SIZES = {  # 社員数, 商品数, 月数
    "small": (60, 30, 24),
    "medium": (2_000, 500, 36),
    "large": (20_000, 5_000, 36),
}

FORMULAS = [
    # 見込み: 締め月までは実績、それ以降は予算。明細（売上・人件費・経費）の段で切り替える
    ("IsActual", ["Month"], "Month <= Cutoff", "boolean"),
    ("InPeriod", ["Version", "Month"], 'IF(Version = Version."実績", IF(IsActual, 1), 1)'),  # 実績は締め月まで
    # --- 売上と粗利
    ("RevenueRaw", ["Product", "Version", "Month"], "Units * Price * InPeriod"),
    ("Revenue", ["Product", "Version", "Month"],
     'IF(Version = Version."見込み", IF(IsActual, RevenueRaw[SELECT: Version."実績"], RevenueRaw[SELECT: Version."予算"]), RevenueRaw)'),
    ("GrossByProduct", ["Product", "Version", "Month"], "Revenue * (1 - CogsRate[BY: Product.Category])"),
    ("GrossProfit", ["Version", "Month"], "GrossByProduct[REMOVE SUM: Product]"),
    ("RevenueByCategory", ["Category", "Version", "Month"], "Revenue[BY SUM: Product.Category]"),
    # --- 人件費: 在籍月だけ、月ごとの所属部署へ
    ("Employed", ["Employee", "Month"],
     "Month >= HireMonth AND (Month < LeaveMonth OR ISBLANK(LeaveMonth)[EXPAND: Month])", "boolean"),
    ("PayrollRaw", ["Employee", "Version", "Month"], "Salary * (1 + BenefitRate) * IF(Employed, 1) * InPeriod"),
    ("PayrollByEmployee", ["Employee", "Version", "Month"],
     'IF(Version = Version."見込み", IF(IsActual, PayrollRaw[SELECT: Version."実績"], PayrollRaw[SELECT: Version."予算"]), PayrollRaw)'),
    ("Payroll", ["Department", "Version", "Month"], "PayrollByEmployee[BY SUM: Employee.DeptOf]"),
    ("Headcount", ["Department", "Version", "Month"], "PayrollByEmployee[BY COUNT: Employee.DeptOf]"),
    ("OtherOpexF", ["Department", "Version", "Month"],
     'IF(Version = Version."見込み", IF(IsActual, OtherOpex[SELECT: Version."実績"], OtherOpex[SELECT: Version."予算"]), OtherOpex)'),
    ("Opex", ["Department", "Version", "Month"], "Payroll + OtherOpexF"),
    # --- 営業利益、累計、資金、予実差
    ("OperatingIncome", ["Version", "Month"], "GrossProfit - Opex[REMOVE SUM: Department]"),
    ("IncomeYTD", ["Version", "Month"], "PREVIOUS(Month) + OperatingIncome"),
    ("Cash", ["Version", "Month"], 'PREVIOUS(Month) + OperatingIncome + IF(Month = Month."m01", OpeningCash)[EXPAND: Version]'),
    ("FullYear", ["Version"], "OperatingIncome[REMOVE SUM: Month]"),
    ("Variance", ["Month"], 'OperatingIncome[SELECT: Version."見込み"] - OperatingIncome[SELECT: Version."予算"]'),
]


def build(engine=None, employees: int = 60, products: int = 30, months: int = 24, seed: int = 0) -> Model:
    rng = random.Random(seed)
    m = Named(Model(engine=engine)) if engine is not None else Named(Model())
    month_names = [f"m{i:02d}" for i in range(1, months + 1)]
    emps = [f"e{i:05d}" for i in range(employees)]
    prods = [f"p{i:04d}" for i in range(products)]
    cutoff = months // 4  # 締め月（この月までは実績がある）

    m.add_dimension("Version", VERSIONS)
    m.add_dimension("Month", month_names, ordered=True)
    m.add_dimension("Department", DEPARTMENTS)
    m.add_dimension("Category", CATEGORIES)
    m.add_dimension("Employee", emps)
    m.add_dimension("Product", prods)
    m.add_property("Product", "Category", "Category", {p: CATEGORIES[i % 3] for i, p in enumerate(prods)})

    m.add_input("Cutoff", [], {(): month_names[cutoff - 1]}, kind="member:Month")
    m.add_input("BenefitRate", [], {(): 0.15})
    m.add_input("OpeningCash", [], {(): 50_000.0})
    m.add_input("CogsRate", ["Category"], {("ハード",): 0.6, ("ソフト",): 0.2, ("サービス",): 0.4})

    units, price = {}, {}
    for p in prods:
        base = rng.randint(10, 200)
        price[(p, "予算")] = float(rng.randint(10, 100))
        price[(p, "実績")] = price[(p, "予算")] * rng.choice([0.9, 1.0, 1.1])
        for i, t in enumerate(month_names):
            units[(p, "予算", t)] = float(base + rng.randint(-5, 5))
            if i < cutoff:
                units[(p, "実績", t)] = float(base + rng.randint(-20, 20))
    m.add_input("Units", ["Product", "Version", "Month"], units)
    m.add_input("Price", ["Product", "Version"], price)

    salary, dept, hire, leave = {}, {}, {}, {}
    for i, e in enumerate(emps):
        salary[(e, "予算")] = float(rng.randint(300, 800))
        salary[(e, "実績")] = salary[(e, "予算")]
        hire[(e,)] = month_names[0] if rng.random() < 0.8 else rng.choice(month_names)
        if rng.random() < 0.1:
            leave[(e,)] = rng.choice(month_names)
        home = DEPARTMENTS[i % 3]
        move = rng.randrange(months) if rng.random() < 0.1 else None  # 1 割が途中で異動
        for j, t in enumerate(month_names):
            dept[(e, t)] = DEPARTMENTS[(i + 1) % 3] if move is not None and j >= move else home
    m.add_input("Salary", ["Employee", "Version"], salary)
    m.add_input("DeptOf", ["Employee", "Month"], dept, kind="member:Department")
    m.add_input("HireMonth", ["Employee"], hire, kind="member:Month")
    m.add_input("LeaveMonth", ["Employee"], leave, kind="member:Month")
    m.add_input("OtherOpex", ["Department", "Version", "Month"],
                {(d, v, t): float(rng.randint(1_000, 5_000)) for d in DEPARTMENTS for v in ["予算", "実績"]
                 for i, t in enumerate(month_names) if v == "予算" or i < cutoff})

    for name, dims, formula, *kind in FORMULAS:
        m.add_formula(name, dims, formula, kind=kind[0] if kind else "number")
    return m


EDITS = [  # (説明, 変更を加える関数)
    ("給与を 1 人変更", lambda m, r: m.set_cell("Salary", float(r.randint(300, 900)),
                                          Employee=r.choice(m.dimension("Employee").members), Version="予算")),
    ("1 人を異動", lambda m, r: m.set_cell("DeptOf", r.choice(DEPARTMENTS),
                                       Employee=r.choice(m.dimension("Employee").members),
                                       Month=r.choice(m.dimension("Month").members))),
    ("販売数量を 1 か所入力", lambda m, r: m.set_cell("Units", float(r.randint(10, 200)),
                                             Product=r.choice(m.dimension("Product").members), Version="予算",
                                             Month=r.choice(m.dimension("Month").members))),
    ("締め月を 1 か月進める", lambda m, r: _advance_cutoff(m)),
    ("社員を 1 人追加", lambda m, r: _hire(m, r)),
]


def _advance_cutoff(m: Model) -> None:
    months = m.dimension("Month").members
    i = months.index(m.get("Cutoff"))
    m.set_cell("Cutoff", months[i + 1] if i + 1 < len(months) - 1 else months[0])  # 最後まで来たら最初に戻す


def _hire(m: Model, r: random.Random) -> None:
    name = f"new{len(m.dimension('Employee').members)}"
    m.add_member("Employee", name)
    m.set_cell("Salary", float(r.randint(300, 800)), Employee=name, Version="予算")
    m.set_cell("HireMonth", r.choice(m.dimension("Month").members), Employee=name)
    for t in m.dimension("Month").members:
        m.set_cell("DeptOf", "開発", Employee=name, Month=t)


def bench(size: str, engine_name: str, repeats: int) -> dict:
    t0 = time.perf_counter()
    m = build(engine_for(engine_name), *SIZES[size])
    load = time.perf_counter() - t0
    t0 = time.perf_counter()
    m.recalc()
    full = time.perf_counter() - t0
    cells = sum(m.engine.size(m.raw(n)) for n in m.metrics)
    rng = random.Random(1)
    edits = {}
    for label, edit in EDITS:
        times, touched = [], []
        for _ in range(repeats):
            edit(m, rng)
            m.slice_log.clear()
            t0 = time.perf_counter()
            m.recalc()
            times.append(time.perf_counter() - t0)
            touched.append(len(m.slice_log))
        edits[label] = (statistics.median(times) * 1000, statistics.median(touched))
    return {"size": size, "engine": engine_name, "load": load, "full": full, "cells": cells, "edits": edits}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", choices=list(SIZES))
    ap.add_argument("--engine", choices=["reference", "rust"], default="rust")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    if args.show:
        m = build(None, *SIZES[args.size or "small"])
        for name in ["OperatingIncome", "Variance", "Cash", "Headcount"]:
            order = {d.name: d for d in m.dimensions.values()}
            print(f"--- {name}\n{m.value(name).format(order)}\n")
        return
    for size in [args.size] if args.size else list(SIZES):
        r = bench(size, args.engine, args.repeats)
        print(f"\n{size}（{r['engine']}）: 全セル {r['cells']:,}、読み込み {r['load']:.1f} 秒、全体の再計算 {r['full'] * 1000:,.0f} ms")
        for label, (ms, n) in r["edits"].items():
            print(f"  {label}: {ms:,.1f} ms（再計算した Metric {n:.0f} 個）")


if __name__ == "__main__":
    main()
