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

## License

MIT License (`LICENSE`).
