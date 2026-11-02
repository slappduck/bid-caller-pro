"""The alert emails work for paying customers and live trials only.

A saved search outlives the trial that made it. Without a check, a trial that
lapsed months ago keeps getting the daily new-bid email and the weekly
planned-work email for free, and each of those is a paid scan. The emails
are most of what a customer pays for.
"""
import datetime
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls

NOW = datetime.datetime(2026, 11, 2, 12, 0)


def trial(days_ago):
    return {"started": (NOW - datetime.timedelta(days=days_ago)).isoformat()}


class AlertsActiveTests(unittest.TestCase):
    def test_paying_customer(self):
        with patch("license_server._has_paid_licence", return_value=True):
            self.assertTrue(ls._alerts_active({}, "a@x.com", NOW))

    def test_mid_trial(self):
        db = {"trials": {"email:a@x.com": trial(ls.TRIAL_DAYS - 1)}}
        with patch("license_server._has_paid_licence", return_value=False):
            self.assertTrue(ls._alerts_active(db, "A@x.com ", NOW))

    def test_lapsed_trial(self):
        db = {"trials": {"email:a@x.com": trial(ls.TRIAL_DAYS + 1)}}
        with patch("license_server._has_paid_licence", return_value=False):
            self.assertFalse(ls._alerts_active(db, "a@x.com", NOW))

    def test_nobody(self):
        with patch("license_server._has_paid_licence", return_value=False):
            self.assertFalse(ls._alerts_active({}, "a@x.com", NOW))
            self.assertFalse(ls._alerts_active({}, "", NOW))


class AlertJobsSkipLapsedAccountsTests(unittest.TestCase):
    SEARCHES = [{"user_id": "paid", "location": "Aurora, MO", "radius": 50},
                {"user_id": "lapsed", "location": "Joplin, MO", "radius": 50}]
    BID = {"title": "Sidewalk Replacement", "deadline": "2099-12-01",
           "url": "https://x.gov/1", "status": "Open"}

    def setUp(self):
        self._orig = (ls.SUPABASE_URL, ls.SUPABASE_SERVICE_ROLE_KEY,
                      ls.RESEND_API_KEY, ls.OPENAI_API_KEY)
        ls.SUPABASE_URL = "https://example.supabase.co"
        ls.SUPABASE_SERVICE_ROLE_KEY = "fake"
        ls.RESEND_API_KEY = "fake"
        ls.OPENAI_API_KEY = "fake"
        self.cache = {}
        self.scanned = []
        emails = {"paid": "paid@x.com", "lapsed": "lapsed@x.com"}
        self._p = [
            patch("license_server._cache", side_effect=lambda: self.cache),
            patch("license_server._save_cache"),
            patch("license_server._db", return_value={}),
            patch("license_server._fetch_all_saved_searches", return_value=self.SEARCHES),
            patch("license_server._get_user_email", side_effect=lambda u: emails[u]),
            patch("license_server._alerts_active",
                  side_effect=lambda db, e, now=None: e == "paid@x.com"),
            patch("license_server._notify_user"),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()
        (ls.SUPABASE_URL, ls.SUPABASE_SERVICE_ROLE_KEY,
         ls.RESEND_API_KEY, ls.OPENAI_API_KEY) = self._orig

    def test_daily_bid_email(self):
        def scan(location, radius, *a, **k):
            self.scanned.append(location)
            return {"location": location, "bids": {"Aurora": [self.BID]}}
        sent = []
        with patch("license_server._perform_scan", side_effect=scan), \
             patch("license_server._is_open_bid", return_value=True), \
             patch("license_server._send_alert_email",
                   side_effect=lambda e, *a: sent.append(e)):
            r = ls._run_saved_search_alerts()
        self.assertEqual(self.scanned, ["Aurora, MO"], "a lapsed trial was scanned")
        self.assertEqual(sent, ["paid@x.com"])
        self.assertEqual(r["skipped_inactive"], 1)

    def test_weekly_planned_work_email(self):
        def upcoming(location, radius, *a, **k):
            self.scanned.append(location)
            return {"location": location, "items": {"Aurora": [self.BID]}}
        sent = []
        with patch("license_server._perform_upcoming", side_effect=upcoming), \
             patch("license_server._send_upcoming_email",
                   side_effect=lambda e, *a: sent.append(e) or True):
            r = ls._run_upcoming_alerts()
        self.assertEqual(self.scanned, ["Aurora, MO"])
        self.assertEqual(sent, ["paid@x.com"])
        self.assertEqual(r["skipped_inactive"], 1)


if __name__ == "__main__":
    unittest.main()
