"""Offline tests for bid_portals.py — the persistent per-city bid-portal
directory. No network access: Upstash is force-disabled and each test gets
its own throwaway local file, so these are safe to run anywhere, anytime."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_portals as bp


class BidPortalsTests(unittest.TestCase):
    def setUp(self):
        self._orig_url = bp.UPSTASH_URL
        self._orig_token = bp.UPSTASH_TOKEN
        self._orig_file = bp._LOCAL_FILE
        bp.UPSTASH_URL = ""
        bp.UPSTASH_TOKEN = ""
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(path)  # start from "file doesn't exist yet"
        bp._LOCAL_FILE = path

    def tearDown(self):
        bp.UPSTASH_URL = self._orig_url
        bp.UPSTASH_TOKEN = self._orig_token
        try:
            os.unlink(bp._LOCAL_FILE)
        except OSError:
            pass
        bp._LOCAL_FILE = self._orig_file

    def test_seed_present_on_first_load(self):
        d = bp.load_directory()
        got = bp.get_portals(d, "Springfield", "MO")
        self.assertTrue(got)
        self.assertEqual(got[0]["url"], "https://www.springfieldmo.gov/bids.aspx")

    def test_lookup_is_case_and_whitespace_insensitive(self):
        d = bp.load_directory()
        self.assertTrue(bp.get_portals(d, "  SPRINGFIELD ", "mo"))

    def test_unknown_city_returns_empty(self):
        d = bp.load_directory()
        self.assertEqual(bp.get_portals(d, "Nowhereville", "ZZ"), [])

    def test_aggregator_url_is_not_learned(self):
        d = bp.load_directory()
        bp.learn_portal(d, "Test City", "MO", "https://www.bidnetdirect.com/mo/listing")
        bp.learn_portal(d, "Test City", "MO", "https://sub.demandstar.com/x")
        self.assertEqual(bp.get_portals(d, "Test City", "MO"), [])

    def test_real_url_is_learned_and_persists_across_loads(self):
        d = bp.load_directory()
        bp.learn_portal(d, "Test City", "MO", "https://www.testcity.gov/bids.aspx")
        bp.save_directory(d)

        d2 = bp.load_directory()
        got = bp.get_portals(d2, "Test City", "MO")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["url"], "https://www.testcity.gov/bids.aspx")
        self.assertEqual(got[0]["source"], "learned")

    def test_learning_same_url_twice_does_not_duplicate(self):
        d = bp.load_directory()
        url = "https://www.testcity.gov/bids.aspx"
        bp.learn_portal(d, "Test City", "MO", url)
        bp.learn_portal(d, "Test City", "MO", url)
        self.assertEqual(len(bp.get_portals(d, "Test City", "MO")), 1)

    def test_entry_ages_out_after_max_consecutive_failures(self):
        d = bp.load_directory()
        url = "https://www.failcity.gov/bids.aspx"
        bp.learn_portal(d, "Fail City", "MO", url)
        for _ in range(bp.MAX_FAIL):
            bp.record_result(d, "Fail City", "MO", url, False)
        self.assertEqual(bp.get_portals(d, "Fail City", "MO"), [])

    def test_single_transient_failure_does_not_drop_entry(self):
        d = bp.load_directory()
        url = "https://www.flakycity.gov/bids.aspx"
        bp.learn_portal(d, "Flaky City", "MO", url)
        bp.record_result(d, "Flaky City", "MO", url, False)
        self.assertTrue(bp.get_portals(d, "Flaky City", "MO"))

    def test_success_resets_failure_count(self):
        d = bp.load_directory()
        url = "https://www.recovering.gov/bids.aspx"
        bp.learn_portal(d, "Recovering City", "MO", url)
        for _ in range(bp.MAX_FAIL - 1):
            bp.record_result(d, "Recovering City", "MO", url, False)
        bp.record_result(d, "Recovering City", "MO", url, True)
        entries = d[bp._key("Recovering City", "MO")]
        self.assertEqual(entries[0]["fail_count"], 0)

    def test_is_aggregator_url(self):
        self.assertTrue(bp.is_aggregator_url("https://www.demandstar.com/x"))
        self.assertTrue(bp.is_aggregator_url("https://sub.bidnetdirect.com/x"))
        self.assertFalse(bp.is_aggregator_url("https://www.springfieldmo.gov/bids.aspx"))


if __name__ == "__main__":
    unittest.main()
