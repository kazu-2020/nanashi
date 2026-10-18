"""The SQL subset of POST /sql (docs/sql.md).

nanashi_core.parse_sql parses the text to names. plan resolves the names against a Model (Python owns the names
and the ids) and checks them. The engine runs the Plan (Core.select on the Rust path). reference_select gives the
result rows in pure Python: it is the standard for the Rust path.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any

from .engine import native
from .messages import render


@dataclass(frozen=True)
class Plan:
    """A resolved SQL query. It holds ids only, so it is hashable and is the SummaryCache key.

    A column or a sort key is ("dim", index into group) or ("agg", index into aggs)."""
    group: tuple[str, ...]                               # dimension ids, GROUP BY order
    aggs: tuple[tuple[str, str], ...]                    # (Metric id, agg), unique
    region: tuple[tuple[str, tuple[str, ...]], ...]      # (dimension id, sorted member ids), sorted by id
    columns: tuple[tuple[str, str, int], ...]            # (output name, kind, index), SELECT order
    order: tuple[tuple[str, int, bool, bool], ...]       # (kind, index, desc, nulls_first)
    limit: int | None


class SqlError(ValueError):
    """An SQL error. code is a key of messages.MESSAGES, and params fill the text."""

    def __init__(self, code: str, **params):
        super().__init__(render(code, params))
        self.code, self.params = code, params


def sql_error(e: Exception) -> SqlError:
    """A nanashi_core.Diagnostic as a SqlError."""
    code, params = e.args
    return SqlError(code, **params)


def plan(m, text: str) -> Plan:
    """Parse text and resolve its names against the Model m. Raises SqlError."""
    core = native()
    try:
        select, filters, group_by, order_by, limit = core.parse_sql(text)
    except core.Diagnostic as e:
        raise sql_error(e) from None
    objects = {d.name: ("dim", d.id) for d in m.dimensions.values()}
    objects |= {x.name: ("metric", x.id) for x in m.metrics.values()}
    objects = {k: v for k, v in objects.items() if not k.startswith("__")}

    def find(name: str, kind: str) -> str:
        if name not in objects:
            raise SqlError("sql_unknown", name=name)
        found, id = objects[name]
        if found != kind:
            raise SqlError("sql_not_dim" if kind == "dim" else "sql_not_metric", name=name)
        return id

    items = [(kind, find(name, "dim" if kind == "dim" else "metric"), alias) for kind, name, alias in select]

    def position(p: int) -> tuple[str, str, Any]:
        if not 1 <= p <= len(items):
            raise SqlError("sql_position", pos=p, n=len(items))
        return items[p - 1]

    group: list[str] = []
    for r in group_by:
        if isinstance(r, int):
            kind, id, _ = position(r)
            if kind != "dim":
                raise SqlError("sql_group_position", pos=r)
        else:
            id = find(r, "dim")
        if id not in group:
            group.append(id)

    aggs: list[tuple[str, str]] = []
    columns = []
    for kind, id, alias in items:
        if kind == "dim":
            if id not in group:
                raise SqlError("sql_not_grouped", name=m.dimensions[id].name)
            columns.append((alias or m.dimensions[id].name, "dim", group.index(id)))
            continue
        metric = m.metrics[id]
        if kind != "count" and metric.kind != "number":
            raise SqlError("sql_kind", metric=metric.name, kind=metric.kind, agg=kind.upper())
        if (id, kind) not in aggs:
            aggs.append((id, kind))
        columns.append((alias or f"{kind}({metric.name})", "agg", aggs.index((id, kind))))
    if not aggs:
        raise SqlError("sql_no_aggregate")

    region: dict[str, set[str]] = {}
    for dname, members in filters:
        d = m.dimensions[find(dname, "dim")]
        ids = set()
        for x in members:
            if x not in d:
                raise SqlError("sql_member", dim=d.name, member=x)
            ids.add(d.id_of(x))
        region[d.id] = region[d.id] & ids if d.id in region else ids
    for id, _ in aggs:
        metric = m.metrics[id]
        for d in [*group, *region]:
            if d not in metric.dims:
                raise SqlError("sql_missing_dim", metric=metric.name, dim=m.dimensions[d].name)

    order = []
    names: dict[str, int | None] = {}  # output name -> column, None if 2 columns have the name
    for i, (name, _, _) in enumerate(columns):
        names[name] = None if name in names else i
    for r, desc, nulls_first in order_by:
        if isinstance(r, int):
            position(r)
            _, kind, i = columns[r - 1]
        elif r in names:
            if names[r] is None:
                raise SqlError("sql_ambiguous", name=r)
            _, kind, i = columns[names[r]]
        elif r in objects and objects[r][1] in group:
            kind, i = "dim", group.index(objects[r][1])
        else:
            raise SqlError("sql_order", name=r)
        order.append((kind, i, desc, nulls_first))
    return Plan(tuple(group), tuple(aggs), tuple(sorted((d, tuple(sorted(ms))) for d, ms in region.items())),
                tuple(columns), tuple(order), limit)


def _sort_number(x: float) -> tuple[int, float]:
    """NaN is above every number (DuckDB)."""
    return (1, 0.0) if math.isnan(x) else (0, x)


def reference_select(m, p: Plan) -> list[list]:
    """The rows of p from Model.summarize, in pure Python. A dimension value is a member name, an aggregate
    value a float or None (an absent COUNT is 0). Without ORDER BY, the rows are in member-number order."""
    coords = {d: list(ms) for d, ms in p.region}
    dims = [m.dimensions[d] for d in p.group]
    values = []
    for id, agg in p.aggs:
        s = m.summarize(id, coords, keep=p.group, agg=agg)
        at = [s.dims.index(d) for d in p.group]
        values.append({tuple(s.coords[i][r] for i in at): s.values[r] for r in range(len(s))})
    if dims:
        groups = sorted({k for v in values for k in v}, key=lambda k: [d._by_id[x] for d, x in zip(dims, k)])
    else:
        groups = [()]
    cells = [[v.get(g, 0.0 if agg == "count" else None) for v, (_, agg) in zip(values, p.aggs)] for g in groups]
    rows = list(range(len(groups)))
    for kind, i, desc, nulls_first in reversed(p.order):
        if kind == "dim":
            ranks = dims[i].rank_table()
            number = [dims[i]._by_id[g[i]] for g in groups]
            rows.sort(key=lambda r: number[r] if ranks is None else ranks[number[r]], reverse=desc)
        else:
            rows.sort(key=lambda r: (0, 0.0) if cells[r][i] is None else _sort_number(cells[r][i]), reverse=desc)
            rows.sort(key=lambda r: (cells[r][i] is None) != nulls_first)
    if p.limit is not None:
        rows = rows[:p.limit]
    return [[dims[i].member_of(groups[r][i]) if kind == "dim" else cells[r][i] for _, kind, i in p.columns]
            for r in rows]


def json_number(x):
    """A float in proto3 JSON: NaN and the infinities are strings. Other values stay the same."""
    if not isinstance(x, float) or math.isfinite(x):
        return x
    return "NaN" if math.isnan(x) else "Infinity" if x > 0 else "-Infinity"


def reference_json(m, p: Plan, seq: int, max_cells: int) -> bytes:
    """The JSON body of POST /sql from reference_select. NaN and the infinities are strings (proto3 JSON)."""
    rows = reference_select(m, p)
    if (cells := len(rows) * len(p.columns)) > max_cells:
        raise SqlError("sql_too_large", rows=len(rows), cells=cells, max=max_cells)
    body = {"seq": seq, "columns": [c[0] for c in p.columns], "rows": [[json_number(x) for x in r] for r in rows]}
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
