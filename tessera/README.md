# tessera

tessera is the sparse multidimensional calculation engine for planning tools such as Pigment.
It keeps values in units of **Metrics**, and each Metric has named dimensions.
A formula applies to the full Metric, not to each cell as in Excel.
The engine keeps only the cells that have a value.
If you change one input cell, the engine calculates again only the affected range.

The goal is interactive response speed also for large models.
An example is a profit and loss plan with 20,000 employees, 5,000 products and 36 months (about 4.9 million cells, 18 formula Metrics).
In this plan, a full recalculation takes about 90 ms.
A change to a salary or to a department assignment takes less than 1 ms.

## Getting started

You need Python 3.12 or later and the Rust toolchain (cargo).
Run the commands in the `tessera/` directory.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
VIRTUAL_ENV=$PWD/.venv .venv/bin/maturin develop --release -m native/Cargo.toml
SPARSE_ENGINE=rust .venv/bin/python -m unittest discover -s tests -t .
```

The second line from the end builds the Rust engine (`nanashi_core`) and installs it in `.venv`.
Without the Rust engine, you can calculate with only the reference implementation engine.
But save and load (Parquet) uses `nanashi_core`.
[docs/development.md](docs/development.md) gives information about the free-threaded Python, the tests that use PostgreSQL and object storage, the examples and the benchmarks.

## Minimal example

```python
from sparse_engine import Model, Named
from sparse_engine.rust_engine import RustEngine

m = Named(Model(engine=RustEngine()))  # Named takes names. Model itself takes ids (see below)
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

m.set_cell("DeptOf", "開発", Employee="bob", Month="Mar")   # bob moves to a different department in March
print(m.value("Cost").format(m.dimensions))
```

Use `add_input` to add an input Metric.
Use `add_formula` to add a Metric that a formula calculates.
You set the dimensions and the value kind of a Metric when you add it.

`Model` takes and gives ids (UUIDs) for a Metric, a dimension, a member and a property. Coordinates are a mapping
`{dimension id: member id}`. Each definition returns the id that it used. Only the formula text uses names.
`Named` (`sparse_engine/named.py`) wraps a `Model` and changes names to ids before each call, and ids to names in
the results. Use `Named` in scripts, tests and notebooks. Use `Model` where ids come from outside (the HTTP server).

```python
model = m.model                                   # the Model under the facade
cost, dept = m.metric("Cost").id, m.dimension_id("Department")
model.get(cost, {dept: m.member_id("Department", "開発"), m.dimension_id("Month"): m.member_id("Month", "Mar")})
```
You cannot change them later.
When you read a value, the engine calculates again only the range that changed.

## Documentation

| Document | Contents |
|---|---|
| [docs/modeling.md](docs/modeling.md) | Read values, dimensions and members (add, rename, delete, ID), plan input (override, spread), what-if analysis |
| [docs/formulas.md](docs/formulas.md) | The formula language, the type check, the estimate of the number of cells, how blank values work |
| [docs/recalculation.md](docs/recalculation.md) | The calculation plan and incremental recalculation, incremental aggregation, changes to definitions |
| [docs/engine.md](docs/engine.md) | The structure of the engine (Store and Planner), the design of the reference implementation and of the Rust engine |
| [docs/persistence.md](docs/persistence.md) | Save and load, transactions and the journal, the PostgreSQL journal |
| [docs/concurrency.md](docs/concurrency.md) | Concurrent reads and writes (`Workspace`, `Replica`, failover to a standby) |
| [docs/server.md](docs/server.md) | The API of the HTTP server, authentication, limits, how to stop the server |
| [router/README.md](../router/README.md) | The Go router that resends requests to the writer (`router/` at the root of the repository) |
| [docs/performance.md](docs/performance.md) | Performance measurements (recalculation, reads, free-threaded Python, memory, journals, save formats) |
| [docs/development.md](docs/development.md) | The development environment, the test policy, the source structure, and the locations to change when you add an expression node |
| [docs/limitations.md](docs/limitations.md) | Limitations and future work |
| [docs/member-numbering.md](docs/member-numbering.md) | Design note: member names, IDs, numbers, ranks, tombstones for deleted members, wider keys, reorganization of Views (the first stage of the separation of numbers and ranks is implemented) |
| [docs/out-of-core.md](docs/out-of-core.md) | Design note: a plan to keep stored data on NVMe (SSD), and measurements of pread, mmap and heap (not implemented) |
| [docs/scale-out.md](docs/scale-out.md) | Design note: separate storage from calculation, workers for heavy recalculations, readers for simulations (not implemented) |

## License

MIT License ([LICENSE](../LICENSE)).
