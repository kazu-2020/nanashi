# SQL reads (`POST /sql`)

`POST /sql` reads the model with a subset of DuckDB SQL. The model is 1 wide table with the name `model`. Each dimension is a column, and each Metric is a column that you aggregate.

```
POST /sql
Content-Type: application/json
Accept: application/vnd.apache.arrow.stream     (optional; the default is JSON)

{"sql": "SELECT Month, Product, SUM(Revenue) AS rev, AVG(Price) FROM model
         WHERE Version = 'Actual' AND Region IN ('East', 'West')
         GROUP BY Month, Product ORDER BY rev DESC LIMIT 100"}
```

The body has `sql`, and also `app_id` or `appId` as in `POST /query`. Another field gives 400 `bad_request`.
`POST /sql` is a read. It works on a standby and on a `--follow` server, and it needs no `client_op_id`. The router does not resend a 500 of `POST /sql`.

## Supported SQL

| Part | Supported |
|---|---|
| `SELECT` | Dimension columns, and `SUM`, `AVG`, `MIN`, `MAX`, `COUNT` of a Metric, each with an optional `AS alias` |
| `FROM` | `model` only (in any case) |
| `WHERE` | `dim = 'member'` and `dim IN ('a', 'b')`, joined with `AND`. 2 terms on 1 dimension keep the members that both terms have |
| `GROUP BY` | Dimension names or 1-based positions in the SELECT list |
| `ORDER BY` | An output column name, a 1-based position, or a GROUP BY dimension, with `ASC`, `DESC`, `NULLS FIRST`, `NULLS LAST` |
| `LIMIT` | An integer |

Other SQL gives 400 with the code `sql_unsupported`. Examples are `HAVING`, `DISTINCT`, `OFFSET`, `JOIN`, a subquery, `WITH`, `*`, `COUNT(*)` and an expression such as `SUM(a + b)`.
Identifiers and members are names, not UUIDs. A name must be exactly equal to the name of a dimension, a Metric or a member. SQL keywords and `model` can be in any case. A name that starts with `__` is hidden.
If 2 output columns have the same name, `ORDER BY` with that name gives `sql_ambiguous`.

## Rules

- Each dimension in `SELECT` must be in `GROUP BY`. A `GROUP BY` dimension that is not in `SELECT` still makes separate groups.
- The Metric of each aggregate must have all the `GROUP BY` and `WHERE` dimensions. If not, the answer is 400 `sql_missing_dim`. `POST /query` ignores such a dimension. Then a total includes all the members of that dimension, which is wrong in a wide table.
- If a Metric is not a number, you can use only `COUNT` on it.
- The groups are the groups of all the aggregates in `SELECT` (an outer join). An aggregate with no cells in a group is NULL. `COUNT` is 0. If only a Metric that is not in `SELECT` has a group, the result does not have that group.
- Without `GROUP BY`, the result has exactly 1 row (SUM is NULL and COUNT is 0 if there are no cells).
- `ORDER BY` on a dimension uses the member order of the dimension (like a DuckDB ENUM), not the text order of the names.
- NULLs are last for `ASC` and `DESC` unless the SQL says `NULLS FIRST`. NaN is above all numbers. A tie keeps the order of the member numbers, so `LIMIT` gives the same rows each time.
- Without `ORDER BY`, the rows are in the order of the member numbers of the `GROUP BY` dimensions. This order can change in a later release, but it does not change for one version.

## Result

The JSON answer is `{"seq": 812, "columns": ["Month", "Product", "rev", "avg(Price)"], "rows": [["Mar", "P-104", 98213.5, 12.0], ...]}`.
The columns are in SELECT order. A column has the alias, else the dimension name, else `<agg>(<Metric name>)` with the aggregate in lowercase.
A dimension value is a member name. NULL is `null`. NaN and the infinities are the strings `"NaN"`, `"Infinity"` and `"-Infinity"`.

If `Accept` names `application/vnd.apache.arrow.stream` (with a q above 0), the answer is an Arrow IPC stream with 1 record batch and `seq` in the schema metadata.
A dimension column is `dictionary<int32, utf8>` and is not null. Its dictionary has all the members of the dimension, and its keys are the member numbers.
An aggregate column is a nullable `float64`. The reference engine does not make Arrow: it answers 501 `not_implemented`. The reference engine also needs `nanashi_core`, because Rust parses the SQL.

## Errors

An SQL error is 400 `{"error": "sql", "code": "<code>", "message": "<Japanese text>"}`. The codes are the `sql_*` keys of `MESSAGES` in `sparse_engine/messages.py`.
A text with more than 256 operators (for example `AND`, `=` and `+`) is `sql_too_complex`. A SELECT list with more than 1000 items is `sql_columns`.
A result with more cells (rows x columns, after `LIMIT`) than `--max-cells` is 413 `{"error": "too_large", "code": "sql_too_large"}`. One memory budget (`max_bytes`) holds the stored data and every buffer of the query. A query over the budget is 400 `sql_budget`.

## Implementation

Rust parses the text to names (`native/engine/src/sql.rs`), `sql.plan` changes them to a `Plan` of ids, and `Core.select` runs the `Plan` (`native/engine/src/select.rs`).
The tests compare `Core.select` with `sql.reference_select`, which uses pure Python.
