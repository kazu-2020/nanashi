"""読み出しの費用を測る。1 セル、範囲、ページ、集計を、Metric 全体を Cube にする場合と比べる。

    python bench_reads.py            # 損益計画（大、490 万セル）
    python bench_reads.py --size medium

読み出しは公開中の版に対して行われるので、大きな Metric を丸ごと Python の dict にする経路は、
1 セル読むだけでも時間がかかり、その間 GIL を握ってライターも止める。
"""
from __future__ import annotations

import argparse
import statistics
import tempfile
import threading
import time

from bench import median_ms
from examples.fpa import SIZES, build
from sparse_engine.rust_engine import RustEngine
from sparse_engine.named import Named
from sparse_engine.workspace import Workspace


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", choices=list(SIZES), default="large")
    args = ap.parse_args()
    m = build(RustEngine(), *SIZES[args.size])
    m.recalc()
    emp, prod = m.dimension("Employee").members[1], m.dimension("Product").members[1]
    cases = [
        ("value: PayrollByEmployee を丸ごと Cube に", lambda: m.value("PayrollByEmployee")),
        ("get: PayrollByEmployee の 1 セル", lambda: m.get("PayrollByEmployee", Employee=emp, Version="予算", Month="m01")),
        ("get: Revenue の 1 セル", lambda: m.get("Revenue", Product=prod, Version="予算", Month="m01")),
        ("slice: 1 商品の全版・全月", lambda: m.slice("Revenue", Product=prod)),
        ("rows: 1 部署の先頭 50 行", lambda: m.rows("Payroll", Department="営業", limit=50)),
        ("summarize: 売上の月別合計（全商品）", lambda: m.summarize("Revenue", keep=["Month"], Version="予算")),
        ("summarize: 1 社員の人件費の総合計", lambda: m.summarize("PayrollByEmployee", Employee=emp)),
    ]
    n = {name: m.engine.size(m.raw(name)) for name in ("PayrollByEmployee", "Revenue", "Payroll")}
    print(f"{args.size}: PayrollByEmployee {n['PayrollByEmployee']:,} セル、Revenue {n['Revenue']:,} セル")
    for label, fn in cases:
        print(f"  {label}: {median_ms(fn, 5):.2f} ms")
    with tempfile.TemporaryDirectory() as tmp:
        print(f"  save（スナップショット）: {median_ms(lambda: m.save(tmp), 3):.0f} ms")

    # 読み手がいる間の書き込み: 8 人が読み続ける中で、給与の変更を確定する。読み手が value で Metric を
    # 丸ごと読むと（以前の get の経路）、その間 GIL を握るので書き込みが待たされる。get なら待たされないが、
    # 読み手が Python の処理を休みなく回していると、ライターが GIL を取り直すたびに切り替えの間隔
    # （既定 5 ms）を待つ。切り替えの間隔を短くすると、その待ちが減る
    print("  8 人が休みなく読み続ける中での書き込み（給与を 1 人変更）:")
    read_whole = lambda v: Named(v).value("PayrollByEmployee").get(Employee=emp, Version="予算", Month="m01")
    read_one = lambda v: Named(v).get("PayrollByEmployee", Employee=emp, Version="予算", Month="m01")
    for label, read, interval, writes in [("読み手が value で丸ごと読む（以前の経路）", read_whole, None, 5),
                                          ("読み手が get で 1 セル読む", read_one, None, 20),
                                          ("同上、切り替えの間隔 0.5 ms", read_one, 0.0005, 20)]:
        print(f"    {label}: 中央値 {contended_write(m, read, interval, writes, emp):.2f} ms")


def contended_write(m, read, interval, writes: int, emp: str) -> float:
    import sys
    ws = Workspace(m.fork().model)
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            read(ws.version)
    saved = sys.getswitchinterval()
    if interval is not None:
        sys.setswitchinterval(interval)
    threads = [threading.Thread(target=reader, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    xs = []
    try:
        for i in range(writes):
            t0 = time.perf_counter()
            ws.write(lambda x, i=i: Named(x).set_cell("Salary", 400.0 + i, Employee=emp, Version="予算"))
            xs.append(time.perf_counter() - t0)
    finally:
        stop.set()
        for t in threads:
            t.join()
        sys.setswitchinterval(saved)
        ws.close()
    return 1e3 * statistics.median(xs)


if __name__ == "__main__":
    main()
