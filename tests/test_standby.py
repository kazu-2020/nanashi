"""待機系と昇格（Workspace(standby=True)）。同じ記録先を開いた 2 つの Workspace のうち、書き込みを受けるのは 1 つ。"""
import threading
import unittest
import urllib.request

from sparse_engine.engine import ReferenceEngine
from sparse_engine.journal import Fenced
from sparse_engine.workspace import NotLeader, Role, Workspace

from .journals import DSN, JournalCase, PgStore
from .test_journal import check_same_state
from .test_replica import wait_for
from .test_workspace import model, move

LEASE_TTL = 0.6


class Standby(JournalCase):
    """a が先に開いて書き手になり、b は待機系として追従する。"""

    def setUp(self):
        super().setUp()
        m = model(ReferenceEngine())
        self.journals.journal().start(m)
        self.a = Workspace(m, self.journal(endpoint="http://a"), standby=True, interval=0.05)
        self.addCleanup(self.a.close)
        self.b = Workspace.open(self.journal(endpoint="http://b"), ReferenceEngine(), standby=True, interval=0.05)
        self.addCleanup(self.b.close)

    def journal(self, **pg_options):
        """記録先。pg_options（endpoint、lease_ttl など）は PostgreSQL の記録先だけが受け取る。"""
        if self.store is PgStore:
            return self.journals.journal(lease_ttl=LEASE_TTL, **pg_options)
        return self.journals.journal()

    def leader_of(self, ws: Workspace):
        """ws から見た書き手の番地（ファイルの記録先は番地を持たないので None）。"""
        return ws.journal.leader()

    def test_second_process_starts_as_standby(self):
        self.assertEqual((self.a.role, self.b.role), (Role.LEADER, Role.STANDBY))
        with self.assertRaises(NotLeader) as cm:
            self.b.submit(move("p0", "p1", "Jan", 1))
        self.assertEqual(cm.exception.leader, "http://a" if self.store is PgStore else None)
        self.assertEqual(self.b.ready(), [])  # 読み出しは受けられる
        self.assertEqual(self.a.write(move("p0", "p1", "Jan", 5)), 1)
        wait_for(lambda: self.b.seq == 1)  # 書き込みは受けないが、追従する
        self.assertEqual(self.b.version.get("Stock", Product="p1", Month="Jan"), 105.0)
        self.assertIs(self.b.role, Role.STANDBY)

    def test_standby_takes_over_when_the_leader_closes(self):
        self.a.write(move("p0", "p1", "Jan", 5))
        self.a.write(move("p0", "p1", "Feb", 3))
        self.a.close()
        wait_for(lambda: self.b.role is Role.LEADER, timeout=2.0)
        self.assertEqual(self.b.seq, 2)  # a の書き込みを含む版で書き手になる
        self.assertEqual(self.b.version.get("Stock", Product="p1", Month="Feb"), 103.0)
        self.assertEqual(self.b.write(move("p2", "p3", "Jan", 1)), 3)
        check_same_state(self, self.b.version.model, self.journal().open(ReferenceEngine()))

    def test_promotion_catches_up_with_writes_made_after_the_last_follow(self):
        # 権利を取る直前に別のプロセスが確定した記録は、権利を取ったあとに追いついてから書き手になる
        other = self.journal(heartbeat=False, acquire_wait=0)
        committed, real_take = threading.Event(), self.b.journal.take

        def take():
            if not committed.is_set():
                try:
                    other.refresh()  # FileJournal の open は、作ったときより後の記録を読み直さない
                    m = other.open(ReferenceEngine())
                    move("p2", "p3", "Jan", 7)(m)  # a が権利を持っている間は Fenced（何もしない）
                    other.release()
                    committed.set()
                except Fenced:
                    pass
            return real_take()
        self.b.journal.take = take
        self.a.write(move("p0", "p1", "Jan", 5))
        self.a.close()
        wait_for(lambda: self.b.role is Role.LEADER, timeout=2.0)
        self.assertTrue(committed.is_set())
        self.assertEqual(self.b.version.get("Stock", Product="p3", Month="Jan"), 107.0)
        self.assertEqual(self.b.write(move("p4", "p5", "Jan", 1)), self.b.journal.head)
        check_same_state(self, self.b.version.model, self.journal().open(ReferenceEngine()))


class FileStandby(Standby, unittest.TestCase):
    pass


class PgStandby(Standby, unittest.TestCase):
    store = PgStore

    def take_by_force(self, endpoint: str = "http://c", seconds: float = 60.0) -> None:
        """別のプロセスがリースを奪ったのと同じ状態にする（世代番号を進め、持ち主を変える）。"""
        import psycopg
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("update nanashi_model set writer_epoch = writer_epoch + 1, lease_holder = 'intruder',"
                         " lease_endpoint = %s, lease_expires = now() + make_interval(secs => %s) where model_id = %s",
                         (endpoint, seconds, self.journals.model_id))

    def test_lease_endpoint_names_the_leader(self):
        self.assertEqual((self.leader_of(self.a), self.leader_of(self.b)), ("http://a", "http://a"))

    def test_server_answers_421_with_the_leader(self):
        from sparse_engine.server import Server

        from .test_server import Client, write
        server = Server(self.b, "127.0.0.1", 0).start()
        try:
            c = Client(server.url, token=None)
            self.assertEqual(c.get("/ready"), (200, {"seq": 0, "ready": True, "role": "standby", "reasons": []}))
            self.assertEqual(c.get("/health"), (200, {"seq": 0, "role": "standby"}))
            status, err = c.post("/writes", {"client_op_id": "x", "ops": [write("Stock", 1, Product="p0", Month="Jan")]})
            self.assertEqual((status, err["error"], err["leader"]), (421, "not_leader", "http://a"))
            with urllib.request.urlopen(server.url + "/stats", timeout=5) as r:  # Prometheus のテキスト
                self.assertIn("nanashi_leader 0\n", r.read().decode())
        finally:
            server.stop()

    def test_leader_demotes_when_its_write_is_fenced(self):
        self.take_by_force()
        with self.assertRaises(NotLeader) as cm:
            self.a.write(move("p0", "p1", "Jan", 1))
        self.assertEqual(cm.exception.leader, "http://c")
        self.assertIs(self.a.role, Role.STANDBY)
        self.assertIs(self.b.role, Role.STANDBY)  # 奪ったプロセスが期限内に持っているので、b も取れない

    def test_leader_demotes_when_the_heartbeat_loses_the_lease(self):
        self.take_by_force()
        wait_for(lambda: self.a.role is Role.STANDBY, timeout=LEASE_TTL + 1.0)  # 書き込みがなくても気づく
        self.assertEqual(self.a.ready(), [])  # 待機系として読み出しは受けられる

    def test_writes_queued_before_the_demotion_fail_with_not_leader(self):
        started, gate = threading.Event(), threading.Event()

        def blocked(m):  # まとまりの途中でライターを止める書き込み
            move("p0", "p1", "Jan", 1)(m)
            started.set()
            gate.wait()
        first = self.a.submit(blocked)
        started.wait()
        queued = self.a.submit(move("p2", "p3", "Jan", 1))  # 次のまとまり
        self.take_by_force(seconds=0.0)  # 奪われてすぐ切れたリース（待たずに取り直せる）
        gate.set()
        with self.assertRaises(NotLeader):  # 止めていたまとまりの確定は締め出され、待機系に戻る
            first.result(timeout=5)
        with self.assertRaises(NotLeader):  # 次のまとまりは、空いたリースを取り直さずに拒む
            queued.result(timeout=5)
        # 空いたリースは、2 つの待機系（a と b）のどちらか 1 つだけが見張りで取って書き手になる
        wait_for(lambda: Role.LEADER in (self.a.role, self.b.role), timeout=2.0)
        leaders = [ws for ws in (self.a, self.b) if ws.role is Role.LEADER]
        self.assertEqual(len(leaders), 1)
        self.assertEqual(leaders[0].write(move("p4", "p5", "Jan", 1)), 1)
