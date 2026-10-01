"""書き手のサーバーのプロセスを止めて、もう一方のサーバーに引き継がせる（tests/failover.py）。"""
import signal
import unittest

from .journals import DSN, PG_AVAILABLE


@unittest.skipUnless(PG_AVAILABLE, "PostgreSQL（NANASHI_PG_DSN）と psycopg、nanashi_core が必要")
class Failover(unittest.TestCase):
    def test_sigterm_handover(self):
        from .failover import run, violations
        report = run(signal.SIGTERM)
        self.assertEqual(violations(report), [])
        self.assertLess(report.gap, 5.0, report.failures)

    def test_sigkill_handover(self):
        # リースが期限まで残るので、引き継ぎは lease_ttl（30 秒）近く待つ
        from .failover import run, violations
        report = run(signal.SIGKILL)
        self.assertEqual(violations(report), [])

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
