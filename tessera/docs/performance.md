# Performance

These are measurements of the Rust engine on an Apple M4 (10 cores, 16 GB of memory). Each value is the median of 5 runs.
The data comes from random numbers, and the values vary by about 20% from run to run.

Profit and loss plan and headcount plan (`examples/fpa.py`, 18 formula Metrics):

| Size | Total cells | Full recalculation | Change the salary of 1 employee | Move 1 employee | Add 1 employee | Move the closing month forward by 1 month |
|---|---|---|---|---|---|---|
| 2 thousand employees, 500 products | 490 thousand | 13 ms | 0.3 ms | less than 0.1 ms | 0.3 ms | 5.1 ms |
| 20 thousand employees, 5 thousand products | 4.9 million | 87 ms | 0.4 ms | 0.1 ms | 0.3 ms | 50 ms |

When the closing month moves forward, the actual values replace the forecast values of that month for all employees and all products.
The quantity of recalculation is proportional to the quantity of changed values (about 60 thousand cells). Thus this change takes more time than the other changes.

The same model, when you rename or delete members:

| Size | Rename an employee | Delete 1 employee | Delete 1 product | Delete 1 department | Delete 1 month in the middle |
|---|---|---|---|---|---|
| 2 thousand employees, 500 products | 0.1 ms | 2.1 ms | 1.6 ms | 11 ms | 16 ms |
| 20 thousand employees, 5 thousand products | 0.2 ms | 10 ms | 4.3 ms | 56 ms | 230 ms |

When you delete a department, the department of its employees (about 1/3 of all employees) becomes blank. Then the engine recalculates the aggregations of payroll and headcount.
When you delete a month in the middle, all cumulative totals and cash from that month, and all full-year aggregations, change. Thus it takes about the same time as a full recalculation.

A model with more formula Metrics (`bench_metrics.py`):

| Formula Metrics | Total cells | Full recalculation | Change 1 cell (recalculated Metrics) |
|---|---|---|---|
| 300 | 25.22 million | 516 ms | 1.4 ms (95) |
| 1000 | 76.99 million | 1,150 ms | 4.6 ms (394) |

The same model (1000 Metrics), when you change definitions:

| Definition change | Time |
|---|---|
| Add 1 formula | 7.2 ms |
| Replace 64 downstream formulas, the values of all cells change | 157 ms |
| Replace 64 downstream formulas, the values of only 1 product change | 7.8 ms |
| Replace 529 downstream formulas, the values of all cells change | 365 ms |
| Replace 529 downstream formulas, the values of only 1 product change | 25 ms |

In the profit and loss plan (4.9 million cells), it takes 6.0 ms to add 1 formula.
It takes 7.3 ms to change the formula of the cost of sales for only 1 product, and 15 ms for all products.

Models with a large number of Metrics (`bench_layout.py`, 1 measurement).
The "chain" model adds `x{i} = x{i-1} + 1` in 1 transaction. The other model has 2,500 pairs of an input `x{i}` and a formula `y{i} = x{i} + 1`.
"Rebuild" is the time to discard the plan, compile all of it, and recalculate all Metrics.
It is the same flow as when you open a model from the journal, or when you change a dimension or a value kind.

| Model | Add Metrics | Rebuild (compile and full recalculation) |
|---|---|---|
| 2,000 chains | 0.07 s | 0.03 s |
| 5,000 chains | 0.19 s | 0.09 s |
| 20,000 chains | 0.83 s | 0.48 s |
| 2,500 inputs + 2,500 formulas | 0.26 s | 0.57 s |

The engine uses the affected range of a change to 1 cell of each input ([how to select the partition dimension](engine.md)).
It converts these ranges 1 time into a list for each Metric.
In the rebuild of a model with many inputs, most of the remaining time is the cost to propagate the affected range of each input.
1 propagation goes through all stages of the plan. Thus this cost is proportional to the number of inputs multiplied by the number of Metrics.
If you double the pairs of inputs and formulas, the time increases by about 4 times. Thus the square remains in models with many inputs.

The retail model has more cells (`bench.py`, 17.56 million cells).
A full recalculation takes about 1.7 seconds, and a change to 1 cell takes less than 1 ms.
A change to the price of 1 product takes about 12 ms, because the aggregation by category changes for all stores and all months.

We also measure writes that publish versions (`bench_versions.py`).
For each transaction, the engine copies the published version, writes to the copy, recalculates, and publishes the copy as the next version.
This flow is for a single writer that receives writes from many users at the same time.

| Change | Write in place | Write and publish versions |
|---|---|---|
| 1000 Metrics, 1 cell | 6.2 ms | 7.5 ms (1.0 ms of this is the copy) |
| Profit and loss plan (4.9 million cells), change the salary of 1 employee | 0.4 ms | 0.6 ms (0.2 ms of this is the copy) |

The remaining time of the copy is the time for Python to copy the Metric definitions. It is proportional to the number of Metrics (about 1 ms for 1000 Metrics).
The cost of a transaction (`transaction`) is also this copy plus the time to find the difference between the two states.
With a transaction, a change to 1 cell of the 1000 Metrics model increases from 6.2 ms to 7.7 ms. The salary change in the profit and loss plan increases from 0.4 ms to 0.7 ms.
If the journal is written fully to the disk (`F_FULLFSYNC` on macOS, fsync on other systems), each commit takes about 3 ms in this local environment (an SSD on macOS).
For the salary change in the profit and loss plan, the time with the journal is 4.0 ms.
To receive writes from many users, you must have a function that writes many transactions to the disk together (group commit).
(The input values are different from the table above. Thus the times to write in place are also a little different from the table above.)

Reads (`bench_reads.py`, profit and loss plan (large), PayrollByEmployee has 1.37 million cells):

| Read | Time |
|---|---|
| `value`: all of PayrollByEmployee to a Cube | 483 ms |
| `get`: 1 cell of PayrollByEmployee | less than 0.01 ms |
| `slice`: all versions and all months of 1 product (Revenue) | 0.02 ms |
| `rows`: the first 50 rows of 1 department (Payroll) | 0.02 ms |
| `summarize`: the monthly totals of sales (all products) | 2.2 ms |
| `save` (snapshot, 1.02 million input cells) | 33 ms |

`summarize` reads the stored data one time in the engine, in parallel and with no copy (see the read query in [Engine](engine.md)).
It does not make a Python object for each cell: the result is columns (`Summary`), with the values in an `array.array`.
`bench_query.py --k 8` compares it with DuckDB 1.5.6 (4 threads) and Polars 2.0.0 on the same cells (PayrollByEmployee at 8 times the large size, 11.0 million cells, and Revenue, 3.24 million cells).
DuckDB and Polars read the member numbers as u32 columns. The script checks that the 3 give the same groups and values before it measures.
The measurement used a cloud VM with 4 vCPUs (median of 9 rounds, ms, and the range). The table gives the second of 2 runs.

| Query | Groups | tessera | DuckDB | Polars |
|---|---|---|---|---|
| sum by Month, Version=予算 | 36 | 22.0 (17.6 to 29.1) | 22.9 (20.1 to 29.5) | 34.5 (26.0 to 64.7) |
| sum by Employee (all) | 157,922 | 60.8 (50.9 to 73.9) | 67.1 (59.9 to 106.6) | 104.1 (90.9 to 130.8) |
| sum by Version, Month, 1000 Employees (partition dimension) | 81 | 1.8 (1.5 to 2.2) | 3.2 (2.3 to 7.1) | 6.6 (5.9 to 26.9) |
| total, Month in m01..m12 | 1 | 15.0 (13.7 to 20.6) | 13.3 (10.6 to 20.5) | 26.7 (20.3 to 38.9) |
| avg by Version | 3 | 30.8 (27.0 to 41.2) | 23.5 (19.9 to 26.8) | 29.2 (25.2 to 45.5) |
| 2 Metrics: sum by Month, Version=予算 | 72 | 28.7 (22.9 to 39.8) | 29.5 (26.6 to 38.6) | 50.2 (45.4 to 105.6) |

Before this path (a filtered copy of the stored data and a compiled `REMOVE`), the first 5 queries took 200, 454, 3.2, 186 and 146 ms on the same VM (through `Named`).
A filter on the partition dimension is faster than DuckDB, because it reads only the rows of the selected members.
In the 2 runs, a full scan took 0.9 to 1.4 times the time of DuckDB. The total and the AVG by Version were the slowest. The cause is the exact sum: `FixSum` uses about 4 ns for each value on 1 thread, and a plain f64 sum uses less than 1 ns. DuckDB and Polars add in f64, so their sums can change with the order of the rows.

`bench_query.py --k 8 --sql` runs the same kind of queries as SQL through `plan` and `Model.select` (the work of `POST /sql` without HTTP: parse, names, run, join, ORDER BY, LIMIT and encode). DuckDB runs the same SQL on the u32 member-number tables, and gives Python tuples (`fetchall`) or an Arrow table. tessera gives member names (JSON) or a dictionary of names (Arrow).
The measurement used the same cloud VM (4 vCPUs, DuckDB 4 threads, median of 9 rounds, ms, and the range). The table gives the second of 2 runs. The first run was in the same range.

| Query | Rows | tessera JSON | DuckDB fetchall | tessera Arrow | DuckDB Arrow |
|---|---|---|---|---|---|
| sum by Month, Version=予算 | 36 | 17.4 (15.6 to 20.8) | 17.6 (17.0 to 30.3) | 17.2 (15.9 to 21.7) | 17.4 (16.7 to 31.1) |
| sum by Employee (all) | 157,922 | 69.5 (64.5 to 83.4) | 91.6 (83.1 to 105.2) | 51.2 (48.8 to 55.9) | 54.2 (49.1 to 59.0) |
| top 100 Employees by sum | 100 | 51.4 (50.6 to 60.6) | 52.6 (49.7 to 58.8) | 56.3 (50.5 to 57.4) | 52.4 (48.6 to 59.6) |
| sum by Version, Month, 1000 Employees | 81 | 2.9 (2.3 to 3.6) | 2.3 (1.8 to 3.9) | 3.1 (2.6 to 4.0) | 2.3 (1.9 to 3.2) |
| avg by Version | 3 | 22.8 (21.0 to 25.3) | 17.2 (16.0 to 18.8) | 23.0 (21.2 to 27.3) | 16.6 (15.7 to 19.5) |
| 2 Metrics: sum by Month, Version=予算 | 36 | 22.4 (21.1 to 26.0) | 23.4 (22.0 to 25.4) | 22.2 (19.6 to 28.8) | 24.5 (22.1 to 26.8) |

The 1000 Employees query has an IN list of 1000 names. In a separate measurement (large, k=1), `plan` (the parse and the name lookup) took 1.3 ms and `Model.select` took 0.8 ms. AVG is slower for the reason above (the exact sum).

Write while 8 readers read continuously (change the salary of 1 employee, 0.6 ms when it is the only operation).
In standard Python, this write takes 84 ms (median), also when the readers use `get`.
The cause is not the cost of the reads. Each time the writer gets the GIL again, it waits for the Python thread switch interval (5 ms by default).
With `sys.setswitchinterval(0.0005)`, the time is 8 ms.
Free-threaded Python (3.14t) has no GIL. In the same conditions the time is 2.9 ms, and you do not have to change the switch interval.

| Measurement (profit and loss plan, large) | Standard Python 3.14 | Free-threaded 3.14t |
|---|---|---|
| Write while 8 readers `get` continuously | 84 ms | 2.9 ms |
| Write while 8 readers read continuously through HTTP | 2.0 ms | 2.3 ms |
| HTTP reads during that time | 2,200 per second | 9,700 per second |
| Save a snapshot | 33 ms | 39 ms |
| Full recalculation | 133 ms | 128 ms |
| Change the salary of 1 employee | 0.4 ms | 0.4 ms |
| HTTP write (only operation) | 0.8 ms | 1.8 ms |

Work on 1 thread has the same speed.
Only the time of an HTTP write as the only operation increases by about 1 ms, because of the fixed cost of free-threaded Python.
With many readers, the number of reads increases by more than 4 times, and writes do not wait.

Memory usage (`bench_memory.py`). The model is the profit and loss plan (large) multiplied by 4, with 19.65 million cells.
The engine opens it from a snapshot and does a full recalculation.
We measured this table on a cloud Linux VM (Intel Xeon 2.1 GHz, 4 vCPUs, 15 GB of memory). The Rust heap does not change with the machine.

| Measurement | Per cell |
|---|---|
| Stored data | 16.0 B |
| Rust heap (after recalculation) | 16.0 B |
| Maximum Rust heap (during recalculation) | 19.2 B |
| Increase of this from the time when the model was opened | 15.9 B |
| Same as above, calculated on 1 thread | 15.9 B |
| RSS (after recalculation) | 23.0 B |

After recalculation, the stored data is the only data that Rust keeps. The table counts 16 B (key and value) for each cell. Packed keys decrease this to about 10 B (see "Packed keys" below).
A fused formula that calculates the whole Metric again writes its result directly into the packed keys and the value column ("Fusion" in [Engine](engine.md)). Such a result uses about 10 B for each cell from the start, and the write-back does not copy it.
We measured `examples/fpa.py` at 32 times its large size (157 M cells, 4 threads). The maximum Rust heap of the full recalculation went from 2,112 MB to 1,727 MB (13.4 to 11.0 B for each cell). The maximum is now in the last level, which has large aggregations. It holds the stored data, the columns of one streamed result, and the buffers of two `BY` aggregations that read the largest Metric.
The increase during recalculation (312 MB) is only about 1.25 times the new values of the formula Metrics (249 MB).
Fused formulas do not do reads with a limited range.
Thus a full recalculation does not make indexes for dimensions other than the partition dimension. (The engine makes these indexes when incremental recalculation reads a limited range.)
You cannot compare RSS between Linux (glibc) and macOS, because the allocators are different.

RSS is larger than the Rust heap because the standard macOS allocator keeps large released blocks for reuse (MALLOC_LARGE (empty) in `vmmap`).
RSS varies much from run to run. Thus, to compare memory usage, compare the Rust heap.
To read the Rust heap, use `nanashi_core.heap()`. The count is always on, so subtract a value that you read before the model exists. To read the stored data of each Metric, use `m.memory()`.

A comparison of journals (`bench_journal.py`, profit and loss plan, PostgreSQL in local Docker):

| Measurement | File | PostgreSQL |
|---|---|---|
| Commit 1 change at a time in a transaction | median 5.9 ms | median 1.6 ms |
| 8 users write to `Workspace` at the same time | 517 per second, median 15 ms | 681 per second, median 11 ms |
| Commit a change of 100 thousand cells | 43 ms | 27–44 ms (the update of the history table takes 0.4–0.5 seconds after the commit) |
| Commit a change of 1 million cells | 375 ms | 152–167 ms (the same, 4.2–6.4 seconds) |
| Open from a snapshot and 1000 journal entries | 0.14–0.19 seconds | 0.18 seconds |
| Get the history of 1 cell (3 million history rows) | Read the journal from the start | 0.2 ms |

PostgreSQL looks faster for small commits because the two journals write to the disk with different reliability.
The file journal uses `F_FULLFSYNC` on macOS to write through to the cache of the device (about 3 ms each time).
PostgreSQL in Docker Desktop writes inside a virtual machine, and each write takes 52 microseconds (`pg_test_fsync`).
This speed shows that the data does not get to the physical device. Thus, in this environment, a PostgreSQL commit is not safe from a power failure.
In a production environment (with writes to the disk and network round trips), we expect commit times to increase by a few ms. You must measure again.

### Packed keys

The keys of a base are frame-of-reference blocks of 1,024 keys (`PackedKeys`, [Engine](engine.md)). In the retail model, the keys use about 2 B for each cell.
A random read with `PackedKeys::get` is about 1.6 times slower than a `Vec` read (`bench_reads` in `packed.rs`, run with `--ignored`).

## Save and journal formats

(Profit and loss plan (large), PostgreSQL in local Docker, Python 3.14.)
"Register bonuses by employee × month" is a write that adds an input Metric of 720 thousand cells.
"Spread sales volume" is a write that changes 225 thousand cells. Both times are through `Workspace`.

| Measurement | Time or size |
|---|---:|
| Save a snapshot | 33 ms |
| Size of a snapshot | 1.4 MB |
| Load a snapshot | 15 ms |
| Register bonuses by employee × month (PostgreSQL) | 485 ms |
| Spread sales volume (PostgreSQL) | 88 ms |
| Write 1 cell (PostgreSQL, median) | 3.1 ms |
| Size of the files for large changes (PostgreSQL) | 0.8 MB |
| Update of the history table after the commit (PostgreSQL) | 2,925 ms |
| After the writes above, open from a snapshot and the journal (PostgreSQL) | 115 ms |
| Get the history of 1 bonus cell (PostgreSQL) | 8.1 ms |

By default, the save occurs in a different thread every 1000 journal entries, thus writes do not wait.

The file journal writes large changes to the same Parquet files as `PgJournal`.

| Measurement (file journal) | Python 3.14 | Python 3.14t |
|---|---:|---:|
| Register bonuses by employee × month (720 thousand cells) | 488 ms | 594 ms |
| Spread sales volume (225 thousand cells) | 88 ms | 92 ms |
| Write 1 cell (median) | 5.0 ms | 6.0 ms |
| After the writes above, open from a snapshot and the journal | 50 ms | 53 ms |
| Get the history of 1 bonus cell | 10.5 ms | 9.1 ms |
| Size of the journal (without snapshots) | 0.8 MB | Same as left |

To open, the engine reads large changes from Parquet as change blocks, and Rust writes them together.
For the history, the file journal reads the journal from the start. But Rust finds the 1 cell in the change blocks, thus the engine does not convert all rows to Python.
Almost all of the time of a 1-cell write is the write to the disk (`F_FULLFSYNC`, about 3 ms each time).

## Cold start of the HTTP server

The router starts an engine when the first request for a model comes.
This table gives the time from the process start of `sparse_engine.server` with `--pg` until `GET /ready` returns 200.
The snapshot is on the local disk, at the published version, so the journal replay is empty.
Linux, 4 cores, PostgreSQL on the same host, 3 runs each.

| Model | Cells | Snapshot | Cold start |
|---|---|---|---|
| Profit and loss plan (small) | 13,841 | 0.1 MB | 0.30 to 0.58 s |
| Profit and loss plan (large) | 4,909,264 | 1.5 MB | 0.94 to 1.08 s |

Most of the time is the start of Python and the import of `nanashi_core`.
A snapshot in object storage adds the download time, and a journal with many entries after the snapshot adds the replay time.
