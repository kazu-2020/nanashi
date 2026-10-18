# Design note: member numbers and key width

This document is a design study. If we implemented a part, the text says so.
For the current behavior, see [Engine](engine.md), [Model operations](modeling.md), and [Limitations and future work](limitations.md).

## Background

The Rust engine keeps the key of one cell as a 64-bit integer (`native/engine/src/key.rs`).
The key packs the member number of each dimension.
Each dimension uses `ceil(log2(number of members))` bits.
If the bits fit, the engine adds 1 more bit to each dimension for future member additions.
The limit is not "the product of the member counts is 2^64 or less".
The limit is "the sum of the rounded-up bit counts is 64 or less".

A usual FP&A Metric can also reach this limit.

| Dimension | Members | Bits |
|---|---|---|
| Month | 120 | 7 |
| Version | 10 | 4 |
| Entity | 200 | 8 |
| Department | 500 | 9 |
| Account | 2,000 | 11 |
| Product | 20,000 | 15 |
| Customer | 200,000 | 18 |
| Total | | 72 |

An intermediate result of a formula can have more dimensions because of `EXPAND` or a join.
The type check applies the same limit to these intermediate results (`key_too_wide`).

The bit sum includes only the dimensions of one Metric (or of one intermediate result).
It does not include all the dimensions in the model.
No part of the code limits the number of dimensions directly.
Only the bit sum is limited.
Each dimension uses a minimum of 1 bit, thus each new dimension always increases the sum.

Only the Rust memory uses the packed key.
Python and Rust exchange lists of member numbers.
The journal keeps IDs, which do not change.
A snapshot keeps lists of member numbers together with `model.json`.
Thus, a change to the key format has an effect only on the engine and on the snapshot format.

## Three layers of member order

The order of members has 3 layers with different properties.

| Layer | What it is | Where we use it | Current implementation |
|---|---|---|---|
| Number | The identity of a member. The key packs it | Storage and evaluation | Same value as the rank |
| Rank (Natural Order) | The default order that the list itself has | Time-series calculation, default display order | Same value as the number |
| View display order | The sort order of each screen (by name, by attribute, by value in descending order, by manual order) | Display only | Not in the engine |

The View display order is kept outside the engine.
A sort such as "by value in descending order" cannot come from the key order, for all possible numbers.
Thus, the View layer must do this sort in all cases.
A View sort does not touch the member numbers of the engine.

The current engine uses one value for both the number and the rank.
This is possible because only 2 operations change numbers:

- An addition adds a member only at the end. The existing numbers do not change.
- A removal decreases each number after the removed member by 1. The order stays the same, thus the engine does not sort the keys again (`Store::remove_member`).
  If the removed member was the last one, no number changes. Then the engine only removes its cells, and the segments without them stay shared with the other versions.
  A Metric of kind `member:dim` (`values=true`) still rebuilds its base: the engine must also look at each value.

Because one value is both the number and the rank, the key order is also the default display order.
Thus, `rows()` only reads in key order and applies the offset and the limit.
A reference to the previous period (`PREVIOUS`, `Month - 1`) is the number minus 1.
A comparison of two points in time is a comparison of their numbers.

## Name, ID, number, and rank

The current `Dimension` (`sparse_engine/core.py`) keeps 3 values for each member: the name, the ID, and the number.
This design divides the number into 2 values, thus it keeps 4 values.

| | Use | Changes? | Current storage |
|---|---|---|---|
| Name | For people to read, and to write in formulas (`Month."Mar"`) | `rename_member` changes it | `members[position]` |
| ID | Journal (the journal and the table of cell changes). A map in the model connects it to the UUID that goes out of the engine ([ids.md](../../docs/ids.md)) | Does not change. A removed ID is not used again | `ids[position]` |
| Number | The key packs it | Does not change after it is set (this design) | `_index[name]` (position) |
| Rank | Order (Natural Order) | Changes when you reorder members or insert a member in the middle | Same as the number |

The ID and the number are different values.
For example, Month has Jan, Feb, and Mar (IDs 11, 12, 13).
If you remove Feb, the number of Mar changes from 2 to 1, but its ID stays 13.

The key does not pack the ID directly for this reason.
The ID is one sequence for the full model, shared by dimensions, members, and Metrics, and it only increases.
A Version dimension can have only 3 members, but if their IDs are about 1,000,000, the dimension uses 20 bits.
With numbers, it uses only 2 bits.
The ID guarantees that a value does not change.
The number guarantees that the values have no gaps.
The roles are different, thus we keep both.

### Divide the number and the rank

The number identifies a member, and it does not change after it is set.
The rank is the position of the member in the list.
It changes when you reorder members or insert a member in the middle.

This example uses the account list "売上, 原価, 販管費" (Sales, Cost, SG&A).
It inserts 粗利 (Gross profit) between 原価 and 販管費.

| Member | Number | Rank |
|---|---|---|
| 売上 | 0 | 0 |
| 原価 | 1 | 1 |
| 粗利 | 3 (new number) | 2 |
| 販管費 | 2 (no change) | 3 (moves 1 position back) |

If the number and the rank are different values, only the rank table changes (one entry for each member).
No cell of a Metric changes, and no recalculation occurs.
In the current format, one value is both the number and the rank, thus the number of 販管費 changes.
Then the engine must change the keys and sort them again in all Metrics that have the account dimension.
If a reader keeps an old version, the versions cannot share the base, and the memory temporarily doubles.

## Two types of master (list)

We divide dimensions into 2 types.
The type depends on whether the rank has an effect on the calculation.

| | Time-series master (the system supplies it) | User master (accounts, departments, and so on) |
|---|---|---|
| Meaning of the rank | The calculation itself (previous period, comparison, cumulative sum) | Default display order only |
| Reorder | Not possible | Possible |
| Insert in the middle | Not possible (only add future periods at the end) | Possible |
| Number and rank | One value (as now) | Different values |
| Removal | Only from the start (see below) | Put a tombstone, then compact all at a later time |

For a time-series master, a rank table only adds work to each lookup of the previous period.
Thus, a time-series master keeps one value for both the number and the rank.

We decided that a dimension with `ordered=True` is a time-series master.
The code has no separate type for "a master that the system supplies".
Thus, the actual rule is this: a dimension with `ordered=True` rejects a reorder and an insertion in the middle.

## Removal and tombstones

### Cells already use a removal mark

The Store puts a delta in a persistent tree (`OrdMap<u64, Option<f64>>`) on top of a base that does not change.
If the delta becomes larger than 1/8 of the base, the Store merges them again.
A `None` in the delta is a removal mark.
Thus, a cell removal already puts a mark and merges at a later time.
Step 1 of a member removal also uses this path: it makes the inputs of that member blank and does an incremental recalculation.
Nothing more is necessary at the cell level.

### Put a mark on the number

Step 2 of a removal is the slow part.
`Store::remove_member` reads the base of all Metrics that have the dimension, compacts the numbers, and makes the base again.
It does this for each removed member that is not the last one (the last number does not move, so the engine only removes its cells).

We can only mark a number as removed and compact at a later time.
Then, if you remove 1000 members, the engine makes the base again only 1 time.
The engine compacts the numbers at one of these times:

- When the removed numbers are more than 1/8 of the number range
- When an addition causes an overflow of the bit width
- When the engine takes a snapshot

### Use a free number again only with a rank table

Assume that one value is both the number and the rank, and that a new member gets a free number.
Then the new member is in the middle of the key order, and the default display order becomes incorrect.
Only these 2 options are consistent:

| | (a) Put a mark, do not use numbers again | (b) Use numbers again, keep a rank table |
|---|---|---|
| Removal | Only put a mark. Compact all at a later time | Only put a mark. The next addition fills the free number |
| Default display order | Key order, as now | The rank table sets it |
| Reorder and insertion in the middle | Not possible | Possible |
| Size of the change | Small | Large |

In (b), a number is used again only after all its cells are removed.
Also, all values of member type that refer to that number must be removed first.

We use numbers again for this reason: the numbers stay without gaps, and the number of live members sets the bit width of the dimension.
If we do not use numbers again, the numbers only increase.
Then, in a dimension with many additions and removals, the bit width continues to increase.

| Example: a customer dimension with 10,000 additions and 10,000 removals each month | Use numbers again | Do not use numbers again |
|---|---|---|
| Live members | Always 100,000 | Always 100,000 |
| Largest number after 3 years | About 100,000 | About 460,000 |
| Bit width of this dimension | 17 bits | 19 bits (continues to increase) |

If we do not use numbers again, a compaction becomes necessary at some time.
The compaction is the same slow work that the marks must prevent.

A slot map usually adds a generation number to each number that it uses again.
This prevents an old reference from pointing to a new member.
Here, before the engine uses a number again, it removes all cells and values that refer to that number.
Thus, a generation number is not necessary.

### Removal in a time-series master

The previous-period reference, comparisons, and scan use ±1 and the order of numbers.
Thus, they do not operate correctly if there is a gap in the middle.
A removal from the start (for example, keep only the last 36 months) makes a gap only at the start.
Then ±1 between the remaining periods stays correct.
The previous period of the first period becomes blank, and this is also correct in meaning.
Thus, a time-series master can use marks if it prevents a removal in the middle and permits only a removal from the start.

### What to divide when we put marks

`DimInfo::size` has 2 meanings.
If the numbers have gaps, we must divide these meanings:

- Number range: the bit width of `Packing::new`, the size of `Sel`, the inverted index, the 2^22 check
- Number of live members: the estimate of the cell count, the range in which `EXPAND` and `IFBLANK` make values, the range check of `AsAxis`, `len(members)` in Python

The snapshot file `inputs.*.parquet` keeps numbers together with the order in `model.json`.
Thus, the snapshot must keep the relation between numbers with gaps and IDs, and the save format version must increase.
The journal keeps IDs, thus it does not change.

## Make the key wider

After we decide how to use the number and the rank, we make the key wider.
We examined these options:

| Option | Width increase | Cost | Result |
|---|---|---|---|
| Pack with mixed radix (key = Σ mᵢ × Π sizeⱼ) | It only removes the waste from rounding up. The example above still needs about 70 bits and does not fit | A division is necessary to get a dimension. Additions cause more compactions | Does not solve the problem |
| A variable-length key for each cell (`Vec<u32>`) | No limit | Sort, join, and aggregation are 5 to 10 times slower. Memory is 2.5 to 3.5 times larger | Rejected |
| For each Metric, set new numbers only for the members that it uses | Increases with sparsity | Each key copy between Metrics needs a conversion | Too complex |
| Make the key type generic, and also supply a fixed-length array of N 64-bit words | 64 × N bits | Only above 64 bits, each word adds 8 bytes to each cell | Accepted |

The accepted option has these parts:

- Implement a `Key` trait for `u64` and for `[u64; N]`.
  The trait has get, set, clear, and lexicographic order from the most significant part.
  `[u64; N]` is a fixed-length array in Rust.
  N is a type parameter (const generics), thus the compiler sets it, and the array does not use the heap.
  On x86_64, the Rust `u128` has 16-byte alignment, thus `(u128, f64)` uses 32 bytes.
  `[u64; 2]` uses only 24 bytes.
- Make generic over `K` all the parts that keep keys.
  These parts are `Packing`, `Proj`, `Cube`, `cells.rs`, the base, the delta, and the inverted index in `store.rs`, `restrict.rs`, and the join and aggregation in `eval/`.
  The partition dimension is in the most significant position, and the engine uses a binary search on it.
  This still operates correctly with lexicographic comparison.
- Use one width for the full model.
  When a Metric or an intermediate result does not fit the current width for the first time, the engine packs all stored data again with the wider width.
  A small model stays at 64 bits, and its performance does not change.
- Change these items to agree with the width: the memory estimate in `budget.rs`, the type check limit (`key_fits`), and `RustEngine.key_bits` in Python.
  `key_fits` does not count the sum of the bits of each dimension.
  It counts the number of words that the packing below needs.
- Add `RustEngine(key_bits=128)` to force the width, so that a small model can also use the wide path.
  Add tests that match the incremental recalculation with the reference implementation and with a full recalculation.

### Number of words N

N is not the number of dimensions.
N is the number of 64-bit words.
One word can hold any number of dimensions.
For example, 6 dimensions use 54 bits (1 word), 7 dimensions use 72 bits (2 words), and 13 dimensions use 129 bits (3 words).

The compiler sets N, thus we must select in advance the values of N for which the compiler makes code.
At first, we supply N = 1 (`u64`), 2, and 4.
For a dimension set that is wider, the type check rejects it, as it does now for 64 bits.
More words only increase the quantity of generated code.
If we want to remove the limit, we can use a stride (the number of words is set at run time) only for keys wider than 256 bits.
An FP&A Metric almost never uses more than 256 bits (about 25 dimensions of 1,000 members).
Thus, we will add the stride only when it is necessary.

### How to pack into words

Pack the number of each dimension bit by bit, from the first word.

1. Examine the dimensions in sequence from the first. If a dimension fits in the current word, put it there. If not, go to the next word. One dimension does not cross 2 words.
2. In a word, an earlier dimension goes in more significant bits.
3. The first word (`key[0]`) is the most significant part of the full key.

The 7 dimensions (72 bits) from the background go into 2 words as follows:

```text
key[0]: [未使用 10][Month 7][Version 4][Entity 8][Department 9][Account 11][Product 15]
key[1]: [未使用 46][Customer 18]
```

- To get the value of a dimension, shift and mask the word that holds it (`(key[word[i]] >> shift[i]) & mask[i]`). The only difference from the current `Packing::get` is the word position.
- The comparison uses the lexicographic order of the array. An earlier dimension is more significant, thus the order is the same as a sort by the dimensions in sequence. If the partition dimension is first, the most significant bits of `key[0]` can limit the range.
- The key type changes from `u64` to `[u64; 2]` in the stored data, the delta tree and the inverted index. `PackedKeys` encodes `u64` offsets, so it needs a new encoding for the wider key.

If dimensions do not cross words, some bits at the word boundary are not used (in the example above, 56 bits of the 2 words).
We can remove this waste if we treat 128 bits as one integer and pack without gaps.
But then the engine must get a dimension at the boundary from 2 words and combine the parts.
The waste at the boundary is usually only a few bits to a little more than 10 bits.
Thus, we use the format in which dimensions do not cross words, because it is simpler to get values.

### Practical guide

| Members in each dimension | Bits for 1 dimension | Dimensions in 64 bits | Dimensions in 128 bits |
|---|---|---|---|
| Up to 8 (Version, Scenario, and so on) | 3 | 21 | 42 |
| Up to 1,000 (departments, accounts) | 10 | 6 | 12 |
| Up to 1,000,000 (customers, SKUs) | 20 | 3 | 6 |

An FP&A Metric usually has 5 to 8 dimensions and uses about 80 to 100 bits, thus 128 bits are almost always sufficient.
A model with 13 dimensions (including Customer 200,000, Employee 50,000, and Product 20,000) uses 129 bits.
A key comes near 128 bits in these 3 conditions:

- Intermediate results of a formula: `EXPAND`, a join, or a `BY` with a Metric of member type gives an intermediate result more dimensions than the final result. Thus, an intermediate result reaches the limit before the Metric itself.
- A dimension that the original dimension sets is also kept: for example, Employee × Department. Department comes from Employee, thus the cell count does not increase, but the bits are used.
- Option (a), numbers are not used again: a removal does not decrease the number range. Thus, in a dimension with many additions and removals, the bit width increases until a compaction.

The limit of members in one dimension (u32, about 4.3 billion) does not change.
This limit applies to the number of members that exist at the same time.
It does not apply to the largest ID that was issued.

## Plan

We decided that a user master must permit a reorder of the list itself and an insertion in the middle.
Thus, we skip (a) and do these steps in this sequence:

1. Keep the View display order outside the engine. Do not change the engine.
2. In a user master, divide the number and the rank ((b)). Do this in 2 phases.
   - Phase 1 (implemented): Add only the rank table. [modeling.md](modeling.md) gives how to use it.
   - Phase 2 (when necessary): A removal puts a mark on the number, the compaction occurs at a later time, and free numbers are used again. This change decreases the cost of a removal. It needs the items in "What to divide when we put marks" above.
3. After the key format is decided, make the key wider (N = 1, 2, 4).

A `BY` that uses an attribute as a dimension replaces the original dimension.
Thus, the key width of the result usually does not increase.
If the original dimension and the attribute dimension are both kept, the attribute dimension uses all its bits.
This is also true when the original dimension sets the attribute dimension.
