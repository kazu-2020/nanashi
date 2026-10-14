import math
import unittest

from bench_load import parse_head, percentiles, summarize


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


class ParseHeadTest(unittest.TestCase):
    def test_http11_with_length_keeps_the_connection(self):
        self.assertEqual(parse_head(b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\n"), ("200", 12, True))

    def test_http10_or_close_or_no_length_does_not(self):
        self.assertEqual(parse_head(b"HTTP/1.0 503 Busy\r\nContent-Length: 5\r\n\r\n"), ("503", 5, False))
        self.assertEqual(parse_head(b"HTTP/1.1 200 OK\r\nconnection: close\r\ncontent-length: 5\r\n\r\n"), ("200", 5, False))
        self.assertEqual(parse_head(b"HTTP/1.1 200 OK\r\n\r\n"), ("200", None, False))


if __name__ == "__main__":
    unittest.main()
