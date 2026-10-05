# HTTP server

A thin server publishes `Workspace` as a JSON API. It uses only the standard library.

```bash
.venv/bin/python -m sparse_engine.server plan/ --port 8080 --checkpoint-every 1000   # FileJournal
.venv/bin/python -m sparse_engine.server s3://nanashi/plans --pg postgresql://... --model-id plan-2027 \
    --host 0.0.0.0 --advertise http://plan-a:8080 --tokens tokens.json                 # {"<token>": "<user>"}
```

| Request | Content |
|---|---|
| `GET /` | The definitions of dimensions and Metrics, the property values of the dimensions, and the sequence number of the current published version. The keys are the UUIDs ([ids.md](../../docs/ids.md)) |
| `GET /metrics/<uuid>/cell?<dimension uuid>=<member uuid>` | 1 cell |
| `GET /metrics/<uuid>/slice?<dimension uuid>=a,b` | The cells of a range |
| `GET /metrics/<uuid>/rows?<dimension uuid>=a&offset=0&limit=50` | A list of rows and the total number of rows |
| `GET /metrics/<uuid>/summary?keep=<dimension uuid>&agg=sum&<dimension uuid>=a,b` | Aggregation |
| `GET /metrics/<uuid>/overrides?<dimension uuid>=a,b` | The cells that override the formula of an overridable Metric (`set_cell` on the Metric). The body is the same as slice. |
| `GET /operations/<client_op_id>` | The result of a write: `{"state": "committed", "seq"}` or `{"state": "rejected", "status", "body"}`. If the server does not know the operation, it returns 404 `unknown_operation` |
| `POST /writes` | `{"client_op_id", "reason", "expect", "ops": [{"op": "set_cell", "metric": "<uuid>", "value": 1, "coords": {"<dimension uuid>": "<member uuid>"}}, ...]}` |

Each reference in a path, a query, or an op is a UUID. [ids.md](../../docs/ids.md) gives the rules and the arguments of each op.
The engine changes a UUID to the name of the object at the boundary, and changes the names in a result to UUIDs.
In a result, a dimension is a dimension UUID, a member is a member UUID, and a value of a member-type Metric is a member UUID.
An op is an object with `op` (the operation name) and the arguments by name, for example `{"op": "add_member", "dim": "<uuid>", "id": "<uuid>", "name": "A"}`.
`WRITE_OPS` in `server.py` lists the permitted operations.
| `GET /health` | Whether the server is alive (the sequence number of the current published version and the role `role`). No authentication is necessary. |
| `GET /ready` | Whether the server can receive requests. It returns 503 and the reason in these conditions: the writer stopped, the server could not open the journal again, the server cannot extend the lease, the standby monitor failed, or `Replica` cannot catch up. It also returns the role `role` (`leader`, `standby`, or `follower` with `--follow`). No authentication is necessary. |
| `GET /stats` | Numbers for monitoring (Prometheus text format). For example: the number and time of commits, cancelled writes, the queue length, whether the server is the writer (`nanashi_leader`), the lease state, the time since the last snapshot, and the delay of `Replica`. |

With `--pg`, if a request has the header `X-Nanashi-Model` and its value is not the `--model-id` of the server, the server answers 421 `not_leader` without a `leader` field.

Authentication, not the request body, sets the user that the audit records.
With `--tokens`, the server requires `Authorization: Bearer <token>`.
With `--user-header X-Forwarded-User`, the server uses the value of the header that an authenticating proxy added.
You cannot use `--tokens` and `--user-header` together.
If neither option is given, the server does no authentication (the user is blank). Then it refuses to listen on an address other than 127.0.0.1 (`--insecure` removes this limit).

### Authentication through a proxy

Any client can send a user header. Thus, the server trusts the header only on a connection from a trusted proxy.
`--trusted-proxy <CIDR>` sets the addresses of the proxies, for example `10.0.1.0/24`.
A single address (`10.0.1.5`) is a network of 1 address.
An IPv4-mapped address (`::ffff:10.0.1.5`), as a dual-stack log shows it, means the IPv4 address.
To give more than 1 network, use the option again (`--trusted-proxy 10.0.1.0/24 --trusted-proxy 10.0.2.0/24`). A comma-separated list is not permitted.
`--user-header` needs `--trusted-proxy`, and `--trusted-proxy` needs `--user-header`. Otherwise the server does not start.

The server examines the address of the TCP connection, not a header such as `X-Forwarded-For`.
If the connection does not come from a trusted network, the server returns 401. It does not use the header in this case.
If the connection comes from a trusted network but the header is missing, the server also returns 401.
`GET /health` and `GET /ready` need no authentication, as before.

The server does not trust 127.0.0.1 or `::1` automatically.
If the proxy runs on the same host, give `--trusted-proxy 127.0.0.1`.
Do not give a network that contains clients, because each of these clients can then set any user.
Make sure that clients cannot connect to the server without the proxy (for example, with a security group). The trusted network is the only protection.

```bash
# oauth2-proxy at 10.0.1.5 -> engine
.venv/bin/python -m sparse_engine.server s3://nanashi/plans --pg postgresql://... --model-id plan-2027 \
    --host 0.0.0.0 --advertise http://plan-a:8080 --user-header X-Forwarded-Email --trusted-proxy 10.0.1.5
```

Make sure that the proxy removes the user header that a client sent, and sets it again on each request.
With `--pass-user-headers` (the default in reverse proxy mode), oauth2-proxy sends these headers:

| Header | Content | Use for the audit |
|---|---|---|
| `X-Forwarded-User` | The user ID from the identity provider (with OIDC, usually the `sub` claim). With `--prefer-email-to-user`, the email address | Stable, but people cannot read the ID easily |
| `X-Forwarded-Email` | The `email` claim | Recommended. People can read it. Make sure that the identity provider does not let users change it |
| `X-Forwarded-Preferred-Username` | The `preferred_username` claim | Do not use. Some identity providers let users change it, and some do not send it |

Use 1 header for all servers of a model. If you change the header, the same person has 2 different users in the journal.
If the router is between the proxy and the engine, see [router.md](../../router/README.md). Then `--trusted-proxy` of the engine contains the address of the router.

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
- 400: an error in a formula or an argument (`formula` or `bad_request`). This includes a name that belongs to a different UUID.
- 409 `duplicate_id`: the UUID of a new object belongs to an object of a different kind, or to a removed object.
- 409 `conflict`: a later write changed the same cells (`expect`).
- 401: an authentication error.
- 413: the request is too large.

The server records each rejected write (400, and 409 `duplicate_id`) with its `client_op_id` for the `op_window` of the journal.
If a client sends the same `client_op_id` again, the server returns the same status and body, and does not apply the write.
A conflict is not recorded: the client reads the new version, plans again, and sends the write again.
`GET /operations/<client_op_id>` gives the result of a committed or rejected write.

A write to a standby gets 421, and `leader` contains the address of the writer (next paragraph).
A formula error also returns `code` (a key of `messages.MESSAGES`). Use this code, not the message text, to identify the error.
An internal error returns 500 with a fixed message. The server logs the cause with an `error_id`.

If many servers open the same model, the server with the write right (the lease) becomes the writer. The other servers become standbys and follow it (`Workspace(standby=True)`, [Concurrent reads and writes](concurrency.md)).
A standby receives reads. It refuses writes with 421 and `{"error": "not_leader", "leader": <address of the writer>}`. Thus the sender must resend to `leader`.
The router ([router.md](../../router/README.md)) uses `nanashi_model.lease_endpoint` to find the writer.
`--advertise` sets the address that the server gives to other processes when it is the writer.
The default is `http://<host>:<port>`. If the server listens on `0.0.0.0`, `--advertise` is necessary.
`--lease-ttl` sets the lease time (30 seconds by default).

On SIGTERM and SIGINT, the server completes the received requests, commits the writes in the queue, releases the lease, and then stops. (Containers on ECS and similar services stop with SIGTERM.)
The standby receives the notification, gets the right immediately, and becomes the writer.
If the server crashes (SIGKILL), the failover occurs after the lease expires, within the interval at which the standby tries to get the right (1 second).

With `--idle-exit SECONDS`, the server stops on the same path when it gets no request for that time. Then it exits with code 0. Requests to `/health`, `/ready`, and `/stats` do not count, so a monitor does not keep the server alive. While a request is in progress, the count does not start. If the value is 0 (the default), the server does not stop for this reason.

In the profit and loss plan (large), a read of 1 cell through HTTP takes 0.24 ms. A write that changes the salary of 1 employee takes 0.9 ms.
A write while 8 readers read continuously takes 2.2 ms (`bench_http.py`, local loopback, about 2,800 reads per second).
At start, the server sets the Python thread switch interval to 0.5 ms (`--switch-interval`).
