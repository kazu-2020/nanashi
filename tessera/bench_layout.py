"""Metric の数を増やしたときの、定義の変更と作り直しの費用を測る（分割軸の選択を含む）。

    python bench_layout.py                  # 鎖 2,000 / 5,000 / 20,000 本と、入力の多いモデル
    python bench_layout.py --sizes 5000     # 鎖の本数を指定する

- 鎖: x0 を入力とし、x{i} = x{i-1} + 1 を 1 つのトランザクションでまとめて足す。
- 入力の多いモデル: 入力 x{i} と式 y{i} = x{i} + 1 を 2,500 組。
どちらも、作り直し（計画を捨てて全体をコンパイルし、全 Metric を計算し直す。記録先から開くときや、
軸や種類を変えたときと同じ流れで、分割軸の選択を含む）の時間も測る。
"""
from __future__ import annotations

import argparse
import time

from sparse_engine import Model, Named
from sparse_engine.rust_engine import RustEngine


def rebuild_time(m: Model) -> float:
    """記録先から開くとき（journal.open）と同じく、計画を捨てて全体をコンパイルし、全 Metric を計算し直す時間。"""
    t = time.perf_counter()
    m._invalidate()
    m.recalc()
    return time.perf_counter() - t


def chain(n: int) -> tuple[float, float]:
    m = Named(Model(engine=RustEngine()))
    m.add_dimension("T", ["a", "b"])
    m.add_input("x0", ["T"], {("a",): 1.0})
    t = time.perf_counter()
    with m.transaction():
        for i in range(1, n):
            m.add_formula(f"x{i}", ["T"], f"x{i-1} + 1")
    added = time.perf_counter() - t
    assert m.get(f"x{n-1}", T="a") == n
    return added, rebuild_time(m)


def wide(n: int) -> tuple[float, float]:
    m = Named(Model(engine=RustEngine()))
    m.add_dimension("T", ["a", "b"])
    m.add_dimension("P", ["p", "q", "r"])
    t = time.perf_counter()
    with m.transaction():
        for i in range(n):
            m.add_input(f"x{i}", ["T", "P"], {("a", "p"): 1.0})
            m.add_formula(f"y{i}", ["T", "P"], f"x{i} + 1")
    added = time.perf_counter() - t
    return added, rebuild_time(m)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="+", default=[2000, 5000, 20000])
    args = ap.parse_args()
    print("| モデル | Metric を足す | 作り直し（コンパイルと全体の再計算） |")
    print("|---|---|---|")
    for n in args.sizes:
        added, rebuilt = chain(n)
        print(f"| 鎖 {n:,} 本 | {added:.2f} s | {rebuilt:.2f} s |")
    added, rebuilt = wide(2500)
    print(f"| 入力 2,500 + 式 2,500 | {added:.2f} s | {rebuilt:.2f} s |")


if __name__ == "__main__":
    main()
