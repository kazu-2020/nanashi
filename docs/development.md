# 開発

## 使い始める

Python 3.12 以上と、Rust のツールチェーン（cargo）を使う。

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
VIRTUAL_ENV=$PWD/.venv .venv/bin/maturin develop --release -m native/Cargo.toml
```

`pyproject.toml` が依存の版を決めている。実行時に要る Python のパッケージはなく、開発用に maturin、psycopg、boto3、pyflakes、ベンチマークの入力を作るのに numpy を使う。
最後の行で、Rust のエンジン（`nanashi_core`）をビルドして `.venv` に入れる。
Rust のエンジンがなくても、参照実装のエンジンだけで計算できる。
保存と読み込み（Parquet の読み書き）には、参照実装のエンジンでも `nanashi_core` を使う。

読み手を多く抱えるサーバーでは、free-threaded の Python（3.14t）を使う。
Rust のエンジンは GIL なしで動く宣言をしていて（`gil_used = false`）、格納データは Arc と永続的な木で共有するので、読み手が何人いても書き込みを待たせない（[性能](performance.md)の表）。

```bash
brew install python-freethreading          # macOS。Linux はディストリビューションのパッケージか uv で
python3.14t -m venv .venv-ft
.venv-ft/bin/pip install -e ".[dev]"
VIRTUAL_ENV=$PWD/.venv-ft .venv-ft/bin/maturin develop --release -m native/Cargo.toml
```
GitHub Actions（`.github/workflows/test.yml`）が、両方のエンジンでテストと静的検査を回す。
Python のテストは Python の版ごとのジョブで、Rust の単体テストと clippy は別のジョブで並べて回す。
ビルドした `nanashi_core` は `native/` の中身ごとにキャッシュし、`native/` を変えていなければビルドを省く。
`main` 以外のブランチは、push ではなく pull request で回す。

テストは次のように実行する。
`SPARSE_ENGINE` で既定のエンジンを選ぶ（`reference` または `rust`。指定しなければ `reference`）。
CI は両方で回す。

```bash
SPARSE_ENGINE=rust .venv/bin/python -m unittest discover -s tests -t .
SPARSE_ENGINE=rust .venv/bin/python -m unittest tests.test_reads   # 1 つのファイル（クラスやテストまで指定もできる）
SPARSE_ENGINE=rust .venv/bin/python -m tests.parallel   # モジュールごとに別のプロセスで並べて回す（CI はこれを使う）
cargo test --release --workspace --manifest-path native/Cargo.toml   # Rust の単体テスト
```

`native/` を変えたら、Python のテストの前に `maturin develop` でビルドし直す（テストは `.venv` に入った `nanashi_core` を読む）。
`nanashi_core` をビルドしていないと、Rust のエンジンのテストは失敗せずにスキップされるので、スキップの数も見る。
テストの多くは PostgreSQL への往復やサーバーの起動、リースの期限を待つ時間が占めるので、`tests.parallel` で並べると順に回すより 3 倍ほど速い（4 コアで 137 秒が 37 秒）。
最後にモジュールごとの件数とスキップの数、全体の合計を出す。

静的検査は CI と同じく次の 2 つである。

```bash
.venv/bin/pyflakes sparse_engine tests examples bench*.py
cargo clippy --release --workspace --all-targets --manifest-path native/Cargo.toml -- -D warnings
```

PostgreSQL の記録先（`PgJournal`）を使うときと、そのテストを回すときは、PostgreSQL と `psycopg` を用意する。
記録先のスナップショットと大量の変更のファイルは、S3 互換のオブジェクトストレージに置く（`boto3` を使う）。
手元では S3 の代わりに [RustFS](https://github.com/rustfs/rustfs)（Apache-2.0）の Docker イメージを使う。
`compose.yaml` が、PostgreSQL と RustFS をまとめて起動する。

```bash
.venv/bin/pip install "psycopg[binary]"
docker compose up -d   # PostgreSQL は 55432 番、RustFS は 59000 番（S3 の API）と 59001 番（管理画面）
```

テストは `NANASHI_PG_DSN`（既定は `postgresql://postgres@127.0.0.1:55432/nanashi`）と `NANASHI_S3_ENDPOINT`（既定は `http://127.0.0.1:59000`）につなぐ。
PostgreSQL につながらなければ（`psycopg` が libpq を見つけられないときも）記録先のテストを飛ばし、RustFS につながらなければ、ファイルをオブジェクトストレージに置くテストだけを飛ばす（ローカルのディレクトリに置くテストは回す）。
テスト用のバケット（`nanashi-test`）はテストが作る。

`compose.yaml` は開発とテスト用で、PostgreSQL にはパスワードなしで、RustFS には固定の認証情報（`nanashi` / `nanashi-secret`）でつながる（どちらも手元からだけつながるように 127.0.0.1 に限っている）。

損益計画と人員計画のサンプル（`examples/fpa.py`）は、ベンチマークも兼ねている。

```bash
.venv/bin/python -m examples.fpa --size small --show   # 主要な Metric を表示する
.venv/bin/python -m examples.fpa --size medium         # その規模の時間を測る（既定のエンジンは rust）
```

`--size` を付けないと、large（社員 2 万人、約 490 万セル）まですべての規模を回す。
ほかのベンチマークはリポジトリ直下の `bench*.py` で、大きなモデルではメモリを数 GB 使うので、1 本ずつ実行する。
キーの表し方による速さとメモリの違いは `native/engine/examples/key_layout.rs` で測る（[設計メモ](member-numbering.md)の「キーの表し方の測定」）。

## テストの方針

テストは参照実装と Rust の両方で同じものを回す。
中心になるのは、ランダムな変更（入力の変更、メンバーの追加と削除と名前の変更、締め月の移動、異動）を数百回加えたあとに、次の 3 つが一致することを確かめるテストである。

- 差分再計算の結果
- 同じ入力から全体を計算し直した結果
- 参照実装の結果（Rust のエンジンのとき）

テスト自体がバグを検出できることは、影響範囲の伝搬や差分集計をわざと壊した実装でテストが失敗することで確かめている。
Rust のエンジンの速さのための調整値（`native/engine/src/config.rs` の `Config`）は、`RustEngine(par_min=0, widen_min_rows=0)` のようにエンジンごとに変えられる（受け付ける名前は `native/src/lib.rs` の `configure`）。
大きなモデルでだけ働く経路を小さなモデルでも働かせて、結果が変わらないことを確かめている。

- 並列化と範囲の全体への広げ方（`par_min=0, widen_min_rows=0`）、集計を読みながら行う経路（`stream_always=True`）: `tests/test_redefine.py`
- メモリの予算で式の評価を分けること（`max_bytes`）: `tests/test_failures.py`
- 差分のまとめ直しと転置索引（`compact_min`、`postings_min_rows`）: Rust の性質テスト `native/engine/tests/store_model.rs`

記録先を使うテスト（記録と再生の共通の性質、`Workspace`、HTTP サーバー）は、ファイルの記録先と PostgreSQL の記録先の両方で回す（`tests/journals.py` の `JournalCase`）。
本番で使う組み合わせは Rust のエンジンと PostgreSQL の記録先なので、`Workspace` と HTTP サーバーのテストには、この組み合わせ（Rust のエンジンと `store = PgStore`）のクラスを必ず含める。
PostgreSQL は `NANASHI_PG_DSN`（既定は手元の 55432 番）で指定し、つながらなければその組み合わせのテストはスキップする（CI では PostgreSQL を立てて回す）。

`tests/failover.py` は、書き手を止めても、確定を返した書き込みが失われず二重にもならないことを、2 つのサーバーのプロセスで確かめる。
同じモデルを開いた 2 つのサーバー（書き手と待機系）の書き手に 4 つの送り手が書き込み続ける中で、そのプロセスを止める。
送り手は確定を受け取るまで同じ `client_op_id` で再送し、つながらないときと 503 のときはもう一方へ、421 のときは応答にある書き手へ送る。
両方を止めたあと、記録先の記録と開き直したモデルの値を、受け取った確定と突き合わせ、確定が途切れた最も長い間も測る（`python -m tests.failover --signal TERM`。PostgreSQL が要る。リースの期限は 3 秒にして回す）。
SIGTERM なら約 0.1 秒、SIGKILL ならリースの期限と待機系が権利を試す間隔の分（約 3.2 秒）途切れる。
`--via-router` なら、送り手はルーターにだけ送り、送り先を変えない（ルーターが 200 以外を返せば失敗として数える）。
ルーターの送り直しの判断は、Go のテスト（`router/` で `go test ./...`）で、偽のエンジンと偽の書き手の引き先を使って確かめる。
待機系が書き手の番地を返して書き込みを拒むこと、起動し直したプロセスが待機系として追従することも、同じ仕組みで確かめる（`tests/test_failover.py`）。
待機系の役割の変わり方そのもの（昇格、降格、降格の直前に列に入った書き込み）は、1 つのプロセスの中の 2 つの `Workspace` で確かめる（`tests/test_standby.py`）。

## 構成

| 場所 | 役割 |
|---|---|
| `sparse_engine/model.py` | Model。定義、操作、読み出し、トランザクション、分割軸の選択。ためている変更は `Pending` にまとめ、計画と再計算は Planner に任せる |
| `sparse_engine/planner.py` | 計画と再計算の段取りの口（`Planner`）と、Python の参照実装（`PyPlanner`。計算計画、影響範囲、差分集計、scan） |
| `sparse_engine/evaluate.py` | 型の検査、影響範囲、参照実装の評価器 |
| `sparse_engine/parser.py` | 式の文字列の解析と、構文木から文字列への変換 |
| `sparse_engine/delta.py` | 差分集計の対象になる式の判定 |
| `sparse_engine/engine.py`、`rust_engine.py` | 格納と評価の口（`Store`）と、参照実装、Rust の橋渡し（`RustEngine` と `RustPlanner`） |
| `sparse_engine/storage.py`、`npz.py` | 保存と読み込み（Parquet）と、以前の版の形式（npz）の読み込み |
| `sparse_engine/journal.py` | トランザクションの記録、記録先（ファイル）、スナップショットと記録の再生による復元 |
| `sparse_engine/workspace.py` | 版の公開と単一ライター（同時の読み書き、グループコミット、楽観的な排他） |
| `sparse_engine/pg_journal.py` | PostgreSQL の記録先（リースと締め出し、大量の変更の後からの反映） |
| `sparse_engine/objects.py` | ファイルの置き場所（`put`、`get`、`list`、`delete` を持つ BlobStore。S3 互換か、ローカルのディレクトリ）。記録先のスナップショットと大量の変更のファイルを置く |
| `sparse_engine/server.py` | HTTP サーバー（Workspace を JSON の API で公開する） |
| `router/` | ルーター（Go）。`router.go` が書き手への送り直し（応答ごとの次の動きは `decide`）、`pg.go` が記録先から書き手を引く `PgResolver`、`cmd/nanashi-router/` がコマンド |
| `native/engine/` | Rust のエンジン（`nanashi-engine`、Python に依存しない）。`key.rs` がキーの詰め方、`store.rs` が格納、`ast.rs` が式の構文木、`eval/` が評価（`join.rs` が突き合わせ、`agg.rs` が集計）、`check.rs` が型検査と BY の書き換え、`graph.rs` が計算計画、`plan.rs` が差分集計の判定と影響範囲と再計算の段取り、`pq.rs` が Parquet の読み書き、`config.rs` が速さのための調整値。`tests/` に格納の性質テスト（BTreeMap と突き合わせる） |
| `native/src/lib.rs` | Python から使う薄い層（`nanashi_core`、PyO3）。受け取った番号と長さはここで検査する |
| `examples/fpa.py` | 損益計画と人員計画のサンプル |
| `bench.py`、`bench_metrics.py`、`bench_versions.py`、`bench_journal.py`、`bench_reads.py`、`bench_http.py`、`bench_memory.py` | ベンチマーク |
| `tests/test_expr_coverage.py` | すべての種類の式のノードを、すべての実装の場所（構文、型推論、影響範囲、評価、依存、Rust）に通す |

式の意味は、Python の参照実装と Rust の両方に実装している。
Rust のエンジンを使うときの本番の経路は、構文（`expr.py`、`parser.py`）、名前の解決（`evaluate.resolve` の軸の名前の解決、`rust_engine._tree`）、型検査と BY の書き換え（`check.rs`）、計算計画（`graph.rs`）、差分集計の判定と影響範囲と再計算の段取り（`plan.rs`）、評価（`eval/`）で、Python の `evaluate.py`、`delta.py`、`planner.py` は参照実装にだけ使う。
式のノードを 1 種類足すときは、次のすべてを直す。

- Python: `expr.py`（ノードと `_children`）、`parser.py`（`parse` と `to_formula`）、`evaluate.py`（`infer`、`estimate`、`affected`、`collect_refs`、評価器）、`rust_engine._tree`（Rust へ渡すタプル）。集計にかかわるノードなら `delta.py`（差分集計の判定）も
- Rust: `native/src/lib.rs` の `node`（タプルから構文木へ）、`ast.rs`、`check.rs`、`graph.rs`、`plan.rs`、`eval/`

そのうえで、`tests/test_expr_coverage.py` のモデルにそのノードを使う式を足す。
足し忘れた場所があれば、このテストが失敗する。
