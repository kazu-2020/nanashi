# Development

## Getting started

You need Python 3.12 or later and the Rust toolchain (cargo).
Run the commands in this document in the `tessera/` directory.
`compose.yaml` and the router (`router/`) are at the root of the repository.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
VIRTUAL_ENV=$PWD/.venv .venv/bin/maturin develop --release -m native/Cargo.toml
```

`pyproject.toml` sets the versions of the dependencies.
At run time, the engine needs no Python packages.
For development, it uses maturin, psycopg, boto3 and pyflakes, and numpy to make the benchmark input.
The last line builds the Rust engine (`nanashi_core`) and installs it in `.venv`.
Without the Rust engine, you can calculate with only the reference implementation engine.
Save and load (Parquet read and write) uses `nanashi_core`, also with the reference implementation engine.

For a server with many readers, use the free-threaded Python (3.14t).
The Rust engine declares that it operates without the GIL (`gil_used = false`).
Versions share the stored data through Arc and a persistent tree.
Thus, writes do not wait for readers, also when there are many readers (the table in [Performance](performance.md)).

```bash
brew install python-freethreading          # macOS. On Linux, use the distribution package or uv
python3.14t -m venv .venv-ft
.venv-ft/bin/pip install -e ".[dev]"
VIRTUAL_ENV=$PWD/.venv-ft .venv-ft/bin/maturin develop --release -m native/Cargo.toml
```
GitHub Actions (`.github/workflows/tessera-test.yml`) runs the tests and the static checks with the two engines.
The workflow starts only when `tessera/` or the workflow file changes.
The Python tests run in one job for each Python version.
The Rust unit tests and clippy run in parallel in a different job.
The cache keeps the built `nanashi_core` for each content of `native/`.
If `native/` did not change, the job does not build it again.
For branches other than `main`, the tests run on pull requests, not on push.

Run the tests as follows.
Use `SPARSE_ENGINE` to select the default engine (`reference` or `rust`; if you do not set it, `reference`).
CI runs the tests with the two engines.

```bash
SPARSE_ENGINE=rust .venv/bin/python -m unittest discover -s tests -t .
SPARSE_ENGINE=rust .venv/bin/python -m unittest tests.test_reads   # one file (you can also select a class or a test)
SPARSE_ENGINE=rust .venv/bin/python -m tests.parallel   # runs each module in a different process in parallel (CI uses this)
cargo test --release --workspace --manifest-path native/Cargo.toml   # Rust unit tests
```

If you change `native/`, build again with `maturin develop` before you run the Python tests.
The tests read the `nanashi_core` that is installed in `.venv`.
If you did not build `nanashi_core`, the tests for the Rust engine do not fail, but they are skipped.
Thus, also look at the number of skipped tests.
Most of the test time is waiting: round trips to PostgreSQL, server starts and lease expiry.
Thus, `tests.parallel` is about 3 times faster than a sequential run (37 seconds, not 137 seconds, with 4 cores).
At the end, it shows the number of tests and skipped tests for each module, and the total.

The static checks are the same 2 checks as in CI.

```bash
.venv/bin/pyflakes sparse_engine tests examples bench*.py
cargo clippy --release --workspace --all-targets --manifest-path native/Cargo.toml -- -D warnings
```

To use the PostgreSQL journal (`PgJournal`) or to run its tests, install PostgreSQL and `psycopg`.
The journal keeps snapshots and files of large changes in an S3-compatible object storage (with `boto3`).
On your local computer, use the Docker image of [RustFS](https://github.com/rustfs/rustfs) (Apache-2.0) in place of S3.
`compose.yaml` starts PostgreSQL and RustFS together.

```bash
.venv/bin/pip install "psycopg[binary]"
docker compose up -d   # PostgreSQL on port 55432, RustFS on port 59000 (S3 API) and port 59001 (console)
```

The tests connect to `NANASHI_PG_DSN` (default `postgresql://postgres@127.0.0.1:55432/nanashi`) and `NANASHI_S3_ENDPOINT` (default `http://127.0.0.1:59000`).
If the tests cannot connect to PostgreSQL, they skip the journal tests.
This also occurs when `psycopg` cannot find libpq.
If the tests cannot connect to RustFS, they skip only the tests that keep files in object storage.
The tests that keep files in a local directory still run.
The tests make the test bucket (`nanashi-test`).

`compose.yaml` is for development and tests only.
The connection to PostgreSQL has no password.
The connection to RustFS uses fixed credentials (`nanashi` / `nanashi-secret`).
The two services accept connections only from 127.0.0.1, thus only from the local computer.

The example of a profit and loss plan and a headcount plan (`examples/fpa.py`) is also a benchmark.

```bash
.venv/bin/python -m examples.fpa --size small --show   # shows the main Metrics
.venv/bin/python -m examples.fpa --size medium         # measures the time for this size (the default engine is rust)
```

If you do not give `--size`, the example runs all sizes up to large (20,000 employees, about 4.9 million cells).
The other benchmarks are the `bench*.py` files in the repository root.
With large models, they use some GB of memory.
Thus, run them one at a time.
We removed the measurement programs for key layouts ([member-numbering.md](member-numbering.md)) and for stored data on NVMe ([out-of-core.md](out-of-core.md)). They are in the git history.

## Test policy

The same tests run with the reference implementation and with Rust.
The main test applies hundreds of random changes.
The changes are input changes, member additions, deletions and renames, moves of the closing month, and department transfers.
After the changes, the test makes sure that these 3 results are the same:

- The result of the incremental recalculation
- The result of a full recalculation from the same input
- The result of the reference implementation (for the Rust engine)

We make sure that the tests can find bugs.
For this, we break the propagation of the affected range or the incremental aggregation on purpose, and make sure that the tests fail.
You can change the tuning values for the speed of the Rust engine (`Config` in `native/engine/src/config.rs`) for each engine, for example `RustEngine(par_min=0, widen_min_rows=0)`.
`configure` in `native/src/lib.rs` shows the names that it accepts.
With these values, the paths that operate only for large models also operate for small models.
The tests make sure that the results do not change.

- Parallel execution and the widening of a range to the full range (`par_min=0, widen_min_rows=0`), the path that aggregates while it reads (`stream_always=True`): `tests/test_redefine.py`
- Fusion of operations (match with `fuse=False`), the unit of parallel work (`chunk=3`), aggregation into an array of all combinations of the aggregation target (`dense_always=True`): `tests/test_fusion.py`
- Split of formula evaluation by the memory budget (`max_bytes`): `tests/test_failures.py`
- Compaction of deltas and the inverted index (`compact_min`, `postings_min_rows`): the Rust property test `native/engine/tests/store_model.rs`

The tests that use a journal (the common properties of journal and replay, `Workspace`, the HTTP server) run with the file journal and with the PostgreSQL journal (`JournalCase` in `tests/journals.py`).
In production, the combination is the Rust engine and the PostgreSQL journal.
Thus, the tests for `Workspace` and the HTTP server must include a class for this combination (the Rust engine and `store = PgStore`).
Set PostgreSQL with `NANASHI_PG_DSN` (the default is local port 55432).
If the tests cannot connect, they skip the tests for this combination (CI starts PostgreSQL and runs them).

`tests/failover.py` uses 2 server processes.
It makes sure that a write that got a commit response is not lost and is not committed two times when the writer stops.
The 2 servers (the writer and the standby) open the same model.
4 senders write to the writer continuously, and the test stops the writer process during the writes.
Each sender resends with the same `client_op_id` until it receives a commit.
If the sender cannot connect or gets 503, it sends to the other server.
If it gets 421, it sends to the writer that the response gives.
After the test stops the two servers, it compares the journal and the values of the opened-again model with the received commits.
It also measures the longest gap in commits (`python -m tests.failover --signal TERM`; it needs PostgreSQL, and it uses a lease time of 3 seconds).
With SIGTERM, the gap is about 0.1 seconds.
With SIGKILL, the gap is the lease time plus the interval at which the standby tries to get the writer right (about 3.2 seconds).
With `--via-router`, the senders send only to the router and do not change the destination (if the router returns a status other than 200, the test counts a failure).
The Go tests (`go test ./...` in `router/` at the root of the repository) make sure that the router makes correct resend decisions.
They use a fake engine and a fake writer lookup.
The same method also makes sure of two other properties (`tests/test_failover.py`).
A standby rejects writes and returns the address of the writer.
A process that starts again follows the journal as a standby.
The role changes of the standby (promotion, demotion, writes that entered the queue immediately before a demotion) are tested with 2 `Workspace` objects in one process (`tests/test_standby.py`).

## Structure

| Location | Function |
|---|---|
| `sparse_engine/model.py` | Model. Definitions, operations, reads, transactions, selection of the partition dimension. It collects pending changes in `Pending`, and gives planning and recalculation to the Planner |
| `sparse_engine/named.py` | `Named`, the name facade on a Model. It changes names to ids before each call and ids to names in the results. Tests, examples and benchmarks use it. The server does not |
| `sparse_engine/planner.py` | The interface for the plan and the recalculation schedule (`Planner`), and the Python reference implementation (`PyPlanner`: calculation plan, affected range, incremental aggregation, scan) |
| `sparse_engine/evaluate.py` | The type check, the affected range, the evaluator of the reference implementation |
| `sparse_engine/parser.py` | Parse of formula text, and conversion from the syntax tree to text |
| `sparse_engine/delta.py` | The decision about which formulas can use incremental aggregation |
| `sparse_engine/engine.py`, `rust_engine.py` | The interface for storage and evaluation (`Store`), the reference implementation, the bridge to Rust (`RustEngine` and `RustPlanner`) |
| `sparse_engine/storage.py` | Save and load (Parquet) |
| `sparse_engine/journal.py` | The transaction journal, the file journal, recovery from a snapshot and journal replay |
| `sparse_engine/workspace.py` | Publication of versions and the single writer (concurrent reads and writes, group commit, optimistic locking) |
| `sparse_engine/pg_journal.py` | The PostgreSQL journal (lease and fencing, deferred application of large changes) |
| `sparse_engine/objects.py` | The file location (a BlobStore with `put`, `get`, `list` and `delete`; S3-compatible or a local directory). It keeps the snapshots and the files of large changes for the journal |
| `sparse_engine/server.py` | The HTTP server (it makes a Workspace available through a JSON API) |
| `../router/` | The router (Go). `router.go` resends to the writer (`decide` sets the next action for each response). `pg.go` is `PgResolver`, which finds the writer in the journal. `proxy.go` trusts the user header only from `--trusted-proxy`. `cmd/nanashi-router/` is the command |
| `native/engine/` | The Rust engine (`nanashi-engine`, no dependency on Python). `key.rs` packs keys. `store.rs` is the storage. `ast.rs` is the syntax tree of formulas. `eval/` is the evaluation (`fuse.rs` fuses element-wise operations, `join.rs` does joins, `agg.rs` does aggregation). `check.rs` does the type check and the BY rewrite. `graph.rs` makes the calculation plan. `plan.rs` does the incremental aggregation decision, the affected range and the recalculation schedule. `pq.rs` reads and writes Parquet. `config.rs` has the tuning values for speed. `tests/` has the property tests for the storage (they match the results with a BTreeMap) |
| `native/src/lib.rs` | The thin layer for Python (`nanashi_core`, PyO3). It checks the numbers and lengths that it receives |
| `examples/fpa.py` | The example of a profit and loss plan and a headcount plan |
| `bench.py`, `bench_metrics.py`, `bench_versions.py`, `bench_journal.py`, `bench_reads.py`, `bench_http.py`, `bench_memory.py`, `bench_layout.py` | Benchmarks |
| `tests/test_expr_coverage.py` | It sends all kinds of expression nodes through all implementation locations (syntax, type inference, affected range, evaluation, dependencies, Rust) |

The Python reference implementation and Rust both implement the meaning of formulas.
With the Rust engine, the production path has these parts:

- Syntax (`expr.py`, `parser.py`)
- Name resolution (resolution of dimension names in `evaluate.resolve`, `rust_engine._tree`)
- Type check and BY rewrite (`check.rs`)
- Calculation plan (`graph.rs`)
- Incremental aggregation decision, affected range and recalculation schedule (`plan.rs`)
- Evaluation (`eval/`)

The Python files `evaluate.py`, `delta.py` and `planner.py` are only for the reference implementation.
If you add a kind of expression node, change all of these locations:

- Python: `expr.py` (the node and `_children`), `parser.py` (`parse` and `to_formula`), `evaluate.py` (`infer`, `estimate`, `affected`, `collect_refs`, the evaluator), `rust_engine._tree` (the tuple for Rust). If the node is related to aggregation, also `delta.py` (the incremental aggregation decision)
- Rust: `node` in `native/src/lib.rs` (from the tuple to the syntax tree), `ast.rs`, `check.rs`, `graph.rs`, `plan.rs`, `eval/`

Then add a formula that uses the node to the model in `tests/test_expr_coverage.py`.
If you forgot a location, this test fails.
