# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

nanashi は、Pigment のような計画ツールのための疎な多次元計算エンジンである。Python のパッケージ `sparse_engine` と、Rust のエンジン `nanashi_core`（`native/`）の 2 層からなる。サーバーの前に置く Go のルーター（`router/`）もある。
コメント、docstring、エラーの文言、ドキュメント、コミットメッセージはすべて日本語で書く。
仕様は `docs/` にある（一覧は README.md の「ドキュメント」）。振る舞いや性能を変えたら、該当する `docs/` の文書と表も直す。

## コマンド

```bash
# 準備（Rust のエンジンは .venv に入れる。psycopg[binary] は手元で libpq なしに PostgreSQL へつなぐため）
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]" "psycopg[binary]"
VIRTUAL_ENV=$PWD/.venv .venv/bin/maturin develop --release -m native/Cargo.toml

# PostgreSQL（55432 番）と S3 互換のオブジェクトストレージ RustFS（59000 番）
docker compose up -d

# テスト。SPARSE_ENGINE で既定のエンジンを選ぶ（reference / rust。指定しなければ reference）。CI は両方で回す
SPARSE_ENGINE=reference .venv/bin/python -m unittest discover -s tests -t .
SPARSE_ENGINE=rust .venv/bin/python -m unittest discover -s tests -t .

# モジュールごとに別のプロセスで並べて回す（CI はこれを使う。待ち時間が多いので、順に回すより 3 倍ほど速い）
SPARSE_ENGINE=rust .venv/bin/python -m tests.parallel

# 1 つのファイル、クラス、テストだけ
SPARSE_ENGINE=rust .venv/bin/python -m unittest tests.test_reads
SPARSE_ENGINE=rust .venv/bin/python -m unittest tests.test_reads.<クラス>.<テスト>

# Rust の単体テスト（native/engine/tests/ の性質テストを含む）
cargo test --release --workspace --manifest-path native/Cargo.toml

# ルーター（Go）。PgResolver のテストは、Python のテストが作った nanashi_model の表を使う
(cd router && go vet ./... && go test ./...)

# 静的検査（CI と同じ）
.venv/bin/pyflakes sparse_engine tests examples bench*.py
cargo clippy --release --workspace --all-targets --manifest-path native/Cargo.toml -- -D warnings
```

- `native/` を変えたら、Python のテストの前に `maturin develop` でビルドし直す。テストは `.venv` に入った `nanashi_core` を読む。
- テストは、前提が欠けると失敗せずにスキップする。スキップの数を必ず見る。
  - `nanashi_core` が入っていなければ、Rust のエンジンのテストをスキップする。
  - PostgreSQL（`NANASHI_PG_DSN`）につながらないか、`psycopg` が libpq を見つけられなければ、記録先のテストをスキップする。
  - RustFS（`NANASHI_S3_ENDPOINT`）につながらなければ、S3 に置くテストをスキップする。
- 書き手の引き継ぎの試験台は `.venv/bin/python -m tests.failover --signal TERM` で回す。2 つのサーバーのプロセスを立て、PostgreSQL が要る。
- `examples.fpa` は `--size` を付けないと、large（約 490 万セル）まですべての規模を回す。ベンチマーク（`bench*.py`）とあわせて、メモリを数 GB 使うので 1 本ずつ実行する。
- 開発環境の詳細（free-threaded の Python 3.14t、`compose.yaml` の認証情報）は `docs/development.md` にある。

## アーキテクチャ

全体像は `docs/engine.md`、ソースの対応表は `docs/development.md` の「構成」にある。作業で外せない点は次のとおり。

### 2 つのエンジンと二重の実装

`Model`（`sparse_engine/model.py`）は、定義、操作、読み出し、トランザクション、分割軸の選択だけを持つ。それ以外はエンジンの 2 つの口に任せる。

- **`engine.Store`**: 格納と評価
- **`planner.Planner`**: 型検査、計算計画、差分集計の判定、影響範囲、再計算の段取り

| | Store | Planner |
|---|---|---|
| 参照実装（`ReferenceEngine`） | Python の dict | `PyPlanner`、`evaluate.py`、`delta.py` |
| Rust（`RustEngine`） | `native/engine/src/store.rs`、`eval/` | `RustPlanner` → `check.rs`、`graph.rs`、`plan.rs` |

- 参照実装は正しさの基準である。
- Rust の経路で Python が行うのは、構文と名前の解決だけである。
- Store と Planner の口はすべて必須で、Model は口の有無を調べない。口を足すときは、参照実装と Rust の両方に足す。
- 計算は `native/engine/`（crate `nanashi-engine`、Python に依存しない）が行う。`native/src/lib.rs`（PyO3）は橋渡しで、Python から受け取った番号と長さはここで検査する。

### 式のノードを足す・変えるとき

直す場所は `docs/development.md` の「構成」に一覧がある。

- Python: `expr.py`、`parser.py`、`evaluate.py`、`rust_engine._tree`
- Rust: `native/src/lib.rs` の `node`、`ast.rs`、`check.rs`、`graph.rs`、`plan.rs`、`eval/`

そのうえで、`tests/test_expr_coverage.py` のモデルにそのノードを使う式を足す。足し忘れた場所があれば、このテストが落ちる。

- 式の誤りと警告（型検査と循環の検査）は、Rust でも文言を作らない。コードと値（`nanashi_core.Diagnostic`）で返し、Python の `FormulaError.code` と `.params` にする。文言は `sparse_engine/messages.py` の `MESSAGES` だけに持つ。
- 集計関数は `expr.AGGREGATIONS` の 1 か所に持つ。

### 壊してはいけない約束

- 1 セルのキーは、各軸のメンバー番号を詰めた u64 で表す。軸のビット幅の合計が 64 を超える Metric は、型検査で拒否する。式の途中の結果も、メンバーの追加も同じように確かめる。
- 公開済みの版は変えない。
  - 格納データの本体は版どうしで共有し、差分は永続的な木に持つ。
  - 読み手が古い版を持ったまま書き込めることを前提に、`fork`、トランザクションの取り消し、`Workspace` の版が成り立っている。
- `Model` を複数のスレッドから直接使わない。同時に使うときは `Workspace` か `Replica` を通す。
- HTTP の書き込みは `client_op_id` が必須で、再送しても二重に確定しない。
- 本番の記録先は `PgJournal` で、`FileJournal` は主に開発と検証に使う。
- ルーター（`docs/router.md`）は書き込みを送り直す。送り直しても二重に確定しないのは `client_op_id` があるためで、応答ごとの次の動き（`router.go` の `decide`）を変えるときはこの前提を崩さない。

## テストの考え方

- 中心のテストは、ランダムな変更（入力、メンバーの追加・削除・名前の変更、異動など）を数百回加えたあとに、Rust の差分再計算の結果を、参照実装の結果と、全体を計算し直した結果の両方と突き合わせる。
- 影響範囲の伝搬や差分集計を変えたら、わざと壊した実装でテストが落ちることも確かめる。
- 速さのための調整値（`native/engine/src/config.rs`）は、`RustEngine(par_min=0, widen_min_rows=0)` のようにエンジンごとに変えられる。大きなモデルでだけ働く経路を、小さなモデルでも働かせるテストに使う（`tests/test_redefine.py`、`tests/test_failures.py`、`native/engine/tests/store_model.rs`）。
- 記録先を使うテストは、`tests/journals.py` の `JournalCase` を継いで、ファイルと PostgreSQL の両方で回す。`Workspace` と HTTP サーバーのテストには、本番の組み合わせ（Rust のエンジンと `store = PgStore`）のクラスを必ず含める。
- 上限の検査（`Model(max_cells=...)`、`RustEngine(max_bytes=...)`）を確かめるテストは、検査が効かなかったときに作られる量も小さく収まるように組む。検査が外れると、何十億セルを確保しにいく。
