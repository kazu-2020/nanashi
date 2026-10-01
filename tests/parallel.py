"""tests/ のテストを、モジュールごとに別のプロセスで並べて回す。

    python -m tests.parallel [-j 並列数] [モジュール ...]

テストの多くは、PostgreSQL への往復やサーバーのプロセスの起動、リースの期限を待つ時間が占める。
1 つのプロセスで順に回すと CPU が空くので、モジュールごとにプロセスを分けて並べる。
各モジュールは `python -m unittest <モジュール>` で回すので、1 つずつ回したときと同じ結果になる。
PostgreSQL のテストはモデルの ID を毎回変えるので、並べて回しても互いの記録に触れない。
最後に、モジュールごとの時間、件数、スキップの数と、全体の合計を出す。1 つでも落ちれば終了コードは 1 にする。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
SKIPPED = re.compile(r"^(?:OK|FAILED) \(.*?skipped=(\d+)", re.M)


@dataclass
class Result:
    module: str
    returncode: int
    seconds: float
    output: str

    @property
    def ran(self) -> int | None:
        m = RAN.search(self.output)
        return int(m.group(1)) if m else None

    @property
    def skipped(self) -> int:
        m = SKIPPED.search(self.output)
        return int(m.group(1)) if m else 0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.ran is not None


def modules() -> list[str]:
    """tests/test_*.py を、ファイルの大きい順に並べる。大きいモジュールほど長くかかるので、先に始める。"""
    files = sorted((ROOT / "tests").glob("test_*.py"), key=lambda p: (-p.stat().st_size, p.name))
    return [f"tests.{p.stem}" for p in files]


def run(module: str) -> Result:
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", module],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    return Result(module, proc.returncode, time.monotonic() - start, proc.stdout)


def main() -> int:
    ap = argparse.ArgumentParser(description="tests/ のテストを、モジュールごとに別のプロセスで並べて回す")
    # 待ち時間が多いので、CPU の数より多く並べたほうが速い（4 コアで 4 並列は 46 秒、8 並列は 37 秒だった）
    ap.add_argument("-j", "--jobs", type=int, default=2 * (os.cpu_count() or 1), help="同時に回すプロセスの数（既定は CPU の数の 2 倍）")
    ap.add_argument("modules", nargs="*", help="回すモジュール（既定は tests/test_*.py のすべて）")
    args = ap.parse_args()

    start = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = [pool.submit(run, m) for m in (args.modules or modules())]
        for f in as_completed(futures):
            r = f.result()
            results.append(r)
            status = "ok" if r.ok else "失敗"
            print(f"{r.seconds:7.1f}s  {status:4}  {r.module}  {r.ran or 0} 件、スキップ {r.skipped}", flush=True)
            if not r.ok:
                print(r.output, flush=True)

    failed = [r for r in results if not r.ok]
    ran = sum(r.ran or 0 for r in results)
    skipped = sum(r.skipped for r in results)
    print(f"\n{len(results)} モジュール、{ran} 件を {time.monotonic() - start:.1f} 秒で回した（スキップ {skipped}）")
    if failed:
        print("失敗したモジュール: " + ", ".join(r.module for r in failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
