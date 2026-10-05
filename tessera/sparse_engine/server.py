"""薄い HTTP サーバー。Workspace を JSON の API で公開する。

    python -m sparse_engine.server plan/ --port 8080 --engine rust --checkpoint-every 1000

読み出しは公開中の版に対して行い、必要な分だけ読む（get、slice、rows、summarize）。
--follow なら書き込まず、記録先に追従する読み出し専用のサーバーになる（workspace.Replica。読み手を複数の
プロセスに増やすとき。書き込みは 405）。
書き込みは 1 つの要求を 1 つのトランザクションとして Workspace に渡す。再送しても二重に確定しないよう、
書き込みには client_op_id を必ず付ける。読んだ版の通し番号を expect に付けると、その後に同じセルを
変えた書き込みがあれば 409 で拒否する（楽観的な排他）。

同じモデルを複数のサーバーで開くと、記録先の書き込みの権利（リース）を持つ 1 つが書き手（leader）になり、
ほかは待機系（standby）として追従する（Workspace(standby=True)）。待機系は読み出しを受け、書き込みは
421 と書き手の番地（leader）で拒む。書き手が止まれば（SIGTERM で権利を手放す、落ちて期限が切れる）
待機系の 1 つが権利を取って書き手になる。ほかのプロセスに知らせる自分の番地は --advertise で決める
（既定は http://<host>:<port>。0.0.0.0 で待ち受けるときは必須）。リースの期限は --lease-ttl（秒）。

    GET  /                                    The model (dimensions, Metrics) and the seq of the published version
    GET  /health                              生きているか（通し番号と役割 role。認証なしで読める）
    GET  /ready                               要求を受けられるか（受けられなければ 503 と理由。役割 role も返す。
                                              認証なしで読める）
    GET  /stats                               観察用の数（Prometheus のテキスト形式）
    GET  /metrics/<uuid>/cell?<dim uuid>=<member uuid>   1 cell ({"value": ..., "seq": ...})
    GET  /metrics/<uuid>/slice?<dim uuid>=a,b            A range ({"dims": [...], "cells": [[coordinates..., value], ...]})
    GET  /metrics/<uuid>/rows?<dim uuid>=a&offset=0&limit=50   A page of rows and the total number of rows
    GET  /metrics/<uuid>/summary?keep=<dim uuid>&agg=sum&<dim uuid>=a,b   Aggregation
    GET  /metrics/<uuid>/overrides?<dim uuid>=a,b        The cells that override the formula of an overridable Metric
                                              (the same body as slice)
    GET  /operations/<client_op_id>           The result of a write: committed (seq) or rejected (status, body)
    POST /writes                              {"client_op_id", "reason", "expect", "ops": [...]}

Each reference in a path, a query, or an op is a UUID (docs/ids.md). The Model API also takes and gives UUIDs,
so the server checks the shape of a request and passes the ids through. Each element of ops is {"op": <operation name>, <argument>:
<value>, ...}. _OPS gives the arguments of each operation. WRITE_OPS lists the permitted operations. Give the cells
of add_input as [[coordinates, value], ...].

The server records a rejected write (400, or 409 duplicate_id) with its client_op_id. If the same client_op_id
comes again, it returns the same answer (a conflict 409 is not recorded: the client plans again).

応答は JSON。失敗は {"error": 種類, "message": 文言} で、400（式や引数の誤り）、401（認証）、404、
409（Conflict）、413（本文や読み出しが大きすぎる）、421（待機系。leader に書き手の番地）、429（Overloaded）、
503（閉じている、混んでいる）を使う。
500 の文言は固定で、原因はサーバーのログに error_id と一緒に残す。

Authentication, not the request body, sets the user that the audit records (user).

- With tokens ({token: user}), the server requires Authorization: Bearer <token>.
- With user_header (for example, X-Forwarded-User), the value of that header is the user. Use this
  behind an authenticating proxy that sets the header again. The server trusts the header only on a
  connection from an address in trusted_proxies (CIDR). On other connections, it returns 401.
- With neither, the server does no authentication and the user is None (for local development).
  Then main refuses to listen on an address other than 127.0.0.1.

大きさの上限: 本文は max_body バイト、slice、rows、summary で返すセルは max_cells、同時に処理する
要求は max_threads（超えたら 503）。要求の読み書きが request_timeout 秒止まれば接続を切る。

SIGTERM と SIGINT で、受け付けた要求を処理し終え、列の書き込みを確定させ、リースを手放してから止まる
（ECS などのコンテナは SIGTERM で止める）。
With --idle-exit SECONDS, the server stops on the same path when no request comes for that time.
Requests to /health, /ready, and /stats do not count, so a monitor does not keep the server alive.

標準ライブラリの HTTP サーバーで、要求ごとにスレッドを作る。読み手が多いプロセスでは
--switch-interval で Python のスレッド切り替えの間隔を短くする（docs/performance.md）。
"""
from __future__ import annotations

import argparse
import hmac
import inspect
import ipaddress
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.parse
import uuid
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .core import Dimension
from .evaluate import FormulaError
from .journal import AlreadyCommitted, Stale
from .model import DuplicateId, Metric
from .parser import to_formula
from .workspace import Conflict, NotLeader, Overloaded, Rejected, Replica, Role, Workspace, rejection_of

log = logging.getLogger(__name__)

WRITE_OPS = frozenset({"set_cell", "spread", "add_member", "move_member", "rename_member", "remove_member", "add_formula",
                       "add_input", "add_property", "set_property_values", "add_dimension", "remove_metric",
                       "rename_metric", "rename_dimension", "rename_property"})
READ_PARAMS = frozenset({"offset", "limit", "keep", "agg"})


class ApiError(Exception):
    def __init__(self, status: int, kind: str, message: str, **extra):
        super().__init__(message)
        self.status, self.kind, self.extra = status, kind, extra

    @property
    def body(self) -> dict:
        return {"error": self.kind, "message": str(self), **self.extra}


class Names:
    """Checks the UUIDs of a request against the model before they go to the Model API.

    Each method that takes a UUID raises ValueError (400) if the model does not have the object."""

    def __init__(self, model):
        self.m = model

    def dim(self, id) -> Dimension:
        d = self.m.dimensions.get(id) if isinstance(id, str) else None
        if d is None:
            raise ValueError(f"軸 {id} がない")
        return d

    def metric(self, id) -> Metric:
        m = self.m.metrics.get(id) if isinstance(id, str) else None
        if m is None or m.name.startswith("__"):
            raise ValueError(f"Metric {id} がない")
        return m

    def member(self, d: Dimension, id) -> str:
        """The member id (checked)."""
        if not isinstance(id, str) or id not in d._by_id:
            raise ValueError(f"{d.name} にメンバー {id} がない")
        return id

    def prop(self, d: Dimension, id) -> str:
        """The property id (the Model takes it in place of the name)."""
        if not isinstance(id, str) or id not in d.properties:
            raise ValueError(f"{d.name} にプロパティ {id} がない")
        return id

    def kind(self, kind) -> str:
        """The kind (checked: the dimension of "member:<dim uuid>" must exist)."""
        if isinstance(kind, str) and kind.startswith("member:"):
            self.dim(kind.removeprefix("member:"))
        return kind

    def value_dim(self, m: Metric) -> Dimension | None:
        """The dimension of the values of a member-type Metric, or None."""
        return self.m.dimension(m.kind.removeprefix("member:")) if m.kind.startswith("member:") else None

    def value_in(self, m: Metric, value):
        """A value of a write (checked: a member-type value is a member UUID of the value dimension)."""
        d = self.value_dim(m)
        return value if d is None or value is None else self.member(d, value)

    def coords_in(self, coords) -> dict[str, Any]:
        """{dim uuid: member uuid (or a list of them)}, checked."""
        if not isinstance(coords, dict):
            raise ValueError("座標は {軸の ID: メンバーの ID} のオブジェクト")
        out = {}
        for k, v in coords.items():
            d = self.dim(k)
            out[d.id] = [self.member(d, x) for x in v] if isinstance(v, list) else self.member(d, v)
        return out


def _cube_out(cube) -> dict:
    """A Cube of the Model (dimension ids, member ids) as the body of a read."""
    return {"dims": list(cube.dims), "cells": _rows_out(cube.cells.items())}


def _rows_out(rows) -> list:
    """[(coordinates, value), ...] -> [[member uuid..., value], ...]."""
    return [[*k, v] for k, v in rows]


def _coords(query: dict[str, list[str]]) -> dict[str, Any]:
    """<dim uuid>=<member uuid>[,<member uuid>...] -> {dim uuid: member uuid or a list of them}."""
    out = {}
    for k, vs in query.items():
        if k in READ_PARAMS:
            continue
        members = [m for v in vs for m in v.split(",")]
        out[k] = members[0] if len(members) == 1 else members
    return out


def _int(query: dict[str, list[str]], key: str, default):
    if key not in query:
        return default
    try:
        return int(query[key][0])
    except ValueError:
        raise ApiError(400, "bad_request", f"{key} は整数") from None


def apply_ops(model, ops: list) -> None:
    """Apply the write operations to the Model in sequence. Each op is {"op": <name>, <argument>: <value>, ...}.
    The arguments refer to objects by UUID (docs/ids.md). An error in the shape or the arguments is a
    ValueError (400)."""
    names = Names(model)
    for op in ops:
        if not isinstance(op, dict):
            raise ValueError(f"操作は {{\"op\": 操作名, 引数...}} の形（{op!r}）")
        name = op.get("op")
        if name not in WRITE_OPS:
            raise ValueError(f"操作 {name!r} は使えない（{', '.join(sorted(WRITE_OPS))}）")
        args = {k: v for k, v in op.items() if k != "op"}
        fn = _OPS[name]
        try:
            inspect.signature(fn).bind(model, names, **args)
        except TypeError as e:
            raise ValueError(f"{name}: 引数が合わない（{e}）") from None
        fn(model, names, **args)


def _dims(n: Names, dims) -> list[str]:
    if not isinstance(dims, list):
        raise ValueError("dims は軸の ID の配列")
    return [n.dim(d).id for d in dims]


def _cell_map(n: Names, m: Metric, cells) -> dict:
    """[[[member uuid...], value], ...] -> {(member uuid, ...): value}, checked."""
    if not isinstance(cells, list) or not all(isinstance(c, list) and len(c) == 2 and isinstance(c[0], list)
                                              for c in cells):
        raise ValueError("add_input の cells は [[座標の列, 値], ...]")
    ds = [n.m.dimension(d) for d in m.dims]
    out = {}
    for key, value in cells:
        if len(key) != len(ds):
            raise ValueError(f"{m.name}: キー {key} の長さが軸 {m.dims} と合わない")
        out[tuple(n.member(d, x) for d, x in zip(ds, key))] = n.value_in(m, value)
    return out


def _op_add_dimension(m, n, id, name, ordered=False):
    m.add_dimension(name, [], ordered=ordered, id=id)


def _op_add_member(m, n, dim, id, name, at=None):
    m.add_member(n.dim(dim).id, name, at=at, id=id)


def _op_rename_member(m, n, dim, id, name):
    d = n.dim(dim)
    m.rename_member(d.id, n.member(d, id), name)


def _op_move_member(m, n, dim, id, at):
    d = n.dim(dim)
    m.move_member(d.id, n.member(d, id), at)


def _op_remove_member(m, n, dim, id):
    d = n.dim(dim)
    m.remove_member(d.id, n.member(d, id))


def _op_rename_dimension(m, n, id, name):
    m.rename_dimension(n.dim(id).id, name)


def _op_add_property(m, n, dim, id, name, target):
    m.add_property(n.dim(dim).id, name, n.dim(target).id, {}, id=id)


def _op_rename_property(m, n, dim, id, name):
    d = n.dim(dim)
    m.rename_property(d.id, n.prop(d, id), name)


def _op_set_property_values(m, n, dim, prop, values):
    d = n.dim(dim)
    p = n.prop(d, prop)
    t = m.dimension(d.properties[p][0])
    if not isinstance(values, dict):
        raise ValueError("values は {メンバーの ID: メンバーの ID か null}")
    m.set_property_values(d.id, p, {n.member(d, k): None if v is None else n.member(t, v) for k, v in values.items()})


def _op_add_input(m, n, id, name, dims, kind="number", cells=None, partition=None):
    dims, kind = _dims(n, dims), n.kind(kind)
    if partition is not None:
        partition = n.dim(partition).id
    cells = _cell_map(n, Metric(name, tuple(dims), kind), cells or [])
    m.add_input(name, dims, cells, kind=kind, partition=partition, id=id)


def _op_add_formula(m, n, id, name, formula, dims, kind="number", overridable=False, partition=None):
    if partition is not None:
        partition = n.dim(partition).id
    m.add_formula(name, _dims(n, dims), formula, kind=n.kind(kind), overridable=overridable, partition=partition, id=id)


def _op_rename_metric(m, n, id, name):
    m.rename_metric(n.metric(id).id, name)


def _op_remove_metric(m, n, id):
    m.remove_metric(n.metric(id).id)


def _target(m, n, metric, override: bool) -> Metric:
    """The Metric that a set_cell or spread writes: with override, the hidden override input of a formula Metric."""
    mt = n.metric(metric)
    if not override:
        return mt
    if not mt.overridable:
        raise ValueError(f"{mt.name} は上書きできる計算 Metric ではない")
    return m.metrics[mt.override]


def _op_set_cell(m, n, metric, value, coords=None, override=False):
    mt = _target(m, n, metric, override)
    m.set_cell(mt.id, n.value_in(mt, value), n.coords_in(coords or {}))


def _op_spread(m, n, metric, total, how="proportional", where=None, coords=None):
    mt = n.metric(metric)
    named = None
    if where is not None:
        if not isinstance(where, dict):
            raise ValueError("where は {\"軸の ID.プロパティの ID\": メンバーの ID}")
        named = {}
        for path, value in where.items():
            d = n.dim(path.partition(".")[0])
            p = n.prop(d, path.partition(".")[2])
            named[f"{d.id}.{p}"] = n.member(m.dimension(d.properties[p][0]), value)
    m.spread(mt.id, total, n.coords_in(coords or {}), how=how, where=named)


_OPS = {name[4:]: fn for name, fn in list(globals().items()) if name.startswith("_op_")}
assert set(_OPS) == WRITE_OPS


PROBES = {"health", "ready", "stats"}  # monitors call these; they do not count as use (--idle-exit)


def is_probe(path: str) -> bool:
    """Return True if the request path is a probe. It must not raise: do_GET calls it in a finally."""
    return path.split("?", 1)[0].strip("/") in PROBES


def misdirected(header: str | None, model_id: str | None) -> bool:
    """Return True if the router sent the request for a different model (X-Nanashi-Model)."""
    return header is not None and model_id is not None and header != model_id


def idle_for(active: int, last: float, now: float) -> float:
    """Return the seconds without use. It is 0 while a request is in progress."""
    return 0.0 if active else max(0.0, now - last)


class Handler(BaseHTTPRequestHandler):
    server: "Server"

    def setup(self) -> None:
        self.timeout = self.server.request_timeout  # 読み書きが止まった接続でスレッドを塞がない
        super().setup()

    def log_message(self, fmt, *args):  # 標準の出力を logging に寄せる
        log.debug("%s " + fmt, self.address_string(), *args)

    # ------------------------------------------------ 応答

    def _json(self, status: int, body: Any) -> None:
        if isinstance(body, _Text):
            data, kind = body.encode("utf-8"), "text/plain; version=0.0.4; charset=utf-8"
        else:
            data, kind = json.dumps(body, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _fail(self, e: ApiError) -> None:
        self._json(e.status, e.body)

    def _run(self, fn) -> None:
        try:
            status, body = fn()
        except ApiError as e:
            self._fail(e)
        except (DuplicateId, FormulaError, ValueError) as e:  # A bad request (Model raises ValueError for a user error)
            self._json(*rejection_of(e))
        except Exception:  # それ以外は内部の誤り。中身は応答に出さず、ログで引けるように ID を付ける
            error_id = uuid.uuid4().hex[:12]
            log.exception("要求の処理に失敗した（error_id=%s）", error_id)
            self._fail(ApiError(500, "internal", "内部エラー", error_id=error_id))
        else:
            self._json(status, body)

    # ------------------------------------------------ 読み出し

    def _misdirected(self) -> bool:
        """Answer 421 without a leader and return True if the request names a different model.
        The router then forgets this address and finds the writer of that model again."""
        header = self.headers.get("X-Nanashi-Model")
        if not misdirected(header, self.server.model_id):
            return False
        self._fail(ApiError(421, "not_leader", f"このエンジンはモデル {self.server.model_id} のもの（{header} ではない）"))
        return True

    def do_GET(self) -> None:
        if self._misdirected():
            return
        try:
            self._run(self._get)
        finally:
            if not is_probe(self.path):
                self.server.touch()

    def _user(self) -> str | None:
        """Return the authenticated user, or None without an authentication setting. Raise 401 on failure."""
        srv = self.server
        if srv.user_header is not None:
            # Do not fall back to the header: a client that bypasses the proxy can set any user.
            if not srv.trusted(self.client_address[0]):
                raise ApiError(401, "unauthorized", "信頼するプロキシ（trusted_proxies）からの接続ではない")
            user = self.headers.get(srv.user_header)
            if not user:
                raise ApiError(401, "unauthorized", f"{srv.user_header} がない")
            return user
        if srv.tokens is not None:
            scheme, _, token = (self.headers.get("Authorization") or "").partition(" ")
            if scheme.lower() == "bearer":
                for known, user in srv.tokens.items():
                    if hmac.compare_digest(known.encode(), token.strip().encode()):
                        return user
            raise ApiError(401, "unauthorized", "Authorization: Bearer <トークン> が要る")
        return None

    def _get(self) -> tuple[int, Any]:
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query, keep_blank_values=True)
        parts = [p for p in url.path.split("/") if p]
        v = self.server.workspace.version
        if parts == ["health"]:
            return 200, {"seq": v.seq, "role": role_of(self.server.workspace)}
        if parts == ["ready"]:
            reasons = self.server.workspace.ready()
            return (503 if reasons else 200), {"seq": v.seq, "ready": not reasons, "role": role_of(self.server.workspace),
                                               "reasons": reasons}
        self._user()
        if parts == ["stats"]:
            return 200, _Text(stats_text(self.server.workspace))
        n = Names(v)
        if not parts:
            return 200, {"seq": v.seq, "dimensions": _dimensions_out(n), "metrics": _metrics_out(n)}
        if len(parts) == 2 and parts[0] == "operations":
            result = self.server.workspace.operation(urllib.parse.unquote(parts[1]))
            if result is None:
                raise ApiError(404, "unknown_operation", "この操作は知らない（覚えておく範囲の外か、まだ来ていない）")
            return 200, result
        if len(parts) == 3 and parts[0] == "metrics":
            try:
                m = n.metric(urllib.parse.unquote(parts[1]))
            except ValueError as e:
                raise ApiError(404, "not_found", str(e)) from None
            what = parts[2]
            if what == "overrides":  # Read the hidden "__override__" input through its formula Metric.
                if not m.overridable:
                    raise ApiError(400, "bad_request", f"{m.name} は上書きできる計算 Metric ではない")
                m, what = v.metrics[m.override], "slice"
            coords = n.coords_in(_coords(query))
            if what == "cell":
                bad = [k for k, x in coords.items() if not isinstance(x, str)]
                if bad:
                    raise ApiError(400, "bad_request", f"cell では軸 {bad} に 1 つのメンバーを指定する")
                return 200, {"seq": v.seq, "value": v.get(m.id, coords)}
            limit = self.server.max_cells
            if what == "slice":
                count = v.summarize(m.id, coords, agg="count").cells.get((), 0)  # Count the cells before the full read
                if count > limit:
                    raise ApiError(413, "too_large", f"範囲のセルが {count:,} 件あり、上限 {limit:,} を超える（rows でページングする）")
                return 200, {"seq": v.seq, **_cube_out(v.slice(m.id, coords))}
            if what == "rows":
                page = _int(query, "limit", limit)
                if page > limit:
                    raise ApiError(400, "bad_request", f"limit は {limit:,} まで")
                rows, total = v.rows(m.id, coords, offset=_int(query, "offset", 0), limit=page)
                return 200, {"seq": v.seq, "dims": list(m.dims), "rows": _rows_out(rows), "total": total}
            if what == "summary":
                keep = [n.dim(d).id for x in query.get("keep", []) for d in x.split(",") if d]
                groups = 1
                for d in keep:
                    if d in m.dims:
                        c = coords.get(d)
                        groups *= len(v.dimension(d).members) if c is None else 1 if isinstance(c, str) else len(c)
                if groups > limit:
                    raise ApiError(413, "too_large", f"集計の結果が最大 {groups:,} 件になり、上限 {limit:,} を超える")
                cube = v.summarize(m.id, coords, keep=keep, agg=query.get("agg", ["sum"])[0])
                return 200, {"seq": v.seq, **_cube_out(cube)}
        raise ApiError(404, "not_found", f"{url.path} はない")

    # ------------------------------------------------ 書き込み

    def do_POST(self) -> None:
        if self._misdirected():
            return
        try:
            self._run(self._post)
        finally:
            self.server.touch()

    def _post(self) -> tuple[int, Any]:
        url = urllib.parse.urlsplit(self.path)
        if [p for p in url.path.split("/") if p] != ["writes"]:
            raise ApiError(404, "not_found", f"{url.path} はない")
        user = self._user()
        if isinstance(self.server.workspace, Replica):
            raise ApiError(405, "read_only", "このサーバーは記録先に追従する読み出し専用（書き込みは書き手のサーバーへ送る）")
        length = self.headers.get("Content-Length")
        if length is None:
            raise ApiError(411, "length_required", "Content-Length が要る")
        try:
            length = int(length)
        except ValueError:
            raise ApiError(400, "bad_request", "Content-Length は整数") from None
        if length < 0 or length > self.server.max_body:
            self.close_connection = True  # 読まずに返すので、残りの本文を次の要求と取り違えない
            raise ApiError(413, "too_large", f"本文は {self.server.max_body:,} バイトまで")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise ApiError(400, "bad_request", "本文が JSON ではない") from None
        if not isinstance(body, dict) or not isinstance(body.get("ops"), list) or not body["ops"]:
            raise ApiError(400, "bad_request", "ops（操作の列）が要る")
        if "user" in body:
            raise ApiError(400, "bad_request", "user は認証で決まる（本文では指定しない）")
        if not isinstance(body.get("client_op_id"), str) or not body["client_op_id"]:
            raise ApiError(400, "bad_request", "client_op_id（再送しても二重に確定しないための ID）が要る")
        ops, client_op_id = body["ops"], body["client_op_id"]
        try:
            seq = self.server.workspace.write(
                lambda m: apply_ops(m, ops), user=user, reason=body.get("reason"),
                client_op_id=client_op_id, expect=body.get("expect"), timeout=self.server.write_timeout)
        except Rejected as e:
            return e.status, e.body
        # _run answers a terminal rejection (DuplicateId, FormulaError, ValueError). The writer recorded it already
        except Conflict as e:
            raise ApiError(409, "conflict", str(e), seq=e.seq, user=e.user) from None
        except NotLeader as e:
            raise ApiError(421, "not_leader", str(e), leader=e.leader) from None
        except AlreadyCommitted as e:
            return 200, {"seq": e.seq, "resent": True}
        except Overloaded as e:
            raise ApiError(429, "overloaded", str(e)) from None
        except TimeoutError:
            raise ApiError(504, "timeout", "確定を待つ時間を過ぎた（再送すれば二重には確定しない）") from None
        except Stale as e:
            raise ApiError(503, "stale", f"別のプロセスが書き込んだので開き直した。読み直して再送する: {e}") from None
        except RuntimeError as e:
            raise ApiError(503, "closed", str(e)) from None
        return 200, {"seq": seq}


def _dimensions_out(n: Names) -> dict:
    """The dimensions of GET /, keyed by UUID."""
    out = {}
    for d in n.m.dimensions.values():
        props, values = {}, {}
        for pid, (t, mapping) in d.properties.items():
            props[pid] = {"name": d.property_names[pid], "target": t}
            values[pid] = dict(mapping)  # the map holds member UUIDs
        out[d.id] = {"name": d.name, "ordered": d.ordered,
                     "members": [{"id": d.ids[i], "name": d.members[i]} for i in d.order()],
                     "properties": props, "property_values": values}
    return out


def _metrics_out(n: Names) -> dict:
    """The Metrics of GET /, keyed by UUID. Names starting with "__" stay hidden."""
    return {m.id: {"name": m.name, "dims": list(m.dims), "kind": m.kind,
                   "overridable": m.overridable,
                   "formula": None if m.written is None else to_formula(m.written, n.m)}
            for m in n.m.metrics.values() if not m.name.startswith("__")}


class _Text(str):
    """JSON でなく、テキストのまま返す応答の本文。"""


def role_of(ws) -> str:
    """このプロセスの役割（leader、standby、--follow なら follower）。"""
    return "follower" if isinstance(ws, Replica) else ws.role.value


def stats_text(ws) -> str:
    """Workspace か Replica の観察用の数を、Prometheus のテキスト形式にする。"""
    st, now = ws.stats, time.monotonic()
    rows = [("nanashi_seq", "公開中の版の通し番号", "gauge", ws.seq),
            ("nanashi_ready", "要求を受けられるか", "gauge", int(not ws.ready()))]
    if isinstance(ws, Workspace):
        lease = ws.journal.lease() if ws.journal is not None else {"held": False, "expires_in": None}
        rows += [
            ("nanashi_leader", "書き手か（書き込みを受けるか）", "gauge", int(ws.role is Role.LEADER)),
            ("nanashi_commits_total", "確定した書き込み", "counter", st.commits),
            ("nanashi_commit_batches_total", "記録の書き出し（まとめて確定した回数）", "counter", st.batches),
            ("nanashi_commit_seconds_sum", "記録の書き出しと確定にかかった時間の合計", "counter", st.commit_seconds),
            ("nanashi_commit_seconds_max", "記録の書き出しと確定にかかった時間の最大", "gauge", st.commit_seconds_max),
            ("nanashi_rejected_total", "失敗して取り消した書き込み", "counter", st.rejected),
            ("nanashi_journal_errors_total", "記録の書き出しに失敗したまとまり", "counter", st.journal_errors),
            ("nanashi_queue_length", "書き込みの列に待っている数", "gauge", ws.queued()),
            ("nanashi_lease_held", "書き込みの権利を持っているか", "gauge", int(lease["held"])),
            ("nanashi_lease_expires_seconds", "書き込みの権利の残りの秒数", "gauge", lease["expires_in"]),
            ("nanashi_catch_ups_total", "ほかのプロセスの書き込みに追いついた回数", "counter", st.catch_ups),
            ("nanashi_reopen_failures_total", "開き直しに失敗した回数", "counter", st.reopen_failures),
            ("nanashi_snapshots_total", "置いたスナップショット", "counter", st.snapshots),
            ("nanashi_snapshot_failures_total", "置けなかったスナップショット", "counter", st.snapshot_failures),
            ("nanashi_snapshot_age_seconds", "最後にスナップショットを置いてからの秒数", "gauge",
             None if st.snapshot_at is None else now - st.snapshot_at),
            ("nanashi_snapshot_seq", "最後のスナップショットの通し番号", "gauge", st.snapshot_seq),
        ]
    else:
        rows += [("nanashi_catch_ups_total", "記録先に追いついた回数", "counter", st.catch_ups),
                 ("nanashi_replica_lag", "記録先の最後の記録から遅れている数", "gauge", ws.lag())]
    out = []
    for name, help_, kind, value in rows:
        if value is None:
            continue
        out += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}", f"{name} {value}"]
    return "\n".join(out) + "\n"


class Server(ThreadingHTTPServer):
    """An HTTP server that publishes a Workspace. Run serve_forever in a different thread, or use start().
    To open the workspace after the server binds its address, give None and set it before serve_forever.
    user_header needs trusted_proxies: the CIDRs (or single addresses) of the proxies that set the header."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, workspace: Workspace | Replica | None, host: str = "127.0.0.1", port: int = 8080, *,
                 write_timeout: float | None = 30.0, tokens: dict[str, str] | None = None,
                 user_header: str | None = None, trusted_proxies: Iterable[str] = (), max_body: int = 16 << 20,
                 max_cells: int = 100_000, max_threads: int = 64, request_timeout: float | None = 30.0,
                 model_id: str | None = None):
        if tokens is not None and user_header is not None:
            raise ValueError("tokens と user_header はどちらか一方")
        trusted = parse_networks(trusted_proxies)
        if user_header is not None and not trusted:
            raise ValueError("user_header には trusted_proxies（見出しを付けるプロキシの番地）が要る")
        if user_header is None and trusted:
            raise ValueError("trusted_proxies は user_header と一緒に使う")
        super().__init__((host, port), Handler)
        self.workspace = workspace
        self.write_timeout = write_timeout
        self.tokens, self.user_header, self.trusted_proxies = tokens, user_header, trusted
        self.max_body, self.max_cells, self.request_timeout = max_body, max_cells, request_timeout
        self.model_id = model_id  # with a PgJournal: refuse requests that X-Nanashi-Model sends for another model
        self._slots = threading.BoundedSemaphore(max_threads)
        self._thread: threading.Thread | None = None
        # Connections in progress (probes too) and the monotonic time of the last use (probes not).
        self._active, self._last, self._use = 0, time.monotonic(), threading.Lock()

    def trusted(self, host: str) -> bool:
        """Return True if host is in one of the trusted_proxies."""
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return False
        if getattr(addr, "scope_id", None):
            return False  # a link-local address with a zone; the router also refuses it
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped  # a dual-stack socket shows an IPv4 client as ::ffff:a.b.c.d
        return any(addr in net for net in self.trusted_proxies)

    def process_request(self, request, client_address) -> None:
        """同時に処理する要求が max_threads に達していれば、スレッドを作らずに 503 を返す。"""
        if not self._slots.acquire(blocking=False):
            body = json.dumps({"error": "busy", "message": "同時に処理できる要求の数を超えた"}).encode()
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                                b"Retry-After: 1\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        with self._use:
            self._active += 1
        # 枠を返すのは、スレッドを起こせなかったとき（Exception）だけにする。KeyboardInterrupt（Ctrl-C）は
        # スレッドを起こしたあとにも届き、そのスレッドも枠を返す。ここでも返すと 2 度返して ValueError になり、
        # socketserver がそれを握りつぶして止まらなくなる（止まるので、枠が 1 つ減っても困らない）
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            with self._use:
                self._active -= 1
            raise

    def process_request_thread(self, request, client_address) -> None:
        """ThreadingMixIn のものと同じだが、接続を閉じる前に枠を返す。

        閉じたのを見てすぐにつなぎ直した要求が、まだ返していない枠のせいで 503 にならないようにする。
        """
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self._slots.release()
            with self._use:
                self._active -= 1
            self.shutdown_request(request)

    def touch(self) -> None:
        with self._use:
            self._last = time.monotonic()

    def idle(self) -> float:
        with self._use:
            return idle_for(self._active, self._last, time.monotonic())

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> Server:
        self._thread = threading.Thread(target=self.serve_forever, name="nanashi-http", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join()
        self.workspace.close()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="nanashi の HTTP サーバー")
    ap.add_argument("path", help="記録先のディレクトリ（FileJournal）。--pg ならスナップショットと"
                    "大量の変更のファイルの置き場所（s3://<バケット>/<接頭辞> かディレクトリ）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--engine", choices=["rust", "reference"], default="rust")
    ap.add_argument("--pg", metavar="DSN", help="PostgreSQL の記録先を使う")
    ap.add_argument("--model-id", default="default", help="--pg のときのモデルの ID")
    ap.add_argument("--migrate", action="store_true", help="--pg のとき、開く前にスキーマを最新の版にする")
    ap.add_argument("--checkpoint-every", type=int, default=1000, help="この件数の記録ごとにスナップショットを取る")
    ap.add_argument("--max-queue", type=int, default=1000, help="書き込みの列の上限（溢れたら 429）")
    ap.add_argument("--switch-interval", type=float, default=0.0005,
                    help="Python のスレッド切り替えの間隔（秒）。読み手が多いときの書き込みの待ちを減らす")
    ap.add_argument("--tokens", metavar="FILE", help="{トークン: 利用者} の JSON。Bearer トークンで認証する")
    ap.add_argument("--user-header", metavar="NAME",
                    help="認証を済ませたプロキシが付ける、利用者の見出し（例: X-Forwarded-User）。--trusted-proxy が要る")
    ap.add_argument("--trusted-proxy", metavar="CIDR", action="append", default=[],
                    help="--user-header の見出しを信頼する送信元（例: 10.0.1.0/24、10.0.1.5）。複数なら繰り返す。"
                    "ほかの送信元には 401 を返す（127.0.0.1 も自動では信頼しない）")
    ap.add_argument("--insecure", action="store_true", help="認証なしで 127.0.0.1 以外でも待ち受ける")
    ap.add_argument("--max-body", type=int, default=16 << 20, help="本文の上限（バイト）")
    ap.add_argument("--max-cells", type=int, default=100_000, help="slice、rows、summary で返すセルの上限")
    ap.add_argument("--max-threads", type=int, default=64, help="同時に処理する要求の上限（超えたら 503）")
    ap.add_argument("--max-bytes", type=int, help="Rust のエンジンで、1 つの式の評価が持つ途中結果の上限（バイト）")
    ap.add_argument("--follow", action="store_true",
                    help="書き込まず、記録先に追従する読み出し専用のサーバーにする（読み手を増やすとき）")
    ap.add_argument("--advertise", metavar="URL",
                    help="--pg のとき、書き手としてほかのプロセスに知らせる自分の番地（既定は http://<host>:<port>）")
    ap.add_argument("--lease-ttl", type=float, default=30.0, help="--pg のとき、書き込みの権利（リース）の期限（秒）")
    ap.add_argument("--idle-exit", type=float, default=0.0, metavar="SECONDS",
                    help="この秒数のあいだ要求がなければ止まる（/health、/ready、/stats は数えない。0 なら止まらない）")
    args = ap.parse_args(argv)
    tokens = None
    if args.tokens:
        with open(args.tokens, encoding="utf-8") as f:
            tokens = json.load(f)
    if tokens is not None and args.user_header is not None:
        ap.error("--tokens と --user-header はどちらか一方")
    if args.user_header is not None and not args.trusted_proxy:
        ap.error("--user-header には、見出しを付けるプロキシの番地を --trusted-proxy で指定する")
    if args.trusted_proxy and args.user_header is None:
        ap.error("--trusted-proxy は --user-header と一緒に使う")
    try:
        parse_networks(args.trusted_proxy)
    except ValueError as e:
        ap.error(str(e))
    # --user-header always comes with --trusted-proxy (above), so it also allows a non-loopback address.
    if tokens is None and args.user_header is None and not args.insecure and not _loopback(args.host):
        ap.error(f"{args.host} で待ち受けるには --tokens か --user-header で認証する（試すだけなら --insecure）")
    if args.pg and not args.follow and args.advertise is None and _unspecified(args.host):
        ap.error(f"{args.host or '(空)'} で待ち受けるときは、ほかのプロセスに知らせる自分の番地を --advertise で指定する")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.setswitchinterval(args.switch_interval)
    if args.engine == "rust":
        from .rust_engine import RustEngine
        engine = RustEngine(max_bytes=args.max_bytes)
    else:
        from .engine import ReferenceEngine
        engine = ReferenceEngine()
    # 先に待ち受けて番地を決める（--port 0 でも、知らせる番地に実際の番号が入る）。要求は serve_forever まで受けない
    server = Server(None, args.host, args.port, tokens=tokens, user_header=args.user_header,
                    trusted_proxies=args.trusted_proxy, max_body=args.max_body, max_cells=args.max_cells,
                    max_threads=args.max_threads, model_id=args.model_id if args.pg else None)
    if args.pg:
        from .pg_journal import PgJournal, migrate
        if args.migrate:
            migrate(args.pg)
        advertise = args.advertise or f"http://{args.host}:{server.server_address[1]}"
        journal = PgJournal(args.pg, args.model_id, args.path, heartbeat=not args.follow, lease_ttl=args.lease_ttl,
                            endpoint=advertise)
    else:
        from .journal import FileJournal
        journal = FileJournal(args.path)
    if args.follow:
        ws = Replica(journal, engine)
    else:
        ws = Workspace.open(journal, engine, checkpoint_every=args.checkpoint_every, max_queue=args.max_queue,
                            standby=True)
    server.workspace = ws
    log.info("公開中の版 %d、%s で待ち受ける（%s）", ws.seq, server.url, role_of(ws))
    _stop_on_signal(server)
    if args.idle_exit > 0:
        _stop_when_idle(server, args.idle_exit)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        ws.close()
        if hasattr(journal, "close"):  # PgJournal: リースの延長を止めて、接続を閉じる
            journal.close()


def _stop_on_signal(server: Server) -> None:
    """SIGTERM と SIGINT で serve_forever を止めるようにする。本線のスレッドで、serve_forever の直前に呼ぶ。

    シグナルを KeyboardInterrupt にして本線のスレッドに投げると、どこで割り込むか選べない。要求のスレッドを
    起こしている途中（Thread.start の中のロック）に届くと RuntimeError に化け、socketserver がそれを握りつぶして
    止まらなくなっていた。そこで、受けたシグナルの番号をパイプに書くだけにし（ロックを取らないので、どこに
    割り込んでもよい）、見張りのスレッドがそれを読んで server.shutdown() を呼ぶ。止まるのは serve_forever の
    区切りになる。最初のシグナルで KeyboardInterrupt に戻すので、片付けが止まっても 2 度目のシグナルで抜けられる。
    起動の間（ここより前）は、今までどおり SIGINT は KeyboardInterrupt、SIGTERM は既定の動作で止まる。"""
    r, w = os.pipe()
    os.set_blocking(w, False)

    def on_signal(signum, frame) -> None:
        for s in (signal.SIGTERM, signal.SIGINT):
            signal.signal(s, signal.default_int_handler)
        try:
            os.write(w, bytes([signum]))
        except OSError:
            pass

    def watch() -> None:
        signum = os.read(r, 1)[0]
        log.info("%s を受けたので止める", signal.Signals(signum).name)
        server.shutdown()

    threading.Thread(target=watch, name="nanashi-signal", daemon=True).start()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, on_signal)


def _stop_when_idle(server: Server, limit: float) -> None:
    """Call server.shutdown() after limit seconds without use, or when the parent process (the router) is gone.
    This is the same path as SIGTERM."""
    server.touch()  # count from the start of serving; opening the workspace can take a long time
    parent = os.getppid()

    def watch() -> None:
        while server.idle() < limit:
            if os.getppid() != parent:
                log.info("親のプロセス（ルーター）が止まったので止める")
                break
            time.sleep(min(1.0, limit / 4))
        else:
            log.info("%g 秒のあいだ要求がなかったので止める", limit)
        server.shutdown()

    threading.Thread(target=watch, name="nanashi-idle", daemon=True).start()


def parse_networks(values: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse CIDRs. A single address is a network of 1 address. Host bits are set to zero.
    An IPv4-mapped network (::ffff:10.0.1.0/120) becomes the IPv4 network, as trusted() unmaps the client."""
    if isinstance(values, str):
        raise TypeError("trusted_proxies は番地（CIDR）の列で渡す")
    out = []
    for v in values:
        try:
            net = ipaddress.ip_network(v.strip(), strict=False)
        except ValueError:
            raise ValueError(f"{v!r} は番地（CIDR。例: 10.0.1.0/24）ではない") from None
        mapped = net.network_address.ipv4_mapped if isinstance(net, ipaddress.IPv6Network) else None
        if mapped is not None and net.prefixlen >= 96:
            net = ipaddress.IPv4Network((mapped, net.prefixlen - 96))
        out.append(net)
    return tuple(out)


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _unspecified(host: str) -> bool:
    """すべてのインターフェースで待ち受ける番地（0.0.0.0、::、空）。ほかのプロセスはこの番地ではつなげない。"""
    if not host:
        return True
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


if __name__ == "__main__":
    main()
