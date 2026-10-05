"""記録先（ファイルと PostgreSQL）の性能を比べる。

    python bench_journal.py                  # 全部
    python bench_journal.py --only commit    # 1 つだけ（commit, workspace, bulk, open, history）

PostgreSQL は NANASHI_PG_DSN（既定は手元の 55432 番）を使う。モデルは損益計画（大、490 万セル）。
"""
from __future__ import annotations

import argparse
import os
import statistics
import tempfile
import threading
import time
import uuid

from examples.fpa import SIZES, build
from sparse_engine.journal import LOG_VERSION, FileJournal, now
from sparse_engine.rust_engine import RustEngine
from sparse_engine.workspace import Workspace

DSN = os.environ.get("NANASHI_PG_DSN", "postgresql://postgres@127.0.0.1:55432/nanashi")


def ms(xs) -> str:
    xs = sorted(xs)
    return f"中央値 {1e3 * statistics.median(xs):.1f} ms、95% {1e3 * xs[int(len(xs) * 0.95)]:.1f} ms"


class Target:
    """記録先を作って片付ける。"""

    def __init__(self, kind: str):
        self.kind = kind
        self.tmp = tempfile.TemporaryDirectory()
        self.model_id = f"bench-{uuid.uuid4().hex[:8]}"
        self.made = []

    def make(self):
        if self.kind == "file":
            return FileJournal(self.tmp.name)
        from sparse_engine.pg_journal import PgJournal, migrate
        migrate(DSN)
        j = PgJournal(DSN, self.model_id, self.tmp.name)
        self.made.append(j)
        return j

    def close(self) -> None:
        if self.kind == "pg":
            self.make().drop()
            for j in self.made:
                j.close()
        self.tmp.cleanup()


def model():
    m = build(RustEngine(), *SIZES["large"])
    m.recalc()
    return m


def bench_commit(kind: str, m) -> None:
    """損益計画の給与の変更を、1 件ずつトランザクションで確定する。"""
    t = Target(kind)
    t.make().start(m)
    emps = m.dimension("Employee").members
    times = []
    for i in range(200):
        s = time.perf_counter()
        m.set_cell("Salary", float(500 + i % 9), Employee=emps[i], Version="予算")
        times.append(time.perf_counter() - s)
    m.journal = None
    print(f"  {kind:4s} 1 件ずつ確定: {ms(times[20:])}")
    t.close()


def bench_workspace(kind: str, m) -> None:
    """8 人が 50 件ずつ給与を書き込む。"""
    t = Target(kind)
    t.make().start(m)
    m.journal = None
    ws = Workspace(m.fork(), t.make())  # 渡したモデルは公開済みの版になるので、複製を渡す
    emps = m.dimension("Employee").members
    latency: list[float] = []
    lock = threading.Lock()

    def client(k: int) -> None:
        for j in range(50):
            e = emps[2000 + k * 50 + j]
            s = time.perf_counter()
            ws.write(lambda mm, e=e: mm.set_cell("Salary", 700.0, Employee=e, Version="予算"), user=f"c{k}")
            with lock:
                latency.append(time.perf_counter() - s)

    threads = [threading.Thread(target=client, args=(k,)) for k in range(8)]
    s = time.perf_counter()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    total = time.perf_counter() - s
    ws.close()
    print(f"  {kind:4s} 8 人が同時に書き込む: 毎秒 {400 / total:.0f} 件、応答 {ms(latency)}")
    t.close()


def bench_bulk(kind: str) -> None:
    """大量のセルを書き換えた記録を 1 回で確定する（記録先の費用だけを測る）。"""
    for n in (1_000, 10_000, 100_000, 1_000_000):
        t = Target(kind)
        j = t.make()
        rows = [[[f"k{i // 1000}", f"t{i % 1000}", "v7"], float(i), float(i + 1)] for i in range(n)]
        rec = {"v": LOG_VERSION, "at": now(), "user": "etl", "reason": None, "client_op_id": None, "ops": [],
               "changes": {"cells": [{"metric": "m-bulk", "rows": rows}]}}
        s = time.perf_counter()
        j.append(rec)
        line = f"  {kind:4s} {n:>9,} セルの確定: {1e3 * (time.perf_counter() - s):,.0f} ms"
        if kind == "pg":
            s = time.perf_counter()
            j.index_pending()
            line += f"（確定の後の履歴への反映 {1e3 * (time.perf_counter() - s):,.0f} ms）"
        print(line)
        t.close()


def bench_open_and_history(kind: str, m) -> None:
    """1000 件の記録を積んでから、開き直す時間と、セルの履歴を引く時間を測る。"""
    t = Target(kind)
    t.make().start(m)
    emps = m.dimension("Employee").members
    for i in range(1000):
        m.set_cell("Salary", float(500 + i), Employee=emps[i % 50], Version="予算")
    m.journal = None
    s = time.perf_counter()
    opened = t.make().open(RustEngine())
    opened.recalc()
    print(f"  {kind:4s} スナップショット＋1000 件の再生で開く: {time.perf_counter() - s:.2f} 秒")
    j = t.make()
    s = time.perf_counter()
    for _ in range(20):
        h = j.cell_history(opened, "Salary", Employee=emps[3], Version="予算")
    print(f"  {kind:4s} セルの履歴を引く（{len(h)} 件）: {1e3 * (time.perf_counter() - s) / 20:.1f} ms")
    t.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--only", choices=["commit", "workspace", "bulk", "open", "history"])
    p.add_argument("--kinds", default="file,pg")
    args = p.parse_args()
    kinds = args.kinds.split(",")
    m = model() if args.only != "bulk" else None
    parts = [args.only] if args.only else ["commit", "workspace", "bulk", "open"]
    for part in parts:
        print(part)
        for kind in kinds:
            if part == "commit":
                bench_commit(kind, m)
            elif part == "workspace":
                bench_workspace(kind, m)
            elif part == "bulk":
                bench_bulk(kind)
            else:
                bench_open_and_history(kind, m)


if __name__ == "__main__":
    main()
