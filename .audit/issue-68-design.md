# Issue 68 design: id keys inside the engine

Source: https://github.com/kazu-2020/nanashi/issues/68. Read it first.

## Decision: the internal key is the int handle

`Model._new_id()` gives each dimension, member, property and Metric a handle. Handles are unique in one model across all kinds. The journal `changes`, the parquet columns (`d<id>`), `nanashi_cell_change` and the Workspace conflict check already use handles. UUID keys would change all of those formats. So the internal key is the handle (`int`). The UUID stays at the HTTP boundary (`Model.ids`, `Model._uuids`).

## Target data shapes

- `Model.metrics: dict[int, Metric]` (key = `Metric.id`). `Model.dimensions: dict[int, Dimension]` (key = `Dimension.id`). Insertion order = definition order, as now.
- One name index per kind, kept in sync by define, rename and remove: Metric name -> handle, dimension name -> handle, and per dimension: property name -> handle, member name -> position (`Dimension._index`, exists).
- `Metric.dims: tuple[int, ...]` (dim handles). `Metric.partition: int | None`. `Metric.kind`: `"number"`, `"boolean"` or `"member:<dim handle>"`.
- The hidden override input: link it by handle from its owner (for example `Metric.override: int | None`). Its name stays `__override__<owner name>` and changes with the owner name.
- `Dimension.properties: dict[int, tuple[int, dict[int, int]]]` = prop handle -> (target dim handle, {member handle: member handle}). Property names are an attribute (prop handle -> name, plus the name index).
- `Restrict = dict[int, frozenset[int]]` = dim handle -> member handles.
- Reference engine `Cube`: `dims` are dim handles, keys are tuples of member handles. Member-type values stay member numbers (positions) inside the engines, as now.
- `Pending`, `MetricState`, `_plan` (`Step.names`), `_edges`, `DeltaPlan.source/aux`, estimates, samples: all keyed by Metric handle.
- Planner synthetic refs (`__new{i}`, `__old_value`, ...): use keys that cannot collide with handles (for example negative ints).
- AST reference nodes hold handles: `Ref.name` (Metric), `DimRef.dim`, `Member.dim/member`, `Expand.dims`, `By.dim/prop` (prop = property handle or member-type Metric handle; handles are unique, so no ambiguity), `Remove.dim`, `Shift.dim`, `AsAxis.dim`, `Select.dim/member`.
- `parse(text)` still gives a name AST. A bind step (name AST -> handle AST) runs at definition time (`add_formula`). Bind errors are `FormulaError` with the names the user wrote (`unknown_metric`, `unknown_dim`, `unknown_member`, `no_property`). The Python builders in `expr.py` (`ref("Price")`) also make name ASTs that go through bind.
- `to_formula(ast, model)` makes the display text from the current names (GET /, errors, diagnostics).
- Logs (`eval_log`, `delta_log`, `slice_log`) are observation data. They keep handles and show current names when read (`slice_log` already converts lazily).

## The Python API stays name-based

- Public Model methods take names as now. They also accept a handle (`int`) for a Metric, dimension, member or property argument, so the server can pass handles. Coordinates stay `**coords` with dimension names as keys; a member value is a name or a handle.
- Public reads (`value`, `slice`, `rows`, `summarize`, `get`, `cell_history`) return names as now. The server can use internal handle reads to skip the name step.
- Add `Model.metric(name_or_handle) -> Metric`. `Model.dimension(name_or_handle)` exists. Tests that index internals by name (`m.metrics["X"]`, `m.dimensions["X"]`, `m.layout["X"]`, ...) change to these accessors or to `[m.metric("X").id]`. Use a codemod script for the mechanical test edits.

## Errors

- Error text that the user sees stays Japanese and shows current names.
- Python type check and plan errors may carry handles in `FormulaError.params`. Translate them to names at one point when they leave the Model (a table of param keys in `messages.py` says which params are a Metric, a dimension, a list of dims, a property or a member). `FormulaError.params` that callers see hold names, as now.
- Rust diagnostics use `DimInfo.name` and the per-call Metric names. Keep `DimInfo.name` current when a dimension is renamed (add a small Rust call), or change the Rust diagnostics to numbers. Pick the smaller change.

## Renames become attribute changes

- `rename_metric`, `rename_member`, `rename_dimension`, `rename_property`: check the new name against the name index, change the attribute and the index. No recalc, no re-keying, no formula rewrite. `expr.rename_metrics`, `expr.rename_member`, `delta.rename`, `engine.rename_member` go away.
- Re-defining an object by UUID with a new name (add_dimension, add_property, add_member, add_input/add_formula) renames it. The "a dimension or a property cannot change its name" rule goes away.

## Persistence (no compatibility needed: the system is not in production)

- journal `changes` and `model.json` keep formulas as the handle AST (a JSON tree), not as text. Replay does not depend on the order of renames. Bump `LOG_VERSION` and `FORMAT_VERSION`.
- `changes` records dimension and property renames.

## Units (each ends green on both engines)

1. Metrics keyed by handle; `Ref` and Metric `By` hold handles; `rename_metric` attribute-only.
2. Dimensions and properties keyed by handle; dim fields of the AST hold handles; `Restrict` keys are dim handles; add `rename_dimension`, `rename_property`.
3. Members keyed by handle in `Restrict`, reference cubes, property maps, `Pending.added`, the AST; `rename_member` attribute-only.
4. Persistence: handle AST in the journal and `model.json`; replay without rename order.
5. Server: UUID -> handle at the boundary; `rename_dimension` and `rename_property` HTTP ops; docs (`docs/ids.md`, `tessera/docs/`).
6. api: `RenameList` and property rename RPCs.

## Gates for every unit

- `SPARSE_ENGINE=reference .venv/bin/python -m tests.parallel` and `SPARSE_ENGINE=rust ...`: all pass. Baseline is 756 tests, 42 skipped (S3). The skip count must not grow.
- `.venv/bin/pyflakes sparse_engine tests examples bench*.py`.
- If `native/` changes: `cargo test --release --workspace --manifest-path native/Cargo.toml`, clippy with `-D warnings`, then `maturin develop --release` before the Python tests.
- New behavior gets tests first (red, then green).
