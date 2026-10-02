# Design note: store data on NVMe (SSD)

This document is a design study. We implemented only the first step of item 2 of the plan: the base is now an immutable column with no unused capacity. For all other parts, there is only an example for measurement (`native/engine/examples/out_of_core.rs`).

## Conclusion

- The memory peak is not the stored data. It is the intermediate results of recalculation (see the memory breakdown in [Performance](performance.md)). In the retail model, the intermediate results are more than 6 times the stored data. Thus, the first task is to decrease the intermediate results (fusion of element-wise operations), not to use the disk.
- You can put the base of the stored data on NVMe. A sequential read with pread takes 4–7 ns for each cell. The full recalculation takes 15–100 ns for each cell, so the read time can be hidden by the calculation.
- If you put the base on NVMe, use pread, not mmap. Read a fixed quantity at a time into a buffer that the engine owns.
  - On macOS, mmap in the cold state is only 1/5 of the speed of pread.
  - The RSS includes the mmap pages, but the memory budget (`max_bytes`) does not see them.
  - A read failure causes SIGBUS, and the engine cannot return it as an error.
- A lookup of one key at a time from the disk takes 20–120 µs for each key. This is 80–270 times slower than from memory. Keep the indexes, the fences and the delta tree in memory. Do a join as a match in key order (merge), not as a lookup of one key at a time.
- A sort that changes the order of the dimensions (rekey) takes 2–6 times longer as an external sort than in memory. Do the sort in memory while the data fits in memory. Write to the disk only when the data is more than the budget.
- Do not use fsync on the files for intermediate results. If the process stops, the engine can calculate the results again. With fsync, the merge was 3 times slower (macOS).

## Plan

1. Decrease the intermediate results. Connect the element-wise operations, read the input one time, and do not materialize the intermediate Metrics. Do not use the disk.
   We implemented this for the full evaluation ([Engine](engine.md)). In the retail model, the heap increase during recalculation changed from 4,275 MB to 1,194 MB (about 1.4 times the new values). See the section "Before and after operation fusion" in [Performance](performance.md).
2. Put the base of each input Metric in immutable segment files.
   - A published version does not change. Thus, a segment file also does not change after the engine writes it, and versions can share it.
   - Keep the delta tree (imbl), the indexes for dimensions other than the partition dimension, and the fences for each segment in memory.
   - To read, use pread and read a fixed quantity at a time.
   - As the first step, we changed the keys and values of the base from `Vec` to immutable columns with no unused capacity (`Box<[u64]>`, `Box<[f64]>`). The packed layout of u64 and f64 is the same as the data buffer of an Arrow array. It contains no pointers, so a file or shared memory can hold the same layout.
   - We also implemented and measured a type that lets an owner decide the location of the data (heap, file mapping, shared memory). This type is `Column`: it uses a raw pointer and an `unsafe` `Deref`, and reads the data as `&[T]` for all owners. We did not keep it (it is in [#26](https://github.com/kazu-2020/nanashi/pull/26) up to commit `ecd5ef5`). Until we add a second owner, there is no reason to keep about 100 lines of `unsafe` code, and our policy is to not use `unsafe`. When we really add a second location, we will design the owner abstraction in a form with no `unsafe` (for example, a type that holds the mapping and lends `&[T]`).
   - For memory accounting, the engine counts the size of the base (`base` in `Model.memory()`) for all locations. `max_bytes` is a budget for intermediate results only, and it does not change with the location of the base. If we add a file mapping or shared memory as a location, the engine will count the part on the Rust heap in a different field.
   - The speed did not change after the change to columns (see "Before and after the change to immutable columns for the base" below).
   - As the next step, we tried a change that puts the journal snapshot in the same flat format as the base. The snapshot also holds the values of the formula Metrics, so the recalculation at open is only incremental. We measured this change and did not keep it (see "Proposal: put formula Metric values in the snapshot" below).
3. Write to NVMe only when the intermediate results are more than `max_bytes`. Use an external sort for the rekey, and do the merge in parallel for each key range (`rekey_external` in the example).

We will decide on items 2 and 3 after item 1. Before the decision, we will measure again where the memory peak stays.

## Measurement method

```bash
cargo run --release -p nanashi-engine --example out_of_core --manifest-path native/Cargo.toml -- [セル数] [置き場所] [pread|mmap|heap ...]
```

The measurement does not use the engine itself. It makes files in the same layout as the base of the Store (two columns: u64 keys in ascending order and f64 values) and measures them.

- A has 67.11 million cells, and B has 80.45 million cells. They have the same 4 dimensions. B has 80% of the keys of A, plus the adjacent keys. Together, the files are 2,252 MB.
- Storage modes:
  - heap is the same as the current engine. It reads the two columns into the heap and holds them there.
  - mmap maps the files read-only. "Warm" means that the pages are in the page cache. "Cold" means that we removed the pages from the cache before the operation.
  - pread reads 1 million cells (16 MB) at a time and does not keep them in the cache. In memory, it holds only the first key of each 16 KB (the fences).
- Operations:
  - scan reads all the cells.
  - agg adds the values into two dimensions (120,000 groups).
  - merge matches A + B in key order. heap writes the result to a Vec. The other modes write the result to a file.
  - lookup finds one key at a time. Cold finds 20,000 keys. Warm and heap find 1 million keys.
  - rekey changes the order of the dimensions and sorts again. heap sorts in memory. The other modes use an external sort.
- We ran each storage mode 3 times in a different process and used the median (the values in parentheses are the minimum to the maximum). The checks (sum, number of cells, number of keys found) agreed in all 4 storage modes in both environments.
- We used the quantity read from the disk to make sure that the cold measurements read the disk (`ri_diskio_bytesread` on macOS, `read_bytes` in `/proc/self/io` on Linux).

The method to keep data out of the cache is different for each OS.

| | macOS | Linux |
|---|---|---|
| pread | Set `F_NOCACHE` on the file | Remove the read range with `posix_fadvise(POSIX_FADV_DONTNEED)` |
| Write | `F_NOCACHE` | Send to the device with `sync_file_range`, then remove the range in the same way |
| mmap cold | `msync(MS_INVALIDATE)` | Unmap the pages with `madvise(MADV_DONTNEED)`, then remove them with `posix_fadvise` |
| Memory used | `proc_pid_rusage` | `/proc/self/status` (`RssAnon`, `VmRSS`) |

We do not use `O_DIRECT` on Linux because it requires 4 KB alignment of the buffer and the offset.

We do not mix the numbers from the two environments. Each environment has a separate table.

## Results: local machine (Apple M4)

- Apple M4 (10 cores, 16 GB memory), internal SSD.
- The disk had only 18 GB of free space (92% used). If the process writes many GB continuously, the writes become slow.
- A single write stream was about 2 GB per second (we checked this with dd and Python).
- At the time of this measurement, the merge of the rekey external sort used 1 thread, and the CPU was the limit. We measured the parallel merge for each key range only in the cloud.

| Operation | Storage mode | Threads | ms | ns/unit | GB/s | MB read | MB written | Max heap MB |
|---|---|---|---|---|---|---|---|---|
| scan | pread | 1 | 427 (426–452) | 6.4 | 2.51 | 1024 | 0 | 16 |
| agg | pread | 1 | 412 (409–422) | 6.1 | 2.61 | 1024 | 0 | 19 |
| merge | pread | 1 | 1832 (1796–4733) | 12.4 | 1.29 | 2253 | 1433 | 96 |
| lookup | pread | 1 | 2366 (2346–2470) | 118301 | - | 471 | 0 | 0 |
| scan | pread | 10 | 375 (372–406) | 5.6 | 2.86 | 1024 | 0 | 160 |
| agg | pread | 10 | 376 (369–404) | 5.6 | 2.86 | 1024 | 0 | 203 |
| merge | pread | 10 | 1466 (1379–4067) | 9.9 | 1.61 | 1204 | 1433 | 699 |
| lookup | pread | 10 | 466 (408–513) | 23297 | - | 471 | 0 | 0 |
| rekey | pread | 10 | 3471 (3118–7621) | 51.7 | 0.31 | 2049 | 2048 | 144 |
| scan | mmap cold | 1 | 1992 (1959–2022) | 29.7 | 0.54 | 1024 | 0 | 0 |
| agg | mmap cold | 1 | 1170 (1167–1195) | 17.4 | 0.92 | 1024 | 0 | 3 |
| merge | mmap cold | 1 | 5745 (5580–8528) | 38.9 | 0.41 | 2253 | 1433 | 40 |
| lookup | mmap cold | 1 | 2243 (2219–2272) | 112138 | - | 493 | 0 | 0 |
| scan | mmap cold | 10 | 546 (532–606) | 8.1 | 1.97 | 1024 | 0 | 0 |
| agg | mmap cold | 10 | 558 (538–619) | 8.3 | 1.92 | 1024 | 0 | 36 |
| merge | mmap cold | 10 | 5751 (3343–6573) | 39.0 | 0.41 | 2252 | 1433 | 328 |
| lookup | mmap cold | 10 | 384 (351–390) | 19213 | - | 492 | 0 | 0 |
| rekey | mmap cold | 10 | 8773 (4374–9692) | 130.7 | 0.12 | 2049 | 2048 | 128 |
| scan | mmap cold SEQUENTIAL | 1 | 2499 (1980–3516) | 37.2 | 0.43 | 1024 | 0 | 0 |
| scan | mmap cold SEQUENTIAL | 10 | 850 (812–2672) | 12.7 | 1.26 | 1024 | 0 | 0 |
| scan | mmap warm | 1 | 41 (41–41) | 0.6 | 26.24 | 0 | 0 | 0 |
| agg | mmap warm | 1 | 36 (35–36) | 0.5 | 29.85 | 0 | 0 | 3 |
| merge | mmap warm | 1 | 1364 (1173–1852) | 9.2 | 1.73 | 0 | 1433 | 40 |
| merge (to Vec) | mmap warm | 1 | 489 (438–496) | 3.3 | 4.83 | 0 | 0 | 2056 |
| lookup | mmap warm | 1 | 435 (413–449) | 435 | - | 0 | 0 | 0 |
| scan | mmap warm | 10 | 13 (13–14) | 0.2 | 80.92 | 0 | 0 | 0 |
| agg | mmap warm | 10 | 15 (14–15) | 0.2 | 70.99 | 0 | 0 | 41 |
| merge | mmap warm | 10 | 1212 (876–2967) | 8.2 | 1.95 | 0 | 1433 | 328 |
| merge (to Vec) | mmap warm | 10 | 145 (138–156) | 1.0 | 16.24 | 0 | 0 | 2056 |
| lookup | mmap warm | 10 | 77 (71–79) | 77 | - | 0 | 0 | 0 |
| rekey | mmap warm | 10 | 3702 (2649–8773) | 55.2 | 0.29 | 1024 | 2048 | 128 |
| load | heap | 1 | 754 (745–789) | 5.1 | 3.13 | 2252 | 0 | 2252 |
| scan | heap | 1 | 220 (175–277) | 3.3 | 4.89 | 0 | 0 | 0 |
| agg | heap | 1 | 33 (32–35) | 0.5 | 32.31 | 0 | 0 | 3 |
| merge (to Vec) | heap | 1 | 984 (898–1023) | 6.7 | 2.40 | 0 | 0 | 2056 |
| lookup | heap | 1 | 441 (413–444) | 441 | - | 0 | 0 | 0 |
| scan | heap | 10 | 14 (12–15) | 0.2 | 76.99 | 0 | 0 | 0 |
| agg | heap | 10 | 16 (15–18) | 0.2 | 69.04 | 0 | 0 | 37 |
| merge (to Vec) | heap | 10 | 251 (190–293) | 1.7 | 9.42 | 0 | 0 | 2056 |
| lookup | heap | 10 | 83 (82–107) | 83 | - | 0 | 0 | 0 |
| rekey | heap | 10 | 590 (432–801) | 8.8 | 1.82 | 0 | 0 | 1024 |

For lookup, ns/unit is for each key. For the other operations, ns/unit is for each input cell.
With 1 thread, scan and merge on heap were slower than on mmap warm. We did not find the cause.

## Results: cloud (Linux VM)

- The cloud environment of Claude Code. Intel Xeon 2.1 GHz with 4 vCPUs, 15 GB memory, Linux 6.18.
- The disk is a virtio block device (ext4). We do not know the hardware behind it.
  - If the data is in the cache outside the VM (on the host), the guest sees fast reads, also in the cold state.
  - Thus, the cold numbers are an upper limit for the speed of NVMe. They are not the speed of NVMe itself.
- In this VM, the first access to newly allocated memory is slow. The first access to 2 GB took 2.1 seconds, and the second access took 0.1 seconds. We measured this with an anonymous mmap in Python and wrote 1 byte for each 4 KB.
  - Almost all of the time for heap load and merge (to Vec) is this cost.
  - For this reason, do not compare the ratios to heap with the M4 table.

| Operation | Storage mode | Threads | ms | ns/unit | GB/s | MB read | MB written | Max heap MB |
|---|---|---|---|---|---|---|---|---|
| scan | pread | 1 | 300 (288–374) | 4.5 | 3.58 | 1024 | 0 | 16 |
| agg | pread | 1 | 293 (288–294) | 4.4 | 3.67 | 1024 | 0 | 19 |
| merge | pread | 1 | 2213 (2047–2627) | 15.0 | 1.07 | 2253 | 1433 | 96 |
| lookup | pread | 1 | 1444 (1405–1537) | 72182 | - | 352 | 0 | 0 |
| scan | pread | 4 | 273 (252–309) | 4.1 | 3.94 | 1072 | 0 | 64 |
| agg | pread | 4 | 249 (183–269) | 3.7 | 4.31 | 1016 | 0 | 84 |
| merge | pread | 4 | 1387 (1049–2157) | 9.4 | 1.70 | 2048 | 1433 | 315 |
| lookup | pread | 4 | 464 (446–792) | 23179 | - | 382 | 0 | 0 |
| rekey | pread | 4 | 4513 (4112–4922) | 67.3 | 0.24 | 2130 | 2048 | 512 |
| scan | mmap cold | 1 | 395 (378–397) | 5.9 | 2.71 | 1024 | 0 | 0 |
| agg | mmap cold | 1 | 352 (307–836) | 5.2 | 3.05 | 1024 | 0 | 3 |
| merge | mmap cold | 1 | 2424 (2164–2430) | 16.4 | 0.97 | 2252 | 1433 | 40 |
| lookup | mmap cold | 1 | 426 (365–438) | 21315 | - | 512 | 0 | 0 |
| scan | mmap cold | 4 | 383 (336–470) | 5.7 | 2.81 | 1024 | 0 | 0 |
| agg | mmap cold | 4 | 368 (351–377) | 5.5 | 2.91 | 1024 | 0 | 17 |
| merge | mmap cold | 4 | 1881 (1846–2147) | 12.7 | 1.25 | 2252 | 1433 | 136 |
| lookup | mmap cold | 4 | 303 (299–393) | 15172 | - | 512 | 0 | 0 |
| rekey | mmap cold | 4 | 4067 (3896–4608) | 60.6 | 0.26 | 2274 | 2048 | 512 |
| scan | mmap cold SEQUENTIAL | 1 | 385 (375–588) | 5.7 | 2.79 | 1024 | 0 | 0 |
| scan | mmap cold SEQUENTIAL | 4 | 435 (353–516) | 6.5 | 2.47 | 1024 | 0 | 0 |
| scan | mmap warm | 1 | 145 (144–151) | 2.2 | 7.38 | 0 | 0 | 0 |
| agg | mmap warm | 1 | 124 (123–128) | 1.9 | 8.63 | 0 | 0 | 3 |
| merge | mmap warm | 1 | 1434 (1282–1555) | 9.7 | 1.65 | 0 | 1433 | 40 |
| merge (to Vec) | mmap warm | 1 | 6261 (6255–6345) | 42.4 | 0.38 | 0 | 0 | 2056 |
| lookup | mmap warm | 1 | 907 (902–925) | 908 | - | 0 | 0 | 0 |
| scan | mmap warm | 4 | 38 (36–40) | 0.6 | 28.49 | 0 | 0 | 0 |
| agg | mmap warm | 4 | 36 (36–42) | 0.5 | 29.74 | 0 | 0 | 17 |
| merge | mmap warm | 4 | 492 (459–522) | 3.3 | 4.79 | 0 | 1433 | 136 |
| merge (to Vec) | mmap warm | 4 | 725 (510–793) | 4.9 | 3.26 | 0 | 0 | 2056 |
| lookup | mmap warm | 4 | 244 (219–260) | 244 | - | 0 | 0 | 0 |
| rekey | mmap warm | 4 | 3322 (3181–3405) | 49.5 | 0.32 | 1184 | 2048 | 512 |
| load | heap | 1 | 4609 (3934–6934) | 31.2 | 0.51 | 2252 | 0 | 2252 |
| scan | heap | 1 | 138 (135–145) | 2.1 | 7.75 | 0 | 0 | 0 |
| agg | heap | 1 | 142 (139–144) | 2.1 | 7.58 | 0 | 0 | 3 |
| merge (to Vec) | heap | 1 | 7112 (6863–7416) | 48.2 | 0.33 | 0 | 0 | 2056 |
| lookup | heap | 1 | 894 (872–948) | 894 | - | 0 | 0 | 0 |
| scan | heap | 4 | 36 (36–39) | 0.5 | 29.52 | 0 | 0 | 0 |
| agg | heap | 4 | 85 (76–86) | 1.3 | 12.64 | 0 | 0 | 18 |
| merge (to Vec) | heap | 4 | 601 (579–641) | 4.1 | 3.93 | 0 | 0 | 2056 |
| lookup | heap | 4 | 228 (214–229) | 228 | - | 0 | 0 | 0 |
| rekey | heap | 4 | 2204 (2182–2752) | 32.8 | 0.49 | 0 | 0 | 1024 |

In addition to the heap, the RSS includes the pages that mmap touched.
After scan on mmap cold, the RSS was 1,035 MB. After merge, it was 2,263 MB. Both values are equal to the size of the files that the operation read (for pread, 28–309 MB).

## Proposal: put formula Metric values in the snapshot

We implemented and measured this proposal: put the journal snapshot in the same flat format as the base (a header, then two columns of u64 keys and f64 values, with no compression). If the model has completed a full calculation, the snapshot also holds the values of the formula Metrics and the counts for incremental aggregation. This was step 2 of [#25](https://github.com/kazu-2020/nanashi/issues/25). The implementation is in [#26](https://github.com/kazu-2020/nanashi/pull/26): commit `a3e15fb` (implementation and tests), `9bac262` and `dc1a5b7` (the S3 measurement in `bench_open.py`), and `164d075` (the measurement table). Commit `e99da61` reverted it.
At open, the bytes read (a Python `bytes` object) are the owner, and the base refers to them. The engine replays the journal after the snapshot as input changes, so the first recalculation is incremental and covers only the changed range.
The engine uses the values only if the same engine with the same calculation version calculated them. If the journal contains a definition change, the engine goes back to a full recalculation from that point.

The table shows the time to open the model and complete the first recalculation (cloud Linux VM, Intel Xeon 2.1 GHz with 4 vCPUs, minimum of 3 runs).
"Inputs only" is the current format (Parquet, with a full recalculation at open). "With values" is the format that we tried.
We measured with a local directory (`FileJournal`) and with the production combination (`PgJournal`). In the production combination, PostgreSQL holds the journal, and S3-compatible object storage holds the files. The object storage is RustFS in Docker on the same VM, connected through loopback.

| Model | Journal entries after the snapshot | Local disk: inputs only | Local disk: with values | S3: inputs only | S3: with values | Snapshot: inputs only | With values |
|---|---|---|---|---|---|---|---|
| P&L plan (large), 4.91 million cells | 0 | 337 ms | 163 ms | 403 ms | 675 ms | 1.4 MB | 79 MB |
| | 7 | 286 ms | 219 ms | 390 ms | 710 ms | | |
| P&L plan (large) × 4, 19.65 million cells | 0 | 1,291 ms | 646 ms | 1,322 ms | 1,966 ms | 5.8 MB | 318 MB |
| | 7 | 1,209 ms | 890 ms | 1,390 ms | 2,092 ms | | |
| Retail model (large), 17.05 million cells | 0 | 909 ms | 528 ms | 924 ms | 1,882 ms | 10.6 MB | 305 MB |
| | 10 | 882 ms | 583 ms | 1,011 ms | 1,860 ms | | |

- From the local disk, the time to open is half of the full recalculation (2/3 to 3/4 with a small number of journal entries). Almost half of the load time is the hash check (sha256). For P&L plan (large), the construction of the base takes 40 ms.
- A direct fetch from the object storage is 1.5 to 2 times slower. The time to fetch the uncompressed values is more than the recalculation time that the format saves. For P&L plan (large), the load took 671 ms: 315 ms for the fetch (one GET for each file, in sequence; 196 ms with 8 in parallel), 201 ms for the hash check, and 40 ms for the construction of the base and the load of the definitions.
- We read the files and did not use a file mapping (mmap) because the hash check reads all the bytes. A mapping does not decrease the quantity of data to read.

We did not keep this proposal for these reasons:

- The production journal backend is `PgJournal`. A restarted writer, a standby on a different node, and a `Replica` all fetch the snapshot from the object storage. In that case, the proposal is slower. It is faster only when the files are on the local disk, and there is no cache yet that gives this condition.
- By default, `Workspace` makes a snapshot after each 1000 journal entries. The stored quantity increases from 1.4–10 MB to 79–318 MB.
- The engine uses the "calculation version" number to decide if it can use the saved values. If a person changes the meaning of evaluation or aggregation and does not increase the number, the model opens silently. It then has the incremental changes on top of values that the old meaning calculated. The current method does a full recalculation, so this accident cannot occur.
- The only gain is "half the time to open when the same node opens the model again" (a range of about 1 second). This gain is not sufficient to accept the 3 problems above.

If a local cache on the same node becomes necessary (files with the sha256 of the manifest as the key), measure again with this measurement as the starting point.
At that time, parallel fetch, hash check during the fetch, and compression of the value columns are also candidates. But we expect that the fetch time will still be more than the recalculation time.

## Before and after the change to immutable columns for the base

We changed the keys and values of the base from `Vec` to immutable columns and compared the speed. We used the cloud Linux VM (Intel Xeon 2.1 GHz with 4 vCPUs). We installed the engine before the change and the engine after the change in different virtual environments and ran them in turns.
We measured the version of `Column` with a raw pointer. `Box<[T]>` also reads the data as `&[u64]` and `&[f64]`, so the machine code for the reads must be the same as with `Vec`. All the measured differences were in the range of the variation between runs.

| Measurement | Before (`Vec`) | Immutable columns |
|---|---|---|
| Full recalculation of P&L plan (large) (`examples.fpa`, 5 runs) | 248 ms (223–283) | 241 ms (218–249) |
| Full recalculation of the retail model (large) (`bench.py --size large`, median of 3 runs, 2 times) | 594, 589 ms | 611, 604 ms |
| Read personnel cost (1.37 million cells) with `rows` (Rust part only, median of 10 runs, 3 times) | 133, 121, 127 ms | 126, 142, 139 ms |
| Convert the same Metric to a Python dict with `value` (median of 5 runs, 3 times) | 1348, 1497, 1433 ms | 1424, 1444, 1356 ms |
| Monthly sum of sales (`summarize`) | 4.95, 4.47 ms | 4.48, 4.36 ms |
| Save the snapshot | 84, 78 ms | 91, 99 ms |

Example (heap in `out_of_core.rs`, A has 8.39 million cells, 2 runs).

| Operation | Threads | Before (`Vec`) ms | Immutable columns ms |
|---|---|---|---|
| scan | 1 | 18, 21 | 26, 18 |
| agg | 1 | 19, 20 | 23, 21 |
| merge (to Vec) | 1 | 322, 296 | 299, 292 |
| lookup | 1 | 775, 657 | 629, 641 |
| scan | 4 | 6, 6 | 7, 5 |
| agg | 4 | 27, 19 | 22, 21 |
| merge (to Vec) | 4 | 73, 83 | 89, 85 |
| lookup | 4 | 144, 167 | 167, 155 |
| rekey | 4 | 196, 188 | 206, 193 |

The memory of the stored data (`base` in `m.memory()`) is the same as the value that the engine counted from the allocated capacity of `Vec` (16 B for each cell). There was no unused capacity before the change. When the engine splits the sorted cells into two columns, it allocates exactly the capacity for the number of cells.

## Findings

- Sequential operations (scan, agg) with pread read 2.5–4 GB per second (4–7 ns for each cell). The full recalculation takes 15–100 ns for each cell. Thus, the calculation can hide the cost to read the input from the disk one time.
- The speed of mmap cold is very different between the environments.
  - On macOS with 1 thread, page faults are the limit. The speed is only 1/5 of pread (0.5 GB per second). With 10 threads, it was 2 GB per second. `MADV_SEQUENTIAL` made it slower.
  - On Linux, read-ahead works, and the speed is almost the same as pread.
  - If the engine must run on both OSes, the speed of pread is easier to predict.
- mmap warm has the same speed as heap and has no cost if there is sufficient memory. But the touched pages go into the RSS, and the memory budget does not see them.
- A lookup of one key at a time (lookup) with pread cold takes 70–120 µs for each key (20–23 µs in parallel). This is 80–270 times slower than in memory (heap) with the same number of threads.
  - For each key, pread reads only the 16 KB of keys that the fence selects and one page of values.
  - mmap cold does not use fences. Its binary search touches many pages. Read-ahead occurs, and during 20,000 lookups it read the full key file (512 MB). Thus, you cannot compare the time for each key with pread.
- If merge reads with pread and writes to a file, it takes 9–15 ns for each cell.
- The rekey external sort took about 2 times as long as heap (cloud), also with a parallel merge for each range. With the 1-thread merge on M4, it took about 6 times as long. The heap of the external sort holds one run (128 MB) plus the ranges that the threads merge in parallel (512 MB with 4 threads).
