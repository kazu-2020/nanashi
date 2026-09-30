"""HTTP サーバー（Workspace を JSON の API で公開する）。"""
import http.client
import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from sparse_engine.engine import ReferenceEngine
from sparse_engine.server import Server
from sparse_engine.workspace import Workspace

from .journals import JournalCase, PgStore
from .test_workspace import hold, model, workspace

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None


TOKENS = {"t-alice": "alice", "t-bob": "bob"}


class Client:
    def __init__(self, url: str, token: str | None = "t-alice", headers: dict | None = None):
        self.url = url
        self.headers = dict(headers or {})
        if token is not None:
            self.headers["Authorization"] = f"Bearer {token}"

    def get(self, path: str) -> tuple[int, dict]:
        return self._call(urllib.request.Request(self.url + path, headers=self.headers))

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **self.headers})
        return self._call(req)

    @staticmethod
    def _call(req) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
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
        self.assertEqual(self.c.get("/health"), (200, {"seq": 0}))
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
        self.assertEqual(self.ws.version.model.last_record["user"], "bob")

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
        with mock.patch("sparse_engine.workspace.Version.get", side_effect=KeyError("secret")):
            status, err = self.c.get("/metrics/Stock/cell?Product=p1&Month=Jan")
        self.assertEqual((status, err["error"], err["message"]), (500, "internal", "内部エラー"))
        self.assertNotIn("secret", json.dumps(err))
        self.assertTrue(err["error_id"])

    def test_definition_changes_through_the_api(self):
        ops = [{"op": "add_dimension", "args": ["Region", ["N", "S"]]},
               {"op": "add_input", "args": ["Weight", ["Region"], [[["N"], 1.0], [["S"], 3.0]]]},
               {"op": "add_formula", "args": ["Share", ["Region"], "Weight / Weight[REMOVE SUM: Region]"]},
               {"op": "add_member", "args": ["Region", "W"]},
               {"op": "rename_metric", "args": ["Share", "Ratio"]}]
        self.assertEqual(self.c.post("/writes", {"client_op_id": "d", "ops": ops})[0], 200)
        self.assertEqual(self.c.get("/metrics/Ratio/cell?Region=S")[1]["value"], 0.75)
        self.assertEqual(self.c.get("/")[1]["dimensions"]["Region"]["members"], ["N", "S", "W"])


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
        server = Server(self.ws, "127.0.0.1", 0, user_header="X-Forwarded-User").start()
        try:
            self.assertEqual(Client(server.url, token=None).get("/")[0], 401)
            c = Client(server.url, token=None, headers={"X-Forwarded-User": "carol"})
            self.assertEqual(c.post("/writes", {"client_op_id": "p", "ops": [write("Stock", 1, Product="p0", Month="Jan")]})[0], 200)
            self.assertEqual(self.ws.version.model.last_record["user"], "carol")
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
