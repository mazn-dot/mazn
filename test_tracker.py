import unittest
from unittest.mock import patch

import tracker


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.wallets = {"A": "0x" + "1" * 40}
        self.sample = {"0x" + "a" * 40: {"amount": 10.0, "count": 12, "symbol": "ABC", "name": "Alpha"}}

    def test_report_and_opportunity_paths(self):
        with patch.object(tracker, "transfers", side_effect=[self.sample, self.sample, self.sample, {}]):
            report = tracker.get_report(5, "out", self.wallets)
            opportunity = tracker.get_opportunity(5, self.wallets)
            clean = tracker.get_clean_opportunity(5, self.wallets)
        self.assertEqual(report["_meta"]["source"], "public_bsc_rpc")
        self.assertEqual(opportunity["ranked"][0]["symbol"], "ABC")
        self.assertEqual(clean["clean"][0]["count"], 12)

    def test_best_and_discovery_paths(self):
        result = {"_meta": {"source": "public_bsc_rpc"}, "ranked": [{"contract": "0x" + "a" * 40, "symbol": "ABC", "name": "Alpha", "amount": 2.0, "count": 12}]}
        with patch.object(tracker, "get_opportunity", return_value=result), patch.object(tracker, "rpc", return_value=None):
            best = tracker.get_best_opportunities(self.wallets)
            discovery = tracker.get_top_counterparties(5, self.wallets)
        self.assertEqual(best["ranked"][0]["symbol"], "ABC")
        self.assertEqual(discovery["top_depositors"], [])

    def test_rpc_limit_returns_none_without_external_call(self):
        tracker._rpc_times.clear()
        with patch.object(tracker.requests, "post") as post:
            for _ in range(45):
                tracker._rpc_times.append(__import__("time").time())
            self.assertIsNone(tracker.rpc("eth_blockNumber", []))
            post.assert_not_called()

    def test_rpc_failure_is_safe(self):
        with patch.object(tracker.requests, "post", side_effect=tracker.requests.RequestException("offline")):
            self.assertIsNone(tracker.rpc("eth_blockNumber", []))


if __name__ == "__main__":
    unittest.main()
