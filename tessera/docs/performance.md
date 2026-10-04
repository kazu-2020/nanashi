# Performance

These are measurements of the Rust engine on an Apple M4 (10 cores, 16 GB of memory). Each value is the median of 5 runs.
The data comes from random numbers, and the values vary by about 20% from run to run.
The M4 tables show the values from before we added the fusion of operations ([Engine](engine.md)).
We measured the values before and after fusion again in a different environment (see "Before and after the fusion of operations" below).

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

The same model (1000 Metrics), when you change definitions. Before this work, each of these changes took a full recalculation of about 1,350 ms:

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

Before, each selection of the partition dimension went through the state of all Metrics 1 time.
Thus the time increased with the square of the number of Metrics (with 20,000 chains, 60 seconds to add and 32 seconds to rebuild).
Now the engine uses the affected range of a change to 1 cell of each input ([how to select the partition dimension](engine.md)).
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

Before the copies shared the base of the stored data, the first write after a copy copied all stored data of each recalculated Metric.
Thus a change to 1 cell of the 1000 Metrics model took 342 ms.
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

Before `get` became a read API, a read of 1 cell took about 500 ms, the same as `value`. During that time, it held the GIL.
`save` also went through all cells in Python and took about 500 ms.
After `save` changed to Parquet, its time increased from 11 ms to 33 ms, but the size decreased from 17.9 MB to 1.4 MB ([Save and journal formats](#save-and-journal-formats)).

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
We measured this table on a cloud Linux VM (Intel Xeon 2.1 GHz, 4 vCPUs, 15 GB of memory) when we added the fusion of operations.
The Rust heap does not change with the machine. The values from before fusion were the same as the values measured on the M4.

| Measurement | Per cell | Before fusion |
|---|---|---|
| Stored data | 16.0 B | 17.6 B (base 16.0 B, indexes for dimensions other than the partition dimension 1.6 B) |
| Rust heap (after recalculation) | 16.0 B | 17.6 B |
| Maximum Rust heap (during recalculation) | 19.2 B | 28.6 B |
| Increase of this from the time when the model was opened | 15.9 B | 25.2 B |
| Same as above, calculated on 1 thread | 15.9 B | 25.2 B |
| RSS (after recalculation) | 23.0 B | 58 B (M4) |

The stored data is equal to the 16 B of the key and the value. After recalculation, the stored data is the only data that Rust keeps.
The increase during recalculation (312 MB) is only about 1.25 times the new values of the formula Metrics (249 MB).
Fused formulas do not do reads with a limited range.
Thus a full recalculation does not make indexes for dimensions other than the partition dimension. (The engine makes these indexes when incremental recalculation reads a limited range.)
You cannot compare RSS between Linux (glibc) and macOS, because the allocators are different.
Before the engine aggregated while it read, the maximum was 98.6 B per cell (about 5.6 times the stored data).
The cause was 1 formula that aggregates payroll by employee × month (5.51 million cells) to departments.
It copied all entries at each step: the read, the mapping, the result of the join, and the sort before the aggregation. It used 531 MB (now 12 MB, for the array of the mapping).
With the same change, the full recalculation decreased from 577–615 ms to 393–419 ms.
In the retail model (17.56 million cells), the maximum increase of the heap during recalculation was still 5.2–6.6 GB after that change.
This was more than 6 times the stored data (879 MB).
Most of it was not aggregation. It was the intermediate results of 17.56 million cells that each operation made.

### Before and after operation fusion

The engine now evaluates a formula that connects element-wise operations without intermediate results ([Engine](engine.md)).
We measured before and after on the same VM as above. Full recalculation shows the median of 3 runs, and the minimum–maximum in parentheses.

| Model | Measurement | Before fusion | After fusion |
|---|---|---|---|
| Retail (xlarge of `bench.py`, 17.56 million cells) | Full recalculation | 8,877 ms (8,332–9,060) | 2,687 ms (2,437–2,876) |
| | Maximum increase of the heap | 4,275 MB | 1,194 MB |
| Profit and loss plan (large) × 4 (19.65 million cells) | Full recalculation | 2,485 ms (2,380–2,520) | 1,476 ms (1,419–1,478) |
| | Same as above, 1 thread | 5,083 ms | 5,024 ms |
| | Maximum increase of the heap | 496 MB | 312 MB |
| Profit and loss plan (large) (4.9 million cells) | Full recalculation (1 run) | 538 ms | 363 ms |

The increase in the retail model (1,194 MB) is now about 1.4 times the new values of the formula Metrics (878 MB). Before, it was about 4.9 times.
When the engine evaluates each formula separately, the maximum heap compared to the size of the result is as follows.
(The source is the table for each formula in `bench_memory.py`, and the same measurement method for the retail model.)

| Formula | Result cells | Before fusion | After fusion |
|---|---|---|---|
| Retail `IF(Revenue > 1000, Revenue * 0.1, 0)` | 17.56 million | 1,661 MB (5.9 times) | 281 MB (1.0 times) |
| Retail `Volume * Price` | 17.56 million | 1,100 MB (3.9 times) | 281 MB (1.0 times) |
| Retail `Revenue[FILTER: Active]` | 14.06 million | 777 MB (3.5 times) | 281 MB (1.3 times) |
| Retail `Revenue[REMOVE SUM: Store]` | 360 thousand | 1,099 MB | 17 MB |
| Retail `Revenue[BY SUM: Product.Category]` | 1.8 million | 1,099 MB | 86 MB |
| Profit and loss plan `IF(Version = Version."見込み", IF(IsActual, ...), PayrollRaw)` | 5.51 million | 329 MB (3.7 times) | 89 MB (1.0 times) |
| Profit and loss plan `Salary * (1 + BenefitRate) * IF(Employed, 1) * InPeriod` | 3.05 million | 317 MB (6.5 times) | 49 MB (1.0 times) |
| Profit and loss plan `Month >= HireMonth AND (... ISBLANK(LeaveMonth)[EXPAND: Month])` | 2.88 million | 302 MB (6.6 times) | 53 MB (1.2 times) |

On 1 thread, the time of the profit and loss plan is almost the same before and after fusion.

RSS is larger than the Rust heap because the standard macOS allocator keeps large released blocks for reuse (MALLOC_LARGE (empty) in `vmmap`).
In the version from before the engine aggregated while it read, RSS increased with each recalculation (1,919 MB the first time, 2,559 MB the fourth time).
When we started the process with the environment variable `MallocLargeCache=0`, RSS stopped at 791–812 MB. The recalculation time did not change.
RSS varies much from run to run. Thus, to compare memory usage, compare the Rust heap.
To read the Rust heap after `nanashi_core.track_heap(True)`, use `nanashi_core.heap()`. To read the stored data of each Metric, use `m.memory()`.
While the count is on, each allocation does an atomic addition or subtraction. Thus the count is off by default. (With the count on, parallel recalculation was a few percent slower.)

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

## Save and journal formats

This is a comparison before and after two changes. Snapshots and files for large changes changed from npz to Parquet. Large changes now move as change blocks.
(Profit and loss plan (large), PostgreSQL in local Docker, Python 3.14.)
"Register bonuses by employee × month" is a write that adds an input Metric of 720 thousand cells.
"Spread sales volume" is a write that changes 225 thousand cells. Both times are through `Workspace`.

| Measurement | Before | After |
|---|---:|---:|
| Save a snapshot | 11 ms | 33 ms |
| Size of a snapshot | 17.9 MB | 1.4 MB |
| Load a snapshot | 14 ms | 15 ms |
| Register bonuses by employee × month (PostgreSQL) | 2,023 ms | 485 ms |
| Register bonuses by employee × month (file) | 1,976 ms | 762 ms |
| Spread sales volume (PostgreSQL) | 4,152 ms | 88 ms |
| Write 1 cell (PostgreSQL, median) | 3.3 ms | 3.1 ms |
| Size of the files for large changes (PostgreSQL) | 33.9 MB | 0.8 MB |
| Update of the history table after the commit (PostgreSQL) | 4,027 ms | 2,925 ms |
| After the writes above, open from a snapshot and the journal (PostgreSQL) | 1,943 ms | 115 ms |
| Same as above (file) | 1,873 ms | 1,793 ms |
| Get the history of 1 bonus cell (PostgreSQL) | 22.5 ms | 8.1 ms |

The time to open from the PostgreSQL journal decreased to 1/17.
The cause is that the engine reads large changes from Parquet as change blocks, and Rust writes them together.
(Before, the engine converted npz to Python lists and wrote 1 cell at a time.)
At that time, the file journal still kept changes as JSON lines. Thus the time to open almost did not change. (The next table shows the fix.)
The "after" value of the spread is from after `spread` started to write all cells together.
When the format changed to Parquet, the spread took 3,814 ms. Almost all of that time was 225 thousand calls to `set_cell` (the journal cost was less than 10%).
Now `spread` reads the cells of the range as columns of member numbers for each dimension and distributes the values.
It writes them in 1 call to `write_many` of the engine, and it also extends the changed range 1 time.
`spread` alone decreased from 3.6 seconds to 22 ms (with Python 3.14t, from 3.7 seconds to 25 ms).
The save of a snapshot became slower because of the Parquet encoding and compression.
By default, the save occurs in a different thread every 1000 journal entries, thus writes do not wait.

The file journal also changed. It now writes large changes to the same Parquet files as `PgJournal`, not to JSON lines.
This is the comparison before and after (the same profit and loss plan (large), times through `Workspace`).
"Before" is the state after the spread changed to write together with `write_many`.

| Measurement (file journal) | Before | After | Before (3.14t) | After (3.14t) |
|---|---:|---:|---:|---:|
| Register bonuses by employee × month (720 thousand cells) | 869 ms | 488 ms | 994 ms | 594 ms |
| Spread sales volume (225 thousand cells) | 238 ms | 88 ms | 217 ms | 92 ms |
| Write 1 cell (median) | 5.0 ms | 5.0 ms | 6.0 ms | 6.0 ms |
| After the writes above, open from a snapshot and the journal | 1,808 ms | 50 ms | 1,774 ms | 53 ms |
| Get the history of 1 bonus cell | 584 ms | 10.5 ms | 527 ms | 9.1 ms |
| Size of the journal (without snapshots) | 23.8 MB | 0.8 MB | Same as left | Same as left |

For writes, the cost to convert changes of 225 thousand or 720 thousand cells to JSON strings is gone. (In the spread, this cost was 60% of the write time.)
To open, the engine reads large changes from Parquet as change blocks, and Rust writes them together.
(Before, the engine converted JSON lines to Python lists and wrote 1 cell at a time.)
For the history, the file journal reads the journal from the start. But Rust finds the 1 cell in the change blocks, thus the engine does not convert all rows to Python.
The write of 1 cell does not change, because almost all of its time is the write to the disk (`F_FULLFSYNC`, about 3 ms each time).

## Cold start of the HTTP server

The router starts an engine when the first request for a model comes ([design note](../../docs/engine-lifecycle.md)).
This table gives the time from the process start of `sparse_engine.server` with `--pg` until `GET /ready` returns 200.
The snapshot is on the local disk, at the published version, so the journal replay is empty.
Linux, 4 cores, PostgreSQL on the same host, 3 runs each.

| Model | Cells | Snapshot | Cold start |
|---|---|---|---|
| Profit and loss plan (small) | 13,841 | 0.1 MB | 0.30 to 0.58 s |
| Profit and loss plan (large) | 4,909,264 | 1.5 MB | 0.94 to 1.08 s |

Most of the time is the start of Python and the import of `nanashi_core`.
A snapshot in object storage adds the download time, and a journal with many entries after the snapshot adds the replay time.
