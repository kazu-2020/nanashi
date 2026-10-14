import math
import unittest

from bench_load import percentiles, summarize


class SummarizeTest(unittest.TestCase):
    def test_merges_the_processes_and_counts_only_200_in_the_rate(self):
        a = {"lat": [0.001, 0.002], "status": {"200": 2}}
        b = {"lat": [0.003, 0.004], "status": {"200": 2, "503": 1}}
        r = summarize([a, b], 2.0)
        self.assertEqual(r["rps"], 2.0)
        self.assertEqual(r["status"], {"200": 4, "503": 1})
        self.assertEqual((r["p50"], r["p99"]), (2.0, 4.0))

    def test_empty_latencies_give_nan(self):
        self.assertTrue(math.isnan(percentiles([])["p50"]))


if __name__ == "__main__":
    unittest.main()
