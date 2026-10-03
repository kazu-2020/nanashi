"""要素ごとの演算の融合（native/engine/src/eval/fuse.rs）と、集計先の全組み合わせの配列への集計
（agg.rs の dense_groups）が、演算ごとに評価したときと同じ結果になることを確かめる。

融合は範囲の絞り込みがない全体の評価でだけ働くので、ランダムに入力を変えるたびに全体を計算し直し、
参照実装、融合しない Rust、融合する Rust の 3 つを突き合わせる。空のセル、0 除算、軸の少ない読み出し元、
三値論理、条件の軸が少ない IF、上書き（Coalesce）のように、値があるセルの決まり方が演算ごとに違う式を並べる。
"""
import random
import unittest

from sparse_engine import Model
from sparse_engine.engine import ReferenceEngine

from .test_incremental import same, snapshot

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None

PRODUCTS = [f"p{i}" for i in range(7)]
STORES = [f"s{i}" for i in range(4)]
MONTHS = [f"m{i}" for i in range(5)]
CUBE = [(p, s, t) for p in PRODUCTS for s in STORES for t in MONTHS]
DIMS = ["Product", "Store", "Month"]

FORMULAS = [  # (名前, 軸, 式, 種類)
    ("Mul", DIMS, "A * Price", "number"),                      # 軸の少ない読み出し元を引く
    ("Div", DIMS, "A / Z", "number"),                          # 0 除算は空
    ("Div2", DIMS, "A / B", "number"),                         # 同じ軸の 2 つをカーソルで読む
    ("AddSame", DIMS, "A + B", "number"),                      # 被覆の和を走査する
    ("SubLow", DIMS, "A - Price[EXPAND: Store, Month]", "number"),  # 全組み合わせに値がありうる（融合しない）
    ("MulAdd", DIMS, "A * (Price[EXPAND: Month] + Disc[EXPAND: Product])", "number"),  # 被覆の中で広げる
    ("Cmp", DIMS, "A > B", "boolean"),
    ("AndOr", DIMS, "(A > 2) AND (B < 5) OR Flag", "boolean"),  # 三値論理
    ("NotF", DIMS, "NOT Flag", "boolean"),
    ("If1", DIMS, "IF(A > 3, A * 0.1, 0)", "number"),
    ("If2", DIMS, "IF(Flag, A, B)", "number"),
    ("If3", DIMS, "IF(A > 3, B)", "number"),                   # ELSE なし
    ("IfLow", DIMS, "IF(Active, A, 0)", "number"),             # 条件の軸が少なく、ELSE が定数（融合しない）
    ("IfLow2", DIMS, "IF(Active, A, B)", "number"),            # 条件の軸が少なく、分岐が被覆になる
    ("Filt", DIMS, "A[FILTER: Flag]", "number"),
    ("FiltLow", DIMS, "A[FILTER: Active]", "number"),
    ("OnF", DIMS, "Price[ON: A] + A", "number"),
    ("Scalar", DIMS, "A * Total0", "number"),                  # 軸のない読み出し元（空になりうる）
    ("Chain", DIMS, "Mul + AddSame * 2", "number"),            # 計算 Metric を読む
    ("Over", DIMS, "A * 2", "number"),                         # 上書き（Coalesce）
    # 式の軸をすべて持つ読み出し元がない式（1 つの読み出し元 × 足りない軸、または全組み合わせを走査する）
    ("Emp", ["Product", "Month"], "Month >= Hire AND (Month < Leave OR ISBLANK(Leave)[EXPAND: Month])", "boolean"),
    ("Pay", DIMS, "Sal * (1 + Total0) * IF(Emp, 1) * InPer", "number"),
    ("Sel", ["Product", "Store"], 'A[SELECT: Month."m2"]', "number"),
    # 親の式が、SELECT で消える軸や引き下ろしの行き先の軸を持つ（子を引くキーで、その軸を置き換える）
    ("SelIf", DIMS, 'IF(Month = Month."m1", B[SELECT: Month."m2"], A)', "number"),
    ("PCBy", ["Product", "Category"], "PC * Rate[BY: Product.Category]", "number"),
    ("Prev", DIMS, "A - A[SELECT: Month - 1]", "number"),
    ("RateBy", DIMS, "A * Rate[BY: Product.Category]", "number"),
    ("IfB", DIMS, "IFBLANK(A, 0) + B", "number"),
    ("Sparse", DIMS, "Sal * InPer", "number"),               # 疎な結合（全組み合わせでは走査しない）
    ("MonthGe", ["Product", "Month"], "Month >= Hire", "boolean"),
    ("SumS", ["Product", "Month"], "Mul[REMOVE SUM: Store]", "number"),
    ("AvgS", ["Product", "Month"], "A[REMOVE AVG: Store]", "number"),
    ("MinP", ["Store", "Month"], "B[REMOVE MIN: Product]", "number"),
    ("MaxM", ["Product", "Store"], "A[REMOVE MAX: Month]", "number"),
    ("CntS", ["Product", "Month"], "B[REMOVE COUNT: Store]", "number"),
    ("Cat", ["Category", "Store", "Month"], "Mul[BY SUM: Product.Category]", "number"),
    ("CatMax", ["Category", "Store", "Month"], "A[BY MAX: Product.Category]", "number"),
]


def model(engine, rng: random.Random) -> Model:
    m = Model(engine=engine)
    m.add_dimension("Product", PRODUCTS)
    m.add_dimension("Category", ["c0", "c1", "c2"])
    m.add_dimension("Store", STORES)
    m.add_dimension("Month", MONTHS, ordered=True)
    m.add_property("Product", "Category", "Category", {p: f"c{i % 3}" for i, p in enumerate(PRODUCTS)})

    def sparse(share: float, value) -> dict:
        return {c: value() for c in CUBE if rng.random() < share}

    m.add_input("A", DIMS, sparse(0.5, lambda: rng.randint(0, 6)))
    m.add_input("B", DIMS, sparse(0.5, lambda: rng.randint(-3, 6)))
    m.add_input("Z", DIMS, sparse(0.6, lambda: rng.choice([0, 0, 1, 2])))
    m.add_input("Flag", DIMS, sparse(0.5, lambda: rng.random() < 0.5), kind="boolean")
    m.add_input("Price", ["Product"], {(p,): rng.randint(1, 9) for p in PRODUCTS if rng.random() < 0.7})
    m.add_input("Disc", ["Month"], {(t,): rng.randint(1, 3) for t in MONTHS if rng.random() < 0.5})
    m.add_input("Active", ["Product"], {(p,): rng.random() < 0.6 for p in PRODUCTS if rng.random() < 0.8}, kind="boolean")
    m.add_input("Total0", [], {(): 3})
    m.add_input("Hire", ["Product"], {(p,): rng.choice(MONTHS) for p in PRODUCTS if rng.random() < 0.9}, kind="member:Month")
    m.add_input("Leave", ["Product"], {(p,): rng.choice(MONTHS) for p in PRODUCTS if rng.random() < 0.4}, kind="member:Month")
    m.add_input("Sal", ["Product", "Store"], {(p, s): rng.randint(1, 9) for p in PRODUCTS for s in STORES if rng.random() < 0.9})
    m.add_input("InPer", ["Store", "Month"], {(s, t): rng.choice([0, 1, 1]) for s in STORES for t in MONTHS if rng.random() < 0.8})
    m.add_input("Rate", ["Category"], {("c0",): 0.5, ("c2",): 2})
    m.add_input("PC", ["Product", "Category"], {(p, c): rng.randint(1, 5) for p in PRODUCTS for c in ["c0", "c1", "c2"]
                                                 if rng.random() < 0.6})
    for name, dims, text, kind in FORMULAS:
        m.add_formula(name, dims, text, kind=kind, overridable=name == "Over")
    return m


def edit(rng: random.Random, models: list[Model]) -> None:
    """どのモデルにも同じ変更を加える（値の上書き、削除、0、上書きの設定と解除）。"""
    name = rng.choice(["A", "A", "B", "B", "Z", "Flag", "Price", "Disc", "Active", "Total0", "Over",
                       "Hire", "Leave", "Sal", "InPer", "Rate"])
    dims = {"Price": ["Product"], "Active": ["Product"], "Disc": ["Month"], "Total0": [], "Hire": ["Product"],
            "Leave": ["Product"], "Sal": ["Product", "Store"], "InPer": ["Store", "Month"], "Rate": ["Category"]}.get(name, DIMS)
    coords = {d: rng.choice({"Product": PRODUCTS, "Store": STORES, "Month": MONTHS, "Category": ["c0", "c1", "c2"]}[d])
              for d in dims}
    if name in ("Flag", "Active"):
        value = rng.choice([True, False, None])
    elif name in ("Hire", "Leave"):
        value = rng.choice(MONTHS + [None])
    else:
        value = rng.choice([None, 0, rng.randint(-2, 9)])
    for m in models:
        m.set_cell(name, value, **coords)


def run(test: unittest.TestCase, seed: int, rounds: int, configs: list[dict]) -> None:
    rng = random.Random(seed)
    engines = [ReferenceEngine()] + [RustEngine(**c) for c in configs]
    models = [model(e, random.Random(seed)) for e in engines]
    labels = ["参照"] + [str(c) for c in configs]
    for round_ in range(rounds):
        for _ in range(rng.randint(1, 4)):
            edit(rng, models)
        for m in models[1:]:
            m._invalidate()  # 全体を計算し直す（融合は全体の評価で働く）
        snaps = [snapshot(m) for m in models]
        for label, snap in zip(labels[1:], snaps[1:]):
            for name in snaps[0]:
                with test.subTest(round=round_, engine=label, metric=name):
                    test.assertTrue(same(snaps[0][name], snap[name]), f"{name}\n参照: {snaps[0][name]}\n{label}: {snap[name]}")


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class FusionMatchesReference(unittest.TestCase):
    def test_random(self):
        # compact_min=0 は書き込みのたびに差分を本体へまとめ直すので、入力が差分のない格納データになり、
        # 融合の経路を通る（既定では、入力を変えると差分が残り、融合せずに演算ごとに評価する）
        run(self, seed=61, rounds=60, configs=[
            {"fuse": False},
            {"fuse": True, "compact_min": 0},
            {"fuse": True, "compact_min": 0, "par_min": 0, "chunk": 5},  # 並列の走査と、区間ごとの結果の詰め直し
            {"fuse": True, "compact_min": 0, "par_min": 0, "chunk": 3, "dense_always": True},  # 全組み合わせの配列への集計
        ])

    def test_dense_sequential(self):
        run(self, seed=67, rounds=30, configs=[{"compact_min": 0, "dense_always": True}])


if __name__ == "__main__":
    unittest.main()
