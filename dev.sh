#!/usr/bin/env bash
# Start the full local stack: PostgreSQL, the router, nanashi-api (it starts the engines) and the web dev server.
# Before the first run, set up tessera/.venv (tessera/CLAUDE.md) and run "pnpm install" in web/.
# Open http://127.0.0.1:5173 after the start. Ctrl+C stops all processes.
set -euo pipefail
cd "$(dirname "$0")"
DSN=${NANASHI_PG_DSN:-postgresql://postgres@127.0.0.1:55432/nanashi}

if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  docker compose up -d postgres
fi
# The router needs the nanashi_model table before the first engine starts.
until (cd tessera && .venv/bin/python -m sparse_engine.pg_journal migrate "$DSN") >/dev/null 2>&1; do
  echo "waiting for PostgreSQL ($DSN)"; sleep 1
done

trap 'kill 0' EXIT
mkdir -p .nanashi-data
(cd router && go run ./cmd/nanashi-router --pg "$DSN" --listen 127.0.0.1:8090) &
(cd api && go run ./cmd/nanashi-api --pg "$DSN" --router http://127.0.0.1:8090 --tessera ../tessera --engine-dir ../.nanashi-data) &
(cd web && pnpm dev --host 127.0.0.1) &
wait
