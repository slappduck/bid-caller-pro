"""Tests for /admin/export, the manual data backup.

Two properties matter more than the happy path. The endpoint must be
unreachable without a real admin token -- it returns every customer's name,
email, phone and private pipeline notes in one response. And a table that
does not exist yet (reviews, until the schema is re-run) must be reported by
name rather than taking the whole export down with it.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls

TOKEN = "a-real-admin-token"


class AdminExportAuthTests(unittest.TestCase):
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
        resp = self.client.post("/admin/export", json={})
        self.assertEqual(resp.status_code, 403)

    def test_rejects_a_wrong_token(self):
        resp = self.client.post("/admin/export", json={"admin_token": "nope"})
        self.assertEqual(resp.status_code, 403)

    def test_rejects_the_placeholder_token(self):
        """Shipping with ADMIN_TOKEN unset must not expose every customer."""
        with patch.object(ls, "ADMIN_TOKEN", ls._ADMIN_TOKEN_PLACEHOLDER):
            resp = self.client.post(
                "/admin/export",
                json={"admin_token": ls._ADMIN_TOKEN_PLACEHOLDER})
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["reason"], "admin_not_configured")

    def test_refuses_without_supabase_service_role(self):
        with patch.object(ls, "SUPABASE_SERVICE_ROLE_KEY", ""):
            resp = self.client.post("/admin/export",
                                    json={"admin_token": TOKEN})
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["reason"], "supabase_not_configured")


class AdminExportContentTests(unittest.TestCase):
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

    def _post(self):
        return self.client.post("/admin/export",
                                json={"admin_token": TOKEN}).get_json()

    def test_exports_every_table(self):
        with patch.object(ls, "_supabase_admin_request",
                          return_value=[{"user_id": "u1"}]):
            body = self._post()
        self.assertTrue(body["ok"])
        self.assertEqual(set(body["tables"]), set(ls._EXPORT_TABLES))
        self.assertEqual(body["total_rows"], len(ls._EXPORT_TABLES))
        self.assertEqual(body["errors"], {})

    def test_a_missing_table_is_named_not_fatal(self):
        """`reviews` does not exist until the schema is re-run. The other
        four tables must still come back."""
        def fake(path):
            return None if "reviews" in path else [{"user_id": "u1"}]

        with patch.object(ls, "_supabase_admin_request", side_effect=fake):
            body = self._post()
        self.assertTrue(body["ok"])
        self.assertEqual(body["errors"], {"reviews": "unreachable_or_missing"})
        self.assertEqual(body["row_counts"]["reviews"], 0)
        self.assertEqual(body["row_counts"]["saved_bids"], 1)

    def test_a_non_list_response_is_not_counted_as_data(self):
        with patch.object(ls, "_supabase_admin_request",
                          return_value={"message": "permission denied"}):
            body = self._post()
        self.assertEqual(body["total_rows"], 0)
        self.assertTrue(all(v == "unexpected_shape"
                            for v in body["errors"].values()))

    def test_reports_when_it_was_taken(self):
        """A backup with no timestamp is a backup you cannot reason about."""
        with patch.object(ls, "_supabase_admin_request", return_value=[]):
            body = self._post()
        self.assertIn("exported_at", body)
        self.assertTrue(body["exported_at"])


if __name__ == "__main__":
    unittest.main()
