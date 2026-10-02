"""記録先から開く時間を、スナップショットの形式で比べる。

    python bench_open.py                      # 損益計画（大）の 1 倍と 4 倍、小売モデル（大）
    python bench_open.py --models fpa4,retail --records 0,10

比べるのは次の 2 つで、どちらも手元のディレクトリ（FileJournal）から開く。
- 入力だけ（Parquet。版 4）: 開くときに全体を計算し直す（以前の形式）
- 入力と計算 Metric の値（平らな形式。版 5）: 後の記録の変更範囲だけを差分で計算し直す

スナップショットのあとに records 件の記録（入力の 1 セルの変更）を残しておき、開いて最初の再計算を終えるまでの
時間を、スナップショットの読み込み（ハッシュの検査を含む）、記録の再生、再計算に分けて測る。
測定はそれぞれ別のプロセスで行う（ページキャッシュは暖めてから測る）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

MB = 1e6


def build(name: str):
    from sparse_engine.rust_engine import RustEngine
    if name.startswith("fpa"):
        from examples.fpa import SIZES, build as fpa
        scale = int(name[3:] or "1")
        e, p, mo = SIZES["large"]
        return fpa(RustEngine(), e * scale, p * scale, mo)
    if name == "retail":
        from bench import build as retail
        return retail("rust", "large")
    raise ValueError(name)


def prepare(path: str, name: str, records: int, legacy: bool) -> None:
    """記録先 path に、モデルのスナップショットと、その後の records 件の記録を置く。"""
    from sparse_engine.journal import FileJournal
    from sparse_engine import storage
    m = build(name)
    m.recalc()
    journal = FileJournal(path, fsync=False)
    if legacy:  # 以前の形式（入力だけを Parquet で）
        original = storage.dump
        storage.dump = lambda model, snapshot=False: original(model)
        try:
            journal.start(m)
        finally:
            storage.dump = original
    else:
        journal.start(m)
    inputs = [n for n, x in m.metrics.items() if x.formula is None and x.dims]
    for i in range(records):
        name_ = inputs[i % len(inputs)]
        dims = m.metrics[name_].dims
        key = {d: m.dimensions[d].members[(i * 7) % len(m.dimensions[d].members)] for d in dims}
        value = True if m.metrics[name_].kind == "boolean" else float(i + 1)
        if m.metrics[name_].kind.startswith("member:"):
            continue
        m.set_cell(name_, value, **key)
    journal.release()


def measure(path: str) -> dict:
    """path の記録先を開き、最初の再計算を終えるまでの時間の内訳。"""
    import nanashi_core
    from sparse_engine.journal import FileJournal, apply
    from sparse_engine.rust_engine import RustEngine

    nanashi_core.track_heap(True)
    journal = FileJournal(path)
    engine = RustEngine()
    t0 = time.perf_counter()
    (base, place), *_ = journal.snapshots()
    model = journal.load_snapshot(place, engine)
    t1 = time.perf_counter()
    incremental = model._plan is not None and not model._pending.full
    n = 0
    for rec in journal.records(after=base):
        apply(model, rec, incremental=incremental)
        n += 1
    t2 = time.perf_counter()
    model.recalc()
    t3 = time.perf_counter()
    heap = nanashi_core.heap()[0]
    storage = sum(v.get("base", 0) + v.get("delta", 0) + v.get("index", 0) for v in model.memory().values())
    blobs = [journal.objects.get(f"{place.uri}/{f}") for f in place.files]
    t4 = time.perf_counter()
    for data in blobs:  # 読み込みのうち、ハッシュの検査にかかる分
        hashlib.sha256(data).hexdigest()
    t5 = time.perf_counter()
    return {"load_ms": 1e3 * (t1 - t0), "hash_ms": 1e3 * (t5 - t4), "replay_ms": 1e3 * (t2 - t1),
            "recalc_ms": 1e3 * (t3 - t2), "total_ms": 1e3 * (t3 - t0), "records": n, "incremental": incremental,
            "heap_mb": heap / MB, "storage_mb": storage / MB, "snapshot_mb": sum(map(len, blobs)) / MB,
            "cells": sum(model.engine.size(v) for v in model._values.values())}


def child(args: list[str]) -> dict:
    out = subprocess.run([sys.executable, __file__, "--child", *args], check=True, capture_output=True, text=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="fpa1,fpa4,retail")
    ap.add_argument("--records", default="0,10")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--child", nargs="*", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.child is not None:
        if args.child[0] == "prepare":
            prepare(args.child[1], args.child[2], int(args.child[3]), args.child[4] == "legacy")
            print("{}")
        else:
            print(json.dumps(measure(args.child[1])))
        return
    print("| モデル | 後の記録 | 形式 | 開くまで | スナップショットの読み込み（うちハッシュの検査） | 記録の再生 | 最初の再計算 | スナップショット | 格納データ | うち Rust のヒープ |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for name in args.models.split(","):
        for records in map(int, args.records.split(",")):
            for legacy in (True, False):
                with tempfile.TemporaryDirectory() as tmp:
                    child(["prepare", tmp, name, str(records), "legacy" if legacy else "flat"])
                    child(["measure", tmp])  # ページキャッシュを暖める
                    runs = [child(["measure", tmp]) for _ in range(args.repeats)]
                r = min(runs, key=lambda x: x["total_ms"])
                label = "入力だけ（Parquet）" if legacy else "計算した値も（平らな形式）"
                print(f"| {name}（{r['cells']:,} セル） | {r['records']} | {label} | {r['total_ms']:,.0f} ms | "
                      f"{r['load_ms']:,.0f} ms（{r['hash_ms']:,.0f} ms） | {r['replay_ms']:,.0f} ms | {r['recalc_ms']:,.0f} ms"
                      f"{'（差分）' if r['incremental'] else '（全体）'} | {r['snapshot_mb']:,.1f} MB | {r['storage_mb']:,.0f} MB | {r['heap_mb']:,.0f} MB |")
                sys.stdout.flush()
    if os.environ.get("NANASHI_BENCH_JSON"):
        print(json.dumps(runs))


if __name__ == "__main__":
    main()
