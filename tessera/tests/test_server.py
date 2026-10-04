"""HTTP サーバー（Workspace を JSON の API で公開する）。"""
import contextlib
import http.client
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from sparse_engine.engine import ReferenceEngine
from sparse_engine.server import Server, idle_for, main
from sparse_engine.workspace import Workspace

from .journals import JournalCase, PgStore
from .test_workspace import hold, model, workspace

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None


TOKENS = {"t-alice": "alice", "t-bob": "bob"}


class Client:
    def __init__(self, url: str, token: str | None = "t-alice", headers: dict | None = None, timeout: float = 10):
        self.url, self.timeout = url, timeout
        self.headers = dict(headers or {})
        if token is not None:
            self.headers["Authorization"] = f"Bearer {token}"

    def get(self, path: str) -> tuple[int, dict]:
        return self._call(urllib.request.Request(self.url + path, headers=self.headers))

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **self.headers})
        return self._call(req)

    def _call(self, req) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


def write(metric: str, value, **coords) -> dict:
    return {"op": "set_cell", "args": [metric, value], "kwargs": coords}


class Api:
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        self.ws = workspace(self, model(self.engine()), max_queue=8)
        self.server = Server(self.ws, "127.0.0.1", 0, tokens=TOKENS, max_cells=50).start()
        self.c = Client(self.server.url)

    def tearDown(self):
        self.server.stop()

    def test_definition_and_health(self):
        status, body = self.c.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(body["seq"], 0)
        self.assertEqual(body["dimensions"]["Month"]["members"], ["Jan", "Feb", "Mar", "Apr"])
        self.assertEqual(body["metrics"]["Total"]["formula"], "ByMonth[REMOVE SUM: Month]")
        self.assertEqual(self.c.get("/health"), (200, {"seq": 0, "role": "leader"}))
        self.assertEqual(self.c.get("/nothing")[0], 404)
        self.assertEqual(self.c.get("/metrics/Nope/cell")[0], 404)

    def test_reads(self):
        self.assertEqual(self.c.get("/metrics/Stock/cell?Product=p1&Month=Jan"), (200, {"seq": 0, "value": 100.0}))
        self.assertEqual(self.c.get("/metrics/Total/cell"), (200, {"seq": 0, "value": 8000.0}))
        status, body = self.c.get("/metrics/Stock/slice?Product=p1,p2&Month=Jan")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(body["cells"]), [["p1", "Jan", 100.0], ["p2", "Jan", 100.0]])
        status, body = self.c.get("/metrics/Stock/rows?Product=p3&limit=2&offset=1")
        self.assertEqual((status, body["total"], body["rows"]), (200, 4, [["p3", "Feb", 100.0], ["p3", "Mar", 100.0]]))
        status, body = self.c.get("/metrics/Stock/summary?keep=Month&Product=p1,p2")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(body["cells"]), [["Apr", 200.0], ["Feb", 200.0], ["Jan", 200.0], ["Mar", 200.0]])
        self.assertEqual(self.c.get("/metrics/Stock/summary?agg=count")[1]["cells"], [[80.0]])
        self.assertEqual(self.c.get("/metrics/Stock/cell?Product=p1,p2&Month=Jan")[0], 400)
        self.assertEqual(self.c.get("/metrics/Stock/cell?Product=p1")[0], 400)
        self.assertEqual(self.c.get("/metrics/Stock/rows?offset=x")[0], 400)

    def test_overrides(self):
        body = {"client_op_id": "ov-1", "ops": [
            {"op": "add_formula", "args": ["Plan", ["Product", "Month"], "Stock * 2"], "kwargs": {"overridable": True}},
            write("Plan", 7, Product="p1", Month="Jan")]}
        self.assertEqual(self.c.post("/writes", body)[0], 200)
        self.assertEqual(self.c.get("/metrics/Plan/overrides"), (200, {"seq": 1, "dims": ["Product", "Month"], "cells": [["p1", "Jan", 7.0]]}))
        self.assertEqual(self.c.get("/metrics/Plan/overrides?Product=p2")[1]["cells"], [])
        self.assertEqual(self.c.get("/metrics/Double/overrides")[0], 400)
        self.assertEqual(self.c.get("/metrics/__override__Plan/slice")[0], 404)

    def test_writes_are_transactions_with_resend(self):
        body = {"client_op_id": "op-1", "reason": "移動",
                "ops": [write("Stock", 95, Product="p0", Month="Jan"), write("Stock", 105, Product="p1", Month="Jan")]}
        self.assertEqual(self.c.post("/writes", body), (200, {"seq": 1}))
        self.assertEqual(self.c.get("/metrics/Stock/cell?Product=p1&Month=Jan")[1]["value"], 105.0)
        self.assertEqual(self.c.post("/writes", body), (200, {"seq": 1}))  # 再送は二重に確定しない
        self.assertEqual(self.c.get("/health")[1]["seq"], 1)
        bad = {"client_op_id": "op-2", "ops": [write("Stock", 1, Product="p0", Month="Jan"),
                                                write("Nope", 1, Product="p0", Month="Jan")]}
        status, err = self.c.post("/writes", bad)
        self.assertEqual((status, err["error"]), (400, "bad_request"))
        self.assertEqual(self.c.get("/metrics/Stock/cell?Product=p0&Month=Jan")[1]["value"], 95.0)  # 全部取り消し
        self.assertEqual(self.c.post("/writes", {"ops": [write("Stock", 1, Product="p0", Month="Jan")]})[0], 400)
        self.assertEqual(self.c.post("/writes", {"client_op_id": "x", "ops": []})[0], 400)
        formula = {"client_op_id": "op-3", "ops": [{"op": "add_formula", "args": ["Bad", ["Product", "Month"], "ByMonth + Stock"]}]}
        status, err = self.c.post("/writes", formula)
        self.assertEqual((status, err["error"], err["code"]), (400, "formula", "not_expanded"))  # 呼ぶ側はコードで見分ける
        self.assertIn("EXPAND", err["message"])

    def test_optimistic_concurrency(self):
        seq = self.c.get("/health")[1]["seq"]
        self.c.post("/writes", {"client_op_id": "a", "ops": [write("Stock", 90, Product="p0", Month="Jan")]})
        bob = Client(self.server.url, "t-bob")
        status, err = bob.post("/writes", {"client_op_id": "b", "expect": seq,
                                              "ops": [write("Stock", 80, Product="p0", Month="Jan")]})
        self.assertEqual((status, err["error"], err["user"], err["seq"]), (409, "conflict", "alice", 1))
        status, body = self.c.post("/writes", {"client_op_id": "c", "expect": seq,
                                               "ops": [write("Stock", 80, Product="p5", Month="Jan")]})
        self.assertEqual(status, 200)  # 別のセルなら通る

    def test_overloaded(self):
        release = hold(self.ws)
        try:
            self.server.write_timeout = 0.05
            results = []

            def post(i):
                results.append(self.c.post("/writes", {"client_op_id": f"q{i}",
                                                       "ops": [write("Stock", 1, Product="p0", Month="Jan")]})[0])
            threads = [threading.Thread(target=post, args=(i,)) for i in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertIn(429, results)  # 列（8 件）が溢れた分は 429
            self.assertIn(504, results)  # 列に入った分は、確定を待ちきれずに 504
        finally:
            release()

    def test_user_comes_from_authentication(self):
        anonymous = Client(self.server.url, token=None)
        self.assertEqual(anonymous.get("/health")[0], 200)  # 死活監視は認証なしで読める
        self.assertEqual(anonymous.get("/metrics/Stock/cell?Product=p1&Month=Jan")[0], 401)
        self.assertEqual(Client(self.server.url, "t-eve").get("/")[0], 401)
        body = {"client_op_id": "u1", "ops": [write("Stock", 90, Product="p0", Month="Jan")]}
        self.assertEqual(anonymous.post("/writes", body)[0], 401)
        status, err = self.c.post("/writes", {**body, "user": "bob"})  # 本文の自己申告は受け付けない
        self.assertEqual((status, err["error"]), (400, "bad_request"))
        self.assertEqual(Client(self.server.url, "t-bob").post("/writes", body)[0], 200)
        self.assertEqual(self.ws.version.last_record["user"], "bob")

    def test_size_limits(self):
        status, err = self.c.get("/metrics/Stock/slice")  # 80 セルは上限 50 を超える
        self.assertEqual((status, err["error"]), (413, "too_large"))
        self.assertEqual(self.c.get("/metrics/Stock/slice?Product=p1,p2")[0], 200)
        self.assertEqual(self.c.get("/metrics/Stock/rows?limit=51")[0], 400)
        status, body = self.c.get("/metrics/Stock/rows")  # limit を省くと上限まで
        self.assertEqual((status, len(body["rows"]), body["total"]), (200, 50, 80))
        self.assertEqual(self.c.get("/metrics/Stock/summary?keep=Product,Month")[0], 413)
        self.assertEqual(self.c.get("/metrics/Stock/summary?keep=Product,Month&Month=Jan")[0], 200)
        self.server.max_body = 100
        big = {"client_op_id": "big", "ops": [write("Stock", 1, Product="p0", Month="Jan")] * 10}
        status, err = self.c.post("/writes", big)
        self.assertEqual((status, err["error"]), (413, "too_large"))
        self.assertEqual(self.c.get("/health")[1]["seq"], 0)

    def test_argument_errors_are_400_and_internal_errors_hide_details(self):
        for op in [write("Nope", 1, Product="p0", Month="Jan"), write("Stock", 1, Product="p0"),
                   write("Stock", 1, Product="p0", Month="Jan", Color="red"),
                   {"op": "add_member", "args": ["Product"]}, {"op": "spread", "args": ["Stock", "x"]},
                   {"op": "set_cell", "args": "Stock"}, "set_cell"]:
            with self.subTest(op=op):
                status, err = self.c.post("/writes", {"client_op_id": f"bad-{op}", "ops": [op]})
                self.assertEqual((status, err["error"]), (400, "bad_request"), err)
        self.assertEqual(self.c.get("/metrics/Stock/cell?Product=p0")[0], 400)
        with mock.patch("sparse_engine.model.Model.get", side_effect=KeyError("secret")):
            status, err = self.c.get("/metrics/Stock/cell?Product=p1&Month=Jan")
        self.assertEqual((status, err["error"], err["message"]), (500, "internal", "内部エラー"))
        self.assertNotIn("secret", json.dumps(err))
        self.assertTrue(err["error_id"])

    def test_definition_changes_through_the_api(self):
        ops = [{"op": "add_dimension", "args": ["Region", ["N", "S"]]},
               {"op": "add_input", "args": ["Weight", ["Region"], [[["N"], 1.0], [["S"], 3.0]]]},
               {"op": "add_formula", "args": ["Share", ["Region"], "Weight / Weight[REMOVE SUM: Region]"]},
               {"op": "add_member", "args": ["Region", "W"]},
               {"op": "add_property", "args": ["Region", "Big", "Region", {"N": "S"}]},
               {"op": "rename_metric", "args": ["Share", "Ratio"]}]
        self.assertEqual(self.c.post("/writes", {"client_op_id": "d", "ops": ops})[0], 200)
        self.assertEqual(self.c.get("/metrics/Ratio/cell?Region=S")[1]["value"], 0.75)
        region = self.c.get("/")[1]["dimensions"]["Region"]
        self.assertEqual(region["members"], ["N", "S", "W"])
        self.assertEqual((region["properties"], region["property_values"]), ({"Big": "Region"}, {"Big": {"N": "S"}}))
        again = [{"op": "add_dimension", "args": ["Region", []]}]
        self.assertEqual(self.c.post("/writes", {"client_op_id": "d2", "ops": again})[0], 400)
        self.assertEqual(self.c.get("/")[1]["dimensions"]["Region"]["members"], ["N", "S", "W"])

    def test_member_order_through_the_api(self):
        ops = [{"op": "add_member", "args": ["Product", "p_new"], "kwargs": {"at": 1}},
               {"op": "move_member", "args": ["Product", "p2", 0]}]
        self.assertEqual(self.c.post("/writes", {"client_op_id": "o", "ops": ops})[0], 200)
        self.assertEqual(self.c.get("/")[1]["dimensions"]["Product"]["members"][:4], ["p2", "p0", "p_new", "p1"])
        rows = self.c.get("/metrics/Stock/rows?Month=Jan&limit=3")[1]["rows"]
        self.assertEqual([r[0] for r in rows], ["p2", "p0", "p1"])  # p_new には値がない
        status, err = self.c.post("/writes", {"client_op_id": "o2", "ops": [{"op": "move_member", "args": ["Month", "Mar", 0]}]})
        self.assertEqual((status, err["error"]), (400, "bad_request"))


class ReferenceApi(Api, unittest.TestCase):
    pass


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustApi(Api, unittest.TestCase):
    engine = staticmethod(RustEngine)


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class FileRustApi(JournalCase, RustApi):
    pass


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class PgRustApi(JournalCase, RustApi):
    store = PgStore


class Limits(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(model(ReferenceEngine()))

    def test_proxy_header_names_the_user(self):
        server = Server(self.ws, "127.0.0.1", 0, user_header="X-Forwarded-User", trusted_proxies=["127.0.0.1"]).start()
        try:
            self.assertEqual(Client(server.url, token=None).get("/")[0], 401)
            c = Client(server.url, token=None, headers={"X-Forwarded-User": "carol"})
            self.assertEqual(c.post("/writes", {"client_op_id": "p", "ops": [write("Stock", 1, Product="p0", Month="Jan")]})[0], 200)
            self.assertEqual(self.ws.version.last_record["user"], "carol")
        finally:
            server.stop()

    def test_requests_beyond_max_threads_are_refused(self):
        server = Server(self.ws, "127.0.0.1", 0, max_threads=2, request_timeout=5).start()
        idle = []
        try:
            for _ in range(2):  # 要求を送らずにつないだままの接続が、処理のスレッドを 2 本とも使う
                s = socket.create_connection(server.server_address[:2])
                idle.append(s)
            for _ in range(50):
                conn = http.client.HTTPConnection(*server.server_address[:2], timeout=5)
                conn.request("GET", "/health")
                r = conn.getresponse()
                status = r.status
                conn.close()
                if status == 503:
                    break
            self.assertEqual(status, 503)
        finally:
            for s in idle:
                s.close()
            server.stop()

    def test_interrupt_while_starting_a_request_thread_stops_the_server(self):
        # SIGTERM（KeyboardInterrupt）が、要求のスレッドを起こしている途中に届いても握りつぶさずに止まる。
        # 起こしたスレッドは自分で枠を返すので、ここで返すと 2 度返して ValueError になり、割り込みが消えていた
        server = Server(self.ws, "127.0.0.1", 0, max_threads=1, request_timeout=5)
        raised = []

        def serve():
            try:
                server.serve_forever(poll_interval=0.05)
            except BaseException as e:
                raised.append(e)
        serving = threading.Thread(target=serve, daemon=True)
        serving.start()
        real_start = threading.Thread.start

        def start(t):
            real_start(t)
            t.join()  # 要求を処理し終えて枠を返してから、割り込みが届く
            raise KeyboardInterrupt
        try:
            with mock.patch.object(threading.Thread, "start", start):
                with socket.create_connection(server.server_address[:2]) as s:
                    s.sendall(b"GET /health HTTP/1.0\r\n\r\n")
                    s.settimeout(5)
                    self.assertTrue(s.recv(100).startswith(b"HTTP/1.0 200"))
                serving.join(timeout=5)
            self.assertEqual([type(e) for e in raised], [KeyboardInterrupt])
            self.assertTrue(server._slots.acquire(blocking=False))  # 枠は 1 度だけ返っている
        finally:
            if serving.is_alive():
                server.shutdown()
            server.server_close()

    def test_stalled_connection_is_dropped(self):
        server = Server(self.ws, "127.0.0.1", 0, max_threads=1, request_timeout=0.2).start()
        try:
            s = socket.create_connection(server.server_address[:2])
            s.sendall(b"GET /health HTTP/1.0\r\n")  # 見出しの途中で止まる
            s.settimeout(5)
            self.assertEqual(s.recv(100), b"")  # 読み書きの期限で切られる
            s.close()
            self.assertEqual(Client(server.url).get("/health")[0], 200)
        finally:
            server.stop()


class ProxyHeader(JournalCase, unittest.TestCase):
    """The user header counts only on a connection from a trusted proxy (--trusted-proxy)."""
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        super().setUp()
        self.ws = workspace(self, model(self.engine()))

    def serve(self, trusted: list[str]) -> Server:
        server = Server(self.ws, "127.0.0.1", 0, user_header="X-Forwarded-User", trusted_proxies=trusted).start()
        self.addCleanup(server.stop)
        return server

    def history(self) -> list[str]:
        """The users in the journal records of the cell that the tests write."""
        h = self.journals.journal().cell_history(self.ws.version, "Stock", Product="p0", Month="Jan")
        return [r["user"] for r in h]

    def test_user_from_a_trusted_proxy_reaches_the_journal(self):
        server = self.serve(["10.0.0.0/8", "127.0.0.0/8"])
        c = Client(server.url, token=None, headers={"X-Forwarded-User": "carol@example.com"})
        status, body = c.post("/writes", {"client_op_id": "p1", "ops": [write("Stock", 1, Product="p0", Month="Jan")]})
        self.assertEqual((status, body), (200, {"seq": 1}))
        self.assertEqual(self.history(), ["carol@example.com"])
        self.assertEqual(Client(server.url, token=None).get("/")[0], 401)  # the header is necessary

    def test_header_from_an_untrusted_source_is_refused(self):
        server = self.serve(["10.0.0.0/8", "::1/128"])  # 127.0.0.1 is not trusted automatically
        c = Client(server.url, token=None, headers={"X-Forwarded-User": "mallory"})
        status, body = c.post("/writes", {"client_op_id": "p1", "ops": [write("Stock", 1, Product="p0", Month="Jan")]})
        self.assertEqual((status, body["error"]), (401, "unauthorized"))
        self.assertEqual(c.get("/metrics/Stock/cell?Product=p0&Month=Jan")[0], 401)
        self.assertEqual(c.get("/health")[0], 200)  # health and ready need no authentication
        self.assertEqual(self.ws.seq, 0)
        self.assertEqual(self.history(), [])

    def test_trusted_addresses(self):
        server = self.serve(["10.1.2.3", "192.168.0.0/16", "fd00::/8", "::ffff:172.16.0.5", "::ffff:172.17.0.0/112"])
        for host, want in [("10.1.2.3", True), ("10.1.2.4", False), ("192.168.40.1", True), ("::ffff:192.168.0.9", True),
                           ("fd12::1", True), ("fe80::1%eth0", False), ("127.0.0.1", False), ("not-an-address", False),
                           ("172.16.0.5", True), ("::ffff:172.16.0.5", True), ("172.17.9.9", True), ("172.18.0.1", False)]:
            self.assertEqual(server.trusted(host), want, host)

    def test_options(self):
        with self.assertRaisesRegex(ValueError, "trusted_proxies"):
            Server(self.ws, "127.0.0.1", 0, user_header="X-Forwarded-User")
        with self.assertRaisesRegex(ValueError, "user_header"):
            Server(self.ws, "127.0.0.1", 0, trusted_proxies=["127.0.0.1"])
        with self.assertRaisesRegex(ValueError, "CIDR"):
            Server(self.ws, "127.0.0.1", 0, user_header="X-Forwarded-User", trusted_proxies=["10.0.0.0/33"])
        with self.assertRaises(TypeError):  # a string is not a list of CIDRs
            Server(self.ws, "127.0.0.1", 0, user_header="X-Forwarded-User", trusted_proxies="127.0.0.1")


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class PgRustProxyHeader(ProxyHeader):
    """The production combination: the Rust engine and PgJournal."""
    engine = staticmethod(RustEngine)
    store = PgStore


class MainOptions(unittest.TestCase):
    """main refuses the authentication options that leave the user header open to any client."""

    def refused(self, *args: str) -> str:
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(err):
            main(["unused-path", *args])
        self.assertEqual(cm.exception.code, 2)
        return err.getvalue()

    def test_user_header_needs_a_trusted_proxy(self):
        self.assertIn("--trusted-proxy", self.refused("--user-header", "X-Forwarded-User"))
        self.assertIn("--trusted-proxy", self.refused("--host", "0.0.0.0", "--user-header", "X-Forwarded-User"))
        self.assertIn("--user-header", self.refused("--trusted-proxy", "10.0.0.0/8"))
        self.assertIn("CIDR", self.refused("--user-header", "X-Forwarded-User", "--trusted-proxy", "10.0.0.0/99"))

    def test_tokens_and_user_header_are_exclusive(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
            json.dump(TOKENS, f)
            f.flush()
            self.assertIn("どちらか一方", self.refused("--tokens", f.name, "--user-header", "X-Forwarded-User",
                                                    "--trusted-proxy", "10.0.0.0/8"))

    def test_no_authentication_listens_only_on_loopback(self):
        self.assertIn("--insecure", self.refused("--host", "0.0.0.0"))


class Observability(JournalCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ws = workspace(self, model(ReferenceEngine()))
        self.server = Server(self.ws, "127.0.0.1", 0, tokens=TOKENS).start()
        self.c = Client(self.server.url)

    def tearDown(self):
        self.server.stop()
        super().tearDown()

    def stats(self) -> dict:
        req = urllib.request.Request(self.server.url + "/stats", headers=self.c.headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertTrue(r.headers["Content-Type"].startswith("text/plain"))
            text = r.read().decode()
        return {line.split()[0]: float(line.split()[1]) for line in text.splitlines() if not line.startswith("#")}

    def test_ready_is_separate_from_health(self):
        self.assertEqual(self.c.get("/ready")[0], 200)
        self.ws._degraded = "記録先からの開き直しに失敗した: OSError"  # 開き直しに失敗し、古い版を公開している
        status, body = Client(self.server.url, token=None).get("/ready")
        self.assertEqual((status, body["ready"]), (503, False))
        self.assertIn("開き直し", body["reasons"][0])
        self.assertEqual(self.c.get("/health")[0], 200)  # 生きてはいる

    def test_failed_reopen_is_reported(self):
        self.ws.journal.catch_up = lambda m: (_ for _ in ()).throw(OSError("記録先が読めない"))
        self.ws.journal.open = lambda engine=None: (_ for _ in ()).throw(OSError("記録先が読めない"))
        self.ws._reload()
        status, body = self.c.get("/ready")
        self.assertEqual(status, 503)
        self.assertEqual(self.stats()["nanashi_reopen_failures_total"], 1)

    def test_stats(self):
        for i in range(3):
            self.c.post("/writes", {"client_op_id": f"s{i}", "ops": [write("Stock", i, Product="p0", Month="Jan")]})
        self.c.post("/writes", {"client_op_id": "bad", "ops": [write("Stock", "x", Product="p0", Month="Jan")]})
        self.ws.checkpoint()
        st = self.stats()
        self.assertEqual(st["nanashi_seq"], 3)
        self.assertEqual(st["nanashi_commits_total"], 3)
        self.assertEqual(st["nanashi_rejected_total"], 1)
        self.assertGreater(st["nanashi_commit_seconds_sum"], 0)
        self.assertEqual(st["nanashi_snapshot_seq"], 3)
        self.assertLess(st["nanashi_snapshot_age_seconds"], 60)
        self.assertEqual(st["nanashi_lease_held"], 1)
        self.assertEqual(st["nanashi_ready"], 1)
        self.assertEqual(Client(self.server.url, token=None).get("/stats")[0], 401)


class PgObservability(Observability):
    store = PgStore
    journal_options = {"lease_ttl": 1.5}  # 延長の間隔を短くする

    def test_heartbeat_failures_are_not_swallowed(self):
        j = self.ws.journal
        self.c.post("/writes", {"client_op_id": "h", "ops": [write("Stock", 1, Product="p0", Month="Jan")]})
        self.assertTrue(j.lease()["held"])
        real = j.conn

        class Broken:
            closed = False

            def execute(self, *a, **k):
                raise OSError("接続が切れた")
        with self.assertLogs("sparse_engine.pg_journal", "WARNING") as logs:
            j.conn = Broken()
            deadline = time.monotonic() + 5
            while j.lease()["error"] is None and time.monotonic() < deadline:
                time.sleep(0.02)
            j.conn = real
        self.assertIn("延長できなかった", logs.output[0])
        status, body = self.c.get("/ready")
        self.assertEqual(status, 503)
        self.assertIn("延長できない", body["reasons"][0])


class IdleExit(unittest.TestCase):
    def test_idle_for(self):
        for active, last, now, want in [(0, 10.0, 13.5, 3.5), (1, 10.0, 13.5, 0.0), (2, 0.0, 99.0, 0.0),
                                        (0, 10.0, 10.0, 0.0), (0, 10.0, 9.0, 0.0)]:
            self.assertEqual(idle_for(active, last, now), want, (active, last, now))

    def start(self, idle_exit: str) -> tuple[subprocess.Popen, str]:
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        proc = subprocess.Popen([sys.executable, "-m", "sparse_engine.server", d.name, "--engine", "reference",
                                 "--port", "0", "--idle-exit", idle_exit],
                                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.stderr.close)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        for line in proc.stderr:
            if m := re.search(r"(http://\S+) で待ち受ける", line):
                return proc, m.group(1)
        self.fail("the server did not start")

    def test_stops_after_the_idle_time(self):
        proc, url = self.start("1")
        self.assertEqual(Client(url, token=None).get("/")[0], 200)
        _, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("要求がなかったので止める", err)

    def test_probes_do_not_keep_the_server_alive(self):
        proc, url = self.start("2")
        c, start = Client(url, token=None, timeout=1), time.monotonic()
        while proc.poll() is None and time.monotonic() - start < 5:
            with contextlib.suppress(OSError):
                c.get("/health")
            time.sleep(0.5)
        self.assertEqual(proc.wait(timeout=1), 0)


class WithJournal(JournalCase, unittest.TestCase):
    def test_server_over_a_journal_survives_restart(self):
        server = Server(workspace(self, model(ReferenceEngine()), checkpoint_every=2), "127.0.0.1", 0, tokens=TOKENS).start()
        c = Client(server.url)
        for i in range(5):
            c.post("/writes", {"client_op_id": f"w{i}", "ops": [write("Stock", 100 + i, Product="p0", Month="Jan")]})
        server.stop()
        server = Server(Workspace.open(self.journals.journal(), ReferenceEngine()), "127.0.0.1", 0, tokens=TOKENS).start()
        try:
            c = Client(server.url)
            self.assertEqual(c.get("/health")[1]["seq"], 5)
            self.assertEqual(c.get("/metrics/Stock/cell?Product=p0&Month=Jan")[1]["value"], 104.0)
            self.assertEqual(c.post("/writes", {"client_op_id": "w3", "ops": [write("Stock", 1, Product="p0", Month="Jan")]}),
                             (200, {"seq": 4}))  # 再起動をまたいでも再送は二重に確定しない
            # 再起動した書き手は、前の書き手の権利（PgJournal のリース）の期限を待たずに書ける
            self.assertEqual(c.post("/writes", {"client_op_id": "w5", "ops": [write("Stock", 7, Product="p0", Month="Jan")]}),
                             (200, {"seq": 6}))
        finally:
            server.stop()


class PgWithJournal(WithJournal):
    store = PgStore


if __name__ == "__main__":
    unittest.main()
