# Identifiers

This note gives the identifier rules for `web/`, `api/` and `tessera/`. It follows issue #6 (ID design, version 4).

## Rules

- The system makes each identifier that goes out of the system as a UUIDv7. `api/` accepts any UUID version in the canonical form. It does not check the version. Examples: an application, a Metric, a dimension (list), a member, a property, an item, an access rule, a comment, a snapshot and a `client_op_id`.
- Usually `web/` makes the identifier. If `api/` makes an object for the user, `api/` makes the identifier one time, in the plan of the RPC.
- A name is an attribute. It is not an identifier.
- Each identifier in a request must be in the canonical form (lowercase, with hyphens). If it is not, `api/` returns InvalidArgument and does not change it. `tessera/` does not parse identifiers. It compares them as strings, so each identifier has one spelling only.
- The internal numbers of the engine do not go out of the engine. These are the member numbers and the Rust dense numbers.
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
| `rename_dimension` | `id`, `name` |
| `add_member` | `dim`, `id`, `name`, `at`? |
| `rename_member` | `dim`, `id`, `name` |
| `move_member` | `dim`, `id`, `at` |
| `remove_member` | `dim`, `id` |
| `add_property` | `dim`, `id`, `name`, `target` (dim uuid) |
| `rename_property` | `dim`, `id`, `name` |
| `set_property_values` | `dim`, `prop`, `values` (`{member uuid: member uuid or null}`) |
| `add_input` | `id`, `name`, `dims`, `kind`?, `cells`?, and the other current options |
| `add_formula` | `id`, `name`, `formula` (text with names), `dims`?, `overridable`? |
| `rename_metric` | `id`, `name` |
| `remove_metric` | `id` |
| `set_cell` | `metric` (uuid), `value`, `override`? (true to write the hidden override input), `coords` (`{dim uuid: member uuid}`). A value of a member-type Metric is a member uuid |
| `spread` | `metric` (uuid), `total`, `how`?, `where`? (`{"<dim uuid>.<prop uuid>": member uuid}`), `coords` |

- If an op defines an object with a UUID that the model has, and the object has the same kind, the op defines the object again. If the name is different, the op renames the object.
- If a rename op or a definition op gives an unknown UUID for an existing object (for example the `dim` of `rename_property`), the engine returns 400 `{"error": "bad_request"}`.
- If the UUID belongs to an object of a different kind, or it is a tombstone, the engine returns 409 `{"error": "duplicate_id"}`.
- If the name belongs to a different UUID, the engine returns 400 `{"error": "bad_request"}`.
- The engine records each rejected operation (400, and 409 `duplicate_id`) with its `client_op_id` for the `op_window`. If a client sends the same `client_op_id` again, the engine returns the same rejection. A 409 `conflict` is not recorded: the client plans again with the new version.

## The Metric catalog

- `app_metric` in `api/` keeps the attributes of a Metric that the engine does not know: the description, the folder and the owner. The key is `(app_id, metric_id)`. `metric_id` is the Metric UUID.
- The catalog does not keep the name. The engine keeps the name, so a rename changes no catalog row.
- `CreateMetric` inserts the row in the same outbox plan as the engine operation. The owner is the user of the request. If the engine refuses the operation, the compensation deletes the row.
- `UpdateMetric` writes the description and the folder in the same plan. If the engine refuses the operation, the compensation puts the old values back. It changes the row only if the row still has the new values, so it does not undo a later change.
- `DeleteMetric` does not delete the row. The engine keeps the UUID as a tombstone, and the readers skip a row for a Metric that the model does not have.
- `GetModel` shows empty values for a Metric without a row, for example a Metric from before the catalog.
- A snapshot keeps the rows. A restore inserts them with the same Metric UUIDs.

## Inside the engine

- The engine keys its internal state by UUID: Metrics, dimensions, members, properties, the journal `changes`, the saved model and the cell history. The member numbers and the Rust dense numbers stay inside the engine.
- The Model API takes ids and gives ids. The server gives the request UUIDs to the Model API and does not look up names.
- The engine keeps a formula as a syntax tree that holds ids. At definition, it binds the names in the formula text to ids one time.
- A rename changes only the name and the name index. It does not change a formula and does not calculate again. The display text of a formula (`GET /`, errors) uses the current names.
- `Named` (`sparse_engine/named.py`) takes names and gives names, for people: tests, examples, benchmarks and notebooks. The server does not use it.
- If the Python API gets no `id`, the engine makes a UUIDv7.
- The hidden override input of an overridable Metric (`__override__<name>`) also has a UUID. `GET /` does not show it. The op `set_cell` with `override: true` writes it through the UUID of its Metric.
