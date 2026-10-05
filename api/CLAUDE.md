# api/

`api/` is the application server (`nanashi-api`). It gives the Connect API to the frontend (`web/`).
It uses `connect-go` on `net/http` of the Go standard library. It does not use a different web framework.

## Commands

```bash
# Start PostgreSQL first (docker compose up -d in the repository root). Without it, the PostgreSQL tests are skipped.
(cd api && go vet ./... && go test ./...)
# End-to-end check with real engines. It needs PostgreSQL and nanashi-router with --tessera on 127.0.0.1:8090.
(cd api && ./e2e.sh)
# ../dev.sh starts the full stack. nanashi-router starts the engine of an application when a request comes for it.
(cd api && go run ./cmd/nanashi-api --pg postgresql://postgres@127.0.0.1:55432/nanashi --router http://127.0.0.1:8090)
```

## Rules

- `proto/` in the repository root is the contract between `api/` and `web/`. If you change it, run `pnpm generate` in `web/`.
- Do not edit `gen/`. `buf generate` makes it.
- The API keeps its own tables (`app_*` in `schema.sql`). It does not write to the engine tables (`nanashi_*`).
- The API trusts the header `X-Nanashi-User` only from this host or from `--trusted-proxy`. If you listen on an address other than loopback, give `--trusted-proxy`. Otherwise the API does not start.
- The API sends all engine requests through the router (`/models/<application ID>/...`). It does not start engine processes.
- Each id in a request is a UUID (`../docs/ids.md`). The interceptor changes the ids to the canonical form (`canonicalize`), so an RPC can trust them.
- A change that the engine and the api tables both take goes through the outbox `app_operation` (`change` in `server.go`, `../docs/engine-lifecycle.md`). A change of the api tables only goes through `apiOnly`. `WriteCells` goes to the engine only. There is no lock.
- The audit trail `app_audit` gets one row for each `client_op_id` that is done: `change` writes it when the row flips to done, `apiOnly` in its transaction. The interceptor audits only `WriteCells`, which has no row.
- The Metric catalog `app_metric` keeps the description, the folder and the owner of a Metric. The engine keeps the name. A change of the catalog goes in the outbox plan of the Metric RPC (`../docs/ids.md`).
- Put the code of a feature in the file of the feature. `server.go` has the parts that every feature uses (the server, the RPC rules, `change`, `apiOnly` and the errors). `engine.go` has the engine data and the HTTP client. The feature files are `access.go`, `model.go` (lists, properties and members), `metric.go`, `data.go` (queries, writes and comments), `import.go`, `snapshot.go` and `item.go`.
- In each file, put the calculations and types first and the actions (I/O) after them. The comment `// The actions follow.` marks the start of the actions.
- Put the tests of a function in the `_test.go` file of its feature. `server_test.go` has the shared test helpers.
