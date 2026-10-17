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
It merges the cells directly into the value array and the packed keys of the new base. The packed keys get one block of 1,024 keys at a time, so the merge needs about 10 to 12 B for each cell.
The base is a list of segments in key order (`Base` and `Segment` in `store.rs`). A segment ends at the first partition-member boundary after `seg_rows` rows (4,194,304 by default), so a segment holds whole partition members. Readers address the base by a global row number, and `Base` finds the segment with a binary search.
The engine cuts a segment only where the member of the partition dimension changes. If the partition dimension has few members, the segments are large. A member with more than `seg_rows` rows is in one segment.
A rewrite of the base (a compaction or a large write) reads the old segments one at a time and drops each old segment after it reads it. If a segment holds no key of the delta, the new cells or the removed cells, it goes into the new base unchanged. Its index stays. Segments do not merge again, so many small writes or deletes can leave small segments. The finished new segments stay until the rewrite ends. So the peak is the old segments that the rewrite did not read, the finished new segments, and one segment that it builds. This is about one base and one segment, not two bases.
If a version that a reader keeps shares the old segments, the rewrite frees no old segment. Then the peak is two bases, as before the segments.
The engine does not change a segment after it makes it, and versions share the segments by `Arc`. The delta is a persistent tree (`OrdMap` of `imbl`), so a change does not break old versions.
Thus, a writer can write while a reader keeps a published version. The engine does not copy all of the base or the delta.
The values of the base are an immutable array with no extra capacity (`Box<[f64]>`). Its layout is the same as the buffer of an Arrow Float64 array.
The keys of the base are packed in blocks of 1,024 keys (`PackedKeys` in `packed.rs`). A block keeps its first key and the offset of each key from it, with the bit width of the largest offset (frame of reference). In the retail model the width is 14 to 19 bits, so the keys use about 2 B for each cell in place of 8 B.
A scan decodes one block at a time into a buffer of 1,024 keys and then reads a plain array. A binary search decodes one key at a time. The cursor of the fused evaluation (`Cursor`) decodes the block of a row near its last row, and reads a far row one key at a time. The `base` value of `Model.memory()` is equal to the size of the packed keys and the value array.
The high bits of the key contain the **partition dimension**. Thus, a binary search can read and write a range that a filter on the partition dimension selects.
For a filter on a different dimension, the engine uses an inverted index. The engine makes the index when it is necessary.
The index is one for each segment and packs the local row numbers in blocks of 1,024 (`PackedKeys`). In the P&L plan, this is about 2 to 4 B for each cell of the base. It stays in memory while a version uses that segment. The build holds the row numbers of one segment as `u32` (4 B for each row of the segment). It packs the entries as it makes them, so it does not hold a `u64` array of all entries. Only the members in the segment have an entry in its index.

The engine selects the partition dimension for each Metric automatically.
For each input, the engine propagates the affected range of a change to one cell. Then it selects the dimension that gives the smallest part of the cells that a change writes.
You can also specify the dimension, for example `add_formula(..., partition="Month")`.

A join (`*`, comparisons, `ON`, `FILTER`, the branches of `IF`) evaluates one side first. If that side is small, the engine reads only the members of that side from the other side (a semi-join filter).
For a join of two operands with the same dimensions, the engine matches cells in key order without a sort and without a copy. An example of such cells is cells read from stored data.
For a join of two stored data with no delta (for example, `A * B` or `A > B` in a full recalculation), the engine matches the two bases directly. It does not copy them to a Cube.
In a full recalculation, the engine evaluates independent Metrics in parallel.
The engine keeps the calculated values in key order. When it writes them back, it moves the values one at a time into the format of stored data (packed keys and a value column). Thus, in one stage, only one Metric at a time has two copies of its values during the move.
A fused formula that calculates the whole Metric again does not make this copy. Its scan writes directly into the format of stored data (see "Fusion" below).

In a full evaluation, the engine fuses a formula that is a chain of element-wise operations. It evaluates the fused formula and does not make an intermediate result (Cube) for each operation (`eval/fuse.rs`).
Fusion applies to these operations: the four arithmetic operations, comparisons, `AND` / `OR` / `NOT`, `IF`, `FILTER`, `ON`, `EXPAND`, `ISBLANK`, `IFBLANK`, dimension values (`Month`), `SELECT`, time shifts (`[SELECT: Month - 1]`), the BY mapping (`BY`), and manual overrides of formula Metrics.
Before the scan, the engine compiles the expression tree into a chain of closures. Each operation and each source gets its own closure. A constant operand of `*`, `/` or a comparison goes into the closure of its operation.
The engine scans each key that can be in the result. For each key, it calls the chain, calculates the value, and writes it directly to the result array.
A source converts the key and looks up the value. `SELECT`, time shifts, and the BY mapping change the key that looks up the child.
The value at each key has the same meaning as in the evaluation of one operation at a time. This includes blank cells, division by zero, three-valued logic, and the expansion to all members.

The expression tree sets the keys that the engine scans:

- The keys of a source that has all dimensions of the formula. For `Volume * Price`, these are the keys of Volume. For `A + B`, these are the union of the keys of A and B. The engine matches the keys in key order. It reads a source with the same key layout with a cursor.
- The keys of one source × all members of the missing dimensions, or all combinations (for example, `Month >= HireMonth AND ...`). The engine counts the keys first, then allocates the result array and writes it. The engine does not fuse if the number of scanned keys is more than 4 times the intermediate results that the evaluation of one operation at a time must make. This prevents a scan of all combinations for a sparse join.

If a source is stored data with a delta (the usual state of an input after edits), the engine first merges the base and the delta into one array in key order. This copy counts against the memory budget. Then the fused scan reads the copy.

The scan can write its result directly into the new base. This applies when the fused formula is the whole formula of the Metric, the recalculation makes the whole Metric again, and the scan gives the keys in the layout of the stored data. Then each part goes in key order into the packed keys and the value column (`eval_columns` in `eval/mod.rs`, `StoreSink` in `store.rs`). The engine does not make the array of keys and values (16 B for each cell) for this result.

The engine calculates the parts in parallel, in batches of 2 parts for each thread. One batch goes into the new base while the threads calculate the next batch, so at most 2 batches of results exist at one time. If the new base and these batches do not fit in the budget, or the layout is different, the scan writes an array of cells. The new base refuses a key that is not above the last key. In an incremental recalculation, the new base also finds the cells whose value changed. It compares the new cells with the old stored data in the same pass.
The engine continues to evaluate one operation at a time for these: an incremental recalculation on a limited range, and an aggregation. For an aggregation, the engine fuses the expression inside the aggregation and makes it one time.

For an aggregation (`REMOVE`, `BY`) on the array path or the hash map path, the engine does not copy the aggregation source. It reads the source and adds each value to the intermediate value of its target.
If the aggregation source is stored data, the engine reads the base directly by row number.
The type check rewrites a `BY` aggregation with a Metric into a join with `On` and `AsAxis` and an aggregation. In a full evaluation, the engine puts the mapping table (for example, employee and month → department) into an array of all combinations. It looks up the targets in this array and does not make the join result. This path uses only the hash map, and only if the targets are few.
The engine tries 3 paths in this order: an array of all target combinations, a hash map, and a sort. If an incremental recalculation narrows the range of the targets, the engine tries the hash map first, because the array ignores the range.
If all combinations of the aggregation targets are few, the engine adds the values into an array of all combinations. For a SUM, "few" means that the arrays (32 B for each combination and array) are not larger than the (target, value) pairs (16 B a row). For `MIN`, `MAX` and `COUNT`, the limit is half of the pairs (16 B or 4 B for each combination and array).
If the slot of each row follows the order of the rows, the engine divides the rows into one part for each thread, and each part has its own array. At the end, the engine adds the arrays in order.
If the first 2^16 rows touch more than 4 MiB of the array (`scatter_bytes`), each row goes to a different cache line. Then the engine uses one array and fills it in rounds. In each round, each thread puts its rows into buckets of 2^14 combinations. Then each bucket adds its rows into its part of the array, which stays in the cache. The buffers of a round have a fixed size of about 1.25 arrays (a 16 B pair and a 4 B slot number for each row), so this fill counts as 3 arrays.
If the arrays are too large but the targets are few, the engine adds each value to its target in a hash map. "Few" means 1/(16 × number of threads) of the source rows or less. Each thread has its own hash map, and the engine merges them at the end.
In other cases, the engine converts the rows to (target, value) pairs, sorts them, and then aggregates them. The engine makes the pairs directly in one array and does not copy the aggregation source to a Cube.

`SUM` and `AVG` add the values into an exact sum (Shewchuk partials, the same algorithm as Python `math.fsum`) and round one time at the end. Thus the result does not change with the order of the rows, the aggregation path, or the number of threads. The reference implementation uses `math.fsum` and gives the same bits.
`X[REMOVE SUM: A, B]` is one aggregation with one rounding. It does not round one time for each dimension.
The exact sum keeps 2 partials inline (24 bytes). If it needs more, it moves them to the heap.
A finite value of 2^960 or more makes the sum NaN. Below this limit the partials cannot overflow, so an overflow does not depend on the order of the rows.

Rust also does the schedule of the incremental recalculation. This includes the propagation of the affected range, the evaluation and write-back of a range, the filter by changed values, the incremental aggregation, and scan.
The Model sends the changed range of the inputs in one call. The engine propagates the affected range as sets of member numbers, not member names.
Thus, if one change causes the recalculation of hundreds of Metrics, there is only one call between Python and Rust.

The incremental recalculation also runs in parallel for each stage.
The Metrics in one stage do not read each other. Thus, the engine sets their affected ranges, calculates them in parallel, and writes them back in order.
If the range is the full Metric, the parallel step also makes the new stored data and compares the old and new values. The write-back then only replaces the data.
If the work of a stage is small (the estimate of recalculated rows is less than 16,384), the cost of the parallel handoff is larger. Thus, the engine calculates the stage in sequence.
A scan writes to its own stored data for each time period. Thus, the engine calculates scans in sequence at the end of the stage.
Some scans do not need an evaluation for each period (`eval/scan.rs`). This applies if only one Metric of the scan changes.
The formula of that Metric must be a tree of `+`, `-`, `*`, `/`, `IF` and `COALESCE` over `PREVIOUS(dim, n)` (n >= 1). An example is `IF(IsActual, Actual, PREVIOUS(Month) * 0.9 + PREVIOUS(Month, 2) * 0.1 + Margin)`.
The condition of `IF` must not read the Metric. `COALESCE` is the override of an overridable Metric.
An operand is a part of the formula that does not read the Metric. The engine evaluates each operand one time for all periods in the range.
A row is a key without the time dimension. The value of a row at a period reads only earlier periods of the same row and the operands.
Thus, the engine sorts each operand by row and then by period, and calculates the rows in parallel. For each row, it calculates the tree for the periods in ascending order. Then it writes all periods back one time.
Each cell gets the same operations in the same order as in the evaluation for each period. Thus, the result is the same bit for bit.
The engine does not use this path if an operand has an aggregation, because an aggregation of all periods can add the values in a different order.
If the operands of all periods do not fit in the memory budget (`max_bytes`, stored data and intermediate results), the engine evaluates one period at a time.
In the reference implementation, Python (`Model.recalc`) does the same schedule.
Tests match the two results. This makes sure that the Rust schedule gives the same result as the Python schedule.
