# Formula language

You write a formula as a string with a syntax that is similar to Pigment.
You can also make the same formula with the Python DSL (for example, `ref("Salary").by("Employee.Department")`).

| Category | Syntax |
|---|---|
| Operators | `+ - * /`, comparisons `= <> < <= > >=`, logic `AND OR NOT`, unary `-` |
| Aggregation | `X[BY SUM: Employee.Department]`, `X[REMOVE SUM: Product, Month]` (SUM, AVG, MIN, MAX, COUNT) |
| BY mapping | `Rate[BY: Employee.Department]` (sends the value of the department to each employee) |
| Slice and filter | `X[SELECT: Version."実績"]`, `X[SELECT: Month - 1]`, `X[FILTER: condition]` |
| Add a dimension | `X[EXPAND: Month]` (copies to all members), `X[ON: Y]` (sends to the cells where Y has a value) |
| Functions | `IF(condition, a[, b])`, `IFBLANK(x, constant)`, `ISBLANK(x)`, `PREVIOUS(Month[, n])` |
| Members | `Month` (the member of each cell), `Month."Mar"` (a member constant) |
| Names | `Revenue`, `売上`, `'Unit Price'` (a name with a space) |

`PREVIOUS(Month)` refers to the value of the previous month of the Metric that contains the formula. Thus, you can write a cumulative value over time, for example an inventory or a cash balance.
A formula that compares a dimension as a member, for example `Month <= Month."Mar"`, uses the member order on an ordered dimension.

For the property of `BY`, you can specify a fixed property of a dimension. You can also specify a Metric of a member value kind.
`Salary[BY SUM: Employee.DeptOf]` aggregates by department. It uses the department of each employee for each month (`DeptOf[Employee, Month]`).
Thus, a hierarchy that changes over time, for example a transfer, is an input change.

## Type check

The engine checks the dimensions and the value kind of each formula before it runs the formula.
There are 3 value kinds: **number**, **boolean**, and **member:<dimension name>** (a member of that dimension).
These cause an error at the first calculation after you register the formula: a member that does not exist, a less-than or greater-than comparison on an unordered dimension, and dimensions that do not agree.

If `+` connects Metrics with different dimensions, the engine implicitly expands the values to the missing dimensions. Then the number of cells can become very large.
Thus, the engine gives an error, except for an operation with a constant. To prevent the error, write `[EXPAND: dimension]` or `[ON: other]` to show what you intend.
The error message shows the correction in the formula syntax.

## Cell count estimate

An explicit `EXPAND`, `IFBLANK`, and an addition with a constant make values for all combinations of the dimensions.
Example: a Metric has 100,000 customers × 20,000 products, and you write `IFBLANK(x, 0)`. The result has 2,000,000,000 cells, also if there are few real transactions. The memory becomes full when the calculation starts.
Thus, after you register a formula and before the first calculation, the engine estimates the maximum number of result cells for each formula Metric. If the estimate is more than `Model(max_cells=...)`, the engine does not calculate and gives an error. The default limit is 1,000,000,000. If the value is `None`, the engine does not do the check.

```text
Filled: 結果のセル数が最大 2,000,000,000 と見積もられ、上限 1,000,000,000 を超える。密になる演算: IFBLANK が ['Customer', 'SKU'] の全組み合わせに展開される（密化）。値のあるセルだけに絞るなら [ON: 相手] か FILTER を使う。上限は Model(max_cells=...) で変えられる
```

The engine calculates the estimate from the current cell counts of the inputs. It goes through the formulas in the order of the plan.
These are the rules for the estimate:

- For an operation that keeps only cells with values on both sides, for example `*` or comparisons, the estimate is the smaller side.
- For an operation that keeps cells with a value on one side, for example `+`, the estimate is the sum.
- For an operation that makes values for all combinations, the estimate is the product of the dimension sizes.
- All estimates have a maximum: the number of all combinations of the result dimensions.

For a scan (a Metric that reads its own value at the previous time period), the engine multiplies by the number of time periods. This is because a value can carry over to all time periods.
In the profit-and-loss plan, the estimates were 1.0 to 1.9 times the real cell counts (1.45 times for the full model).
You can read the estimates with `m.cell_estimates`.

The engine estimates again only when you change a definition. It estimates only the changed Metric and the Metrics that read a Metric with a changed estimate. In a model with 1000 Metrics, this takes 0.07 ms.
The engine does not estimate again if only the input cell counts or the member counts increase. Thus, if the inputs increase later and become more than the limit, the engine does not check until the next definition change.

The estimate is a maximum for the result. Thus, an intermediate result can use all the memory, also if the estimate is in the limit. Examples of intermediate results are an `EXPAND` in the formula and the values that `+` expands.
The Rust engine can set a budget for the intermediate results that one formula evaluation keeps at the same time (`RustEngine(max_bytes=8 << 30)`; for the server, `--max-bytes`).
For an operation that makes all combinations (`EXPAND`, `ISBLANK`, `IFBLANK`), the engine checks the necessary size first. If the size is more than the budget, the engine gives an error before it allocates memory.
A fused formula ([Engine](engine.md)) does not make intermediate results. Thus, the engine counts only the size of the result array against the budget, before it allocates the array.
The engine does not fuse a formula if the intermediate results that the evaluation of one operation at a time must make are more than the budget. It evaluates one operation at a time and gives an error as above.
When the engine calculates the Metrics of one stage in parallel, it divides the budget into equal parts. It calculates together only the Metrics with an estimate in one part. Each other Metric gets the full budget, and the engine calculates these Metrics one at a time.

## Blank cells

A blank cell (a cell with no value) is different from 0. Each operation has a rule for the range of its result.

| Operation | Cells that get a value |
|---|---|
| `+ -` | Cells where one of the two sides has a value (a blank is 0) |
| `* /` and comparisons | Cells where both sides have a value (division by zero gives a blank) |
| `AND OR` | Three-valued logic (`FALSE AND blank = FALSE`, `TRUE AND blank = blank`) |
| `IF(c, a, b)` | Only cells where c has a value (if c is blank, the result is blank) |
| `FILTER(x, c)` | Cells of x where c is TRUE |
| `IFBLANK`, `ISBLANK`, `EXPAND` | All members of the applicable dimensions (densifying) |

If the condition of `IF` is blank, the result is blank. This prevents a result of `IF` that is denser than its condition.
If you want a blank to be FALSE, write it explicitly, for example `IF(IFBLANK(x > 0, FALSE), ...)`.
