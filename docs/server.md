# HTTP server

A thin server publishes `Workspace` as a JSON API. It uses only the standard library.

```bash
.venv/bin/python -m sparse_engine.server plan/ --port 8080 --checkpoint-every 1000   # FileJournal
.venv/bin/python -m sparse_engine.server s3://nanashi/plans --pg postgresql://... --model-id plan-2027 \
    --host 0.0.0.0 --advertise http://plan-a:8080 --tokens tokens.json                 # {"<token>": "<user>"}
```

| Request | Content |
|---|---|
| `GET /` | The definitions of dimensions and Metrics, and the sequence number of the current published version |
| `GET /metrics/<name>/cell?<dimension>=<member>` | 1 cell |
| `GET /metrics/<name>/slice?<dimension>=a,b` | The cells of a range |
| `GET /metrics/<name>/rows?<dimension>=a&offset=0&limit=50` | A list of rows and the total number of rows |
| `GET /metrics/<name>/summary?keep=Month&agg=sum&<dimension>=a,b` | Aggregation |
| `POST /writes` | `{"client_op_id", "reason", "expect", "ops": [{"op": "set_cell", "args": [...], "kwargs": {...}}, ...]}` |
| `GET /health` | Whether the server is alive (the sequence number of the current published version and the role `role`). No authentication is necessary. |
| `GET /ready` | Whether the server can receive requests. It returns 503 and the reason in these conditions: the writer stopped, the server could not open the journal again, the server cannot extend the lease, the standby monitor failed, or `Replica` cannot catch up. It also returns the role `role` (`leader`, `standby`, or `follower` with `--follow`). No authentication is necessary. |
| `GET /stats` | Numbers for monitoring (Prometheus text format). For example: the number and time of commits, cancelled writes, the queue length, whether the server is the writer (`nanashi_leader`), the lease state, the time since the last snapshot, and the delay of `Replica`. |

Authentication, not the request body, sets the user that the audit records.
With `--tokens`, the server requires `Authorization: Bearer <token>`.
With `--user-header X-Forwarded-User`, the server uses the value of the header that an authenticating proxy added.
If neither option is given, the server does no authentication (the user is blank). Then it refuses to listen on an address other than 127.0.0.1 (`--insecure` removes this limit).

These are the limits:

- The request body can be up to 16 MiB (`--max-body`).
- slice, rows, and summary can return up to 100 thousand cells (`--max-cells`).
- The server can process up to 64 requests at the same time (`--max-threads`). Above this, it returns 503.
- If the read or write of a request stops for 30 seconds, the server closes the connection.

Each write is 1 request and 1 transaction, and `client_op_id` is necessary.
If a client sends a write again, the server does not commit it twice. This is also true after a restart.
If `expect` has the sequence number of the version that the client read, and a later write changed the same cells, the server refuses the write with 409.
The server returns these status codes, each with `{"error", "message"}`:

- 429: the queue is full.
- 504: the commit did not complete in the wait time.
- 400: an error in a formula or an argument.
- 401: an authentication error.
- 413: the request is too large.

A write to a standby gets 421, and `leader` contains the address of the writer (next paragraph).
A formula error also returns `code` (a key of `messages.MESSAGES`). Use this code, not the message text, to identify the error.
An internal error returns 500 with a fixed message. The server logs the cause with an `error_id`.

If many servers open the same model, the server with the write right (the lease) becomes the writer. The other servers become standbys and follow it (`Workspace(standby=True)`, [Concurrent reads and writes](concurrency.md)).
A standby receives reads. It refuses writes with 421 and `{"error": "not_leader", "leader": <address of the writer>}`. Thus the sender must resend to `leader`.
The router ([router.md](router.md)) uses `nanashi_model.lease_endpoint` to find the writer.
`--advertise` sets the address that the server gives to other processes when it is the writer.
The default is `http://<host>:<port>`. If the server listens on `0.0.0.0`, `--advertise` is necessary.
`--lease-ttl` sets the lease time (30 seconds by default).

On SIGTERM and SIGINT, the server completes the received requests, commits the writes in the queue, releases the lease, and then stops. (Containers on ECS and similar services stop with SIGTERM.)
The standby receives the notification, gets the right immediately, and becomes the writer.
If the server crashes (SIGKILL), the failover occurs after the lease expires, within the interval at which the standby tries to get the right (1 second).

In the profit and loss plan (large), a read of 1 cell through HTTP takes 0.24 ms. A write that changes the salary of 1 employee takes 0.9 ms.
A write while 8 readers read continuously takes 2.2 ms (`bench_http.py`, local loopback, about 2,800 reads per second).
At start, the server sets the Python thread switch interval to 0.5 ms (`--switch-interval`).
