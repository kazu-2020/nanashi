# nanashi

nanashi is a monorepo for an EPM and FP&A service that uses AI.
Issue #30 gives the full structure of the service.

| Directory | Contents |
|---|---|
| [tessera/](tessera/README.md) | The sparse multidimensional calculation engine (the Python package `sparse_engine` and the Rust engine `nanashi_core`) |
| [router/](router/README.md) | The Go router that resends requests to the writer of a model |
| [api/](api/CLAUDE.md) | The Go application server (Connect) |
| [web/](web/CLAUDE.md) | The frontend (React, HeroUI, Vite+) |
| `proto/` | The Connect contract between `api/` and `web/` |
| `compose.yaml` | PostgreSQL and the S3-compatible object storage (RustFS) for development and tests |

## Start the local environment

Install [mise](https://mise.jdx.dev/) and run `mise install` in the repository root. `mise.toml` sets the versions of Go, Rust, Python, Node.js, and pnpm.
Set up `tessera/.venv` (`tessera/CLAUDE.md`) and run `pnpm install` in `web/`. Then run `./dev.sh`.
It starts PostgreSQL, the router, `nanashi-api` with one engine for each application, and the web dev server.
Open http://127.0.0.1:5173 and log in with a user name. The planning features are in `proto/nanashi/v1/plan.proto`.

## License

MIT License (`LICENSE`).
