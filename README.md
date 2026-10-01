# nanashi

nanashi は、Pigment のような計画ツールのための、疎な多次元計算エンジンである。
名前付きの軸を持つ **Metric** を単位に値を持ち、式は Excel のようにセルごとではなく Metric 全体に適用する。
値のあるセルだけを保持し、入力を 1 セル変えたときは影響する範囲だけを計算し直す。

大規模なモデルでも対話的な速さで応答することを目標にしている。
たとえば社員 2 万人、商品 5 千、36 か月の損益計画（約 490 万セル、計算 Metric 18 個）では、全体の再計算が約 90 ms、給与や所属の変更は 1 ms 未満で反映される。

## 使い始める

Python 3.12 以上と、Rust のツールチェーン（cargo）を使う。

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
VIRTUAL_ENV=$PWD/.venv .venv/bin/maturin develop --release -m native/Cargo.toml
SPARSE_ENGINE=rust .venv/bin/python -m unittest discover -s tests -t .
```

最後から 2 行目で、Rust のエンジン（`nanashi_core`）をビルドして `.venv` に入れる。
Rust のエンジンがなくても参照実装のエンジンだけで計算できるが、保存と読み込み（Parquet）には `nanashi_core` を使う。
free-threaded の Python、PostgreSQL とオブジェクトストレージを使うテスト、サンプルとベンチマークは [docs/development.md](docs/development.md) にある。

## 最小の例

```python
from sparse_engine import Model
from sparse_engine.rust_engine import RustEngine

m = Model(engine=RustEngine())
m.add_dimension("Employee", ["alice", "bob", "carol"])
m.add_dimension("Department", ["営業", "開発"])
m.add_dimension("Month", ["Jan", "Feb", "Mar"], ordered=True)

m.add_input("Salary", ["Employee"], {("alice",): 50, ("bob",): 40, ("carol",): 60})
m.add_input("DeptOf", ["Employee", "Month"],
            {(e, t): d for e, d in [("alice", "営業"), ("bob", "営業"), ("carol", "開発")]
             for t in ["Jan", "Feb", "Mar"]},
            kind="member:Department")
m.add_formula("Cost", ["Department", "Month"], "Salary[EXPAND: Month][BY SUM: Employee.DeptOf]")
m.add_formula("Cash", ["Month"], "PREVIOUS(Month) + 500 - Cost[REMOVE SUM: Department]")

m.set_cell("DeptOf", "開発", Employee="bob", Month="Mar")   # bob が 3 月に異動
print(m.value("Cost").format(m.dimensions))
```

入力の Metric は `add_input`、式で決まる Metric は `add_formula` で登録する。
Metric の軸と値の種類は登録時に決め、あとから変えない。
値を読むと、変更のあった範囲だけが計算し直される。

## ドキュメント

| 文書 | 内容 |
|---|---|
| [docs/modeling.md](docs/modeling.md) | 値の読み出し、軸とメンバー（追加、名前の変更、削除、ID）、計画の入力（上書き、按分）、ホワットイフ分析 |
| [docs/formulas.md](docs/formulas.md) | 式の言語、型の検査、セル数の見積もり、空の扱い |
| [docs/recalculation.md](docs/recalculation.md) | 計算計画と差分再計算、差分集計、定義の変更 |
| [docs/engine.md](docs/engine.md) | エンジンの構成（Store と Planner）、参照実装と Rust のエンジンの作り |
| [docs/persistence.md](docs/persistence.md) | 保存と読み込み、トランザクションと記録、PostgreSQL の記録先 |
| [docs/concurrency.md](docs/concurrency.md) | 同時の読み書き（`Workspace`、`Replica`、待機系への引き継ぎ） |
| [docs/server.md](docs/server.md) | HTTP サーバーの API、認証、制限、止め方 |
| [docs/router.md](docs/router.md) | 書き手へ要求を送り直す Go のルーター（`router/`） |
| [docs/performance.md](docs/performance.md) | 性能の測定値（再計算、読み出し、free-threaded、メモリ、記録先、保存の形式） |
| [docs/development.md](docs/development.md) | 開発環境、テストの方針、ソースの構成と、式のノードを足すときに直す場所 |
| [docs/limitations.md](docs/limitations.md) | 制約と今後 |
| [docs/member-numbering.md](docs/member-numbering.md) | 設計メモ：メンバーの名前、ID、番号、順位、削除の tombstone、キーの幅の拡張、View の組み替え（番号と順位の分離の第 1 段は実装済み） |
| [docs/out-of-core.md](docs/out-of-core.md) | 設計メモ：格納データを NVMe（SSD）に置く案と、pread、mmap、heap の測定（未実装） |

## ライセンス

MIT License（`LICENSE`）。
