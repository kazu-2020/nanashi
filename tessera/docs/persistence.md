# Save and journal

## Save and load

```python
m.save("plan/")                    # The definitions go to model.json. The input data goes to 1 Parquet file for each input Metric.
m2 = Model.load("plan/", RustEngine())
```

The engine does not save the values of formula Metrics. It calculates them again in the first recalculation after the load.
The engine saves each formula as the original text that the user wrote.
It also saves the IDs of dimensions, members, properties, and Metrics, their UUIDs (`uuid`, `member_uuids`), and the tombstones (`tombstones`, the UUIDs of removed objects).
[ids.md](../../docs/ids.md) gives the rules for the UUIDs.

The engine writes the input values of each input Metric to `inputs.<Metric ID>.parquet` (save format version 5).
The columns are the member numbers of each dimension (`d<dimension ID>`, UInt32) and the value column `v`.
The type of `v` is Float64 for number, Boolean for boolean, and UInt32 (the member number) for a member type. The engine compresses the file with zstd.
The column names use the dimension IDs, which do not change when you rename a dimension. Thus other tools, for example DuckDB, can also read the files (the member names are in `model.json`).
The Rust engine writes Parquet directly from the stored data, and it makes the stored data directly from Parquet when it loads.
Thus it does not make Python objects, also for large models (it also releases the GIL).

A member number is the position of the member in the member list of `model.json`.
Some dimensions have an order that is different from the order of the numbers. These are dimensions that have an inserted member or a changed order, and dimensions without order.
For these dimensions, `model.json` also has `member_order`, the list of numbers in the member order.

## Transactions and the journal

You can put many operations into 1 `transaction`.
If an exception occurs in the transaction, or if the recalculation at the end gives a formula error, the engine cancels all operations.

```python
with m.transaction(user="alice", reason="予算の見直し") as txn:
    m.set_cell("Budget", 100, Product="A", Month="Jan")
    m.spread("Budget", 1200, Product="B")
txn.seq, txn.record  # The sequence number and the journal entry of the commit
```

At the start, the engine makes a copy of the model. To cancel, it goes back to that copy.
The copy shares the base of the stored data. Thus the copy is cheap (0.2 ms in the profit and loss plan, 1 ms in a model with 1000 Metrics).

If you attach a journal (`journal`), the engine keeps 1 journal entry for each committed transaction.
If you call an operation outside a transaction, the engine records that 1 call as 1 transaction.

```python
from sparse_engine.journal import FileJournal

FileJournal("plan/").start(m)           # Make the current state the first snapshot, and start the journal
m.set_cell("Price", 12, Product="A")    # This becomes 1 journal entry
m.checkpoint()                          # Make a snapshot (open then reads fewer journal entries)

m2 = FileJournal("plan/").open(RustEngine())  # Restore from the latest snapshot and the journal entries after it
m2.journal.cell_history(m2, "Price", Product="A")  # The change history of the cell (who, when, from which value to which value)
```

A journal entry has these 2 parts:

- **Intent**: the called operation and its arguments (the total of a spread, the text of a formula, and so on). The engine keeps it for audits.
- **Result**: the difference of the model before and after the transaction. It uses IDs that do not change to show these items: added, deleted, and renamed dimensions and members, the member order, properties, Metric definitions, and the values of input cells before and after the change.
  `uuids` has the UUID of each new handle, and `tombstones` the UUIDs that the transaction made tombstones. Thus a replay binds the same UUIDs to the same handles.
  The member order (`member_order`) is a list of member IDs in the member order.
  The engine records it only for dimensions where the order changes in a different way than "remove the deleted members and put the added members at the end".
  A journal entry that changes only the order is not a structural change. Thus `Replica` and other followers do not recalculate when they catch up.

The restore replays only the results, and then does 1 full recalculation at the end.
If the engine replays the operations, values such as the floating-point values of a spread can be different between engine versions.
But if it replays the results, it only writes them, and it gets the same state again.
The journal does not depend on the engine. Thus the reference implementation can open a journal that the Rust engine wrote.

The engine does not record each operation separately to get the result. It compares the model before and after.
Thus the journal keeps all results of operations that change many cells, for example a spread or the deletion of a member.
In the Rust engine, the stored data of a Metric without writes points to the same data as before the copy. Thus the engine does not have to compare it.
For a Metric with writes, the engine compares only the different parts of the delta tree.

In the Rust engine, some journal entries change 1000 cells or more in 1 Metric.
For these entries, the engine keeps the changes to input cells as a change block (`nanashi_core.CellBlock`), not as a list of rows.
Like a list of rows, a change block has a length and returns `[list of coordinate IDs, before, after]` in sequence. But it does not make Python objects until you read them.
Rust does the comparison of changes, the write to the PostgreSQL journal, and the write during journal replay.
The optimistic lock of `Workspace` also uses the change blocks directly to find the same cells.

The production journal is `PgJournal` (next section). `FileJournal` is mainly for development and verification.
`FileJournal` keeps the journal and the snapshots in a directory.

- `log/<first sequence number>.jsonl`: The journal, with 1 transaction on each line.
  After the engine puts a snapshot, it writes the next entries to a new file (a segment).
  To open, the engine reads only the last segment and the segments in the range where it remembers `client_op_id` (the last 100 thousand entries, `op_window`).
  For replay, it reads only the segments after the snapshot.
  The engine appends the entry and writes it to the disk (`F_FULLFSYNC` on macOS), and then commits.
  If the write or the check of the write fails, the engine makes the file the length from before the append again. If it cannot do this, it refuses all subsequent writes.
  If the last line is not complete (the process stopped during the write), readers ignore it, and the next writer removes it.
- `lock`: An exclusive lock (`flock`) that lets only 1 process write. The engine gets it at the first append. If a different process has it, the result is `Fenced`.
- `cells/<random number>-<Metric ID>.parquet`: The cell changes of a journal entry that changes more than 10 thousand cells (`bulk_cells`).
  The format is the same as `PgJournal`. The journal line has only the file name (relative to the directory) and the hash.
  The engine writes the file before it appends the line. Thus the files of a committed journal entry are always complete.
- `snapshots/<sequence number>-<random number>/`: The model at that time, and `manifest.json` with the hashes of the files.
  The engine puts each file first, and then puts `manifest.json` last. Thus it does not use a snapshot that stopped before completion.
  The engine checks the hashes while it reads the files at open. If a file is damaged, it uses the snapshot before that one and replays more journal entries.

`journal.prune(keep=2)` keeps the 2 newest snapshots. It deletes the older snapshots, and the journal and the files for large changes before them.
(After this, you cannot go back to a time before the kept snapshots.)

If you give the ID from the sender to `transaction(client_op_id=...)`, and a transaction with the same ID is already committed, the engine raises `AlreadyCommitted`. It does not do the operations in the transaction.
Thus, if a user does not receive the response and sends again, the engine does not commit twice.

The HTTP server also records a rejected write with its `client_op_id` (`record_rejection`: the HTTP status and body).
`FileJournal` keeps these in memory for the last `op_window` rejections. After a restart, the same write gets the same rejection again, because the model refuses it again.
`PgJournal` keeps them in the table `nanashi_rejection`, so a restarted server gives the same answer.

### Journal in PostgreSQL

`PgJournal` has the same interface as `FileJournal`, and it keeps the journal in PostgreSQL.

```python
from sparse_engine.pg_journal import PgJournal

journal = PgJournal("postgresql://...", "plan-2027", "s3://nanashi/plans")  # The model ID, and the location of the files
ws = Workspace.open(journal, RustEngine())
```

The location of the files is `s3://<bucket>/<prefix>`. The files go into `<model ID>/` below it.
The engine reads the endpoint and the credentials from environment variables, as boto3 specifies.
For a local RustFS, use the values below. Make the bucket first.

```bash
export AWS_ENDPOINT_URL=http://127.0.0.1:59000 AWS_ACCESS_KEY_ID=nanashi AWS_SECRET_ACCESS_KEY=nanashi-secret AWS_REGION=us-east-1
.venv/bin/python -c 'import boto3; boto3.client("s3").create_bucket(Bucket="nanashi")'
```

If the location does not start with `s3://`, the engine puts the files in a local directory (for when you do not have object storage).

There are 5 tables (`nanashi_model`, `nanashi_operation`, `nanashi_cell_change`, `nanashi_snapshot`, `nanashi_rejection`). 1 database can hold many models.
The schema has a version (`nanashi_schema`). To update it to the latest version, run `python -m sparse_engine.pg_journal migrate <DSN>` (for the server, use `--migrate`).
The engine does not run DDL at each connection, because DDL gets table locks and competes with other processes that write.
If the version is not correct, `PgJournal` raises `SchemaError` when it opens.
`journal.prune(keep=2)` deletes old snapshots and the files for large changes before them.
The engine first adds those changes to the cell history table and then deletes the files, thus journal replay and the cell history stay available.
It also forgets the `client_op_id` of journal entries older than the last 100 thousand entries, and the rejections recorded before that point.

- **Operation** (`nanashi_operation`): 1 row for each transaction. It keeps the intent and the results other than cells as JSONB.
- **Rejection** (`nanashi_rejection`): 1 row for each rejected write (`client_op_id`, the HTTP status and body, and the head sequence number at that time). A resent write gets the same answer.
- **Cell change** (`nanashi_cell_change`): 1 row for each changed input cell. It keeps the Metric ID and an array of member IDs, and an index finds the history of 1 cell.
  A process (`index_pending`) adds large changes to this table after the commit.
  If many processes call it at the same time, an advisory lock for each model and a mark on each journal entry prevent duplicate writes of the same entry.
- **Snapshot**: The engine puts the files in `<model ID>/snapshots/<sequence number>-<random number>/` in the object storage.
  It puts `manifest.json` last, and then registers the snapshot in the table.
  The table keeps only the key relative to the location. Thus, if you copy the files to a different location (for example, from a directory to S3), you can open them without changes.
  The engine does not change a file after it puts it. (If it makes a snapshot again with the same sequence number, it puts it in a different location.)
  The engine checks the hashes while it reads the files at open.
  If a file is missing or the hash is not correct, it uses the snapshot before that one and replays more journal entries. (It does not read all files to make a list.)

Only 1 process can write.
At the first write, the process gets the lease and increases the generation number by 1.
A commit is 1 database transaction. It succeeds only if the sequence number is the same as the process read, and the generation number is the number of this process.
If the lease expires and a different process gets the lease, the engine refuses commits from the old process (`Fenced`). This is also true when the new process has not written yet.
If a different process wrote after the load, the local model is old. Thus the engine refuses to give the lease.

While there are no writes, a different thread extends the lease at each 1/3 of the lease time (`heartbeat=False` stops this).
If this thread finds that a different process took the lease, the next commit fails with `Fenced`. This is also true when that lease expired and nobody wrote. Thus a writer that lost the lease does not get it again without notice. The commit after that gets the lease again.
`close` releases the lease. Thus the next process that writes does not wait for the lease to expire.
`Workspace.close` also releases the lease of the journal.
Thus, if you stop the HTTP server and start it again immediately, the first write does not wait.
If the lease of a stopped process remains, `acquire` waits until the lease expires (by default, up to the length of `lease_ttl`), and then gets it.
Thus a restart does not continue to fail until the lease expires (with `acquire_wait=0`, it does not wait).
`Fenced` is a type of `journal.Stale`. When `Workspace` receives it, it catches up to the latest version of the journal.

The lease also contains the address that the owner publishes (`lease_endpoint`, given with `PgJournal(endpoint=...)`).
`journal.leader()` returns the address of the lease that has not expired (or `None` if there is no such lease).
A standby uses it to tell the client the writer when it refuses a write. The router uses it to find the writer.
If the lease is free (no owner, or expired), `journal.take()` gets it without a wait and returns `True`.
If a different process has the lease and it has not expired, `journal.take()` returns `False`.
Unlike `acquire`, it does not check the local sequence number. Thus the process that got the lease must catch up to the journal before it writes (promotion of a standby, [Concurrent reads and writes](concurrency.md)).
`release` sends the same notification as a commit (`NOTIFY nanashi_head`). Thus the standby can try to get the lease without a wait for the interval.

For a change of more than 10 thousand cells, the engine writes the values before and after to 1 Parquet file for each Metric.
It puts these files in the object storage (`<model ID>/cells/`) and then commits.
The write to the cell change table occurs after the commit (`index_pending`).
The Parquet columns are the member IDs of the coordinates (`d<dimension ID>`, Int64), the value before (`old`), and the value after (`new`). A blank value is null.
The engine selects Parquet columns by name when it reads, also in the save format. The column order is not important, and it does not read unknown columns.
Thus an earlier version can read files after a later version adds columns.
The reason for the delay is that it is expensive to insert rows into the table 1 at a time. If this were in the commit path, commits of large writes would become more than 10 times slower.
Before the engine gets the history of a cell, it first adds the changes that are not in the table yet.
Rust makes the rows for the COPY into the table.
After the engine adds more than 100 thousand rows, it updates the statistics of the table.
(Immediately after a large insert, the statistics are old. Then the query plan does not use the cell index, and the history of 1 cell took 250 ms. After the update of the statistics, it takes 0.2 ms.)
