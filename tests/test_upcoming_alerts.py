"""Offline tests for the weekly planned-work alert job (/run-upcoming-alerts).

The whole reason this job exists separately from the daily saved-search job is
that /upcoming returns items with status "Planned", which _is_open_bid
deliberately rejects. Copy the daily job's body without noticing that and the
weekly email is silently empty forever -- so that's the case pinned hardest
here. No network: Supabase, Resend and the Upcoming pipeline are all mocked.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


class RunUpcomingAlertsEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()
        self._orig_secret = ls.CRON_SECRET

    def tearDown(self):
        ls.CRON_SECRET = self._orig_secret

    def test_rejects_when_no_cron_secret_configured(self):
        ls.CRON_SECRET = ""
        resp = self.client.post("/run-upcoming-alerts", json={"token": "anything"})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(resp.get_json()["ok"])

    def test_rejects_wrong_token(self):
        ls.CRON_SECRET = "correct-secret"
        resp = self.client.post("/run-upcoming-alerts",
                                headers={"X-Cron-Secret": "wrong-secret"}, json={})
        self.assertEqual(resp.status_code, 403)

    def test_accepts_correct_token_via_header(self):
        ls.CRON_SECRET = "correct-secret"
        # Nothing is configured in the test environment, so the job itself
        # short-circuits to "not configured" without any network call. This
        # only proves the auth guard let an authenticated request through.
        resp = self.client.post("/run-upcoming-alerts",
                                headers={"X-Cron-Secret": "correct-secret"}, json={})
        self.assertNotEqual(resp.status_code, 403)

    def test_accepts_correct_token_in_body(self):
        ls.CRON_SECRET = "correct-secret"
        resp = self.client.post("/run-upcoming-alerts", json={"token": "correct-secret"})
        self.assertNotEqual(resp.status_code, 403)


class UpcomingAlertsConfigGuardTests(unittest.TestCase):
    """Each missing dependency must be reported by name, not swallowed into a
    cheerful ok:true that emailed nobody."""

    def setUp(self):
        self._orig = (ls.SUPABASE_URL, ls.SUPABASE_SERVICE_ROLE_KEY,
                      ls.RESEND_API_KEY, ls.OPENAI_API_KEY)
        ls.SUPABASE_URL = "https://example.supabase.co"
        ls.SUPABASE_SERVICE_ROLE_KEY = "fake-key-for-test"
        ls.RESEND_API_KEY = "fake-resend-key"
        ls.OPENAI_API_KEY = "fake-openai-key"

    def tearDown(self):
        (ls.SUPABASE_URL, ls.SUPABASE_SERVICE_ROLE_KEY,
         ls.RESEND_API_KEY, ls.OPENAI_API_KEY) = self._orig

    def test_inert_without_supabase_service_role_key(self):
        ls.SUPABASE_SERVICE_ROLE_KEY = ""
        result = ls._run_upcoming_alerts()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "supabase_not_configured")

    def test_inert_without_resend_key(self):
        ls.RESEND_API_KEY = ""
        result = ls._run_upcoming_alerts()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "email_not_configured")

    def test_inert_without_openai_key(self):
        # /upcoming has no non-AI path, so running without a key would report
        # success having found nothing.
        ls.OPENAI_API_KEY = ""
        result = ls._run_upcoming_alerts()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "ai_not_configured")


PLANNED = {"title": "2027 Sidewalk & ADA Program", "scope": "4,000 LF sidewalk",
           "deadline": "FY2027", "url": "https://aurora.mo.gov/cip",
           "status": "Planned"}


class RunUpcomingAlertsJobTests(unittest.TestCase):
    def setUp(self):
        self._orig = (ls.SUPABASE_URL, ls.SUPABASE_SERVICE_ROLE_KEY,
                      ls.RESEND_API_KEY, ls.OPENAI_API_KEY)
        ls.SUPABASE_URL = "https://example.supabase.co"
        ls.SUPABASE_SERVICE_ROLE_KEY = "fake-key-for-test"
        ls.RESEND_API_KEY = "fake-resend-key"
        ls.OPENAI_API_KEY = "fake-openai-key"
        self.cache = {}
        self.sent = []
        self._patchers = [
            patch("license_server._cache", side_effect=lambda: self.cache),
            patch("license_server._save_cache"),
            patch("license_server._fetch_all_saved_searches",
                  return_value=[{"user_id": "u1", "location": "Aurora, MO", "radius": 50}]),
            patch("license_server._get_user_email", return_value="josh@example.com"),
            patch("license_server._send_email",
                  side_effect=lambda to, subj, text, **k: (
                      self.sent.append((to, subj, text)) or True)),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        (ls.SUPABASE_URL, ls.SUPABASE_SERVICE_ROLE_KEY,
         ls.RESEND_API_KEY, ls.OPENAI_API_KEY) = self._orig

    @staticmethod
    def _result(items):
        return {"location": "Aurora, MO", "items": items,
                "total": sum(len(v) for v in items.values()), "city_coords": {}}

    def test_planned_items_are_emailed(self):
        """The bug this file exists for: "Planned" is not an open bid, so the
        daily job's _is_open_bid filter would drop every single item."""
        with patch("license_server._perform_upcoming",
                   return_value=self._result({"Aurora": [PLANNED]})):
            result = ls._run_upcoming_alerts()
        self.assertTrue(result["ok"])
        self.assertEqual(result["emails_sent"], 1)
        to, subject, text = self.sent[0]
        self.assertEqual(to, "josh@example.com")
        self.assertIn("Aurora, MO", subject)
        self.assertIn("2027 Sidewalk & ADA Program", text)
        self.assertIn("https://aurora.mo.gov/cip", text)

    def test_the_email_says_these_are_not_open_bids(self):
        """A contractor who reads this as an open bid and misses the deadline
        that isn't there yet will not trust the next email."""
        with patch("license_server._perform_upcoming",
                   return_value=self._result({"Aurora": [PLANNED]})):
            ls._run_upcoming_alerts()
        text = self.sent[0][2].lower()
        self.assertIn("not open bids", text)

    def test_second_run_with_the_same_items_sends_nothing(self):
        with patch("license_server._perform_upcoming",
                   return_value=self._result({"Aurora": [PLANNED]})):
            ls._run_upcoming_alerts()
            second = ls._run_upcoming_alerts()
        self.assertEqual(second["emails_sent"], 0)
        self.assertEqual(len(self.sent), 1)

    def test_a_newly_appearing_project_is_emailed(self):
        later = dict(PLANNED, title="Curb & Gutter Replacement Phase 2",
                     url="https://aurora.mo.gov/cip2")
        with patch("license_server._perform_upcoming",
                   return_value=self._result({"Aurora": [PLANNED]})):
            ls._run_upcoming_alerts()
        with patch("license_server._perform_upcoming",
                   return_value=self._result({"Aurora": [PLANNED, later]})):
            second = ls._run_upcoming_alerts()
        self.assertEqual(second["emails_sent"], 1)
        self.assertIn("Phase 2", self.sent[1][2])
        self.assertNotIn("2027 Sidewalk", self.sent[1][2])

    def test_seen_store_is_separate_from_the_daily_job(self):
        """Sharing "alert_seen" would let whichever job ran first suppress the
        other's email for the same project."""
        with patch("license_server._perform_upcoming",
                   return_value=self._result({"Aurora": [PLANNED]})):
            ls._run_upcoming_alerts()
        self.assertIn("upcoming_alert_seen", self.cache)
        self.assertNotIn("alert_seen", self.cache)

    def test_a_failing_location_is_reported_and_does_not_stop_the_run(self):
        searches = [{"user_id": "u1", "location": "Broken, ZZ", "radius": 25},
                    {"user_id": "u2", "location": "Aurora, MO", "radius": 50}]

        def flaky(location, radius, force=False):
            if location.startswith("Broken"):
                raise RuntimeError("geocoder down")
            return self._result({"Aurora": [PLANNED]})

        with patch("license_server._fetch_all_saved_searches", return_value=searches), \
             patch("license_server._perform_upcoming", side_effect=flaky):
            result = ls._run_upcoming_alerts()
        self.assertTrue(result["ok"])
        self.assertEqual(result["emails_sent"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("Broken, ZZ", result["errors"][0])

    def test_unresolvable_location_is_skipped_quietly(self):
        with patch("license_server._perform_upcoming", return_value=None):
            result = ls._run_upcoming_alerts()
        self.assertTrue(result["ok"])
        self.assertEqual(result["emails_sent"], 0)
        self.assertEqual(result["errors"], [])

    def test_a_failed_send_is_not_counted_as_sent(self):
        with patch("license_server._perform_upcoming",
                   return_value=self._result({"Aurora": [PLANNED]})), \
             patch("license_server._send_email", return_value=False):
            result = ls._run_upcoming_alerts()
        self.assertEqual(result["emails_sent"], 0)


class PerformUpcomingIsReusableTests(unittest.TestCase):
    """The point of the extraction: the pipeline runs with no Flask request
    around it, returning plain data rather than a Response."""

    def test_returns_a_dict_outside_a_request(self):
        cache = {}
        pages = [{"url": "https://aurora.mo.gov/cip", "content": "x" * 400}]
        with patch("license_server._cache", side_effect=lambda: cache), \
             patch("license_server._save_cache"), \
             patch("license_server._resolve_center",
                   return_value={"lat": 36.9709, "lon": -93.7180,
                                 "city": "Aurora", "state": "MO"}), \
             patch("license_server.OPENAI_API_KEY", "test-key"), \
             patch("license_server.TAVILY_API_KEY", "t"), \
             patch("license_server._tavily_search", return_value=pages), \
             patch("license_server._ai_extract_upcoming", return_value=[]):
            out = ls._perform_upcoming("Aurora, MO", 50)
        self.assertIsInstance(out, dict)
        self.assertEqual(out["location"], "Aurora, MO")
        self.assertEqual(out["total"], 0)

    def test_returns_none_when_the_location_cannot_be_resolved(self):
        with patch("license_server._resolve_center", return_value=None):
            self.assertIsNone(ls._perform_upcoming("Nowhere, ZZ", 25))


if __name__ == "__main__":
    unittest.main()
