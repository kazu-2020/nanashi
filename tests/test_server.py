"""HTTP サーバー（Workspace を JSON の API で公開する）。"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from sparse_engine.engine import ReferenceEngine
from sparse_engine.journal import FileJournal
from sparse_engine.server import Server
from sparse_engine.workspace import Workspace

from .test_workspace import hold, model

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None


class Client:
    def __init__(self, url: str):
        self.url = url

    def get(self, path: str) -> tuple[int, dict]:
        return self._call(urllib.request.Request(self.url + path))

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
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
        self.ws = Workspace(model(self.engine()), max_queue=8)
        self.server = Server(self.ws, "127.0.0.1", 0).start()
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
        body = {"client_op_id": "op-1", "user": "alice", "reason": "移動",
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
        self.assertEqual(status, 400)
        self.assertIn("EXPAND", err["message"])

    def test_optimistic_concurrency(self):
        seq = self.c.get("/health")[1]["seq"]
        self.c.post("/writes", {"client_op_id": "a", "user": "alice", "ops": [write("Stock", 90, Product="p0", Month="Jan")]})
        status, err = self.c.post("/writes", {"client_op_id": "b", "user": "bob", "expect": seq,
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


class WithJournal(unittest.TestCase):
    def test_server_over_a_journal_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = model(ReferenceEngine())
            FileJournal(tmp).start(m)
            server = Server(Workspace(m, FileJournal(tmp), checkpoint_every=2), "127.0.0.1", 0).start()
            c = Client(server.url)
            for i in range(5):
                c.post("/writes", {"client_op_id": f"w{i}", "ops": [write("Stock", 100 + i, Product="p0", Month="Jan")]})
            server.stop()
            server = Server(Workspace.open(tmp, ReferenceEngine()), "127.0.0.1", 0).start()
            try:
                c = Client(server.url)
                self.assertEqual(c.get("/health")[1]["seq"], 5)
                self.assertEqual(c.get("/metrics/Stock/cell?Product=p0&Month=Jan")[1]["value"], 104.0)
                self.assertEqual(c.post("/writes", {"client_op_id": "w3", "ops": [write("Stock", 1, Product="p0", Month="Jan")]}),
                                 (200, {"seq": 4}))  # 再起動をまたいでも再送は二重に確定しない
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()
