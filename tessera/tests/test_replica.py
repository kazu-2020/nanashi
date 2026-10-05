"""記録先に追従する読み出し専用の版（Replica）と、ほかのプロセスの書き込みへの追いつき。"""
import threading
import time
import unittest

from sparse_engine.engine import ReferenceEngine
from sparse_engine.server import Server
from sparse_engine.workspace import Replica, Workspace

from .journals import JournalCase, PgStore
from .test_journal import check_same_state
from .test_server import Client, path, write
from .test_workspace import model, move

try:
    from sparse_engine.rust_engine import RustEngine
except ImportError:  # nanashi_core をビルドしていない環境
    RustEngine = None


def wait_for(fn, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not fn():
        if time.monotonic() > deadline:
            raise AssertionError("時間内に追いつかなかった")
        time.sleep(0.01)


class Follow(JournalCase):
    engine = staticmethod(ReferenceEngine)

    def setUp(self):
        super().setUp()
        m = model(self.engine())
        self.journals.journal().start(m)
        self.ws = Workspace(m, self.journals.journal())
        self.addCleanup(self.ws.close)
        self.replica = Replica(self.journals.journal(heartbeat=False) if self.store is PgStore
                               else self.journals.journal(), self.engine(), interval=0.05)
        self.addCleanup(self.replica.close)

    def test_replica_catches_up_incrementally(self):
        self.ws.write(move("p0", "p1", "Jan", 5))
        self.assertEqual(self.replica.refresh(), 1)
        v = self.replica.version
        self.assertEqual(v.get("Stock", Product="p1", Month="Jan"), 105.0)
        self.assertEqual(v.get("Total"), 8000.0)
        # 入力の変更として書き込むので、変わった範囲だけを計算し直す（全体を計算し直さない）
        double = [r for n, r in v.slice_log if n == "Double"]
        self.assertTrue(double and all(r for r in double), double)
        check_same_state(self, self.ws.version, v)

    def test_structural_changes_are_followed(self):
        self.ws.write(lambda m: m.add_member("Product", "p99"))
        self.ws.write(lambda m: m.set_cell("Stock", 7, Product="p99", Month="Feb"))
        self.ws.write(lambda m: m.add_formula("Triple", ["Product", "Month"], "Stock * 3"))
        self.replica.refresh()
        self.assertEqual(self.replica.version.get("Triple", Product="p99", Month="Feb"), 21.0)
        check_same_state(self, self.ws.version, self.replica.version)

    def test_member_order_is_followed_without_recalculating(self):
        self.ws.write(lambda m: m.move_member("Product", "p3", 0))
        self.replica.refresh()
        v = self.replica.version
        self.assertEqual(v.dimensions["Product"].in_order()[0], "p3")
        self.assertEqual(list(v.slice_log), [])  # 並び順だけなら何も計算し直さない
        check_same_state(self, self.ws.version, v)

    def test_replica_follows_in_the_background(self):
        for i in range(5):
            self.ws.write(move("p0", f"p{i + 1}", "Mar", 1))
        wait_for(lambda: self.replica.seq == self.ws.seq)
        check_same_state(self, self.ws.version, self.replica.version)
        self.assertIsNone(self.replica.error)

    def test_readers_see_whole_versions_while_following(self):
        stop, seen = threading.Event(), []

        def reader():
            while not stop.is_set():
                seen.append(self.replica.version.get("Total"))  # 移すだけなので合計はいつも同じ
        t = threading.Thread(target=reader)
        t.start()
        for i in range(20):
            self.ws.write(move("p0", "p1", "Apr", 1))
            self.replica.refresh()
        stop.set()
        t.join()
        self.assertEqual(set(seen), {8000.0})
        self.assertEqual(self.replica.version.get("Stock", Product="p1", Month="Apr"), 120.0)

    def test_follow_server_is_read_only(self):
        server = Server(self.replica, "127.0.0.1", 0).start()
        try:
            c = Client(server.url, token=None)
            self.ws.write(move("p0", "p1", "Jan", 5))
            self.replica.refresh()
            self.assertEqual(c.get(path(self.replica, "Stock", "cell", Product="p1", Month="Jan")), (200, {"seq": 1, "value": 105.0}))
            status, err = c.post("/writes", {"client_op_id": "x", "ops": [write(self.replica, "Stock", 1, Product="p0", Month="Jan")]})
            self.assertEqual((status, err["error"]), (405, "read_only"))
            self.assertEqual(c.get("/operations/x")[0], 404)  # a follower reads the journal only
        finally:
            server.stop()


class FileFollow(Follow, unittest.TestCase):
    pass


class PgFollow(Follow, unittest.TestCase):
    store = PgStore


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustFileFollow(FileFollow):
    engine = staticmethod(RustEngine)


@unittest.skipIf(RustEngine is None, "nanashi_core のビルドが必要")
class RustPgFollow(PgFollow):
    engine = staticmethod(RustEngine)


class WriterCatchesUp(JournalCase, unittest.TestCase):
    """書き手は、ほかのプロセスが書いていたら、記録に追いついてから次を書く（開き直さない）。"""

    def test_file_writer_catches_up_after_another_writer(self):
        m = model(ReferenceEngine())
        self.journals.journal().start(m)
        a = Workspace(m, self.journals.journal())
        b = Workspace.open(self.journals.journal(), ReferenceEngine())
        a.write(move("p0", "p1", "Jan", 5))
        a.close()  # 書き込みの権利を手放す
        with self.assertRaisesRegex(Exception, "開き直す"):  # b の版は古い
            b.write(move("p2", "p3", "Jan", 1))
        self.assertEqual(b.version.get("Stock", Product="p1", Month="Jan"), 105.0)  # 追いついた
        self.assertEqual(b.write(move("p2", "p3", "Jan", 1)), 2)
        b.close()
        check_same_state(self, b.version, self.journals.journal().open(ReferenceEngine()))


if __name__ == "__main__":
    unittest.main()
