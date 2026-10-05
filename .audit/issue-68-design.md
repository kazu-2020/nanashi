# Issue 68 design: id keys inside the engine

Source: https://github.com/kazu-2020/nanashi/issues/68. Read it first.

## Decision: the internal key is the UUID

The operator ruled out change cost as a criterion (nothing is released). On architectural benefit the UUID wins:

- One identifier for one object across web, api, the engine, the journal and the cell history. The UUID -> handle maps (`Model.ids`, `Model._uuids`) and `_new_id()` go away, and the HTTP boundary passes ids through.
- A UUID is unique everywhere. Handles collide across a fork (see the `_new_id` docstring), so copy, merge, snapshot and restore across models need no renumbering with UUIDs.
- The journal and `nanashi_cell_change` become readable by `api/` without the engine's private numbers.
- Measured (scratchpad `keys.py`, 1,000,000 members, Python 3.12): the id maps take 59 MB with UUID keys and 183 MB with handles, because the handle design must also keep both UUID maps. One UUID -> member number lookup at the HTTP boundary is faster (37.7 ms per 100,000) than UUID -> handle -> number (52.2 ms). An internal-only lookup is slower with UUID keys (34.1 ms against 14.1 ms per 100,000). The production calculation runs in Rust on dense member numbers, so this path is the reference engine and the Restrict conversion only.

The Rust dense numbers and the member numbers (positions) stay engine-internal, as now.

## Target data shapes

- `Model.metrics: dict[str, Metric]` (key = `Metric.id`, the UUID). `Model.dimensions: dict[str, Dimension]` (key = `Dimension.id`). Insertion order = definition order, as now.
- One name index per kind, kept in sync by define, rename and remove: Metric name -> id, dimension name -> id, and per dimension: property name -> id, member name -> position (`Dimension._index`, exists).
- `Metric.dims: tuple[str, ...]` (dim ids). `Metric.partition: str | None`. `Metric.kind`: `"number"`, `"boolean"` or `"member:<dim id>"`.
- The hidden override input: link it by id from its owner (for example `Metric.override: str | None`). Its name stays `__override__<owner name>` and changes with the owner name.
- `Dimension.properties: dict[str, tuple[str, dict[str, str]]]` = prop id -> (target dim id, {member id: member id}). Property names are an attribute (prop id -> name, plus the name index). `Dimension.ids` holds the member UUIDs by position, `_by_id` maps a member id to its position.
- `Restrict = dict[str, frozenset[str]]` = dim id -> member ids.
- Reference engine `Cube`: `dims` are dim ids, keys are tuples of member ids. Member-type values stay member numbers (positions) inside the engines, as now.
- `Pending`, `MetricState`, `_plan` (`Step.names`), `_edges`, `DeltaPlan.source/aux`, estimates, samples: all keyed by Metric id.
- Planner synthetic refs (`__new{i}`, `__old_value`, ...): use keys that are not in the canonical UUID form (for example `__new0`).
- AST reference nodes hold ids: `Ref.name` (Metric), `DimRef.dim`, `Member.dim/member`, `Expand.dims`, `By.dim/prop` (prop = property id or member-type Metric id; ids are unique, so no ambiguity), `Remove.dim`, `Shift.dim`, `AsAxis.dim`, `Select.dim/member`.
- `parse(text)` still gives a name AST. A bind step (name AST -> id AST) runs at definition time (`add_formula`). Bind errors are `FormulaError` with the names the user wrote (`unknown_metric`, `unknown_dim`, `unknown_member`, `no_property`). The Python builders in `expr.py` (`ref("Price")`) also make name ASTs that go through bind.
- `to_formula(ast, model)` makes the display text from the current names (GET /, errors, diagnostics).
- Logs (`eval_log`, `delta_log`, `slice_log`) are observation data. They keep ids and show current names when read (`slice_log` already converts lazily).

## The Model API takes ids only

- Target: each public Model method takes ids for a Metric, dimension, member or property. Coordinates are a mapping dim id -> member id. Reads return ids (cube keys, rows, member-type values). Definitions take an optional id and return the id they used. One argument has one meaning, so no guess between a name and an id.
- Formula text stays written with names (that is what users write). Binding the text to ids at definition is the only name lookup inside the Model. `to_formula` makes the display text.
- Name lookup for people (tests, examples, benchmarks, notebooks) is a separate, explicit layer outside the Model core: a small facade that turns names into ids before it calls the Model and turns ids into names in results. The server does not use it.
- Bridge during units 1-3: a string argument is looked up first as an id, then as a name, so the existing tests stay green while the keys change. A later unit (see Units) deletes the bridge, makes the Model API id-only, and moves tests, examples and benchmarks to the facade.
- Add `Model.metric(id) -> Metric`. `Model.dimension(id)` exists. Use a codemod script for mechanical test edits.

## Errors

- Error text that the user sees stays Japanese and shows current names.
- Python type check and plan errors may carry ids in `FormulaError.params`. Translate them to names at one point when they leave the Model (a table of param keys in `messages.py` says which params are a Metric, a dimension, a list of dims, a property or a member). `FormulaError.params` that callers see hold names, as now.
- Rust diagnostics use `DimInfo.name` and the per-call Metric names. Keep `DimInfo.name` current when a dimension is renamed (add a small Rust call), or change the Rust diagnostics to numbers. Pick the smaller change.

## Renames become attribute changes

- `rename_metric`, `rename_member`, `rename_dimension`, `rename_property`: check the new name against the name index, change the attribute and the index. No recalc, no re-keying, no formula rewrite. `expr.rename_metrics`, `expr.rename_member`, `delta.rename`, `engine.rename_member` go away.
- Re-defining an object by UUID with a new name (add_dimension, add_property, add_member, add_input/add_formula) renames it. The "a dimension or a property cannot change its name" rule goes away.

## Persistence (no compatibility needed: the system is not in production)

- journal `changes` and `model.json` keep formulas as the id AST (a JSON tree), not as text.
- journal `changes`, parquet column names, `nanashi_cell_change` (`metric_id`, `coords`) and the Workspace conflict keys use UUIDs instead of handles. A member-type value in the cell history is a member UUID. Replay does not depend on the order of renames. Bump `LOG_VERSION` and `FORMAT_VERSION`.
- `changes` records dimension and property renames.

## Units (each ends green on both engines)

1. Metrics keyed by UUID; `Ref` and Metric `By` hold ids; `rename_metric` attribute-only.
2. Dimensions and properties keyed by UUID; dim fields of the AST hold ids; `Restrict` keys are dim ids; add `rename_dimension`, `rename_property`.
3. Members keyed by UUID in `Restrict`, reference cubes, property maps, `Pending.added`, the AST; `rename_member` attribute-only.
4. Persistence: id AST in the journal and `model.json`; UUIDs in `changes`, parquet and `nanashi_cell_change`; delete `_new_id`, `Model.ids`, `Model._uuids`; replay without rename order.
5. Model API id-only: delete the bridge; add the name facade; move tests, examples and benchmarks to it.
6. Server: ids pass through the boundary; `rename_dimension` and `rename_property` HTTP ops; docs (`docs/ids.md`, `tessera/docs/`).
7. api: `RenameList` and property rename RPCs.

## Gates for every unit

- `SPARSE_ENGINE=reference .venv/bin/python -m tests.parallel` and `SPARSE_ENGINE=rust ...`: all pass. Baseline is 756 tests, 42 skipped (S3). The skip count must not grow.
- `.venv/bin/pyflakes sparse_engine tests examples bench*.py`.
- If `native/` changes: `cargo test --release --workspace --manifest-path native/Cargo.toml`, clippy with `-D warnings`, then `maturin develop --release` before the Python tests.
- New behavior gets tests first (red, then green).
