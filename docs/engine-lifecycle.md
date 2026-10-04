# Design note: the router owns the engine processes (issue #51)

This document is the design, and the code follows it.
`api/engine.go`, `router/supervisor.go`, `router/README.md`, and `tessera/docs/server.md` give the current behavior.

## Problem

`nanashi-api` starts one engine process for each application when it starts (`StartAll` in `api/server.go`, `Engines.Start` in `api/engine.go`).
This has 3 effects:

- Each engine keeps 1 to 3 PostgreSQL connections. The count grows with the number of applications, not with the number of applications in use.
- If an engine stops with an error, nothing starts it again. Requests for that application get 503 `no_leader` after the router deadline of 90 seconds.
- `PlanServer.lock` is a `sync.Mutex` in one api process. A second api process breaks it.

The goal is that the api uses the engine as it uses a database: one address, read and write calls, and a conflict error.
The api must not know the engine processes, the path to `tessera/`, or the DSN of the engine.

## Decisions

| Point | Decision | Reason |
|---|---|---|
| Who starts an engine | The router, when a request comes for a model that has no live leader. | The router is the one address, and it already finds the writer in the lease. The `Resolver` interface is the seam. |
| Who stops an idle engine | The engine itself, with a new option `--idle-exit`. | Only the engine knows the requests in progress. The router caches the leader address, so it does not see each request. |
| A stopped engine | Stays stopped until the next request. The router never starts an engine on its own. | No request, no process. This also holds after an idle stop, after a crash, and after a second router started a duplicate. |
| Crash loop | Exponential backoff, 1 s to 60 s. After 3 crashes in a row, the router returns 503 `engine_failing` at once. | The api holds its lock while it waits. It must not wait 90 seconds for an engine that cannot start. |
| A process that holds no lease for too long | The router kills it. The next request starts a new one. | This is the one state that otherwise blocks a model for ever. |
| Model creation | Explicit: `PUT /models/<id>` inserts the `nanashi_model` row and starts no engine. An unknown id still gets 404. | A wrong id must not make a process, a row, and a directory. |
| Spawn backend | `exec` of the Python server on the router host. No `Backend` interface. | One implementation. An ECS backend is a different `Resolver` that wraps `PgResolver`. |
| The api lock | Stays as it is. | See "Why the lock stays". |
| The Go client | Stays in `api/engine.go`. | The api is the only client. |

## What does not change

- Each model has one writer. The lease in `nanashi_model` fences old writers. A second engine for a model becomes a standby, costs connections, and stops itself after the idle time.
- `client_op_id` makes a resent write safe. `decide` in `router/router.go` does not change.
- A published version does not change. The engine stops through the SIGTERM path: it completes the requests, commits the queue, releases the lease, and exits with code 0.
- The reference implementation and the Rust engine do not change.

## Usage

The api gets one address.

```bash
nanashi-router --pg "$DSN" --listen 127.0.0.1:8090 --tessera ../tessera --engine-dir ../.nanashi-data --engine-idle 15m
nanashi-api    --pg "$DSN" --router http://127.0.0.1:8090
```

The api code has 4 calls.

```go
engines := &api.Engines{Router: "http://127.0.0.1:8090", HTTP: &http.Client{Timeout: 100 * time.Second}}
engines.Create(ctx, app)              // PUT /models/<app>. Idempotent. Starts no engine.
em, _, err := engines.model(ctx, app) // If the engine is stopped, the router starts it and waits.
err = engines.write(ctx, app, user, ops)
```

Without `--tessera`, the router runs as today: it only finds engines that something else started.

## Shape

### router/supervisor.go (new)

```go
// ErrEngineFailing: the engine of the model crashed 3 or more times in a row, and the next start is not yet permitted.
var ErrEngineFailing = errors.New("engine failing")

// state is the data about the engine of one model. It has no process handle, so the calculations stay pure.
type state struct {
	running bool
	started time.Time // Start time of the running process.
	fails   int       // Crashes in a row. A clean exit (code 0) sets it to 0.
	retryAt time.Time // Do not start before this time.
	lastErr string    // Text of the last crash, for the 503 message.
}

type verb int // none, spawn, kill, failing

// Supervisor is a Resolver that also owns the engine processes on this host.
// Invariant: at most one process of this Supervisor runs for a model (the map under mu is the record).
type Supervisor struct {
	Lease   Resolver                     // *PgResolver. The lease is the only source of the leader address.
	Command func(model string) *exec.Cmd // A calculation: the same model gives the same argv. Tests give a fake script.

	mu     sync.Mutex
	models map[string]*state
	procs  map[string]*exec.Cmd // Running processes, for Close.
}

// Leader asks Lease first. A live leader is returned as it is. ErrUnknownModel is returned as it is.
// If the model exists but has no live leader, plan decides: spawn (start a process, return ""),
// kill (the process held no lease for bootBudget; kill it, return ""), failing (return ErrEngineFailing), none (return "").
// Router.forward backs off and asks again while Leader returns "". No wait loop is necessary here.
func (s *Supervisor) Leader(ctx context.Context, model string) (string, error)

// Close sends SIGTERM to the running engines and waits 15 seconds, then kills them. Call it after http.Server.Shutdown.
func (s *Supervisor) Close()

// plan is the decision for a model that has no live leader.
//   running, now - started < bootBudget -> none
//   running, now - started >= bootBudget -> kill
//   not running, fails >= quietFails, now < retryAt -> failing
//   not running, now < retryAt -> none
//   not running -> spawn
func plan(st state, now time.Time) (verb, state)

// afterExit is the state after the process exits.
//   err == nil (exit 0: idle stop or SIGTERM) -> fails = 0, retryAt = zero
//   crash after a run of healthyRun or more -> fails = 1
//   crash -> fails + 1
//   retryAt = now + min(firstRetry << (fails-1), maxRetry)
func afterExit(st state, err error, now time.Time) state // ranFor is now - st.started
```

Constants: `quietFails = 3`, `firstRetry = 1s`, `maxRetry = 60s`, `healthyRun = 1m`.
`bootBudget = 60s` is the time that a process may run without a lease: 2 times the lease time of the engine. The cold start of the large plan is about 1 second (`tessera/docs/performance.md`), so a slow start does not become a crash loop.

The argv of an engine is `.venv/bin/python -m sparse_engine.server <engine-dir>/<model> --pg <DSN> --model-id <model> --port 0 --idle-exit <seconds>`.
The engine listens on 127.0.0.1 and advertises the port that it got in the lease. The Supervisor never learns the port.
`--migrate` is not given: the router needs the schema before it starts, and `dev.sh` migrates first.

### router/router.go and router/pg.go

- `Router` gets a field `Create func(ctx context.Context, model string) error`. `PUT /models/<id>` calls it and returns 200 `{"ok": true}`. If `Create` is nil, the router returns 405. The `Resolver` interface does not change, so the fakes in the tests do not change.
- `PgResolver.Create` runs `insert into nanashi_model (model_id) values ($1) on conflict do nothing`. It is the same statement that `PgJournal.__init__` runs. The PostgreSQL role of the router needs the insert permission.
- In `forward`, next to the `ErrUnknownModel` case: `ErrEngineFailing` returns 503 `engine_failing` with the last crash text at once.
- `cmd/nanashi-router/main.go` gets `--tessera`, `--engine-dir` (default `../.nanashi-data`, as in the api today), and `--engine-idle` (default 15m). With `--tessera`, the Resolver is a `Supervisor` that wraps the `PgResolver`.

### tessera/sparse_engine/server.py

- New option `--idle-exit SECONDS` (0, the default, never stops).
- `Server` counts the requests in progress and keeps the end time of the last request. `GET /health`, `/ready`, and `/stats` do not count. If probes count, a monitor keeps all engines alive.
- `idle_for(active, last, now) -> float` is a calculation. A watcher thread calls `server.shutdown()` when it reaches the limit. This is the same path as SIGTERM. The process exits with code 0.
- `workspace.py` does not change.

### api

- `Engines` becomes `{Router, HTTP}` plus `Create`. `Start`, `StopAll`, `engineProc`, `Tessera`, `Dir`, `DSN`, and the `exec`, `syscall`, and `filepath` imports go away.
- `CreateApplication` calls `Create`, then writes as before. The first write starts the engine.
- `StartAll` and the flags `--tessera` and `--engine-dir` go away. `dev.sh` gives them to the router.
- `lock` stays. Its comment gets the shape for a second api process (below).

### Expected size

api about -130 lines. router about +230 lines, half tests and README. tessera about +50 lines. No file is deleted.

## Why the lock stays

Two designs tried to remove `PlanServer.lock`. Both lose.

**A strict `expect` in the engine, with a retry loop in the api.**
The lock protects a read-modify-write that spans the engine and the `app_*` tables.
`editMembers` writes `app_property.text_values` after the engine write, from the `meta` that it read before.
`Engines.write` returns at once when `ops` is empty (`api/engine.go`), so an edit of text values only never reaches the engine, and no engine check runs.
Two such edits at the same time give a lost update. Today the lock prevents it. The strict `expect` would make this a regression.
A strict `expect` also conflicts with every unrelated cell write, so a definition change could retry without end.

**A PostgreSQL advisory lock now.**
This is the correct shape for 2 api processes. But today the api is one process, so the lock gives nothing.
With the default pool (`MaxConns = max(4, CPU count)`), a lock that holds a pool connection for the engine write (up to 70 seconds) plus the second connection of the handler can empty the pool.
When a second api process comes, change only the body of `lock`: `pg_advisory_xact_lock(<2-key>, hashtext(app))` in a transaction on a dedicated connection outside the pool. `tessera` uses the 1-key form (`hashtextextended`), so the keys do not collide. The handlers do not change.

## Tradeoffs accepted

- The first request to a cold application waits for the Python start and the journal replay. In exchange, the connections and the memory follow the number of applications in use.
- The idle policy lives in 2 places: the router gives the limit, the engine applies it. In exchange, the router does not count requests, and an orphan engine (after a router SIGKILL) stops itself.
- A router that stops with SIGKILL leaves its engines. The next router adopts them through the lease, and `--idle-exit` stops them. No cleanup code.
- Two routers can start 2 engines for one model. The lease makes one a standby, and it idles out. No code against it.
- There is no limit on the number of engines on one host. The idle stop limits it in practice. If a limit is necessary, add `len(running) < max` to `plan`.

## Alternatives rejected

- **The router counts the last use and sends SIGTERM.** The leader cache keeps `Resolver.Leader` off the request path, so this needs a hook in `forward`. The router cannot know the requests in progress. Orphans after a router crash stay for ever (`Pdeathsig` is Linux only).
- **Implicit creation on the first request to an unknown id.** Removes `Create` from the api, but a wrong id leaves a row and a directory, and 404 `no_model` goes away.
- **Create starts the engine and the router forwards `GET /ready`.** The router rewrites the request, and `Leader` needs a special case for the time before the row exists. The row first, the engine on the first read or write, needs neither.
- **A Spawner or Backend interface for exec and ECS.** One implementation today. `Resolver` is the seam.
- **Keep the processes in the api, add restart and idle stop there.** The api keeps the path, the DSN, and the process state. Two api processes start 2 engines for each model.

## Synthesis record

Three candidate designs were made in parallel and judged by a fourth reviewer against a rubric.
All three agreed on the core shape: a `Supervisor` that wraps `PgResolver`, a start inside `Leader` that returns "", an idle stop in the engine, exit code 0 as a clean stop, exponential backoff, and no Spawner interface.
The base is the candidate with the smallest api diff and pure `plan` and `afterExit`.
Grafted: `Router.Create` as a function field instead of a new method on `Resolver` (so the test fakes do not change), and the kill of a process that holds no lease for `bootBudget`.
Rejected: implicit creation, the strict `expect`, and the advisory lock now, for the reasons above.

## Open questions

- The cold start of the large plan from a local snapshot is about 1 second (measured, `tessera/docs/performance.md`). A snapshot in object storage and a long journal after it add time. Is the router deadline of 90 seconds enough for the largest production model?
- Is 15 minutes a good default for `--engine-idle`? Development wants a short time, production a long one. The router flag covers both.
- `PUT /models/<id>` uses only the router authentication. Is that enough while the api is the only client?

## Plan

Each step ended in a check, in this order.

1. Measured the cold start of `examples.fpa` through `sparse_engine.server` with `--pg`. The result is in `tessera/docs/performance.md`.
2. tessera: `--idle-exit`. The tests start the server with `--idle-exit 1`, send one request, and make sure that the process exits with code 0, also when a monitor probes `/health`.
3. router: `plan` and `afterExit` with table tests. `Supervisor` with a `Command` of `sh -c 'exit 1'` for the crash loop, and `exit 0` for the clean stop. `PUT /models/<id>` and `engine_failing` in `router_test.go`.
4. api: the process management is removed, `Create` is added, and the flags moved to `dev.sh`. `api/e2e.sh` through a router with `--tessera` passed, and the engines stopped themselves after the idle time.
5. Docs: `router/README.md`, `tessera/docs/server.md`, `api/CLAUDE.md`, the comment in `compose.yaml`.
