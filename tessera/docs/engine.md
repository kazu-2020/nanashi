# Engine

The Model does the definitions, the operations, the transactions, and the selection of the partition dimension. The **engine** does all other work.
The engine has two roles. The first role is to store cells and evaluate formulas (`engine.Store`). The second role is to make the plan and the schedule (`engine.planner`, `planner.Planner`).
The Planner does these tasks:

- The type check of formulas: dimensions and value kinds, warnings for densifying operations, error messages, and the rewrite of BY with a Metric.
- The calculation plan: the dependency graph, strongly connected components, the scan decision, and the stages.
- The incremental aggregation decision and the delta expression.
- The calculation of the affected range: the propagation of input changes, the selection of the partition dimension, and the range that a member removal changes.
- The schedule of the recalculation.

In the Rust engine, Rust (`check.rs`, `graph.rs`, `plan.rs`) does these tasks (`RustPlanner`). In the reference implementation, Python (`planner.PyPlanner`, `evaluate.py`, `delta.py`) does them.
All interfaces are mandatory, and the Model does not check if an interface is available. If an engine does not have an interface, the call fails. The engine does not silently use a different path.
The full recalculation uses the same schedule as the incremental recalculation. The schedule tells the engine to calculate all formula Metrics again for all cells.
Tests make sure that the two engines give the same types, errors, plans, incremental aggregation decisions, and ranges (`tests/test_expr_coverage.py`).

A formula error (`FormulaError`) or a warning has a code (`e.code`) and values (`e.params`). Only the table in `sparse_engine/messages.py` (`MESSAGES`) contains the messages.
The Rust type check and the Rust cycle check also do not make messages. They return a code and values (`nanashi_core.Diagnostic`), and Python makes the message from the same table.
The aggregation functions are in one location, `expr.AGGREGATIONS`. These functions are SUM, AVG, MIN, MAX, COUNT, and `first`, which the BY mapping uses internally. The syntax, the type check, the evaluation, and the aggregated reads get the functions from that location.
There are two engines:

- **Reference implementation** (`ReferenceEngine`): It keeps the data in Python dicts. It is the standard for correctness. Tests match the results of the other engine against it.
- **Rust** (`RustEngine`, `native/`): It is for production. Because of the methods below, it does small changes at almost no fixed cost. It does large recalculations in parallel.

The Rust engine keeps the key of one cell as a 64-bit integer. The integer contains the member number of each dimension.
The storage has two layers. The base is an array in key order. The delta is a tree on top of the base, and it receives small changes.
If the delta becomes larger than 1/8 of the base, the engine merges the delta into a new base.
If one change writes more than 1/8 of the base, the engine makes a new base directly and does not use the delta tree.
The engine does not change a base after it makes it, and versions share the base. The delta is a persistent tree (`OrdMap` of `imbl`), so a change does not break old versions.
Thus, a writer can write while a reader keeps a published version. The engine does not copy all of the base or the delta.
The keys and the values of the base are immutable arrays with no extra capacity (`Box<[u64]>`, `Box<[f64]>`). Their layout is the same as the buffers of Arrow UInt64 and Float64 arrays. The `base` value of `Model.memory()` is equal to the size of these two arrays.
The high bits of the key contain the **partition dimension**. Thus, a binary search can read and write a range that a filter on the partition dimension selects.
For a filter on a different dimension, the engine uses an inverted index. The engine makes the index when it is necessary.

The engine selects the partition dimension for each Metric automatically.
For each input, the engine propagates the affected range of a change to one cell. Then it selects the dimension that gives the smallest part of the cells that a change writes.
You can also specify the dimension, for example `add_formula(..., partition="Month")`.

A join (`*`, comparisons, `ON`, `FILTER`, the branches of `IF`) evaluates one side first. If that side is small, the engine reads only the members of that side from the other side (a semi-join filter).
For a join of two operands with the same dimensions, the engine matches cells in key order without a sort and without a copy. An example of such cells is cells read from stored data.
For a join of two stored data with no delta (for example, `A * B` or `A > B` in a full recalculation), the engine matches the two bases directly. It does not copy them to a Cube.
In a full recalculation, the engine evaluates independent Metrics in parallel.
The engine keeps the calculated values in key order. When it writes them back, it moves the values one at a time into the format of stored data (two arrays: keys and values). Thus, in one stage, only one Metric at a time has two copies of its values during the move.

In a full evaluation, the engine fuses a formula that is a chain of element-wise operations. It evaluates the fused formula and does not make an intermediate result (Cube) for each operation (`eval/fuse.rs`).
Fusion applies to these operations: the four arithmetic operations, comparisons, `AND` / `OR` / `NOT`, `IF`, `FILTER`, `ON`, `EXPAND`, `ISBLANK`, `IFBLANK`, dimension values (`Month`), `SELECT`, time shifts (`[SELECT: Month - 1]`), the BY mapping (`BY`), and manual overrides of formula Metrics.
The engine scans each key that can be in the result. For each key, it goes through the expression tree, calculates the value, and writes it directly to the result array.
A source converts the key and looks up the value. `SELECT`, time shifts, and the BY mapping change the key that looks up the child.
The value at each key has the same meaning as in the evaluation of one operation at a time. This includes blank cells, division by zero, three-valued logic, and the expansion to all members.

The expression tree sets the keys that the engine scans:

- The keys of a source that has all dimensions of the formula. For `Volume * Price`, these are the keys of Volume. For `A + B`, these are the union of the keys of A and B. The engine matches the keys in key order. It reads a source with the same key layout with a cursor.
- The keys of one source × all members of the missing dimensions, or all combinations (for example, `Month >= HireMonth AND ...`). The engine counts the keys first, then allocates the result array and writes it. The engine does not fuse if the number of scanned keys is more than 4 times the intermediate results that the evaluation of one operation at a time must make. This prevents a scan of all combinations for a sparse join.

The engine continues to evaluate one operation at a time for these: an incremental recalculation on a limited range, an aggregation, and a formula that reads stored data with a delta. For an aggregation, the engine fuses the expression inside the aggregation and makes it one time.

For an aggregation (`REMOVE`, `BY`) with few aggregation targets, the engine does not copy the aggregation source. "Few" means 1/(16 × number of threads) of the source rows or less. The engine reads the source and adds each value to the intermediate value of its target.
If the aggregation source is stored data, the engine reads the base directly by row number.
The type check rewrites a `BY` aggregation with a Metric into a join with `On` and `AsAxis` and an aggregation. In a full evaluation, the engine puts the mapping table (for example, employee and month → department) into an array of all combinations. It looks up the targets in this array and does not make the join result.
If there are many aggregation targets but all their combinations are few, the engine adds the values into an array of all combinations. "Few" means that the total of the arrays for all threads is half or less of the size of the (target, value) pairs.
The engine divides the rows into one part for each thread, and each part has its own array. At the end, the engine adds the arrays in order. Thus, with the same number of threads, each run gives the same result.
In other cases, the engine converts the rows to (target, value) pairs, sorts them, and then aggregates them. The engine makes the pairs directly in one array and does not copy the aggregation source to a Cube.

Rust also does the schedule of the incremental recalculation. This includes the propagation of the affected range, the evaluation and write-back of a range, the filter by changed values, the incremental aggregation, and scan.
The Model sends the changed range of the inputs in one call. The engine propagates the affected range as sets of member numbers, not member names.
Thus, if one change causes the recalculation of hundreds of Metrics, there is only one call between Python and Rust.

The incremental recalculation also runs in parallel for each stage.
The Metrics in one stage do not read each other. Thus, the engine sets their affected ranges, calculates them in parallel, and writes them back in order.
If the range is the full Metric, the parallel step also makes the new stored data and compares the old and new values. The write-back then only replaces the data.
If the work of a stage is small (the estimate of recalculated rows is less than 16,384), the cost of the parallel handoff is larger. Thus, the engine calculates the stage in sequence.
A scan writes to its own stored data for each time period. Thus, the engine calculates scans in sequence at the end of the stage.
In the reference implementation, Python (`Model.recalc`) does the same schedule.
Tests match the two results. This makes sure that the Rust schedule gives the same result as the Python schedule.
