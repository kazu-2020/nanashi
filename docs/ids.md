# Identifiers

This note gives the identifier rules for `web/`, `api/` and `tessera/`. It follows issue #6 (ID design, version 4).

## Rules

- Each identifier that goes out of the system is a UUIDv7. Examples: an application, a Metric, a dimension (list), a member, a property, an item, an access rule, a comment, a snapshot and a `client_op_id`.
- Usually `web/` makes the identifier. If `api/` makes an object for the user, `api/` makes the identifier one time, in the plan of the RPC.
- A name is an attribute. It is not an identifier.
- `api/` parses each incoming identifier and changes it to the canonical form (lowercase, with hyphens). If the identifier is not a UUID, `api/` returns InvalidArgument. `tessera/` does not parse identifiers. It compares them as strings.
- The internal numbers of the engine do not go out of the engine. These are the handles (`Model._new_id()`), the member numbers and the Rust dense numbers.
- The engine does not use an identifier again after a delete. It keeps the deleted identifiers as tombstones.

## The engine HTTP contract

Each reference in a path, a query or an op is a UUID.

### Reads

`GET /` gives the model. The keys are the UUIDs, in the order of definition. Names starting with `__` stay hidden.

```json
{
  "seq": 12,
  "dimensions": {
    "<dim uuid>": {
      "name": "Product",
      "ordered": false,
      "members": [{"id": "<member uuid>", "name": "A"}],
      "properties": {"<prop uuid>": {"name": "Region", "target": "<dim uuid>"}},
      "property_values": {"<prop uuid>": {"<member uuid>": "<member uuid>"}}
    }
  },
  "metrics": {
    "<metric uuid>": {
      "name": "Revenue",
      "dims": ["<dim uuid>"],
      "kind": "number",
      "overridable": false,
      "formula": "Price * Units"
    }
  }
}
```

- `kind` is `number`, `boolean` or `member:<dim uuid>`.
- `formula` is the display text. The engine makes it from the current names.
- `GET /metrics/<metric uuid>/{cell,slice,rows,summary,overrides}` reads one Metric. A coordinate in the query is `<dim uuid>=<member uuid>[,<member uuid>...]`.
- In a read result, a dimension is a dimension UUID and a member is a member UUID. A value of a member-type Metric is a member UUID.
- `GET /operations/<client_op_id>` gives the result of an operation in the `op_window`. The result is `{"state": "committed", "seq": N}` or `{"state": "rejected", "status": 400, "body": {...}}`. If the engine does not know the operation, the status is 404 with `{"error": "unknown_operation"}`.

### Writes

`POST /writes` takes `{"client_op_id", "reason", "expect"?, "ops": [...]}`. Each op refers to objects by UUID.
An op is a flat object: `{"op": "<name>", <argument>: <value>, ...}`, for example `{"op": "add_member", "dim": "<dim uuid>", "id": "<member uuid>", "name": "A"}`.

| Op | Arguments |
|---|---|
| `add_dimension` | `id`, `name`, `ordered`? |
| `add_member` | `dim`, `id`, `name`, `at`? |
| `rename_member` | `dim`, `id`, `name` |
| `move_member` | `dim`, `id`, `at` |
| `remove_member` | `dim`, `id` |
| `add_property` | `dim`, `id`, `name`, `target` (dim uuid) |
| `set_property_values` | `dim`, `prop`, `values` (`{member uuid: member uuid or null}`) |
| `add_input` | `id`, `name`, `dims`, `kind`?, `cells`?, and the other current options |
| `add_formula` | `id`, `name`, `formula` (text with names), `dims`?, `overridable`? |
| `rename_metric` | `id`, `name` |
| `remove_metric` | `id` |
| `set_cell` | `metric` (uuid), `value`, `override`? (true to write the hidden override input), `coords` (`{dim uuid: member uuid}`). A value of a member-type Metric is a member uuid |
| `spread` | `metric` (uuid), `total`, `how`?, `where`? (`{"<dim uuid>.<prop uuid>": member uuid}`), `coords` |

- If an op defines an object with a UUID that the model has, and the object has the same kind, the op defines the object again (it can also change the name).
  A dimension or a property cannot change its name in this way: the engine returns 400, because the formulas keep the names of dimensions and properties, and the engine has no rename operation for them. The Metric and member renames go through the formulas, as before.
- If the UUID belongs to an object of a different kind, or it is a tombstone, the engine returns 409 `{"error": "duplicate_id"}`.
- If the name belongs to a different UUID, the engine returns 400 `{"error": "bad_request"}`.
- The engine records each rejected operation (400, and 409 `duplicate_id`) with its `client_op_id` for the `op_window`. If a client sends the same `client_op_id` again, the engine returns the same rejection. A 409 `conflict` is not recorded: the client plans again with the new version.

## Inside the engine

- The engine keys its internal state by name, as before. A map in the model connects each UUID to a handle. The engine changes a UUID to a name at the HTTP boundary.
- The journal records the UUID of each new object in `changes`. `model.json` keeps the map and the tombstones. A replay makes the same handles and the same UUIDs again.
- A formula stays as text with names. A rename changes the formulas inside the engine, as before. The display text in `GET /` always uses the current names.
- If the Python API gets no `id`, the engine makes a UUIDv7.
- The hidden override input of an overridable Metric (`__override__<name>`) also has a UUID. `GET /` does not show it. The op `set_cell` with `override: true` writes it through the UUID of its Metric.
