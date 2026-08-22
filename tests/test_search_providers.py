"""Tests for _web_search — which search provider a scan actually spends on.

Tavily's paid allowance was being exhausted in a day: a scan was 35-53
searches and Tavily ran FIRST on every one. Google Programmable Search
replaced it as the lead, then Google announced it is deprecating the Custom
Search JSON API and stopped letting projects enable it, so that integration
was removed rather than left to 403 forever. Brave leads now: a real API,
roughly a thousand queries a month on the free credit.

The `used_scraper` half of the return matters as much as the results: callers
use it to apply DDG's pacing delay, and the old code inferred it from "did
Tavily return nothing", which silently became wrong the moment a second keyed
provider existed.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls

HIT = [{"url": "https://example.gov/bids", "content": "sidewalk bid"}]


class ProviderOrderTests(unittest.TestCase):
    def setUp(self):
        self._p = [
            patch.object(ls, "BRAVE_API_KEY", "b-key"),
            patch.object(ls, "TAVILY_API_KEY", "t-key"),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_brave_is_tried_first_and_tavily_is_never_touched(self):
        """The whole point: a working Brave result must not cost a Tavily
        credit."""
        with patch.object(ls, "_brave_search", return_value=HIT) as b, \
             patch.object(ls, "_tavily_search") as t, \
             patch.object(ls, "_ddg_search") as d:
            results, used_scraper = ls._web_search("q")
        self.assertEqual(results, HIT)
        self.assertFalse(used_scraper)
        b.assert_called_once()
        t.assert_not_called()
        d.assert_not_called()

    def test_tavily_covers_an_empty_brave(self):
        with patch.object(ls, "_brave_search", return_value=[]), \
             patch.object(ls, "_tavily_search", return_value=HIT) as t, \
             patch.object(ls, "_ddg_search") as d:
            results, used_scraper = ls._web_search("q")
        self.assertEqual(results, HIT)
        self.assertFalse(used_scraper)
        t.assert_called_once()
        d.assert_not_called()

    def test_duckduckgo_is_the_last_resort_and_is_flagged_as_scraped(self):
        with patch.object(ls, "_brave_search", return_value=[]), \
             patch.object(ls, "_tavily_search", return_value=[]), \
             patch.object(ls, "_ddg_search", return_value=HIT):
            results, used_scraper = ls._web_search("q")
        self.assertEqual(results, HIT)
        self.assertTrue(used_scraper,
                        "callers pace themselves off this flag")


class ProviderSkippingTests(unittest.TestCase):
    def test_brave_is_skipped_entirely_without_a_key(self):
        with patch.object(ls, "BRAVE_API_KEY", ""), \
             patch.object(ls, "TAVILY_API_KEY", "t-key"), \
             patch.object(ls, "_brave_search") as b, \
             patch.object(ls, "_tavily_search", return_value=HIT):
            ls._web_search("q")
        b.assert_not_called()

    def test_with_no_keys_at_all_only_the_scraper_runs(self):
        with patch.object(ls, "BRAVE_API_KEY", ""), \
             patch.object(ls, "TAVILY_API_KEY", ""), \
             patch.object(ls, "_brave_search") as b, \
             patch.object(ls, "_tavily_search") as t, \
             patch.object(ls, "_ddg_search", return_value=HIT):
            results, used_scraper = ls._web_search("q")
        b.assert_not_called()
        t.assert_not_called()
        self.assertTrue(used_scraper)


class BraveRequestTests(unittest.TestCase):
    def test_no_key_means_no_request(self):
        with patch.object(ls, "BRAVE_API_KEY", ""):
            self.assertEqual(ls._brave_search("q"), [])

    def test_maps_braves_response_shape_to_ours(self):
        payload = (b'{"web":{"results":[{"url":"https://a.gov/x",'
                   b'"description":"sidewalk bid","title":"Bids"}]}}')
        with patch.object(ls, "BRAVE_API_KEY", "k"), \
             patch.object(ls, "_brave_note"), \
             patch.object(ls, "_brave_wait_turn"), \
             patch("license_server.urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value.read.return_value = payload
            out = ls._brave_search("q")
        self.assertEqual(out, [{"url": "https://a.gov/x",
                                "content": "sidewalk bid"}])

    def test_the_title_stands_in_when_there_is_no_description(self):
        payload = b'{"web":{"results":[{"url":"https://a.gov/x","title":"Bids"}]}}'
        with patch.object(ls, "BRAVE_API_KEY", "k"), \
             patch.object(ls, "_brave_note"), \
             patch.object(ls, "_brave_wait_turn"), \
             patch("license_server.urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value.read.return_value = payload
            out = ls._brave_search("q")
        self.assertEqual(out[0]["content"], "Bids")

    def test_a_result_with_no_url_is_dropped_rather_than_returned_empty(self):
        payload = (b'{"web":{"results":[{"description":"no url here"},'
                   b'{"url":"https://a.gov/x","description":"real"}]}}')
        with patch.object(ls, "BRAVE_API_KEY", "k"), \
             patch.object(ls, "_brave_note"), \
             patch.object(ls, "_brave_wait_turn"), \
             patch("license_server.urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value.read.return_value = payload
            out = ls._brave_search("q")
        self.assertEqual([r["url"] for r in out], ["https://a.gov/x"])

    def test_an_empty_response_is_not_an_error(self):
        with patch.object(ls, "BRAVE_API_KEY", "k"), \
             patch.object(ls, "_brave_note"), \
             patch.object(ls, "_brave_wait_turn"), \
             patch("license_server.urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value.read.return_value = b"{}"
            self.assertEqual(ls._brave_search("q"), [])

    def test_count_is_clamped_to_braves_maximum_of_twenty(self):
        """Brave rejects count>20, which would turn a wide scan into a hard
        failure rather than fewer results."""
        with patch.object(ls, "BRAVE_API_KEY", "k"), \
             patch.object(ls, "_brave_note"), \
             patch.object(ls, "_brave_wait_turn"), \
             patch("license_server.urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value.read.return_value = b"{}"
            ls._brave_search("q", max_results=50)
            url = op.call_args[0][0].full_url
        self.assertIn("count=20", url)

    def test_the_key_travels_in_the_header_not_the_query_string(self):
        """A key in the URL ends up in logs and proxy history."""
        with patch.object(ls, "BRAVE_API_KEY", "super-secret"), \
             patch.object(ls, "_brave_note"), \
             patch.object(ls, "_brave_wait_turn"), \
             patch("license_server.urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value.read.return_value = b"{}"
            ls._brave_search("q")
            req = op.call_args[0][0]
        self.assertNotIn("super-secret", req.full_url)
        self.assertEqual(req.get_header("X-subscription-token"), "super-secret")


class BravePacingTests(unittest.TestCase):
    """The free tier allows one request per second, and scans fan out across
    threads. Without pacing, several land in the same second and come back
    429 -- which reads identically to the monthly credit being spent."""

    def test_a_second_call_waits_for_its_turn(self):
        slept = []
        with patch.object(ls, "BRAVE_MIN_INTERVAL", 1.1), \
             patch("license_server.time.sleep", side_effect=slept.append), \
             patch("license_server.time.time", side_effect=[100.0, 100.0,
                                                            100.2, 100.2]):
            ls._brave_last_call[0] = 0.0
            ls._brave_wait_turn()   # first call: no wait
            ls._brave_wait_turn()   # 0.2s later: must wait the remainder
        self.assertTrue(slept, "second call should have paced itself")
        self.assertAlmostEqual(slept[0], 0.9, places=5)

    def test_a_call_after_the_interval_does_not_wait(self):
        slept = []
        with patch.object(ls, "BRAVE_MIN_INTERVAL", 1.1), \
             patch("license_server.time.sleep", side_effect=slept.append), \
             patch("license_server.time.time", return_value=500.0):
            ls._brave_last_call[0] = 100.0
            ls._brave_wait_turn()
        self.assertEqual(slept, [])


class BraveHealthTests(unittest.TestCase):
    def setUp(self):
        ls._brave_state.update({"ok": 0, "failed": 0, "last_error": "",
                                "last_status": 0})

    def test_rate_or_quota_exhaustion_is_reported_distinctly(self):
        ls._brave_note(False, 429, "rate limit")
        self.assertTrue(ls._brave_health()["quota_or_auth_failure"])

    def test_a_bad_key_is_reported_distinctly(self):
        ls._brave_note(False, 401, "unauthorized")
        self.assertTrue(ls._brave_health()["quota_or_auth_failure"])

    def test_a_transient_failure_is_not_a_quota_problem(self):
        ls._brave_note(False, 500, "server error")
        self.assertFalse(ls._brave_health()["quota_or_auth_failure"])

    def test_success_clears_the_failing_flag(self):
        ls._brave_note(False, 500, "boom")
        ls._brave_note(True)
        self.assertFalse(ls._brave_health()["failing"])


if __name__ == "__main__":
    unittest.main()


class CircuitBreakerTests(unittest.TestCase):
    """A provider that has just answered 401/403/429 will answer the same way
    for every remaining query in the scan. Asking it twelve more times costs a
    round trip each -- and for Brave, 1.1s of pacing each -- before falling
    through to the same fallback every time."""

    def setUp(self):
        ls._provider_down_until.clear()
        self.addCleanup(ls._provider_down_until.clear)

    def test_a_quota_refusal_benches_the_provider(self):
        ls._provider_mark_down("brave", 429)
        self.assertTrue(ls._provider_is_down("brave"))

    def test_an_auth_refusal_benches_it_too(self):
        for code in (401, 403):
            ls._provider_down_until.clear()
            ls._provider_mark_down("brave", code)
            self.assertTrue(ls._provider_is_down("brave"), code)

    def test_a_transient_error_does_not_bench_it(self):
        """A 500 or a timeout may well succeed on the next query -- benching
        on those would give away a working provider."""
        for code in (500, 502, 0):
            ls._provider_mark_down("brave", code)
            self.assertFalse(ls._provider_is_down("brave"), code)

    def test_benching_one_provider_leaves_the_other_alone(self):
        ls._provider_mark_down("brave", 429)
        self.assertFalse(ls._provider_is_down("tavily"))

    def test_a_benched_provider_is_skipped_and_the_fallback_runs(self):
        ls._provider_mark_down("brave", 429)
        with patch.object(ls, "BRAVE_API_KEY", "b-key"), \
             patch.object(ls, "TAVILY_API_KEY", "t-key"), \
             patch.object(ls, "_brave_search") as b, \
             patch.object(ls, "_tavily_search", return_value=HIT) as t:
            results, _ = ls._web_search("q")
        b.assert_not_called()
        t.assert_called_once()
        self.assertEqual(results, HIT)

    def test_both_benched_falls_through_to_the_scraper(self):
        ls._provider_mark_down("brave", 429)
        ls._provider_mark_down("tavily", 429)
        with patch.object(ls, "BRAVE_API_KEY", "b"), \
             patch.object(ls, "TAVILY_API_KEY", "t"), \
             patch.object(ls, "_brave_search") as b, \
             patch.object(ls, "_tavily_search") as t, \
             patch.object(ls, "_ddg_search", return_value=HIT):
            results, used_scraper = ls._web_search("q")
        b.assert_not_called()
        t.assert_not_called()
        self.assertTrue(used_scraper)

    def test_the_bench_expires(self):
        ls._provider_mark_down("brave", 429)
        with patch.object(ls, "_provider_down_until", {"brave": 0}):
            self.assertFalse(ls._provider_is_down("brave"))

    def test_a_success_clears_the_bench_early(self):
        """Tavily's credit renews monthly and Brave's cap is per-second, so a
        provider can recover well before the cooldown is up."""
        ls._provider_mark_down("brave", 429)
        ls._provider_clear("brave")
        self.assertFalse(ls._provider_is_down("brave"))
