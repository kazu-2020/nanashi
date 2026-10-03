# Design note: separate storage from calculation, and scale out

This document is a design study. We implemented nothing in it.
For the current behavior, see [Engine](engine.md), [Concurrent reads and writes](concurrency.md), [Save and journal](persistence.md), and [Limitations and future work](limitations.md).
The measurements in [Store data on NVMe (SSD)](out-of-core.md) set many of the decisions below.
Two reviews of the first draft found the problems that the sections "Open problems" and "Risks" list.

## Goal

Today, one process holds the full model in memory and does all the calculation.
The other processes (standby, `Replica`) also hold the full model, and they calculate the same changes again.
In this note, a node is one machine or container. It runs one process with one role.
This design has 3 goals:

1. **Capacity**: the intermediate results of one stage do not have to fit in the memory of one node. The stored data of the writer stays in memory (see "Open problems").
2. **Elastic calculation**: a heavy recalculation uses more than one node. When the work is complete, the nodes stop.
3. **Isolation**: a scenario (a what-if analysis) of one user does not stop the reads and writes of other users. A heavy write of one user completes faster, but the other writes still wait for it.

## What does not change

- Each model has one writer. The writer applies the commits one at a time, and the journal fences old writers. There is no multi-writer mode.
- A published version does not change. Versions share the stored data.
- An HTTP write has a `client_op_id`, and the server does not commit a resent write two times.
- The reference implementation is the standard for correct results. A distributed recalculation must give the same values as the reference implementation, within the tolerance of the tests (an absolute tolerance of 1e-6).
- A small change stays on the writer. The engine sends work to other nodes only above a threshold.

## Measurements that set the design

These values come from [Performance](performance.md) and [Store data on NVMe (SSD)](out-of-core.md).
All values are from the cloud Linux VM (Intel Xeon 2.1 GHz, 4 vCPUs) with the fusion of operations, unless the row says otherwise.

| Measurement | Value |
|---|---|
| Change the salary of 1 employee (4.9 million cells, Apple M4) | 0.4 ms |
| Commit of 1 write (PostgreSQL in Docker) | 1.6 ms |
| Full recalculation, profit and loss plan (4.9 million cells) | 241 ms |
| Full recalculation, profit and loss plan × 4 (19.65 million cells) | 1,476 ms |
| Full recalculation, retail model (17.56 million cells) | 2,687 ms |
| Open from object storage, inputs only, then a full recalculation (4.9 million cells) | 403 ms |
| Open from object storage, snapshot with the formula values (79 MB), no full recalculation | 675 ms |
| The same, from the local disk | 163 ms |
| sha256 check of the same 79 MB | 201 ms |
| Memory increase during a full recalculation | about 1.25 to 1.4 times the new values of the formula Metrics |
| HTTP reads on 1 node while 1 user writes (free-threaded Python) | 9,700 per second |

Four conclusions follow.

- One GET from object storage costs about 10 to 20 ms (315 ms for a few dozen files). This is 25 to 50 times the cost of a small change, before any data moves. Thus small changes must never leave the writer.
- A snapshot with the formula values is faster than a full recalculation only from the local disk. From object storage, it is 1.5 to 2 times slower. Thus a process can use such a snapshot only with a local cache that survives a restart.
- After the fusion of operations, the memory peak is the stored data plus 1.25 to 1.4 times the new values. Workers remove only the second part from the writer.
- One node already serves about 10 thousand reads per second. Read capacity is not the measured bottleneck. The repeated recalculation on each reader is.

For all the models that we measured (up to 20 million cells), one node recalculates faster than any design that moves data between nodes.
We estimate that the design gives a gain only above about 100 million cells. It also gives a gain when 1 node does not have the memory for the intermediate results.
We did not measure this (see "Risks").

## Architecture

### Base files and manifests (the storage layer)

A **base file** is the base of the stored data of 1 Metric as a file. It has 2 immutable columns (u64 keys, f64 values) and the packing of the keys.
The packing is the partition dimension and the bit width of each dimension. The bytes of a base have a meaning only with their packing.
The engine keeps the base in this flat layout today (`Box<[u64]>`, `Box<[f64]>`, the same as the Arrow buffers).

The writer puts each base file in object storage under the sha256 of its bytes.
A base never changes, so a base file never changes, and all versions and all processes can share it.

`FileJournal` also calls a journal file a segment ([Save and journal](persistence.md)). This note does not use that word.

A **manifest** describes 1 published version. For each Metric (input Metric, formula Metric, and the counts for incremental aggregation), it has the hash of the base file.
The manifest also has `model.json`, the sequence number, and a new **calculation version**.
The calculation version is a number in the engine. A developer increases it when the meaning of evaluation, aggregation, key packing, or the file format changes.

Today, a snapshot has `manifest.json` with the hashes of the input files, which are zstd Parquet ([Save and journal](persistence.md)).
This design changes the input files to base files, and adds the formula Metrics, the counts, the packings, and the calculation version.

The writer does not make a manifest for each version. It makes 1 at each checkpoint (`checkpoint_every`, `checkpoint_interval`), as it makes a snapshot today.
At a checkpoint, the writer first merges the delta of each Metric into a new base, because a manifest refers only to bases.
A Metric gets a new base file at a checkpoint only if its cells or its packing changed.

A version between 2 manifests is "the last manifest plus the journal entries after it", as today.
A process that opens a version loads the manifest and applies the entries after it as input changes. The first recalculation is then incremental, not full.

The engine uses the values in a manifest only if the calculation version in the manifest is the version of the engine.
If the versions are different, or if an entry after the manifest changes a definition, the engine does a full recalculation.
A test in CI opens a manifest that the previous calculation version wrote (a fixture in the repository) and compares it with a full recalculation.
A manifest that the same build wrote cannot show a change of meaning.

Each node has a **base cache** on local NVMe, with the hash as the file name.
The cache must survive a restart of the process (a persistent volume). Without this, the second conclusion above applies, and the manifest gives no gain.
The process reads a base file with `pread` in fixed quantities. The fences (the first key of each 16 KB block), the delta tree, and the indexes for the other dimensions stay in memory.

A base on the disk makes a read of 1 cell 80 to 270 times slower (the out-of-core note). Thus a process that serves reads keeps the bases of the Metrics that it serves in memory.
Only workers read bases from the disk during a task.

### Roles

| Role | Holds | Does |
|---|---|---|
| Writer | The lease ([Save and journal](persistence.md)), the published version, the base cache | Applies the commits one at a time, applies all results, calculates small changes alone, sends the tasks of large waves to workers |
| Worker | The base cache, the Catalog and the plan of the versions that it calculates | Calculates 1 task and returns the result. Holds no lease and writes no journal entries |
| Reader | The published version, the base cache | Serves reads and scenarios. Follows the journal with incremental recalculation. Loads a manifest at open, or when its lag is more than a threshold |
| Standby | The same as a reader | The same as a reader. At failover it takes the lease and becomes the writer ([Concurrent reads and writes](concurrency.md)) |

The writer is the `Workspace` of today. The reader and the standby are the `Replica` and the standby of today.
A worker is a new Rust process (`nanashi-worker`, from the crate `nanashi-engine`) with no Python.
If no worker is available, the writer calculates the wave itself. The result is the same, and only the time changes.

### Distributed recalculation

For a Metric that is not in a scan, the recalculation schedule (`plan.rs`) already has the shape that this design needs ([Engine](engine.md), [Recalculation](recalculation.md)).
A stage (`level` in `plan.rs`) is a list of steps (`Step`). `prepare` makes a task (`Task`) from each step: 1 Metric, its affected range, the incremental aggregation flag, and the estimated rows.
`compute` reads stored data and returns a result (`Done`). `apply` applies the results in plan order.
The design makes `compute` a trait with 2 implementations: local (rayon, today) and remote (a client that sends tasks to workers).

A scan (`scan_step`) does not go through `compute`. It writes 1 time period at a time into its own stored data, and all Metrics of the cycle go together.
Thus a scan needs a new task type that runs the full time loop on the worker and returns the full affected range. The first version keeps scans on the writer.

`compute` reads more than the stored data of the referenced Metrics. A remote task must carry all of these:

- The Metric number, the affected range (member sets for each dimension), and the incremental aggregation flag.
- For each referenced Metric, the hash of its base file and its delta. The delta is small, and the writer sends it in the request.
- For each source of an incremental aggregation, the hash of its base file before this recalculation, and its changed range (`regions` in `plan.rs`).
- The hash of the base file and the delta of the Metric itself, and of its counts for incremental aggregation. `compute` compares the old and new values to make the changed member sets.
- The sequence number of the last definition change. The worker caches the Catalog (dimension sizes, property mappings, `Config`), the typed plan, and the packings for that number. The writer sends them only when the number is new.

An earlier stage of the same recalculation can change a referenced Metric. Then the writer first puts the new base file in object storage, or sends it to the worker directly. These are intermediate base files.
They go under a prefix with the writer epoch and the sequence number. The writer deletes them after the recalculation, and `prune` deletes the prefixes of older epochs.

A worker returns the same `Done` as today. It contains the new cells in key order, or the new values in the affected range. It also contains the changed member sets and the counts for incremental aggregation.
Small results come back in the response (the Arrow IPC format, 2 columns). Large results go through object storage as base files, and the response has the hash.

The writer applies the results on 1 thread, in task order, as soon as each result arrives. Thus the order of the applies does not depend on the number of workers, and the writer holds at most 1 result that waits.
The writer tags each task with its writer epoch, and examines the epoch before it sends each stage. A result from a task of an old epoch is ignored.

`remote_min` is a tuning value in `config.rs`, like `par_min`. The writer decides for each wave of a stage (the memory-budgeted group of tasks that `waves` makes today):

- If the estimated rows of the wave are less than `remote_min`, the writer calculates the wave locally.
- Otherwise, it sends the tasks to the available workers, and calculates the rest locally.

A task is a pure function of its inputs, given the same Catalog, plan, and configuration. Thus a worker failure or a deadline costs only a retry, on another worker or locally.
Each task has a deadline from its estimated rows. The writer never waits for a worker without a limit.
When a stage is almost complete, the writer calculates the remaining tasks itself instead of a wait for the slowest worker.

Every worker failure is a retry, never an error. Only a formula error is an error. (Today, an error in a recalculation sets a full recalculation for the next time.)

The time budget of a write also limits a distributed recalculation. The server waits 30 seconds for a commit (`write_timeout`), and the router 70 seconds for each send.
After that, the client gets 504 and resends. `client_op_id` prevents a second commit, but the client still sees the error.

### One Metric on many processes

Step 3 of the plan distributes tasks, that is, Metrics. A model with 1000 Metrics has many tasks in a stage, and the stage divides well.
A model with few, very large Metrics does not. Step 4 divides 1 task by a key range of the partition dimension of the result Metric.

- The range boundaries are fixed in key space (a fixed number of members of the partition dimension for each range). They do not depend on the number of workers.
- The partition dimension is in the high bits of the key. Thus a range of its members is 1 continuous range of the base. The writer puts the results one after the other in key order, without a sort.
- The engine already evaluates every formula under a limit on any dimension (the incremental path). Thus the tests cover the correctness of each node type under a key range.
- The engine does not fuse an evaluation with a limited range today ([Limitations and future work](limitations.md)). Thus a key-range task makes an intermediate result for each operation, 3 to 6 times its result. Fusion of limited ranges is a prerequisite of step 4.
- A source with a different partition dimension has no locality. Each worker reads the full base file of the source and filters it with the index. The engine selects the partition dimension for each Metric, so this case is common (payroll by Employee, the result by Department).
- A `BY` aggregation with a Metric as the mapping (employee and month to department) has no inverse mapping. Each worker reads the full source and the full mapping.
- An aggregation that removes the partition dimension gives a partial result on each worker. The worker returns the partial states (`Acc` in `agg.rs`: sum, count, min, max), never finished values. The writer merges the states in range order. AVG is sum divided by count. The `first` function has 1 value in each group by construction, and the merge asserts this.
- Only an aggregation that is the outermost operation of a formula merges this way. For other formulas (for example `A[REMOVE SUM: Store] / B[REMOVE SUM: Store]`), the planner splits the formula at the aggregation. The workers calculate the aggregation, and the writer evaluates the rest.
- A scan goes through the time dimension in sequence. A key range on a scan is possible only if all Metrics of the cycle have the same partition dimension. That dimension must not be the time dimension. The engine does not require this today.

Floating-point sums can differ in the last bits from a calculation on 1 node. This is also true today between different thread counts ([Engine](engine.md)).
The tests compare with an absolute tolerance of 1e-6.

### Add and remove workers

A worker holds no lease and no journal state. Thus the number of workers can go from 0 to N and back at any time.
But a worker with a cold cache first fetches the base files of its first task. This takes 0.7 to 2 seconds for the measured models, and tens of seconds for a model of 100 million cells.
Thus workers must be warm before the heavy write that needs them. The writer sends the manifest of the published version to new workers, and they fetch its base files.

The signal to add workers is the pending work of the writer: the queue length and the estimated rows of the pending remote tasks.
`/stats` already reports the queue length. The design adds the estimated rows.

If a worker stops during a task, the writer retries the task. The writer loses no data, because the inputs are immutable base files.

### Isolation

A **scenario** is a what-if analysis ([Model operations](modeling.md), `fork`) that runs on a reader.
Today, `Version.fork()` runs in the process that holds the version, that is, usually the writer.
In this design, the HTTP API gets a `/scenarios` endpoint. The reader forks the published version, applies the changes of the user, and recalculates on the reader.
A scenario never takes the lease and never writes to the journal.

When the user commits the scenario, the client sends the changes as a usual write to the writer. The write sets `expect` to the sequence number of the forked version, so the conflict check applies.
A scenario that changed a definition (a formula or a member) also sends the definition changes as operations. The write replays the operations, not the values.

A scenario is the state of 1 reader. The router sends the requests of a scenario to that reader, and the scenario stops when the reader stops.
Each scenario has a time to live and a cell limit (`max_cells`).
There is 1 rayon thread pool for each process ([Limitations and future work](limitations.md)). Thus a heavy scenario slows the other reads of the same reader. Isolation is between nodes, not between users.

The router sends reads to readers in this design. Today it sends all requests to the writer ([Router](../../router/README.md)), so that a user reads the user's own write.
This stays the default: a read without a sequence number goes to the writer.
A client that sends the sequence number of a version can get the read from a reader that reached that version. If no reader reached it within a short wait, the router sends the read to the writer.

For this, the router needs a list of readers: registration in the journal table, health from `/ready`, and removal on failure. Today the journal table has only the address of the writer.
The router also routes by method, not only by status. A write to a reader gets 405 today, and the router returns it as it is.

The writer still completes 1 batch before it starts the next one (no pipelining, see [Limitations and future work](limitations.md)).
With workers, a heavy batch completes faster, but the other writes still wait during that batch.
Pipelining of writes is out of scope: a batch reads the result of the batch before it.

## Open problems

- **The stored data of the writer stays in memory.** Every result goes through `apply` on the writer. The writer makes the new base from it. A base on NVMe makes a read of 1 cell 80 to 270 times slower. Thus goal 1 covers only the intermediate results. The stored data off the writer is item 2 of the out-of-core note, and this note does not solve it.
- **Traffic between stages.** The output of a stage is the input of the next stage. A model can have about 10 stages and tens of MB for each Metric. Then a heavy write pays hundreds of PUT and GET round trips, at 10 to 20 ms each. Estimate this for the target model before step 3.
- **A fenced writer and the checkpoint.** `save_snapshot` registers a snapshot with no check of the writer epoch. Demotion does not stop the checkpoint thread. Today this is harmless, because 2 writers of the same sequence number have the same state. With formula values from 2 builds, the manifest must carry the writer epoch, and the registration must check it.

## Changes for each layer

| Layer | Change |
|---|---|
| `store.rs` | Export a base as a base file and build a base from a base file (2 columns, no copy). Keep the packing with the base. Let a base refer to a buffer that a cache file owns. |
| `plan.rs` | Split `level` into prepare, compute, and apply. Make compute a trait. Apply each result as it arrives, in task order. Add `remote_min` to `config.rs`. Later: a task type for scans, the key-range division, and the merge of `Acc` states. |
| `eval/fuse.rs` | Fuse an evaluation with a limited range (a prerequisite of step 4). |
| New crate `nanashi-worker` | Loads base files from the cache or object storage, caches the Catalog and the plan for each definition version, runs compute, and returns results. No Python. Serialization of `Node`, `Plan`, and `Catalog` (there is none today). |
| Journal | A manifest table (or `nanashi_snapshot` with a kind). The manifest holds the hashes and packings of all base files, the counts for incremental aggregation, the calculation version, and the writer epoch. `prune` deletes base files that no kept manifest refers to, and the intermediate prefixes of old epochs. |
| `Workspace`, `Replica` | See "Roles". Scenarios on readers. |
| Server, router | Reads with a sequence number go to readers. A `/scenarios` endpoint with affinity. A list of readers in the journal table. Mutual authentication between the writer and the workers, and object storage credentials on the workers. |
| `/stats` | For each stage and task: time, bytes moved, cache hits, retries, and deadline hits. |
| Reference implementation | No change in results. The tests run the writer with an in-process worker and `remote_min=0`, so that small models use the remote path (the same method as `par_min=0` today). Fault injection: a worker stops during a task, a result arrives 2 times, a result from an old epoch, a missing intermediate base file. |

## Plan

Each step gives a result alone. After each step, measure the gain. If the next step gives no gain, stop.

1. **Manifest with the formula values, and the base cache on a persistent volume.** Readers and restarted writers open a model without a full recalculation. Steps 3 and 4 need this step. This step gives a gain only on a node with a warm cache. From object storage, a manifest with values opens 1.5 to 2 times slower than a full recalculation.
2. **Reads with a sequence number to readers, and scenarios on readers.** This gives goal 3. It needs no change in the engine and does not depend on step 1. It does not remove the repeated recalculation on each reader. Only step 1 does.
3. **Compute trait, worker process, and tasks by Metric.** This gives goal 2 for models with many Metrics. Before this step, measure a model of 100 million cells or more on 1 node, and estimate the traffic between stages.
4. **Fusion of limited ranges, key-range division of 1 task, and merge of `Acc` states.** This gives goal 2 for models with few, very large Metrics. It also gives goal 1.
5. **Signals to add and remove workers.** Export the pending work in `/stats`, document the rules for a scheduler, and document the warm-up of new workers.

## Decisions and rejected options

- **Multi-writer by dimension (shard the writes by region or by product).** We rejected it. A formula aggregates across all dimensions. A write in 1 shard changes totals in all shards, and this needs distributed transactions. One writer with distributed recalculation gives the same throughput for the calculation, with none of this complexity.
- **A manifest for each version.** We rejected it. The writer commits 500 to 700 writes per second with 8 users ([Performance](performance.md)). A manifest with the formula values is 79 to 318 MB for the measured models. A manifest at each checkpoint, plus the journal, is sufficient (see "Base files and manifests").
- **Workers in Python.** We rejected it. The worker runs only `compute`, which is Rust today. A Rust process starts faster and uses less memory, and the scale-out does not add a Python dependency.
- **Move the data to the calculation (send base files to workers for each task).** Only for intermediate base files. Workers read all other base files from their cache. We expect that a worker with the base files in its cache is about as fast as a local thread. The difference is 1 round trip and the load of the base files. Measure this in step 3.

## Risks

- The gain exists only above a threshold of model size. For all the models that the benchmarks measure today, 1 node stays faster. The threshold of 100 million cells is an estimate from 15 to 100 ns for each cell, not a measurement. Before step 3, measure with a model of 100 million cells or more.
- A manifest from a different engine version must not open without an error. The calculation version in the manifest and the CI test above prevent this. A worker refuses a task with a different calculation version. Upgrade the workers first, then the writer.
- If the writer stops during a recalculation, its intermediate base files remain in object storage. `prune` must also delete the prefixes of old epochs. Today, there is no cleanup for files that nothing refers to.
- The object storage holds more data (base files of the formula Metrics). `prune` and compression of the value columns limit this. We did not measure the compression. Random values compress badly, and keys compress well.
- A deadline that is too short makes the writer calculate locally after it waited. Set the deadline from the estimated rows of the task. Bound the number of tasks and the bytes in flight by the memory budget (`max_bytes`).
- The worker endpoint runs plans and reads object storage. It needs mutual authentication, and a worker that caches base files of many models needs separation between the models.
