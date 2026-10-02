# Design note: separate storage from calculation, and scale out

This document is a design study. Nothing in it is implemented.
For the current behavior, see [Engine](engine.md), [Concurrent reads and writes](concurrency.md), [Save and journal](persistence.md), and [Limitations and future work](limitations.md).
The measurements in [the out-of-core design note](out-of-core.md) set many of the decisions below.

## Goal

Today, one process holds the full model in memory and does all the calculation.
Other processes (standby, `Replica`) also hold the full model, and they calculate the same changes again.
This design has 3 goals:

1. **Capacity**: the memory of one node does not limit the size of a model. Storage is separate from calculation.
2. **Elastic calculation**: a heavy recalculation uses more than one node. The nodes go away when the work is complete.
3. **Isolation**: a heavy write or a what-if simulation of one user does not stop the reads and writes of other users.

## What does not change

- Each model has one writer. The writer serializes the commits, and the journal fences old writers. There is no multi-writer mode.
- A published version does not change. Versions share the stored data.
- An HTTP write has a `client_op_id`, and a resent write is not committed two times.
- The reference implementation is the standard for correct results. A recalculation on many nodes must give the same values as a recalculation on one node.
- A small change stays on the writer. The engine sends work to other nodes only above a threshold, as `par_min` does today for threads.

## Measurements that set the design

These values come from [Performance](performance.md) and [the out-of-core design note](out-of-core.md).

| Measurement | Value |
|---|---|
| Change the salary of 1 employee (4.9 million cells) | 0.4 ms |
| Commit of 1 write (PostgreSQL) | 1.6 ms |
| Full recalculation, profit and loss plan (4.9 million cells) | 87 ms (M4), 363 ms (cloud VM) |
| Full recalculation, retail model (17.56 million cells) | 1.7 s (M4), 2.7 s (cloud VM) |
| Fetch of 79 MB of formula values from object storage | 315 ms in sequence, 196 ms with 8 in parallel |
| sha256 check of the same 79 MB | 201 ms |
| Memory increase during a full recalculation | about 1.25 to 1.4 times the new values |

Three conclusions follow.

- A round trip to another node or to object storage costs milliseconds. This is 100 to 1000 times the cost of a small change. Thus small changes must never leave the writer.
- For a model of 20 million cells or less, one node recalculates faster than any design that moves data between nodes. The design pays off only for models above about 100 million cells, or when one node does not have the memory.
- The memory peak is the intermediate results, not the stored data. If more than one node calculates a stage, each node holds only its part of the intermediate results.

## Architecture

### Segments and manifests (the storage layer)

A **segment** is the base of the stored data of one Metric: two immutable columns (u64 keys, f64 values).
The engine keeps the base in this flat layout today (`Box<[u64]>`, `Box<[f64]>`, the same as the Arrow buffers).
The engine puts each segment in object storage under the sha256 of its bytes.
Because a base never changes, a segment never changes, and all versions and all nodes can share it.

A **manifest** describes one published version: for each Metric (input, formula, and the counts for incremental aggregation), the hash of its segment.
The manifest also has `model.json`, the sequence number, and the calculation version of the engine.
A snapshot today is a manifest with only the inputs. This design extends it with the formula Metrics.

The writer does not make a manifest for each version. It makes one at each checkpoint (`checkpoint_every`, `checkpoint_interval`), as it makes a snapshot today.
A version between two manifests is "the last manifest plus the journal entries after it", as today.
A node that opens a version loads the manifest and applies the entries after it as input changes. The first recalculation is then incremental, not full.

The engine uses the values in a manifest only if the calculation version in the manifest is the version of the engine.
If the versions are different, or if an entry after the manifest changes a definition, the engine does a full recalculation.
A test in CI compares a model opened from a manifest with a full recalculation of the same model. This test finds a change in the meaning of evaluation that did not increase the calculation version.

Each node has a **segment cache** on local NVMe, with the hash as the file name.
The node reads a segment with `pread` in fixed quantities and keeps only the fences in memory, as the out-of-core note recommends.
The delta tree, the indexes for the dimensions other than the partition dimension, and the fences stay in memory.
With the cache, a node that opens a model again reads the local disk, not object storage. The out-of-core note found that only this condition makes the manifest faster than a full recalculation. Measure this again with the cache before step 1 below is complete.

### Roles

| Role | Holds | Does |
|---|---|---|
| Writer | The lease, the published version, the segment cache | Serializes commits, applies all write-backs, calculates small changes alone, sends large stages to workers |
| Worker | Only the segment cache | Calculates one task (one Metric, or one key range of one Metric) and returns the result. Holds no lease and writes no journal entries |
| Reader | The published version, the segment cache | Serves reads and what-if simulations. Loads a new manifest instead of a recalculation when a manifest is newer than its version |

The writer is the `Workspace` of today. The reader is the `Replica` and the standby of today.
A worker is a new Rust process (`nanashi-worker`, from the crate `nanashi-engine`) with no Python. It is a pure function from immutable segments to a result.
If no worker is available, the writer calculates the stage itself. The result is the same, and only the time changes.

### Distributed recalculation

The recalculation schedule (`plan.rs`) already has the shape that this design needs.
A stage (`level`) has tasks. A task is one Metric, its affected range, and the incremental aggregation flag.
`compute` reads only the stored data of the referenced Metrics and returns a result (`Done`). `apply` writes the results back in plan order.
The design makes `compute` a trait with two implementations: local (rayon, today) and remote (a client that sends tasks to workers).

A remote task contains:

- The Metric number, the affected range (member sets for each dimension), and the incremental aggregation flag.
- For each referenced Metric, the hash of its segment and its delta (the delta is small, and the writer sends it inline).
- An earlier stage of the same recalculation can change a referenced Metric. Then the writer first puts the new segment in object storage, or sends it to the worker directly. These are intermediate segments. The writer deletes them after the recalculation.

A worker returns the same `Done` as today. It contains the new cells in key order (or the new values in the range), the changed member sets, and the count store.
Small results come back in the response (Arrow IPC). Large results go through object storage as segments, and the response has the hash.
The writer applies the results in plan order on one thread, as today. Thus the order of the write-backs, and the result, do not depend on the number of workers.

The writer decides for each wave of a stage (the memory-budgeted group of tasks that `waves` makes today):

- If the estimated rows of the wave are less than `remote_min` (a tuning value in `config.rs`, like `par_min`), the writer calculates the wave locally.
- Otherwise, it sends the tasks to the available workers, and calculates the rest locally.

A task is a pure function of immutable inputs. Thus a worker failure or a deadline costs only a retry, on another worker or locally.
Each task has a deadline. The writer never waits for a worker without a limit.

### One Metric on many nodes

The first step distributes tasks, that is, Metrics. A model with 1000 Metrics has many tasks in a stage, and the stage divides well.
A model with few, very large Metrics does not. The second step divides one task by key range.

- The partition dimension is in the high bits of the key. Thus a range of members of the partition dimension is one continuous range of the base. A worker evaluates one range, and the results concatenate in key order without a sort.
- An aggregation that removes the partition dimension gives a partial result on each worker. The writer merges the partial results. SUM, COUNT, MIN, and MAX merge directly. AVG merges as SUM and COUNT. The merge order is fixed (by range), so the result does not change with the number of workers.
- A scan (`PREVIOUS`) goes through the time dimension in sequence, but the other dimensions are independent. If the partition dimension is not the time dimension, each worker scans its range of the partition dimension.
- A join with different dimensions on the two sides reads the other side fully. This is the case today in one process, and the semi-join filter limits the read. A worker reads the other side from its cache.

### Elastic nodes

A worker holds no state of the model. Thus the number of workers can go from 0 to N and back at any time.
The signal to add workers is the work in the queue of the writer: the estimated rows of the pending stages and the queue length. `/stats` already reports the queue length. The design adds the estimated rows of pending remote tasks.
When a worker goes away during a task, the writer retries the task. No data is lost, because the inputs are immutable segments.

### Isolation

A what-if simulation is `Version.fork()` today, and it runs in the process that holds the version.
In this design, a simulation runs on a reader, not on the writer. The HTTP API gets a scenario: the reader forks the published version, applies the changes of the user, and recalculates on the reader.
A scenario never takes the lease and never writes to the journal. When the user commits the scenario, the client sends the input changes as a usual write to the writer.
Thus a heavy simulation of one user uses the CPU of a reader, and the writer continues to accept the writes of other users.

The router sends reads to readers in this design. Today it sends all requests to the writer ([Router](router.md)).
A client that must read its own write sends the sequence number of the write. A reader that is behind that sequence number waits for the journal, or the router sends the read to the writer.

The writer still completes one batch before it starts the next one (no pipelining, see [Limitations](limitations.md)).
With workers, a heavy batch completes faster, but the other writes still wait during that batch.
Pipelining of writes is out of scope: a batch reads the result of the batch before it.

## Changes for each layer

| Layer | Change |
|---|---|
| `store.rs` | Export a base as a segment and build a base from a segment. Let a base refer to a buffer that a cache file owns (the owner abstraction that the out-of-core note describes, without `unsafe`). |
| `plan.rs` | Split `level` into prepare, compute, and apply. Make compute a trait. Add the key-range division and the merge of partial aggregations. Add `remote_min` to `config.rs`. |
| New crate `nanashi-worker` | Loads segments from the cache or object storage, runs compute, and returns results. No Python. |
| Journal | A manifest table (or `nanashi_snapshot` with a kind). The manifest holds the hashes of all segments, the counts, and the calculation version. `prune` deletes segments that no kept manifest refers to. |
| `Workspace`, `Replica` | A reader loads a manifest when it is newer than its version. Scenarios on readers. |
| Server, router | Reads go to readers. A `/scenarios` API. A read-your-write option with the sequence number. |
| Reference implementation | No change in results. The tests run the writer with an in-process worker and `remote_min=0`, so that small models use the remote path (the same method as `par_min=0` today). |

## Steps

Each step gives a result alone. Measure after each step, and stop if the next step does not pay.

1. **Manifest with formula values and the segment cache.** Readers and restarted writers open a model without a full recalculation. This is the base for all later steps. The out-of-core note has the first measurement; measure again with the cache.
2. **Reads to readers, scenarios on readers.** This gives goal 3 for simulations and read capacity that grows with the number of readers. It needs no change in the engine.
3. **Compute trait, worker process, tasks by Metric.** This gives goal 2 for models with many Metrics.
4. **Key-range division of one task, and the merge of partial aggregations.** This gives goal 2 for models with few, very large Metrics. With the cache, a node holds only the segments of its tasks, which gives goal 1.
5. **Autoscale signals.** Export the pending work in `/stats`, and document the rules for a scheduler.

## Decisions and rejected options

- **Multi-writer by dimension (shard the writes by region or by product).** Rejected. A formula aggregates across all dimensions. A write in one shard changes totals in all shards, and this needs distributed transactions. One writer with distributed calculation gives the same throughput for the calculation, with none of this complexity.
- **A manifest for each version.** Rejected. The writer commits hundreds of writes per second, and a manifest of formula values is 79 to 318 MB for the measured models. A manifest at each checkpoint, plus the journal, is sufficient.
- **Workers in Python.** Rejected. The worker runs only `compute`, which is Rust today. A Rust process starts faster and uses less memory, and the scale-out does not add a Python dependency.
- **Move the data to the calculation (send segments to workers for each task).** Only for intermediate segments. Workers read all other segments from their cache. The cache makes a warm worker as fast as a local thread, less one round trip.

## Risks

- The gain exists only above a threshold of model size. For all the models that the benchmarks measure today, one node stays faster. Measure with a model of 100 million cells or more before step 3.
- A manifest from a different engine version must not open silently. The calculation version in the manifest and the CI test above prevent this.
- The object storage holds more data (segments of formula Metrics). `prune` and compression of the value columns limit this. The out-of-core note measured no compression; measure zstd on the value column.
- A deadline that is too short makes the writer calculate locally after it waited. Set the deadline from the estimated rows of the task.
