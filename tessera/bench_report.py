"""bench_results.json を Markdown の表にする。"""
import json

import os

rows = json.load(open("bench_results.json"))
if not any(r["engine"] == "reference" for r in rows) and os.path.exists("bench_results_prev.json"):
    # 参照実装を省いて実行したときは、前回の参照実装の結果を並べる
    prev = [r for r in json.load(open("bench_results_prev.json")) if r["engine"] == "reference"]
    order = {size: i for i, size in enumerate(["small", "medium", "large", "xlarge"])}
    rows = sorted(prev + rows, key=lambda r: order[r["size"]])
edits = next(r["edits"] for r in rows if "edits" in r)


def ms(x: float) -> str:
    return f"{x * 1000:,.0f} ms" if x >= 0.01 else f"{x * 1000:.1f} ms"


print("| エンジン | 規模 | Volume セル数 | 全体再計算 | 直後の最初の変更 | " + " | ".join(edits) + " | ピークメモリ |")
print("|---" * (6 + len(edits)) + "|")
for r in rows:
    if "edits" not in r:
        print(f"| {r['engine']} | {r['size']} | | {'タイムアウト' if r.get('timeout') else 'エラー'} |")
        continue
    cells = f"{r['cells']['Volume']:,}"
    first = ms(r["first_edit"]) if "first_edit" in r else "-"
    print(f"| {r['engine']} | {r['size']} | {cells} | {ms(r['full'])} | {first} | "
          + " | ".join(ms(v) for v in r["edits"].values()) + f" | {r['peak_mb']:,.0f} MB |")

totals = {}
for r in rows:
    if "total" in r:
        totals.setdefault(r["size"], []).append((r["engine"], r["total"]))
print()
for size, ts in totals.items():
    vals = [t for _, t in ts]
    rel = (max(vals) - min(vals)) / abs(vals[0])
    print(f"{size}: Total の相対差 {rel:.1e}（{', '.join(e for e, _ in ts)}）")
