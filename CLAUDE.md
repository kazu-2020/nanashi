# CLAUDE.md

This file gives guidance to Claude Code (claude.ai/code) when it works with the code in this repository.

nanashi is a monorepo for an EPM and FP&A service (issue #30). It has these directories:

- `tessera/`: the sparse multidimensional calculation engine. `tessera/CLAUDE.md` gives the guidance for it.
- `router/`: a Go router. It goes in front of the engine servers. `router/README.md` is its specification.
- `compose.yaml`: PostgreSQL (port 55432) and RustFS (port 59000) for development and tests. Start them with `docker compose up -d`.

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

Each directory has its own workflow in `.github/workflows/`. A workflow starts only when its directory or the workflow file changes.

- `tessera-test.yml`: the Python and Rust tests and the static checks of `tessera/`.
- `router-test.yml`: `go vet` and `go test` of `router/`.

## Router

```bash
# The PgResolver tests use the nanashi_model table. If the table is missing, they are skipped
# Make the table: in tessera/, run .venv/bin/python -m sparse_engine.pg_journal migrate <DSN>
(cd router && go vet ./... && go test ./...)
```

- The router (`router/README.md`) resends writes. Because of `client_op_id`, a resent write is not committed two times. If you change the next action for each response (`decide` in `router.go`), keep this condition.
