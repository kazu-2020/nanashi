# Router

`router/` is a Go router (`nanashi-router`) that you put in front of the engine servers.
It sends each `/models/<model ID>/...` request to the writer server of that model, and removes `/models/<model ID>` from the path.
For example, it sends `/models/plan-2027/writes` to `/writes` on the writer.
It sends the path and the query as it received them (it does not decode `%2F` in Metric names).
A model ID must start with an alphanumeric character and contain only alphanumeric characters, `_` and `-`.
The maximum length is 128 characters (for other IDs, the router returns 400).

```bash
(cd router && go build -o nanashi-router ./cmd/nanashi-router)
router/nanashi-router --pg postgresql://... --listen 0.0.0.0:8090 --tokens tokens.json   # {"<token>": "<user>"}
```

The router finds the writer in `nanashi_model` in the journal (the `lease_endpoint` of a lease that is not expired; this is `--advertise` of the engine).
The router keeps the address for each model.
It forgets the address if it cannot send, if it gets 5xx, or if it gets 421 with no known writer.
The router also sends reads to the writer, so that a user can immediately read the user's own writes.

The router resends as follows for each engine response.
It sends the same bytes in the body each time.
Because of `client_op_id`, a write is not committed two times when the router resends after a response with an unknown commit result (500, 504, or a broken connection).
A 500 for a read is not related to a commit, and a resend gives the same answer.
Thus, the router returns it as it is.

| Engine response | Router |
|---|---|
| 200, 400, 401, 404, 405, 409, 411, 413, 500 for a read | Returns the response as it is |
| 421 with `leader` | Resends to `leader` immediately |
| 429 | Waits, then resends to the same server |
| 500, 504 or 503 (`busy`, `stale`, `closed`) for a write, no connection, broken connection | Waits, finds the writer again, then resends |

The wait starts at 50 ms and doubles up to 1 second (with jitter).
The router waits up to 70 seconds for each send (the engine waits up to about 60 seconds for a write commit).
If the router does not find a writer before `--deadline` (default 90 seconds), it returns 503 `{"error": "no_leader"}`.
For a model that is not in the journal, it returns 404 `{"error": "no_model"}`.
If the engine of the model crashed 3 times in a row, the router returns 503 `{"error": "engine_failing"}` immediately (only with `--tessera`).
The message contains the text of the last crash.
At the deadline, the router returns the last response that it received (504 if the deadline came before a response).

`PUT /models/<model ID>` adds the model to `nanashi_model` and returns 200 `{"ok": true}`.
It starts no engine. If the model is already there, it does nothing, so you can send it again.
The router uses the same authentication as for other requests.
The PostgreSQL role of the router needs the `insert` permission on `nanashi_model` for this request.

With `--tessera <path to tessera/>`, the router starts the engines on its host.
If a request comes for a model that has no live writer, the router starts `<tessera>/.venv/bin/python -m sparse_engine.server <engine-dir>/<model ID> --pg <DSN> --model-id <model ID> --port 0 --idle-exit <seconds>`.
`--engine-dir` is the directory for the engine files (default `../.nanashi-data`).
`--engine-idle` is the time without requests after which the engine stops itself (default 15m).

The router waits for the lease of the new engine, as it waits for any writer, until `--deadline`.
An engine that stops with code 0 (idle stop or SIGTERM) starts again on the next request, without a wait.
After a crash, the router waits 1 second before the next start, and doubles the wait up to 60 seconds.
If an engine runs for 90 seconds without a lease, the router kills it.
On SIGTERM and SIGINT, the router sends SIGTERM to its engines after it stops, and kills them after 15 seconds.

Without `--tessera`, the router only finds engines that something else started.
[engine-lifecycle.md](../docs/engine-lifecycle.md) gives the design.

With `--tokens`, the router finds the user from `Authorization: Bearer <token>`.
It adds the user to `X-Forwarded-User` and sends it to the engine.
The router removes `X-Forwarded-User` if the sender added it.
Start the engine with `--user-header X-Forwarded-User --trusted-proxy <address of the router>` ([server.md](../tessera/docs/server.md)).

With `--user-header <name> --trusted-proxy <CIDR>`, the router is behind an authenticating proxy such as oauth2-proxy.
The rules for these options are the same as for the engine ([server.md](../tessera/docs/server.md)):

- The router uses the header only on a connection from a trusted network. It examines the TCP address, not `X-Forwarded-For`.
- For other connections, the router returns 401 and does not send the request to the engine.
- To give more than 1 network, use `--trusted-proxy` again. A comma-separated list is not permitted.
- The router does not trust 127.0.0.1 or `::1` automatically.
- `--user-header` needs `--trusted-proxy`, and `--trusted-proxy` needs `--user-header`.
- You cannot use `--tokens` and `--user-header` together.

The router sends the user to the engine in the same header, and removes `Authorization`.
Thus, give the engine the same `--user-header`, and give its `--trusted-proxy` the address of the router.
Do not give the engine the address of the OIDC proxy. Only the router connects to the engine.

```bash
# oauth2-proxy (10.0.1.5) -> router (10.0.2.0/24) -> engine
router/nanashi-router --pg postgresql://... --listen 0.0.0.0:8090 \
    --user-header X-Forwarded-Email --trusted-proxy 10.0.1.5
.venv/bin/python -m sparse_engine.server s3://nanashi/plans --pg postgresql://... --model-id plan-2027 \
    --host 0.0.0.0 --advertise http://plan-a:8080 --user-header X-Forwarded-Email --trusted-proxy 10.0.2.0/24
```

[server.md](../tessera/docs/server.md) tells which oauth2-proxy header to use for the audit.
Without `--tokens` or `--user-header`, the router does no authentication and refuses to listen on addresses other than 127.0.0.1 (`--insecure` removes this limit).
`GET /healthz` shows if the router itself is alive.
On SIGTERM and SIGINT, the router refuses new requests.
It waits until the requests that it resends are complete, and then it stops.

We measured the failover with `tests/failover.py --via-router`, where the senders send only to the router.
After the writer stopped, the longest gap in commits was 0.03 to 0.13 seconds with SIGTERM.
With SIGKILL, it was 3.1 to 3.3 seconds (the lease time was 3 seconds).
In all runs, the router returned only 200 to the senders.

The router does not yet send reads to standbys.
It also does not yet find the writer in a way that is specific to an environment such as ECS.
