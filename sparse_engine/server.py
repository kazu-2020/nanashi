"""薄い HTTP サーバー。Workspace を JSON の API で公開する。

    python -m sparse_engine.server plan/ --port 8080 --engine rust --checkpoint-every 1000

読み出しは公開中の版に対して行い、必要な分だけ読む（get、slice、rows、summarize）。
--follow なら書き込まず、記録先に追従する読み出し専用のサーバーになる（workspace.Replica。読み手を複数の
プロセスに増やすとき。書き込みは 405）。
書き込みは 1 つの要求を 1 つのトランザクションとして Workspace に渡す。再送しても二重に確定しないよう、
書き込みには client_op_id を必ず付ける。読んだ版の通し番号を expect に付けると、その後に同じセルを
変えた書き込みがあれば 409 で拒否する（楽観的な排他）。

    GET  /                                    モデルの定義（軸、Metric）と公開中の版の通し番号
    GET  /health                              生きているか（通し番号。認証なしで読める）
    GET  /ready                               要求を受けられるか（受けられなければ 503 と理由。認証なしで読める）
    GET  /stats                               観察用の数（Prometheus のテキスト形式）
    GET  /metrics/<name>/cell?<軸>=<メンバー>   1 セル（{"value": ..., "seq": ...}）
    GET  /metrics/<name>/slice?<軸>=a,b        範囲（{"dims": [...], "cells": [[座標..., 値], ...]}）
    GET  /metrics/<name>/rows?<軸>=a&offset=0&limit=50   行の列と全行数
    GET  /metrics/<name>/summary?keep=Month&agg=sum&<軸>=a,b   集計
    POST /writes                              {"client_op_id", "reason", "expect", "ops": [...]}

ops の各要素は {"op": 操作名, "args": [...], "kwargs": {...}} で、Model の操作（set_cell、spread、
add_member、rename_member、remove_member、add_formula、add_input、add_property、add_dimension、
remove_metric、rename_metric）を順に呼ぶ。add_input の cells は [[座標の列, 値], ...] で渡す。

応答は JSON。失敗は {"error": 種類, "message": 文言} で、400（式や引数の誤り）、401（認証）、404、
409（Conflict）、413（本文や読み出しが大きすぎる）、429（Overloaded）、503（閉じている、混んでいる）を使う。
500 の文言は固定で、原因はサーバーのログに error_id と一緒に残す。

利用者（監査に残す user）は、要求の本文ではなく認証で決める。

- tokens（{トークン: 利用者}）を渡すと、Authorization: Bearer <トークン> を求める
- user_header（例: X-Forwarded-User）を渡すと、その見出しの値を利用者にする（認証を済ませた
  プロキシの後ろに置くとき。プロキシがこの見出しを付け直すこと）
- どちらもなければ認証せず、利用者は None（手元の開発用。main は 127.0.0.1 以外で待ち受けるのを拒む）

大きさの上限: 本文は max_body バイト、slice、rows、summary で返すセルは max_cells、同時に処理する
要求は max_threads（超えたら 503）。要求の読み書きが request_timeout 秒止まれば接続を切る。

標準ライブラリの HTTP サーバーで、要求ごとにスレッドを作る。読み手が多いプロセスでは
--switch-interval で Python のスレッド切り替えの間隔を短くする（README の「性能」）。
"""
from __future__ import annotations

import argparse
import hmac
import inspect
import ipaddress
import json
import logging
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .evaluate import FormulaError
from .journal import AlreadyCommitted, Stale
from .workspace import Conflict, Overloaded, Replica, Workspace

log = logging.getLogger(__name__)

WRITE_OPS = frozenset({"set_cell", "spread", "add_member", "rename_member", "remove_member", "add_formula",
                       "add_input", "add_property", "add_dimension", "remove_metric", "rename_metric"})
READ_PARAMS = frozenset({"offset", "limit", "keep", "agg"})


class ApiError(Exception):
    def __init__(self, status: int, kind: str, message: str, **extra):
        super().__init__(message)
        self.status, self.kind, self.extra = status, kind, extra


def _coords(query: dict[str, list[str]]) -> dict[str, Any]:
    """軸=メンバー（コンマ区切りなら複数）の絞り込み。"""
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


def _cells(cube) -> list:
    return [[*k, v] for k, v in cube.cells.items()]


def apply_ops(model, ops: list) -> None:
    """書き込みの操作を順に Model に適用する。操作の形と引数の誤りは ValueError（400）にする。"""
    for op in ops:
        if not isinstance(op, dict):
            raise ValueError(f"操作は {{\"op\", \"args\", \"kwargs\"}} の形（{op!r}）")
        name = op.get("op")
        if name not in WRITE_OPS:
            raise ValueError(f"操作 {name!r} は使えない（{', '.join(sorted(WRITE_OPS))}）")
        args, kwargs = op.get("args", []), op.get("kwargs", {})
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise ValueError(f"{name}: args は配列、kwargs はオブジェクト")
        args, kwargs = list(args), dict(kwargs)
        if name == "add_input":  # cells は [[座標の列, 値], ...] で来る
            if len(args) >= 3:
                args[2] = _cell_map(args[2])
            if "cells" in kwargs:
                kwargs["cells"] = _cell_map(kwargs["cells"])
        fn = getattr(model, name)
        try:
            inspect.signature(fn).bind(*args, **kwargs)
        except TypeError as e:
            raise ValueError(f"{name}: 引数が合わない（{e}）") from None
        fn(*args, **kwargs)


def _cell_map(cells) -> dict:
    if not isinstance(cells, list) or not all(isinstance(c, list) and len(c) == 2 and isinstance(c[0], list)
                                              for c in cells):
        raise ValueError("add_input の cells は [[座標の列, 値], ...]")
    return {tuple(k): v for k, v in cells}


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
        self._json(e.status, {"error": e.kind, "message": str(e), **e.extra})

    def _run(self, fn) -> None:
        try:
            status, body = fn()
        except ApiError as e:
            self._fail(e)
        except FormulaError as e:
            self._fail(ApiError(400, "formula", str(e)))
        except ValueError as e:  # 引数の誤り（Model は利用者の誤りを ValueError にする）
            self._fail(ApiError(400, "bad_request", str(e)))
        except Exception:  # それ以外は内部の誤り。中身は応答に出さず、ログで引けるように ID を付ける
            error_id = uuid.uuid4().hex[:12]
            log.exception("要求の処理に失敗した（error_id=%s）", error_id)
            self._fail(ApiError(500, "internal", "内部エラー", error_id=error_id))
        else:
            self._json(status, body)

    # ------------------------------------------------ 読み出し

    def do_GET(self) -> None:
        self._run(self._get)

    def _user(self) -> str | None:
        """認証した利用者。認証の設定がなければ None。認証できなければ 401。"""
        srv = self.server
        if srv.user_header is not None:
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
            return 200, {"seq": v.seq}
        if parts == ["ready"]:
            reasons = self.server.workspace.ready()
            return (503 if reasons else 200), {"seq": v.seq, "ready": not reasons, "reasons": reasons}
        self._user()
        if parts == ["stats"]:
            return 200, _Text(stats_text(self.server.workspace))
        if not parts:
            dims = {d.name: {"id": d.id, "members": list(d.members), "ordered": d.ordered,
                             "properties": {p: t for p, (t, _) in d.properties.items()}}
                    for d in v.dimensions.values()}
            metrics = {m.name: {"id": m.id, "dims": list(m.dims), "kind": m.kind, "overridable": m.overridable,
                                "formula": None if m.written is None else _formula(m.written)}
                       for m in v.metrics.values() if not m.name.startswith("__")}
            return 200, {"seq": v.seq, "dimensions": dims, "metrics": metrics}
        if len(parts) == 3 and parts[0] == "metrics":
            name, what = urllib.parse.unquote(parts[1]), parts[2]
            if name not in v.metrics or name.startswith("__"):
                raise ApiError(404, "not_found", f"Metric {name} がない")
            coords = _coords(query)
            if what == "cell":
                bad = [k for k, x in coords.items() if not isinstance(x, str)]
                if bad:
                    raise ApiError(400, "bad_request", f"cell では軸 {bad} に 1 つのメンバーを指定する")
                return 200, {"seq": v.seq, "value": v.get(name, **coords)}
            limit = self.server.max_cells
            if what == "slice":
                n = v.summarize(name, agg="count", **coords).cells.get((), 0)  # 丸ごと読む前に数える
                if n > limit:
                    raise ApiError(413, "too_large", f"範囲のセルが {n:,} 件あり、上限 {limit:,} を超える（rows でページングする）")
                cube = v.slice(name, **coords)
                return 200, {"seq": v.seq, "dims": list(cube.dims), "cells": _cells(cube)}
            if what == "rows":
                page = _int(query, "limit", limit)
                if page > limit:
                    raise ApiError(400, "bad_request", f"limit は {limit:,} まで")
                rows, total = v.rows(name, offset=_int(query, "offset", 0), limit=page, **coords)
                return 200, {"seq": v.seq, "dims": list(v.metrics[name].dims), "rows": [[*k, x] for k, x in rows],
                             "total": total}
            if what == "summary":
                keep = [d for x in query.get("keep", []) for d in x.split(",") if d]
                groups = 1
                for d in keep:
                    if d in v.metrics[name].dims:
                        c = coords.get(d)
                        groups *= len(v.dimension(d).members) if c is None else 1 if isinstance(c, str) else len(c)
                if groups > limit:
                    raise ApiError(413, "too_large", f"集計の結果が最大 {groups:,} 件になり、上限 {limit:,} を超える")
                cube = v.summarize(name, keep=keep, agg=query.get("agg", ["sum"])[0], **coords)
                return 200, {"seq": v.seq, "dims": list(cube.dims), "cells": _cells(cube)}
        raise ApiError(404, "not_found", f"{url.path} はない")

    # ------------------------------------------------ 書き込み

    def do_POST(self) -> None:
        self._run(self._post)

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
        ops = body["ops"]
        try:
            seq = self.server.workspace.write(
                lambda m: apply_ops(m, ops), user=user, reason=body.get("reason"),
                client_op_id=body["client_op_id"], expect=body.get("expect"), timeout=self.server.write_timeout)
        except Conflict as e:
            raise ApiError(409, "conflict", str(e), seq=e.seq, user=e.user) from None
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


class _Text(str):
    """JSON でなく、テキストのまま返す応答の本文。"""


def stats_text(ws) -> str:
    """Workspace か Replica の観察用の数を、Prometheus のテキスト形式にする。"""
    st, now = ws.stats, time.monotonic()
    rows = [("nanashi_seq", "公開中の版の通し番号", "gauge", ws.seq),
            ("nanashi_ready", "要求を受けられるか", "gauge", int(not ws.ready()))]
    if isinstance(ws, Workspace):
        lease = ws.journal.lease() if ws.journal is not None else {"held": False, "expires_in": None}
        rows += [
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


def _formula(written) -> str:
    from .parser import to_formula
    return to_formula(written)


class Server(ThreadingHTTPServer):
    """Workspace を公開する HTTP サーバー。serve_forever を別のスレッドで回すか、start() を使う。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, workspace: Workspace | Replica, host: str = "127.0.0.1", port: int = 8080, *,
                 write_timeout: float | None = 30.0, tokens: dict[str, str] | None = None,
                 user_header: str | None = None, max_body: int = 16 << 20, max_cells: int = 100_000,
                 max_threads: int = 64, request_timeout: float | None = 30.0):
        if tokens is not None and user_header is not None:
            raise ValueError("tokens と user_header はどちらか一方")
        super().__init__((host, port), Handler)
        self.workspace = workspace
        self.write_timeout = write_timeout
        self.tokens, self.user_header = tokens, user_header
        self.max_body, self.max_cells, self.request_timeout = max_body, max_cells, request_timeout
        self._slots = threading.BoundedSemaphore(max_threads)
        self._thread: threading.Thread | None = None

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
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

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
                    help="認証を済ませたプロキシが付ける、利用者の見出し（例: X-Forwarded-User）")
    ap.add_argument("--insecure", action="store_true", help="認証なしで 127.0.0.1 以外でも待ち受ける")
    ap.add_argument("--max-body", type=int, default=16 << 20, help="本文の上限（バイト）")
    ap.add_argument("--max-cells", type=int, default=100_000, help="slice、rows、summary で返すセルの上限")
    ap.add_argument("--max-threads", type=int, default=64, help="同時に処理する要求の上限（超えたら 503）")
    ap.add_argument("--max-bytes", type=int, help="Rust のエンジンで、1 つの式の評価が持つ途中結果の上限（バイト）")
    ap.add_argument("--follow", action="store_true",
                    help="書き込まず、記録先に追従する読み出し専用のサーバーにする（読み手を増やすとき）")
    args = ap.parse_args(argv)
    tokens = None
    if args.tokens:
        with open(args.tokens, encoding="utf-8") as f:
            tokens = json.load(f)
    if tokens is None and args.user_header is None and not args.insecure and not _loopback(args.host):
        ap.error(f"{args.host} で待ち受けるには --tokens か --user-header で認証する（試すだけなら --insecure）")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.setswitchinterval(args.switch_interval)
    if args.engine == "rust":
        from .rust_engine import RustEngine
        engine = RustEngine(max_bytes=args.max_bytes)
    else:
        from .engine import ReferenceEngine
        engine = ReferenceEngine()
    if args.pg:
        from .pg_journal import PgJournal, migrate
        if args.migrate:
            migrate(args.pg)
        journal = PgJournal(args.pg, args.model_id, args.path, heartbeat=not args.follow)
    else:
        from .journal import FileJournal
        journal = FileJournal(args.path)
    if args.follow:
        ws = Replica(journal, engine)
    else:
        ws = Workspace.open(journal, engine, checkpoint_every=args.checkpoint_every, max_queue=args.max_queue)
    server = Server(ws, args.host, args.port, tokens=tokens, user_header=args.user_header, max_body=args.max_body,
                    max_cells=args.max_cells, max_threads=args.max_threads)
    log.info("公開中の版 %d、%s で待ち受ける", ws.seq, server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        ws.close()
        if hasattr(journal, "close"):  # PgJournal: リースの延長を止めて、接続を閉じる
            journal.close()


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


if __name__ == "__main__":
    main()
