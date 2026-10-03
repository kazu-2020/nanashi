# CLAUDE.md

This file gives guidance to Claude Code (claude.ai/code) when it works with the code in this repository.

nanashi is a monorepo for an EPM and FP&A service (issue #30). It has these directories:

- `tessera/`: the sparse multidimensional calculation engine for planning tools such as Pigment. It has 2 layers: the Python package `sparse_engine` and the Rust engine `nanashi_core` (`tessera/native/`).
- `router/`: a Go router. It goes in front of the engine servers. `router/README.md` is its specification.

The engine specifications are in `tessera/docs/` (see "Documentation" in `tessera/README.md` for the list). If you change behavior or performance, also update the related document and tables in `tessera/docs/`.

All paths in this file below are relative to `tessera/`, except `router/`, `compose.yaml`, and `.github/`.

## Language and writing rules

- Write these items in English that follows ASD-STE100 (Simplified Technical English):
  - Code comments and docstrings (Python, Rust, Go).
  - Documents (`README.md` files, `tessera/docs/`, this file).
  - Commit messages, pull request titles, and pull request descriptions.
- Apply these ASD-STE100 rules:
  - Use a maximum of 20 words in an instruction and 25 words in a description.
  - Write one topic in one paragraph. Use a maximum of 6 sentences in a paragraph.
  - Use the active voice and simple tenses (present, past, future).
  - Write instructions in the imperative. Put a condition before the instruction ("If X, do Y.").
  - Use one word for one meaning. Do not use synonyms for the same thing. Use the terms in the glossary below.
  - Do not use more than 3 nouns in a row.
  - Use short, common words ("use", "start", "make sure", "about").
- Error messages that the user sees (`MESSAGES` in `sparse_engine/messages.py` and the other `raise` texts) stay in Japanese.
- Some old comments and docstrings are still in Japanese. If you change code, write new comments in English. Also translate the old comments for the code that you change. Do not translate unrelated comments in the same change.

### Glossary

| Term | Meaning |
|---|---|
| dimension | A named axis of a Metric (for example, `Employee`, `Month`). |
| member, member number | An item of a dimension, and its internal number. |
| partition dimension | The dimension in the high bits of the key. Each Metric has one. |
| input Metric, formula Metric | A Metric with values that a user enters, and a Metric that a formula calculates. |
| stored data | The cells that a Store keeps for a Metric. |
| base, delta | The immutable sorted columns of stored data, and the persistent tree of changes on top of them. |
| version, published version | A state of the model. Nothing changes a published version. |
| full recalculation, incremental recalculation | Calculate all formula Metrics again, or only the affected range. |
| affected range | The cells that a change can make different. |
| incremental aggregation | Update an aggregation result with only the changed source cells. |
| journal | The record of transactions (`FileJournal`, `PgJournal`). |
| reader, writer, standby, failover | The roles of a server process and the change of the writer. |
| reference implementation | The Python engine (`ReferenceEngine`). It is the standard for correct results. |

## Commands

Run the Python and Rust commands in `tessera/`.

```bash
# Setup (install the Rust engine into .venv. psycopg[binary] connects to PostgreSQL without a local libpq)
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]" "psycopg[binary]"
VIRTUAL_ENV=$PWD/.venv .venv/bin/maturin develop --release -m native/Cargo.toml

# PostgreSQL (port 55432) and the S3-compatible object storage RustFS (port 59000). compose.yaml is at the root
docker compose up -d

# Tests. SPARSE_ENGINE sets the default engine (reference / rust; the default is reference). CI runs both
SPARSE_ENGINE=reference .venv/bin/python -m unittest discover -s tests -t .
SPARSE_ENGINE=rust .venv/bin/python -m unittest discover -s tests -t .

# Run each module in a separate process, in parallel (CI uses this. Tests wait a lot, so this is about 3 times faster than one by one)
SPARSE_ENGINE=rust .venv/bin/python -m tests.parallel

# Only one file, class, or test
SPARSE_ENGINE=rust .venv/bin/python -m unittest tests.test_reads
SPARSE_ENGINE=rust .venv/bin/python -m unittest tests.test_reads.<Class>.<test>

# Rust unit tests (includes the property tests in native/engine/tests/)
cargo test --release --workspace --manifest-path native/Cargo.toml

# Router (Go). The PgResolver tests use the nanashi_model table that the Python tests make
(cd ../router && go vet ./... && go test ./...)

# Static checks (the same as CI)
.venv/bin/pyflakes sparse_engine tests examples bench*.py
cargo clippy --release --workspace --all-targets --manifest-path native/Cargo.toml -- -D warnings
```

- If you change `native/`, run `maturin develop` again before the Python tests. The tests load the `nanashi_core` in `.venv`.
- If a prerequisite is missing, a test is skipped. It does not fail. Always look at the number of skipped tests.
  - If `nanashi_core` is not installed, the Rust engine tests are skipped.
  - If the tests cannot connect to PostgreSQL (`NANASHI_PG_DSN`), or `psycopg` cannot find libpq, the journal tests are skipped.
  - If the tests cannot connect to RustFS (`NANASHI_S3_ENDPOINT`), the S3 tests are skipped.
- To test the writer failover, run `.venv/bin/python -m tests.failover --signal TERM`. It starts 2 server processes and needs PostgreSQL.
- If you do not give `--size`, `examples.fpa` runs all sizes up to large (about 4.9 million cells). It and the benchmarks (`bench*.py`) use some GB of memory. Run them one at a time.
- `docs/development.md` gives more data about the development environment (free-threaded Python 3.14t, the credentials in `compose.yaml`).

## Architecture

`docs/engine.md` gives the overview. "Structure" in `docs/development.md` gives the map of the source files. These points are important for all work.

### Two engines and two implementations

`Model` (`sparse_engine/model.py`) has only the definitions, the operations, the reads, the transactions, and the selection of the partition dimension. It sends all other work to the 2 interfaces of the engine:

- **`engine.Store`**: storage and evaluation.
- **`planner.Planner`**: type check, calculation plan, decision for incremental aggregation, affected range, and the schedule of the recalculation.

| | Store | Planner |
|---|---|---|
| Reference implementation (`ReferenceEngine`) | Python dict | `PyPlanner`, `evaluate.py`, `delta.py` |
| Rust (`RustEngine`) | `native/engine/src/store.rs`, `eval/` | `RustPlanner` → `check.rs`, `graph.rs`, `plan.rs` |

- The reference implementation is the standard for correct results.
- On the Rust path, Python does only the syntax and the name resolution.
- All methods of Store and Planner are necessary. Model does not examine if a method is available. If you add a method, add it to both the reference implementation and Rust.
- `native/engine/` (crate `nanashi-engine`, no Python dependency) does the calculation. `native/src/lib.rs` (PyO3) is the bridge. It examines the numbers and lengths that it gets from Python.

### Add or change an expression node

"Structure" in `docs/development.md` gives the list of the files to change.

- Python: `expr.py`, `parser.py`, `evaluate.py`, `rust_engine._tree`
- Rust: the `node` function in `native/src/lib.rs`, `ast.rs`, `check.rs`, `graph.rs`, `plan.rs`, `eval/`

Then add a formula that uses the node to the model in `tests/test_expr_coverage.py`. If you forget a file, this test fails.

- Formula errors and warnings (from the type check and the cycle check) do not have text in Rust. Rust returns a code and values (`nanashi_core.Diagnostic`). Python changes them into `FormulaError.code` and `.params`. Only `MESSAGES` in `sparse_engine/messages.py` has the message text.
- `expr.AGGREGATIONS` is the only location for the aggregation functions.

### Rules that you must not break

- The key of a cell is a u64 that contains the member numbers of all dimensions. The type check rejects a Metric if the total bit width of its dimensions is more than 64. Do the same check for intermediate results and for new members.
- Do not change a published version.
  - Versions share the base of the stored data. A persistent tree keeps the delta.
  - `fork`, transaction rollback, and the versions of `Workspace` need this: a writer can write while a reader keeps an old version.
- Do not use a `Model` directly from more than one thread. For concurrent use, use `Workspace` or `Replica`.
- An HTTP write must have a `client_op_id`. If a client sends the write again, the server does not commit it two times.
- The production journal is `PgJournal`. `FileJournal` is mainly for development and tests.
- The router (`router/README.md`) resends writes. Because of `client_op_id`, a resent write is not committed two times. If you change the next action for each response (`decide` in `router.go`), keep this condition.

## Test policy

- The main tests apply hundreds of random changes (inputs, add, delete, or rename of members, transfers, and more). Then they compare the result of the Rust incremental recalculation with the reference implementation and with a full recalculation.
- If you change the propagation of the affected range or the incremental aggregation, make sure that the tests fail with an intentionally broken implementation.
- You can set the tuning values (`native/engine/src/config.rs`) for each engine, for example `RustEngine(par_min=0, widen_min_rows=0)`. Tests use this to run, on small models, the paths that usually run only on large models (`tests/test_redefine.py`, `tests/test_failures.py`, `native/engine/tests/store_model.rs`).
- Tests that use a journal must inherit `JournalCase` in `tests/journals.py`. Then they run with both the file journal and PostgreSQL. Tests for `Workspace` and the HTTP server must include a class with the production combination (the Rust engine and `store = PgStore`).
- Tests for the limit checks (`Model(max_cells=...)`, `RustEngine(max_bytes=...)`) must stay small also when the check does not work. If the check fails, the engine tries to allocate billions of cells.
