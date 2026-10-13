# api/

`api/` is the application server (`nanashi-api`). It gives the Connect API to the frontend (`web/`).
It uses `connect-go` on `net/http` of the Go standard library. It does not use a different web framework.

## Commands

```bash
(cd api && go vet ./... && go test ./...)
# ../dev.sh starts the full stack.
(cd api && go run ./cmd/nanashi-api --pg postgresql://postgres@127.0.0.1:55432/nanashi)
```

## Rules

- `proto/` in the repository root is the contract between `api/` and `web/`. If you change it, run `pnpm generate` in `web/`.
- Do not edit `gen/`. `buf generate` makes it.
- The API trusts the header `X-Nanashi-User` only from this host or from `--trusted-proxy`. If you listen on an address other than loopback, give `--trusted-proxy`. Otherwise the API does not start.
- If you add a field that holds an id to `proto/`, give it the rule `nanashi.v1.id` (`proto/nanashi/v1/id.proto`).
- Put the code of a feature in the file of the feature.
- In each file, put the calculations and types first and the actions (I/O) after them. The comment `// The actions follow.` marks the start of the actions.
