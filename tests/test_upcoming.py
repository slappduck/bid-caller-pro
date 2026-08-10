"""Tests for /upcoming, the Upcoming Projects tab's backend.

Two things had drifted out of parity with /scan:
  * pages were fetched and AI-extracted one after another, so a full page
    budget ran past the app's own request timeout and the user just saw
    "that took too long";
  * planned projects naming a county or an authority were dropped outright,
    the same recall loss already fixed on the bid path.
"""
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls

CENTER = {"lat": 36.9709, "lon": -93.7180, "city": "Aurora", "state": "MO"}


class UpcomingTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()
        self._orig_gate = ls._license_is_active
        ls._license_is_active = lambda *a, **k: True
        self.cache = {}
        self._patchers = [
            patch("license_server._cache", side_effect=lambda: self.cache),
            patch("license_server._save_cache"),
            patch("license_server._resolve_center", return_value=CENTER),
            patch("license_server.OPENAI_API_KEY", "test-key"),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        ls._license_is_active = self._orig_gate
        for p in self._patchers:
            p.stop()

    @staticmethod
    def _pages(n):
        return [{"url": f"https://city{i}.mo.gov/cip", "content": "x" * 400} for i in range(n)]

    def _post(self, radius=25):
        return self.client.post("/upcoming",
                                json={"location": "Aurora, MO", "radius": radius})

    def test_pages_are_processed_concurrently(self):
        """Sequentially, twelve slow pages would take ~1.2s; fanned out across
        the worker pool it should be a fraction of that."""
        overlap = {"max": 0, "now": 0}
        lock = threading.Lock()

        def slow_extract(area, text):
            with lock:
                overlap["now"] += 1
                overlap["max"] = max(overlap["max"], overlap["now"])
            time.sleep(0.1)
            with lock:
                overlap["now"] -= 1
            return []

        with patch("license_server._tavily_search", return_value=self._pages(12)), \
             patch("license_server.TAVILY_API_KEY", "t"), \
             patch("license_server._ai_extract_upcoming", side_effect=slow_extract):
            started = time.time()
            resp = self._post()
        elapsed = time.time() - started
        self.assertTrue(resp.get_json()["ok"])
        self.assertGreater(overlap["max"], 1, "pages were processed one at a time")
        self.assertLess(elapsed, 1.0, f"took {elapsed:.2f}s — not running in parallel")

    def test_a_planned_project_naming_a_county_is_kept(self):
        item = {"title": "Greene County ADA ramp program", "scope": "curb ramps",
                "timeframe": "FY2027", "city": "Greene County Road District 3"}
        with patch("license_server._tavily_search", return_value=self._pages(1)), \
             patch("license_server.TAVILY_API_KEY", "t"), \
             patch("license_server._ai_extract_upcoming", return_value=[item]), \
             patch("license_server._geo_from_city", return_value=None):
            body = self._post().get_json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["debug"]["funnel"].get("placed_by_search_town"), 1)

    def test_planned_items_keep_their_planned_status(self):
        item = {"title": "Sidewalk plan", "scope": "s", "timeframe": "FY2027",
                "city": "Aurora"}
        with patch("license_server._tavily_search", return_value=self._pages(1)), \
             patch("license_server.TAVILY_API_KEY", "t"), \
             patch("license_server._ai_extract_upcoming", return_value=[item]), \
             patch("license_server._geo_from_city",
                   return_value={"lat": 36.97, "lon": -93.72, "city": "Aurora", "state": "MO"}):
            body = self._post().get_json()
        placed = body["items"]["Aurora"][0]
        self.assertEqual(placed["status"], "Planned")
        # "Planned" must not be counted as an open bid anywhere.
        self.assertFalse(ls._is_open_bid(placed))

    def test_the_funnel_is_reported(self):
        with patch("license_server._tavily_search", return_value=self._pages(2)), \
             patch("license_server.TAVILY_API_KEY", "t"), \
             patch("license_server._ai_extract_upcoming",
                   return_value=[{"title": "no location given", "city": ""}]):
            body = self._post().get_json()
        self.assertEqual(body["debug"]["funnel"].get("no_location"), 2)

    def test_missing_openai_key_is_reported_not_crashed(self):
        with patch("license_server.OPENAI_API_KEY", ""):
            body = self._post().get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["reason"], "ai_unavailable")

    def test_same_day_repeat_is_served_from_cache(self):
        with patch("license_server._tavily_search", return_value=self._pages(1)), \
             patch("license_server.TAVILY_API_KEY", "t"), \
             patch("license_server._ai_extract_upcoming", return_value=[]) as extract:
            self._post()
            first = extract.call_count
            body = self._post().get_json()
        self.assertTrue(body.get("cached"))
        self.assertEqual(extract.call_count, first, "cache did not prevent a re-scan")


if __name__ == "__main__":
    unittest.main()
