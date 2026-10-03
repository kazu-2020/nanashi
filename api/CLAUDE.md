# api/

`api/` is the application server (`nanashi-api`). It gives the Connect API to the frontend (`web/`).
It uses `connect-go` on `net/http` of the Go standard library. It does not use a different web framework.

## Commands

```bash
# Start PostgreSQL first (docker compose up -d in the repository root).
# If the nanashi_model table is missing, the tests are skipped.
# Make the table: in tessera/, run .venv/bin/python -m sparse_engine.pg_journal migrate <DSN>
(cd api && go vet ./... && go test ./...)
(cd api && go run ./cmd/nanashi-api --pg postgresql://postgres@127.0.0.1:55432/nanashi)
```

## Rules

- `proto/` in the repository root is the contract between `api/` and `web/`. If you change it, run `pnpm generate` in `web/`.
- Do not edit `gen/`. `buf generate` makes it.
- The API reads the engine tables (`nanashi_model`). It does not write to them. Only the engine writes to them.
