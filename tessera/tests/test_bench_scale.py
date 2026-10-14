import unittest

from bench_scale import compare, tree_rss


class TreeRssTest(unittest.TestCase):
    def test_sums_the_root_and_all_descendants_only(self):
        ps = "1 0 100\n10 1 5\n20 10 7\n30 1 11\n40 99 1000\n"  # pid ppid rss(KiB)
        self.assertEqual(tree_rss(ps, 10), (5 + 7) * 1024)
        self.assertEqual(tree_rss(ps, 1), (100 + 5 + 7 + 11) * 1024)
        self.assertEqual(tree_rss(ps, 12345), 0)


class CompareTest(unittest.TestCase):
    def test_shows_the_ratio_of_common_phases_and_the_failed_sizes(self):
        phase = lambda s, h: {"seconds": s, "heap_peak": h, "heap_after": h}
        before = [{"scale": 1, "status": "ok", "phases": {"recalc": phase(0.1, 100e6)}}]
        after = [{"scale": 1, "status": "ok", "phases": {"recalc": phase(0.05, 300e6), "new": phase(1, 1)}},
                 {"scale": 2, "status": "over_limit", "rss_max": 9e9}]
        before.append({"scale": 4, "status": "over_limit", "rss_max": 9e9})
        after.append({"scale": 4, "status": "ok", "phases": {"recalc": phase(1, 1)}})
        after.append({"scale": 8, "status": "ok", "phases": {"recalc": phase(1, 1)}})
        lines = compare(before, after).splitlines()
        self.assertEqual(lines[2:], ["| ×1 | recalc | 100 ms → 50 ms (×0.50) | 100 → 300 MB (×3.00) |",
                                     "| ×2 | - → over_limit | | |",
                                     "| ×4 | over_limit → ok | | |",
                                     "| ×8 | - → ok | | |"])


if __name__ == "__main__":
    unittest.main()
