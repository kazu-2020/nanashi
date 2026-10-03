"""書き手のサーバーのプロセスを止めて、もう一方のサーバーに引き継がせる（tests/failover.py）。"""
import shutil
import signal
import unittest

from .journals import DSN, PG_AVAILABLE


@unittest.skipUnless(PG_AVAILABLE, "PostgreSQL（NANASHI_PG_DSN）と psycopg、nanashi_core が必要")
class Failover(unittest.TestCase):
    def test_sigterm_handover(self):
        # 権利を手放したときの通知で待機系がすぐ取るので、間隔を待たない
        from .failover import run, violations
        report = run(signal.SIGTERM)
        self.assertEqual(violations(report), [])
        self.assertLess(report.gap, 2.0, report.failures)

    def test_sigkill_handover(self):
        # リースが期限まで残るので、引き継ぎはその分（LEASE_TTL）と、待機系が権利を試す間隔（1 秒）を待つ
        from .failover import LEASE_TTL, run, violations
        report = run(signal.SIGKILL)
        self.assertEqual(violations(report), [])
        self.assertLess(report.gap, LEASE_TTL + 3.0, report.failures)

    @unittest.skipUnless(shutil.which("go"), "ルーターのビルドに Go が要る")
    def test_router_sigterm_handover(self):
        # 送り手はルーターにだけ送り、書き手を探して送り直すのはルーターに任せる
        from .failover import run, violations
        report = run(signal.SIGTERM, via_router=True)
        self.assertEqual(violations(report), [])
        self.assertLess(report.gap, 2.0)
        self.assertEqual(report.failures, {})  # ルーターは 200 以外を返さず、つながらないこともない

    @unittest.skipUnless(shutil.which("go"), "ルーターのビルドに Go が要る")
    def test_router_sigkill_handover(self):
        from .failover import LEASE_TTL, run, violations
        report = run(signal.SIGKILL, via_router=True)
        self.assertEqual(violations(report), [])
        self.assertLess(report.gap, LEASE_TTL + 3.0)
        self.assertEqual(report.failures, {})

    def test_sigterm_releases_lease(self):
        import psycopg

        from .failover import Write, post, seeded_model, serving
        with seeded_model() as (model_id, tmp), serving(model_id, tmp, "a") as a:
            post(a.url, Write("w-1", "i001", 7))
            a.proc.send_signal(signal.SIGTERM)
            self.assertEqual(a.proc.wait(timeout=10), 0, a.log.read_text())
            with psycopg.connect(DSN) as conn:
                released, = conn.execute("select lease_expires <= now() from nanashi_model where model_id = %s",
                                         (model_id,)).fetchone()
            self.assertTrue(released)

    def test_one_leader(self):
        import psycopg

        from .failover import Write, get, post, seeded_model, serving, try_post
        with (seeded_model() as (model_id, tmp), serving(model_id, tmp, "a", role="leader") as a,
              serving(model_id, tmp, "b", role="standby") as b):
            self.assertEqual([get(s.url + "/ready")[1]["role"] for s in (a, b)], ["leader", "standby"])
            status, body = try_post(b.url, Write("w-1", "i001", 7))  # 待機系は書き手の番地を返して拒む
            self.assertEqual((status, body["error"], body["leader"]), (421, "not_leader", a.url))
            with psycopg.connect(DSN) as conn:
                endpoint, = conn.execute("select lease_endpoint from nanashi_model where model_id = %s",
                                         (model_id,)).fetchone()
            self.assertEqual(endpoint, a.url)  # ルーターはこれで書き手を見つける
            self.assertEqual(post(a.url, Write("w-1", "i001", 7)), 1)

    def test_restarted_process_rejoins_as_standby(self):
        from .failover import Write, get, post, seeded_model, serving, try_post, wait_ready
        from .test_replica import wait_for
        with (seeded_model() as (model_id, tmp), serving(model_id, tmp, "a", role="leader") as a,
              serving(model_id, tmp, "b", role="standby") as b):
            post(a.url, Write("w-1", "i001", 7))
            a.proc.send_signal(signal.SIGTERM)
            self.assertEqual(a.proc.wait(timeout=10), 0, a.log.read_text())
            wait_ready(b, role="leader")
            with serving(model_id, tmp, "a2", role="standby") as a2:  # 起動し直したプロセスは待機系になる
                seq = post(b.url, Write("w-2", "i002", 8))
                wait_for(lambda: get(a2.url + "/health")[1]["seq"] == seq)  # 書き手の確定に追従する
                self.assertEqual(get(a2.url + "/metrics/Double/cell?Item=i002")[1]["value"], 16.0)
                status, body = try_post(a2.url, Write("w-3", "i003", 9))
                self.assertEqual((status, body["leader"]), (421, b.url))
