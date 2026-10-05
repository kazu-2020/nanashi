# Recalculation

When you register a formula, the engine makes a **calculation plan** from the dependency graph of the Metrics.
A self-reference with `PREVIOUS` is a cycle in the dependency graph. If the cycle always goes through the previous time period, the engine uses it as a **scan**. A scan calculates one time period at a time along the time dimension.
All other cycles cause an error.

When you change an input, the engine calculates again only the affected range, in this sequence:

1. The engine records the changed cells as the **affected range**. The affected range is the direct product of the member sets of each dimension.
2. In the order of the calculation plan, the engine uses the formula of each Metric to find the range where values can change. For an aggregation, it moves the range to the aggregation target. For a reference to the previous month, it moves the range one month later.
3. The engine evaluates only that range and compares the old and new values at write-back. Only the cells with changed values become the affected range for the downstream Metrics.
4. If no value changes, the engine does not calculate the downstream Metrics again.

The engine updates SUM and COUNT aggregations with **incremental aggregation**.
For each changed row of the aggregation source, the engine subtracts the old contribution and adds the new contribution. Thus, it does not read the full aggregation source again.
This also applies to BY with a hierarchy that changes over time. For each employee with a changed department, the engine subtracts the contribution in the old department and adds the contribution in the new department.
The engine also keeps the count of each group. This tells if a SUM is 0 or blank.

If the changed range of a dimension includes all members, the engine propagates that dimension as "full" to the downstream Metrics.
This is because the write-back of the full Metric only needs a sort, and a recalculation is faster than an incremental aggregation.
In the Rust engine, the engine also propagates the range as full for a large Metric (4096 rows or more) if the range is half of the cells or more.

## Definition changes

When you change a formula or an input definition, the engine also calculates again only the changed Metric and its affected range.
If you give an existing name to `add_formula` or `add_input`, the new Metric replaces the old Metric.

- **Add a formula**: The engine calculates only that Metric. There are no downstream Metrics, because no formula refers to it yet.
- **Replace a formula**: The engine calculates the full Metric again. It propagates only the cells with changed values to the downstream Metrics.
  This is the same method as for an input change. Thus, if you use `IF` to change the formula for one product only, the downstream Metrics also calculate again only for that product.
- **Replace an input**: The engine propagates only the cells with different values before and after the replacement, as an input change. It also uses incremental aggregation.
- **Replace a property**: The engine handles each formula that uses the property (`[BY: dimension.property]`) as a replaced formula.
- **Rename a Metric** (`rename_metric`): The formulas hold the Metric id, so only the name changes and the display of each formula shows the new name. The values do not change, so the engine does not calculate again.
- **Remove a Metric** (`remove_metric`): You can remove only a Metric that no formula refers to. No other values change, so the engine does not calculate again.
- **Rename a member, a dimension or a property** (`rename_member`, `rename_dimension`, `rename_property`): The formulas, the property maps and the stored data hold the id, so only the name changes. The engine does not calculate again.

```python
m.rename_metric("Margin", "Profit")  # Formulas that refer to Margin now show Profit
m.remove_metric("Profit")            # ValueError if a formula refers to it
```

For the calculation plan, the engine checks the formulas of the changed Metrics again and makes the dependency order again.
The engine selects the partition dimension only for new Metrics. Existing Metrics continue to use their current stored data.

If you change the dimensions or the value kind of a Metric, the engine starts again from the type check of the formulas that refer to it. Thus, it does a full recalculation.
