# nanashi

nanashi is a monorepo for an EPM and FP&A service that uses AI.
Issue #30 gives the full structure of the service.

| Directory | Contents |
|---|---|
| [tessera/](tessera/README.md) | The sparse multidimensional calculation engine (the Python package `sparse_engine` and the Rust engine `nanashi_core`) |
| [router/](router/README.md) | The Go router that resends requests to the writer of a model |
| `proto/` | The Connect contract for the application server and the frontend |
| `compose.yaml` | PostgreSQL and the S3-compatible object storage (RustFS) for development and tests |

## Start the local environment

Install [mise](https://mise.jdx.dev/) and run `mise install` in the repository root. `mise.toml` sets the versions of Go, Rust, Python, Node.js, and pnpm.
Set up `tessera/.venv` (`tessera/CLAUDE.md`). Then run `./dev.sh`.
It starts PostgreSQL and the router. The router starts the engines.

## License

MIT License (`LICENSE`).
