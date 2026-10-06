# CLAUDE.md

This file gives guidance to Claude Code (claude.ai/code) when it works with the code in this repository.

nanashi is a monorepo for an EPM and FP&A service (issue #30). It has these directories:

- `tessera/`: the sparse multidimensional calculation engine. `tessera/CLAUDE.md` gives the guidance for it.
- `router/`: a Go router. It goes in front of the engine servers. `router/README.md` is its specification.
- `api/`: the Go application server. `api/CLAUDE.md` gives the guidance for it.
- `web/`: the frontend (SPA). `web/CLAUDE.md` gives the guidance for it.
- `proto/`: the Connect contract between `api/` and `web/`. `buf.gen.yaml` makes `api/gen/` and `web/src/gen/` from it.
- `docs/`: design notes that span more than one directory (for example `docs/engine-lifecycle.md`).
- `compose.yaml`: PostgreSQL (port 55432) and RustFS (port 59000) for development and tests. Start them with `docker compose up -d`.
- `mise.toml`: the versions of Go, Rust, Python, Node.js, and pnpm. Local development and CI use them. Run `mise install` to install them.
- `dev.sh`: starts the full local stack (PostgreSQL, router with the engines, `api/`, `web/`).

## Skills for the work

- Use the `ponytail` skill and the `pstack:poteto-mode` skill together. One skill does not replace the other skill.
  - `ponytail` sets the size of the solution. Make the smallest change that works.
  - `pstack:poteto-mode` sets the process. Plan the work, delegate it to subagents, and verify it.
- If a task changes more than one file or needs a design decision, invoke `pstack:poteto-mode` before you start the work.
- If the scope of a task becomes larger during the work, invoke `pstack:poteto-mode` at that time.

## Code style

- Keep actions, calculations, and data apart (from "Grokking Simplicity").
  - An action has side effects. Its result changes with the time or the number of runs. Examples: database I/O, network I/O, the clock.
  - A calculation is a pure function. The same input always gives the same output.
  - Data is a fact about an event. Example: a row that you read from the database.
- Put the logic in calculations. Keep actions small, and put them at the edges of the code.
- Use immutable data when the language lets you.

## Language and writing rules

- Write these items in English that follows ASD-STE100 (Simplified Technical English):
  - Code comments and docstrings (Python, Rust, Go).
  - Documents (`README.md` files, `tessera/docs/`, the `CLAUDE.md` files).
  - Commit messages, pull request titles, and pull request descriptions.
- Apply these ASD-STE100 rules:
  - Use a maximum of 20 words in an instruction and 25 words in a description.
  - Write one topic in one paragraph. Use a maximum of 6 sentences in a paragraph.
  - Use the active voice and simple tenses (present, past, future).
  - Write instructions in the imperative. Put a condition before the instruction ("If X, do Y.").
  - Use one word for one meaning. Do not use synonyms for the same thing. Use the terms in the glossary in `tessera/CLAUDE.md`.
  - Do not use more than 3 nouns in a row.
  - Use short, common words ("use", "start", "make sure", "about").
- Error messages that the user sees stay in Japanese.
- Some old comments and docstrings are still in Japanese. If you change code, write new comments in English. Also translate the old comments for the code that you change. Do not translate unrelated comments in the same change.

## CI

Each directory has its own workflow in `.github/workflows/`. A workflow starts only when its directory, the files it uses, or the workflow file changes.

- `tessera-test.yml`: the Python and Rust tests and the static checks of `tessera/`.
- `router-test.yml`: `go vet` and `go test` of `router/`.
- `api-test.yml`: `go vet` and `go test` of `api/`.
- `web-test.yml`: `vp check`, `vp test` and the build of `web/`. It also makes sure that the generated code agrees with `proto/`.

## Router

```bash
# The PgResolver tests use the nanashi_model table. If the table is missing, they are skipped
# Make the table: in tessera/, run .venv/bin/python -m sparse_engine.pg_journal migrate <DSN>
(cd router && go vet ./... && go test ./...)
```

- The router (`router/README.md`) resends writes. Because of `client_op_id`, a resent write is not committed two times. If you change the next action for each response (`decide` in `router.go`), keep this condition.
