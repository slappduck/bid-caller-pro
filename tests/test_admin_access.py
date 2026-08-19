"""Tests for admin-email unlimited access and the two new admin routes:
/admin/whoami and /admin/reviews.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls

TOKEN = "a-real-admin-token"


class AdminEmailAllowlistTests(unittest.TestCase):
    def setUp(self):
        self._orig = ls.ADMIN_EMAILS
        ls.ADMIN_EMAILS = "boss@example.com, other@example.com"

    def tearDown(self):
        ls.ADMIN_EMAILS = self._orig

    def test_a_listed_email_is_admin(self):
        self.assertTrue(ls._is_admin_email("boss@example.com"))

    def test_matching_is_case_insensitive(self):
        self.assertTrue(ls._is_admin_email("Boss@Example.com"))

    def test_an_unlisted_email_is_not_admin(self):
        self.assertFalse(ls._is_admin_email("stranger@example.com"))

    def test_empty_or_none_is_never_admin(self):
        self.assertFalse(ls._is_admin_email(""))
        self.assertFalse(ls._is_admin_email(None))

    def test_an_empty_allowlist_admits_nobody(self):
        ls.ADMIN_EMAILS = ""
        self.assertFalse(ls._is_admin_email("boss@example.com"))


class AdminUnlimitedAccessTests(unittest.TestCase):
    def setUp(self):
        self._orig_emails = ls.ADMIN_EMAILS
        ls.ADMIN_EMAILS = "boss@example.com"
        self._orig_db = ls._db
        self.db = {"revoked": [], "trials": {}, "issued": {}, "emails": {}}
        ls._db = lambda: self.db
        self._orig_save = ls._save_db
        ls._save_db = lambda d: None
        self._orig_verify = ls._verify_supabase_token

    def tearDown(self):
        ls.ADMIN_EMAILS = self._orig_emails
        ls._db = self._orig_db
        ls._save_db = self._orig_save
        ls._verify_supabase_token = self._orig_verify

    def test_admin_email_is_active_with_no_trial_and_no_key(self):
        ls._verify_supabase_token = lambda t: "boss@example.com"
        self.assertTrue(ls._license_is_active("", "some-device", supabase_token="t"))
        # And crucially: it did not consume a trial slot to get there.
        self.assertEqual(self.db["trials"], {})

    def test_a_non_admin_email_still_goes_through_the_normal_trial_path(self):
        ls._verify_supabase_token = lambda t: "nobody@example.com"
        self.assertTrue(ls._license_is_active("", "some-device", supabase_token="t"))
        self.assertIn("email:nobody@example.com", self.db["trials"])


class AdminWhoamiTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()
        self._orig_emails = ls.ADMIN_EMAILS
        ls.ADMIN_EMAILS = "boss@example.com"
        self._orig_verify = ls._verify_supabase_token

    def tearDown(self):
        ls.ADMIN_EMAILS = self._orig_emails
        ls._verify_supabase_token = self._orig_verify

    def test_an_admin_token_reports_true(self):
        ls._verify_supabase_token = lambda t: "boss@example.com"
        body = self.client.post("/admin/whoami",
                                json={"supabase_token": "t"}).get_json()
        self.assertTrue(body["is_admin"])

    def test_a_non_admin_token_reports_false(self):
        ls._verify_supabase_token = lambda t: "nobody@example.com"
        body = self.client.post("/admin/whoami",
                                json={"supabase_token": "t"}).get_json()
        self.assertFalse(body["is_admin"])

    def test_no_token_at_all_reports_false_not_an_error(self):
        body = self.client.post("/admin/whoami", json={}).get_json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["is_admin"])

    def test_never_requires_or_reveals_the_admin_token(self):
        """This endpoint has to stay reachable by any signed-in user asking
        about themselves -- it must never require ADMIN_TOKEN, and its
        response must never include the allowlist itself."""
        ls._verify_supabase_token = lambda t: "boss@example.com"
        resp = self.client.post("/admin/whoami", json={"supabase_token": "t"})
        self.assertNotEqual(resp.status_code, 403)
        self.assertNotIn("boss@example.com", resp.get_data(as_text=True))


class AdminReviewsTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()
        self._p = [
            patch.object(ls, "ADMIN_TOKEN", TOKEN),
            patch.object(ls, "SUPABASE_URL", "https://example.supabase.co"),
            patch.object(ls, "SUPABASE_SERVICE_ROLE_KEY", "svc-key"),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_rejects_without_a_token(self):
        resp = self.client.post("/admin/reviews", json={})
        self.assertEqual(resp.status_code, 403)

    def test_lists_reviews(self):
        rows = [{"id": 1, "rating": 5, "quote": "Great tool", "approved": False}]
        with patch.object(ls, "_supabase_admin_request", return_value=rows):
            body = self.client.post("/admin/reviews",
                                    json={"admin_token": TOKEN}).get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["reviews"], rows)

    def test_approve_patches_before_listing(self):
        calls = []

        def fake(path, method="GET", data=None):
            calls.append((path, method, data))
            return [] if method == "GET" else True

        with patch.object(ls, "_supabase_admin_request", side_effect=fake):
            self.client.post("/admin/reviews",
                             json={"admin_token": TOKEN, "approve": "42"})
        patch_call = [c for c in calls if c[1] == "PATCH"][0]
        self.assertIn("id=eq.42", patch_call[0])
        self.assertEqual(patch_call[2], {"approved": True})

    def test_reject_sets_approved_false(self):
        calls = []

        def fake(path, method="GET", data=None):
            calls.append((path, method, data))
            return [] if method == "GET" else True

        with patch.object(ls, "_supabase_admin_request", side_effect=fake):
            self.client.post("/admin/reviews",
                             json={"admin_token": TOKEN, "reject": "42"})
        patch_call = [c for c in calls if c[1] == "PATCH"][0]
        self.assertEqual(patch_call[2], {"approved": False})

    def test_a_non_numeric_id_is_rejected_not_injected_into_the_query(self):
        with patch.object(ls, "_supabase_admin_request") as mock:
            resp = self.client.post(
                "/admin/reviews",
                json={"admin_token": TOKEN, "approve": "42 or 1=1"})
        self.assertEqual(resp.status_code, 400)
        mock.assert_not_called()

    def test_refuses_without_supabase_configured(self):
        with patch.object(ls, "SUPABASE_SERVICE_ROLE_KEY", ""):
            resp = self.client.post("/admin/reviews", json={"admin_token": TOKEN})
        self.assertEqual(resp.status_code, 503)


if __name__ == "__main__":
    unittest.main()
