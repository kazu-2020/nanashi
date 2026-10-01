"""書き手のサーバーのプロセスを止めて、もう一方のサーバーに引き継がせる（tests/failover.py）。"""
import signal
import unittest

from .journals import PG_AVAILABLE


@unittest.skipUnless(PG_AVAILABLE, "PostgreSQL（NANASHI_PG_DSN）と psycopg、nanashi_core が必要")
class Failover(unittest.TestCase):
    # 止めたサーバーのリースが期限まで残り、B は 30 秒近く書けない。SIGTERM でリースを手放すようにしたら外す
    @unittest.expectedFailure
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
