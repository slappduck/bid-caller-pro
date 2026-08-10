"""Tests for /health.

The point of the endpoint is to make silent degradation visible: when a search
backend is unset or DuckDuckGo starts getting blocked, /scan still returns 200
with a thinner set of bids and nothing says why. These check it actually says
so, and that it never echoes a secret's value.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls

SECRET = "sk-do-not-leak-this-value"

FULLY_CONFIGURED = {
    "OPENAI_API_KEY": SECRET,
    "TAVILY_API_KEY": SECRET,
    "SAM_API_KEY": SECRET,
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_ANON_KEY": SECRET,
    "UPSTASH_URL": "https://example.upstash.io",
    "UPSTASH_TOKEN": SECRET,
    "RESEND_API_KEY": SECRET,
    "SUPABASE_SERVICE_ROLE_KEY": SECRET,
    "CRON_SECRET": SECRET,
}


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()

    def _get(self, **overrides):
        env = dict(FULLY_CONFIGURED)
        env.update(overrides)
        with patch.multiple(ls, **env):
            return self.client.get("/health").get_json()

    def test_plain_root_probe_is_unchanged(self):
        # Uptime monitors point at "/" — its contract must not shift.
        self.assertEqual(self.client.get("/").get_json(),
                         {"service": "Bid Caller Pro License Server", "status": "ok"})

    def test_fully_configured_reports_ok_with_no_problems(self):
        body = self._get()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["problems"], [])
        self.assertTrue(all(body["backends"].values()))

    def test_never_echoes_a_secret_value(self):
        import json
        with patch.object(ls, "_ddg_fail_streak", 0):
            raw = json.dumps(self._get())
        self.assertNotIn(SECRET, raw)

    def test_missing_openai_is_called_out(self):
        body = self._get(OPENAI_API_KEY="")
        self.assertEqual(body["status"], "degraded")
        self.assertFalse(body["backends"]["openai"])
        self.assertTrue(any("OPENAI_API_KEY" in p for p in body["problems"]))

    def test_missing_tavily_is_flagged_as_a_single_point_of_failure(self):
        body = self._get(TAVILY_API_KEY="")
        self.assertTrue(body["local_search"]["is_sole_local_search"])
        self.assertTrue(any("solely on scraping DuckDuckGo" in p for p in body["problems"]))

    def test_blocked_duckduckgo_with_no_tavily_reads_as_search_down(self):
        with patch.object(ls, "_ddg_fail_streak", ls.DDG_TRIP_THRESHOLD):
            body = self._get(TAVILY_API_KEY="")
        self.assertTrue(body["local_search"]["degraded"])
        self.assertTrue(any("effectively down" in p for p in body["problems"]))

    def test_blocked_duckduckgo_is_tolerable_when_tavily_is_configured(self):
        with patch.object(ls, "_ddg_fail_streak", ls.DDG_TRIP_THRESHOLD):
            body = self._get()
        self.assertTrue(body["local_search"]["degraded"])
        self.assertFalse(body["local_search"]["is_sole_local_search"])
        # Tavily is carrying local search, so this is not a search outage.
        self.assertEqual(body["problems"], [])
        self.assertEqual(body["status"], "ok")


if __name__ == "__main__":
    unittest.main()
