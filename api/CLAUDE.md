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
- `plan.go` has the calculations (engine operations, queries, imports, access limits). `server.go` and `engine.go` have the actions.
