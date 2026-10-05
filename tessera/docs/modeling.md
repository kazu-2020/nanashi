# Model operations

The examples use `Named` (`m = Named(Model())`), which takes names. `Model` takes the same operations with ids:
a Metric, dimension, member or property argument is an id, and coordinates are a mapping `{dimension id: member id}`
(for example `model.get(revenue, {product: p0001, month: m01})`). The reads of `Model` give ids. See "Minimal
example" in the [README](../README.md).

## Read values

There are four interfaces that read only the necessary cells, and `value`, which reads all cells.
For a large Metric, `value` converts the full Metric to a Python dict. Thus, for display or an API, use the interfaces that read only the necessary cells.

```python
m.get("Revenue", Product="p0001", Version="予算", Month="m01")   # One cell. None if blank
m.slice("Revenue", Product="p0001")                              # A range as a Cube (each dimension takes a member name or a set of names)
m.rows("Payroll", Department="営業", offset=0, limit=50)          # A list of rows and the total row count (in the order of the declared dimensions)
m.summarize("Revenue", keep=["Month"], Product=["p0001", "p0002"])  # Aggregate and keep only the keep dimensions (SUM, AVG, MIN, MAX, COUNT)
```

In the Rust engine, these interfaces do not read all of the stored data. All of them except the one-cell `get` also release the GIL when they read.
In the profit-and-loss plan with 4.9 million cells, one Metric has 1.37 million cells. A `get` of one cell of this Metric takes less than 0.01 ms. A `value` of the full Metric takes about 500 ms (see the table in [Performance](performance.md)).

## Dimensions and members

You can add a member to a dimension at run time with `add_member`.
A new member starts blank in all Metrics. Only operations that expand values to all members make values for the new member. Examples are an addition with a constant, `IFBLANK`, the BY mapping, and a reference to the previous month.
An addition of a member is like an input change: the engine calculates again only the affected range.

```python
m.add_member("Employee", "dave")                         # Starts blank in all Metrics
m.set_cell("DeptOf", "開発", Employee="dave", Month="Mar")
m.add_member("Month", "Apr")                             # On an ordered dimension, the member goes after the last time period
```

If the dimension has properties, you can set their values when you add the member, for example `m.add_member("Product", "p9", Category="ハード")`.
To change the values of some members later, use `set_property_values`. A `None` value removes the value. The values of the other members do not change.

```python
m.set_property_values("Product", "Category", {"p9": "ソフト", "p1": None})
```

On an unordered dimension (for example, accounts or departments), you can insert a member at a position (`at`). You can change the order with `move_member`.
The position is the index in the member order (starts at 0). `rows()` and the HTTP list (`GET /`) return members in this order.

```python
m.add_member("Account", "粗利", at=2)    # Insert at position 2 of the order
m.move_member("Account", "販管費", 0)    # Move to the start
m.dimensions["Account"].in_order()       # The member names in order
```

In the engine, each member has a number. A new member always gets the last number.
The engine keeps the order separately from the numbers. Thus, an insert or a reorder does not change the numbers or the cells of other members. A reorder does not cause a recalculation.
On an ordered dimension (a time master list), the order sets the meaning of references to the previous period and of less-than or greater-than comparisons. Thus, you can add a member only after the last time period, and you cannot reorder the members.

You can rename a member with `rename_member` and remove it with `remove_member`.

```python
m.rename_member("Employee", "dave", "David")  # Values, properties, and Employee."dave" in formulas show the new name
m.remove_member("Month", "Feb")               # The previous month of Mar becomes Jan
```

Dimensions, members, and Metrics each have a **permanent ID** that is unique in the model.
A rename does not change the ID, and the model does not use the ID of a removed item again.
In the engine, members have numbers, and the numbers become compact when you remove a member. Thus, use IDs for the change journal and for data exchange with external systems.

```python
pid = m.dimensions["Product"].id_of("p9")      # The ID of a member
m.dimensions["Product"].member_of(pid)         # The current name from the ID
m.metric("Revenue").id, m.metric(mid).name     # The id (UUID) of a Metric, and the current name from the id
```

A copy (`fork`) continues to give IDs from the same counter. Thus, an item that you add in the copy and an item that you add in the original can get the same ID.

In the engine, the cells, the property maps and the formulas hold the member id. Thus, a rename changes only the name and does not cause a recalculation.

When you remove a member, the engine removes the cells of that member from all Metrics.
The engine also removes the member from the property mapping tables. Members that referred to the removed member then have no reference.
In a Metric of a member value kind, values that point to the removed member become blank (for example, a close month of Feb).
If a formula contains the member, for example `Month."Feb"`, you must first correct the formula. Then you can remove the member.

The engine does the recalculation for a removal in two steps.
First, the engine makes blank the input cells of the member and the values that point to the member. It then does a recalculation as for a usual input change.
Thus, the incremental aggregation and the filter by changed values apply without changes.
Then the engine removes the member.
After the member is blank, only these two items still change when the engine removes it:

- The cells that an operation which expands values to all members (for example, `X + 1`) made for the member, and the aggregated values of these cells
- References to the previous month across the member

The engine calculates again only the range that these two items affect.

## Plan inputs

If you register a formula Metric with `add_formula(..., overridable=True)`, you can override the formula result manually with `set_cell`.
An override has priority over the formula. The downstream aggregations also use the override value.
If you set a cell to `None` with `set_cell`, the cell returns to the formula result.

```python
m.add_formula("Bonus", ["Employee"], "Salary * 0.1", overridable=True)
m.set_cell("Bonus", 8, Employee="alice")   # Manual input for alice only
```

`spread` sends a total value to a range of an input Metric.
You specify the range with members of dimensions and with filters of the form "dimension.property".
By default, `spread` uses the ratios of the current values. If there are no values, it spreads evenly. With `how="even"`, it always spreads evenly.
The engine writes the spread cells together, not one cell at a time. Thus, a spread to 225,000 cells takes some tens of ms.

```python
m.spread("Budget", 12_000, Version="予算", Month="m01", where={"Product.Category": "ハード"})
```

## What-if analysis

`fork` makes a copy of the model.
Inputs, overrides, and member additions in the copy do not change the original model. Changes to the original do not change the copy.
Thus, you can try a question, for example "what is the profit if we increase the prices?", and keep the original plan. You can compare the results and then discard the copy.

```python
what_if = m.fork()
what_if.set_cell("Salary", 70, Employee="alice")
print(what_if.value("Cash").cells, m.value("Cash").cells)
```

In the Rust engine, the copies share the base of the stored data (the array in key order). Changes go into a persistent tree (the delta), which does not break old versions.
The time for a copy is proportional to the number of Metrics (less than 1 ms for a model with 4.9 million cells). A change in the copy does not copy the base.
