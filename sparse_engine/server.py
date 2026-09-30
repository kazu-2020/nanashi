"""薄い HTTP サーバー。Workspace を JSON の API で公開する。

    python -m sparse_engine.server plan/ --port 8080 --engine rust --checkpoint-every 1000

読み出しは公開中の版に対して行い、必要な分だけ読む（get、slice、rows、summarize）。
書き込みは 1 つの要求を 1 つのトランザクションとして Workspace に渡す。再送しても二重に確定しないよう、
書き込みには client_op_id を必ず付ける。読んだ版の通し番号を expect に付けると、その後に同じセルを
変えた書き込みがあれば 409 で拒否する（楽観的な排他）。

    GET  /                                    モデルの定義（軸、Metric）と公開中の版の通し番号
    GET  /health                              通し番号だけ
    GET  /metrics/<name>/cell?<軸>=<メンバー>   1 セル（{"value": ..., "seq": ...}）
    GET  /metrics/<name>/slice?<軸>=a,b        範囲（{"dims": [...], "cells": [[座標..., 値], ...]}）
    GET  /metrics/<name>/rows?<軸>=a&offset=0&limit=50   行の列と全行数
    GET  /metrics/<name>/summary?keep=Month&agg=sum&<軸>=a,b   集計
    POST /writes                              {"client_op_id", "user", "reason", "expect", "ops": [...]}

ops の各要素は {"op": 操作名, "args": [...], "kwargs": {...}} で、Model の操作（set_cell、spread、
add_member、rename_member、remove_member、add_formula、add_input、add_property、add_dimension、
remove_metric、rename_metric）を順に呼ぶ。add_input の cells は [[座標の列, 値], ...] で渡す。

応答は JSON。失敗は {"error": 種類, "message": 文言} で、400（式や引数の誤り）、404、409（Conflict）、
429（Overloaded）、503（閉じている）を使う。

標準ライブラリの HTTP サーバーで、要求ごとにスレッドを作る。読み手が多いプロセスでは
--switch-interval で Python のスレッド切り替えの間隔を短くする（README の「性能」）。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .evaluate import FormulaError
from .journal import AlreadyCommitted, Stale
from .workspace import Conflict, Overloaded, Workspace

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
    """書き込みの操作を順に Model に適用する。"""
    for op in ops:
        name = op.get("op")
        if name not in WRITE_OPS:
            raise ValueError(f"操作 {name!r} は使えない（{', '.join(sorted(WRITE_OPS))}）")
        args, kwargs = list(op.get("args", [])), dict(op.get("kwargs", {}))
        if name == "add_input":  # cells は [[座標の列, 値], ...] で来る
            if len(args) >= 3 and isinstance(args[2], list):
                args[2] = {tuple(k): v for k, v in args[2]}
            if isinstance(kwargs.get("cells"), list):
                kwargs["cells"] = {tuple(k): v for k, v in kwargs["cells"]}
        getattr(model, name)(*args, **kwargs)


class Handler(BaseHTTPRequestHandler):
    server: "Server"

    def log_message(self, fmt, *args):  # 標準の出力を logging に寄せる
        log.debug("%s " + fmt, self.address_string(), *args)

    # ------------------------------------------------ 応答

    def _json(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
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
        except (ValueError, KeyError, TypeError) as e:  # 引数や式の誤り（FormulaError も ValueError ではないが同じ扱い）
            self._fail(ApiError(400, "bad_request", str(e)))
        except FormulaError as e:
            self._fail(ApiError(400, "formula", str(e)))
        except Exception as e:
            log.exception("要求の処理に失敗した")
            self._fail(ApiError(500, "internal", f"{type(e).__name__}: {e}"))
        else:
            self._json(status, body)

    # ------------------------------------------------ 読み出し

    def do_GET(self) -> None:
        self._run(self._get)

    def _get(self) -> tuple[int, Any]:
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query, keep_blank_values=True)
        parts = [p for p in url.path.split("/") if p]
        v = self.server.workspace.version
        if not parts:
            dims = {d.name: {"id": d.id, "members": list(d.members), "ordered": d.ordered,
                             "properties": {p: t for p, (t, _) in d.properties.items()}}
                    for d in v.dimensions.values()}
            metrics = {m.name: {"id": m.id, "dims": list(m.dims), "kind": m.kind, "overridable": m.overridable,
                                "formula": None if m.written is None else _formula(m.written)}
                       for m in v.metrics.values() if not m.name.startswith("__")}
            return 200, {"seq": v.seq, "dimensions": dims, "metrics": metrics}
        if parts == ["health"]:
            return 200, {"seq": v.seq}
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
            if what == "slice":
                cube = v.slice(name, **coords)
                return 200, {"seq": v.seq, "dims": list(cube.dims), "cells": _cells(cube)}
            if what == "rows":
                rows, total = v.rows(name, offset=_int(query, "offset", 0), limit=_int(query, "limit", None), **coords)
                return 200, {"seq": v.seq, "dims": list(v.metrics[name].dims), "rows": [[*k, x] for k, x in rows],
                             "total": total}
            if what == "summary":
                keep = [d for x in query.get("keep", []) for d in x.split(",") if d]
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
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        except ValueError:
            raise ApiError(400, "bad_request", "本文が JSON ではない") from None
        if not isinstance(body, dict) or not isinstance(body.get("ops"), list) or not body["ops"]:
            raise ApiError(400, "bad_request", "ops（操作の列）が要る")
        if not isinstance(body.get("client_op_id"), str) or not body["client_op_id"]:
            raise ApiError(400, "bad_request", "client_op_id（再送しても二重に確定しないための ID）が要る")
        ops = body["ops"]
        try:
            seq = self.server.workspace.write(
                lambda m: apply_ops(m, ops), user=body.get("user"), reason=body.get("reason"),
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


def _formula(written) -> str:
    from .parser import to_formula
    return to_formula(written)


class Server(ThreadingHTTPServer):
    """Workspace を公開する HTTP サーバー。serve_forever を別のスレッドで回すか、start() を使う。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, workspace: Workspace, host: str = "127.0.0.1", port: int = 8080, *,
                 write_timeout: float | None = 30.0):
        super().__init__((host, port), Handler)
        self.workspace = workspace
        self.write_timeout = write_timeout
        self._thread: threading.Thread | None = None

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
    ap.add_argument("--checkpoint-every", type=int, default=1000, help="この件数の記録ごとにスナップショットを取る")
    ap.add_argument("--max-queue", type=int, default=1000, help="書き込みの列の上限（溢れたら 429）")
    ap.add_argument("--switch-interval", type=float, default=0.0005,
                    help="Python のスレッド切り替えの間隔（秒）。読み手が多いときの書き込みの待ちを減らす")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.setswitchinterval(args.switch_interval)
    if args.engine == "rust":
        from .rust_engine import RustEngine
        engine = RustEngine()
    else:
        from .engine import ReferenceEngine
        engine = ReferenceEngine()
    if args.pg:
        from .pg_journal import PgJournal
        journal = PgJournal(args.pg, args.model_id, args.path)
    else:
        from .journal import FileJournal
        journal = FileJournal(args.path)
    ws = Workspace.open(journal, engine, checkpoint_every=args.checkpoint_every, max_queue=args.max_queue)
    server = Server(ws, args.host, args.port)
    log.info("公開中の版 %d、%s で待ち受ける", ws.seq, server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        ws.close()


if __name__ == "__main__":
    main()
