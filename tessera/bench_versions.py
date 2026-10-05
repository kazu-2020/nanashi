"""版を公開しながら書き込むときの費用を測る。

単一ライターの設計では、トランザクションごとに公開済みの版を複製し（fork）、複製に書き込んで
再計算し、それを次の版として公開する。公開済みの版は読み手が持っているので、書き込みは
格納データの書き込み時コピーを起こす。この費用を、版を持たずにその場で書き込む場合と比べる。

    python bench_versions.py              # 1000 Metric のモデルと損益計画（大）
    python bench_versions.py --model fpa  # 片方だけ
"""
from __future__ import annotations
from sparse_engine.named import Named

import argparse
import statistics
import time


def models(which: str):
    if which in ("metrics", "all"):
        import bench_metrics as bm
        m = bm.build("rust", 1000)
        label, name, coords = bm.EDITS[0]
        yield "1000 Metric、" + label, m, lambda i: m_edit(name, coords, i)
    if which in ("fpa", "all"):
        from examples.fpa import SIZES, build
        from sparse_engine.rust_engine import RustEngine
        m = build(RustEngine(), *SIZES["large"])
        emp = m.dimension("Employee").members[7]
        yield "損益計画（大）、給与を 1 人変更", m, lambda i: m_edit("Salary", {"Employee": emp, "Version": "予算"}, i)


def m_edit(name, coords, i):
    return lambda model: Named(model).set_cell(name, float(100 + i % 7), **coords)


def measure(model, edit, repeats: int) -> dict:
    model.recalc()
    for i in range(3):  # 準備運転
        edit(i)(model)
        model.recalc()

    in_place = []
    for i in range(repeats):
        edit(i)(model)
        t = time.perf_counter()
        model.recalc()
        in_place.append(time.perf_counter() - t)

    published = model
    forks, recalcs, totals = [], [], []
    for i in range(repeats):
        t0 = time.perf_counter()
        working = published.fork()  # 公開済みの版は published が持ち続ける
        t1 = time.perf_counter()
        edit(i)(working)
        working.recalc()
        t2 = time.perf_counter()
        forks.append(t1 - t0)
        recalcs.append(t2 - t1)
        totals.append(t2 - t0)
        published = working  # 次の版として公開する（古い版は捨てる）

    ms = lambda xs: round(1e3 * statistics.median(xs), 2)
    return {"その場で書き込む": ms(in_place), "版を公開しながら": ms(totals),
            "うち複製": ms(forks), "うち書き込みと再計算": ms(recalcs)}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", choices=["metrics", "fpa", "all"], default="all")
    p.add_argument("--repeats", type=int, default=10)
    args = p.parse_args()
    for label, model, edit in models(args.model):
        print(label, measure(model, edit, args.repeats))


if __name__ == "__main__":
    main()
