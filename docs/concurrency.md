# Concurrent reads and writes

If many users read and write at the same time, give the model to a `Workspace`.
`Workspace` publishes each committed state as a **version**.
It applies the writes in sequence on one thread (the writer).

```python
from sparse_engine.workspace import Conflict, Workspace

ws = Workspace.open("plan/", RustEngine(), checkpoint_every=1000)  # holds the model that it recovered from the journal

seq = ws.write(lambda m: m.set_cell("Price", 12, Product="A"), user="alice", reason="値上げ")
v = ws.version                                   # the published version (a read-only view)
v.get("Revenue", Product="A", Month="Jan")       # get, slice, rows, summarize and value are available
what_if = v.fork()                               # a copy for local trials (a usual Model)

try:  # rejects the write if a different user changed the same cell after the version that you read
    ws.write(lambda m: m.set_cell("Price", 13, Product="A"), user="bob", expect=v.seq)
except Conflict as e:
    print(e.user, e.seq)                         # which user changed it, and in which write
```

A version does not change after the engine makes it.
`version` is a read-only view (`Version`) and has no write operations.
You can get the `Model` below it with `version.model`, but an operation on it causes a ValueError.
A read always sees all of the published version.
Thus, a read does not see values from a write that is not complete (for example, new details with an old total).
A read also does not see changes that were cancelled.
A write changes only a copy that is not published.
Versions share the base of the stored data.
Thus, a new version costs only the size of the delta.
When no reader uses an old version, the engine discards it.

The writer collects the writes in the queue into one batch.
It applies each write as a transaction to a copy of the published version.
It commits the journal for the batch with one flush (group commit).
Then it publishes the copy as a new version.

- If one write fails, the writer cancels only that write. It commits the other writes in the same batch.
- If the journal flush fails, the writer discards all of the batch. The published version does not change.
- With `expect=`, give the sequence number of the version that you read. If a later write changed the same cell, the result is `Conflict`. The comparison uses the input cells that the write really changed. For a spread, this is all of the range that the spread changed.
- If `client_op_id=` is already committed, the writer does not apply the write and returns the initial sequence number. For each batch, the writer does one lookup in the journal to find the committed writes.
- `max_queue=` sets the queue length. If the queue is full, `submit` waits for the `timeout=` time and then raises `Overloaded`. `write(timeout=)` is the time to wait for the commit. After this time, the writer removes the write from the queue and raises `TimeoutError`.
- If you set `checkpoint_every=` (number of journal entries) or `checkpoint_interval=` (seconds), a different thread takes a snapshot of the published version at that interval. Writes do not stop during the snapshot, because versions share the base of the stored data.
- The journal can report that the local version is old (`journal.Stale`, when a different process wrote). Then the writer makes that batch fail and catches up to the latest version in the journal. It writes the entries that other processes committed to a copy of the published version as input changes. Then it calculates again only the affected range. If it cannot catch up, it opens the model again.

To add more reader processes, use `Replica` in a process that is not the write process (`Workspace`).
`Replica` is a read-only version that follows the journal.
As with `Workspace`, use `version` to read.

```python
from sparse_engine.workspace import Replica

replica = Replica(PgJournal(dsn, "plan-2027", "s3://nanashi/plans", heartbeat=False), RustEngine())
replica.version.get("Revenue", Product="A", Month="Jan")
```

A different thread monitors the journal.
For `PgJournal`, it monitors the `NOTIFY nanashi_head` that the writer sends at each commit.
For `FileJournal`, it monitors the file length.
It writes the entries that other processes committed to a copy of the published version as input changes.
Then it calculates again only the affected range and publishes the result as a new version.
For journal entries that change the definitions of dimensions, members or Metrics, it does a full recalculation.
With `--follow`, the HTTP server is a read-only server that does not accept writes (405).

Use `Workspace(standby=True)` if many processes open the same model and a different process must continue the writes when the writer stops (failover).
The HTTP server always uses this setting.
Each process is a writer (`Role.LEADER`) or a standby (`Role.STANDBY`).
`ws.role` shows the role.

```python
from sparse_engine.workspace import NotLeader, Workspace

ws = Workspace.open(PgJournal(dsn, "plan-2027", "s3://nanashi/plans", endpoint="http://plan-b:8080"),
                    RustEngine(), standby=True)
ws.role                                          # Role.STANDBY if a writer is available
try:
    ws.write(lambda m: m.set_cell("Price", 12, Product="A"))
except NotLeader as e:
    print(e.leader)                              # the address of the writer (http://plan-a:8080). Send the write there
```

- When the process opens the model, it tries to get the writer right in the journal with `take`. If it gets the right, it catches up to the journal and becomes the writer. If not, it becomes a standby.
- A standby follows the journal with a monitor thread (`nanashi-standby`), as `Replica` does, and accepts reads. It rejects writes immediately with `NotLeader` (`leader` has the address of the writer).
- At each interval (`interval=`, default 1 second), the standby tries to get the writer right. If the writer releases the right with `close` (SIGTERM), the standby gets the right immediately when it receives the notification. If the writer stops and its lease expires, the standby gets the right at the next interval. Then it catches up to the journal again and becomes the writer. While a process has the right, no other process can commit. Thus, the version after the catch-up is the latest version.
- The writer becomes a standby again if a commit is fenced (`Fenced`), or if the monitor finds that the writer does not have the right (it could not extend the lease). The writes in that batch, and the writes that entered the queue immediately before, get `NotLeader`. The writer does not open the model again, and the monitor does the catch-up.
- At each promotion, the process publishes the version again. Thus, a write with `expect` set to the sequence number of an earlier version gets `Conflict` (the same as when you open the model again).
- If many standbys try to get the free right at the same time, only one gets it (one conditional update in the journal).
- `ready()`, which is the same as `/ready`, returns an empty list also on a standby (the standby can accept reads). If the monitor failed, `ready()` returns the reason.

With `standby=False` (the default), the behavior does not change from before.
The process gets the right at the first write.
If a different process writes, the process opens the model again.
You can also use `standby=True` with `FileJournal` (the right is an exclusive `lock`).

In the profit and loss plan (4.9 million cells), 8 users each wrote 50 salary changes.
The engine committed about 500 writes per second (about 190 per second with one transaction commit for each write).
The median response time was 15 ms and the 95th percentile was 21 ms.
One flush committed 4 writes on average (local macOS, with a setting that flushes to disk).
