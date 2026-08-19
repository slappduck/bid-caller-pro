"""Tests for _web_search — which search provider a scan actually spends on.

Tavily's paid allowance was being exhausted in a day: a scan is 35-53 searches
and Tavily ran FIRST on every one. Google Programmable Search is free at
100/day, so it leads now, Tavily is an optional paid fallback, and scraping
DuckDuckGo is the last resort.

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
            patch.object(ls, "GOOGLE_API_KEY", "g-key"),
            patch.object(ls, "GOOGLE_CSE_ID", "cse-id"),
            patch.object(ls, "TAVILY_API_KEY", "t-key"),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_google_is_tried_first_and_tavily_is_never_touched(self):
        """The whole point: a working Google result must not cost a Tavily
        credit."""
        with patch.object(ls, "_google_search", return_value=HIT) as g, \
             patch.object(ls, "_tavily_search") as t, \
             patch.object(ls, "_ddg_search") as d:
            results, scraped = ls._web_search("q")
        self.assertEqual(results, HIT)
        self.assertFalse(scraped)
        g.assert_called_once()
        t.assert_not_called()
        d.assert_not_called()

    def test_tavily_covers_an_empty_google(self):
        with patch.object(ls, "_google_search", return_value=[]), \
             patch.object(ls, "_tavily_search", return_value=HIT) as t, \
             patch.object(ls, "_ddg_search") as d:
            results, scraped = ls._web_search("q")
        self.assertEqual(results, HIT)
        self.assertFalse(scraped)
        t.assert_called_once()
        d.assert_not_called()

    def test_ddg_is_the_last_resort_and_is_reported_as_scraped(self):
        with patch.object(ls, "_google_search", return_value=[]), \
             patch.object(ls, "_tavily_search", return_value=[]), \
             patch.object(ls, "_ddg_search", return_value=HIT) as d:
            results, scraped = ls._web_search("q")
        self.assertEqual(results, HIT)
        self.assertTrue(scraped, "callers need this to apply the DDG pacing delay")
        d.assert_called_once()


class MissingKeyTests(unittest.TestCase):
    def test_google_is_skipped_entirely_without_both_halves(self):
        """A key with no engine id cannot search. It must not be called at
        all, rather than called and failing."""
        with patch.object(ls, "GOOGLE_API_KEY", "g-key"), \
             patch.object(ls, "GOOGLE_CSE_ID", ""), \
             patch.object(ls, "TAVILY_API_KEY", "t-key"), \
             patch.object(ls, "_google_search") as g, \
             patch.object(ls, "_tavily_search", return_value=HIT):
            results, _ = ls._web_search("q")
        self.assertEqual(results, HIT)
        g.assert_not_called()

    def test_no_keys_at_all_goes_straight_to_scraping(self):
        with patch.object(ls, "GOOGLE_API_KEY", ""), \
             patch.object(ls, "GOOGLE_CSE_ID", ""), \
             patch.object(ls, "TAVILY_API_KEY", ""), \
             patch.object(ls, "_google_search") as g, \
             patch.object(ls, "_tavily_search") as t, \
             patch.object(ls, "_ddg_search", return_value=HIT):
            results, scraped = ls._web_search("q")
        self.assertEqual(results, HIT)
        self.assertTrue(scraped)
        g.assert_not_called()
        t.assert_not_called()

    def test_tavily_removed_entirely_still_works(self):
        """Dropping Tavily is the expected end state, not a broken config."""
        with patch.object(ls, "GOOGLE_API_KEY", "g-key"), \
             patch.object(ls, "GOOGLE_CSE_ID", "cse-id"), \
             patch.object(ls, "TAVILY_API_KEY", ""), \
             patch.object(ls, "_google_search", return_value=HIT), \
             patch.object(ls, "_tavily_search") as t:
            results, scraped = ls._web_search("q")
        self.assertEqual(results, HIT)
        self.assertFalse(scraped)
        t.assert_not_called()


class GoogleSearchTests(unittest.TestCase):
    def test_returns_nothing_without_credentials_rather_than_raising(self):
        with patch.object(ls, "GOOGLE_API_KEY", ""), \
             patch.object(ls, "GOOGLE_CSE_ID", ""):
            self.assertEqual(ls._google_search("q"), [])

    def test_maps_googles_response_shape_to_ours(self):
        payload = {"items": [
            {"link": "https://a.gov/bids", "snippet": "sidewalk replacement"},
            {"link": "https://b.gov/rfp", "snippet": "ADA ramps"},
        ]}
        with patch.object(ls, "GOOGLE_API_KEY", "k"), \
             patch.object(ls, "GOOGLE_CSE_ID", "c"), \
             patch.object(ls, "_google_note"), \
             patch("license_server.urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value.read.return_value = \
                __import__("json").dumps(payload).encode()
            out = ls._google_search("q")
        self.assertEqual(out, [
            {"url": "https://a.gov/bids", "content": "sidewalk replacement"},
            {"url": "https://b.gov/rfp", "content": "ADA ramps"},
        ])

    def test_items_without_a_link_are_dropped(self):
        payload = {"items": [{"snippet": "no link here"},
                             {"link": "https://a.gov/x", "snippet": "ok"}]}
        with patch.object(ls, "GOOGLE_API_KEY", "k"), \
             patch.object(ls, "GOOGLE_CSE_ID", "c"), \
             patch.object(ls, "_google_note"), \
             patch("license_server.urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value.read.return_value = \
                __import__("json").dumps(payload).encode()
            out = ls._google_search("q")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], "https://a.gov/x")

    def test_an_empty_response_is_not_an_error(self):
        with patch.object(ls, "GOOGLE_API_KEY", "k"), \
             patch.object(ls, "GOOGLE_CSE_ID", "c"), \
             patch.object(ls, "_google_note"), \
             patch("license_server.urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value.read.return_value = b"{}"
            self.assertEqual(ls._google_search("q"), [])

    def test_num_is_clamped_to_googles_maximum_of_ten(self):
        """Google rejects num>10 outright, which would turn a wide scan into
        a hard failure rather than fewer results."""
        seen = {}
        with patch.object(ls, "GOOGLE_API_KEY", "k"), \
             patch.object(ls, "GOOGLE_CSE_ID", "c"), \
             patch.object(ls, "_google_note"), \
             patch("license_server.urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value.read.return_value = b"{}"
            ls._google_search("q", max_results=50)
            seen["url"] = op.call_args[0][0].full_url
        self.assertIn("num=10", seen["url"])


class GoogleHealthTests(unittest.TestCase):
    def setUp(self):
        ls._google_state.update({"ok": 0, "failed": 0, "last_error": "",
                                 "last_status": 0})

    def test_quota_exhaustion_is_reported_distinctly(self):
        ls._google_note(False, 429, "rate limit")
        self.assertTrue(ls._google_health()["quota_or_auth_failure"])

    def test_api_not_enabled_is_reported_distinctly(self):
        ls._google_note(False, 403, "not enabled")
        self.assertTrue(ls._google_health()["quota_or_auth_failure"])

    def test_a_transient_failure_is_not_a_quota_problem(self):
        ls._google_note(False, 500, "server error")
        self.assertFalse(ls._google_health()["quota_or_auth_failure"])

    def test_success_clears_the_failing_flag(self):
        ls._google_note(False, 500, "boom")
        ls._google_note(True)
        self.assertFalse(ls._google_health()["failing"])


if __name__ == "__main__":
    unittest.main()
