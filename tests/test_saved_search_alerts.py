"""Offline tests for the saved-search alert plumbing in license_server.py.
No network, no real Supabase/Resend calls: the Flask test client exercises
only the auth guard (which must reject before touching anything external),
and _bid_sig is pure."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


class BidSigTests(unittest.TestCase):
    def test_same_bid_same_signature(self):
        b1 = {"title": "Sidewalk repair", "deadline": "2026-09-01", "url": "https://x.gov/1"}
        b2 = {"title": "Sidewalk repair", "deadline": "2026-09-01", "url": "https://x.gov/1"}
        self.assertEqual(ls._bid_sig("Springfield", b1), ls._bid_sig("Springfield", b2))

    def test_different_bids_different_signatures(self):
        b1 = {"title": "Sidewalk repair", "deadline": "2026-09-01", "url": "https://x.gov/1"}
        b2 = {"title": "Curb ramp project", "deadline": "2026-09-01", "url": "https://x.gov/1"}
        self.assertNotEqual(ls._bid_sig("Springfield", b1), ls._bid_sig("Springfield", b2))

    def test_different_city_different_signature(self):
        b = {"title": "Sidewalk repair", "deadline": "2026-09-01", "url": "https://x.gov/1"}
        self.assertNotEqual(ls._bid_sig("Springfield", b), ls._bid_sig("Joplin", b))


class RunSavedSearchAlertsEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()
        self._orig_secret = ls.CRON_SECRET

    def tearDown(self):
        ls.CRON_SECRET = self._orig_secret

    def test_rejects_when_no_cron_secret_configured(self):
        ls.CRON_SECRET = ""
        resp = self.client.post("/run-saved-search-alerts", json={"token": "anything"})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(resp.get_json()["ok"])

    def test_rejects_wrong_token(self):
        ls.CRON_SECRET = "correct-secret"
        resp = self.client.post("/run-saved-search-alerts",
                                 headers={"X-Cron-Secret": "wrong-secret"}, json={})
        self.assertEqual(resp.status_code, 403)

    def test_accepts_correct_token_via_header(self):
        ls.CRON_SECRET = "correct-secret"
        # With no SUPABASE_SERVICE_ROLE_KEY/RESEND_API_KEY configured in this
        # test environment, the job itself short-circuits to "not configured"
        # rather than making any real network call -- this only proves the
        # auth guard let a correctly-authenticated request through.
        resp = self.client.post("/run-saved-search-alerts",
                                 headers={"X-Cron-Secret": "correct-secret"}, json={})
        self.assertNotEqual(resp.status_code, 403)


class RunSavedSearchAlertsJobTests(unittest.TestCase):
    def test_inert_without_supabase_service_role_key(self):
        orig_url, orig_key = ls.SUPABASE_URL, ls.SUPABASE_SERVICE_ROLE_KEY
        ls.SUPABASE_SERVICE_ROLE_KEY = ""
        try:
            result = ls._run_saved_search_alerts()
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "supabase_not_configured")
        finally:
            ls.SUPABASE_URL, ls.SUPABASE_SERVICE_ROLE_KEY = orig_url, orig_key

    def test_inert_without_resend_key(self):
        orig_url = ls.SUPABASE_URL
        orig_svc = ls.SUPABASE_SERVICE_ROLE_KEY
        orig_resend = ls.RESEND_API_KEY
        ls.SUPABASE_URL = "https://example.supabase.co"
        ls.SUPABASE_SERVICE_ROLE_KEY = "fake-key-for-test"
        ls.RESEND_API_KEY = ""
        try:
            result = ls._run_saved_search_alerts()
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "email_not_configured")
        finally:
            ls.SUPABASE_URL = orig_url
            ls.SUPABASE_SERVICE_ROLE_KEY = orig_svc
            ls.RESEND_API_KEY = orig_resend


if __name__ == "__main__":
    unittest.main()
