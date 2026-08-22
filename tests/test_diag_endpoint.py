"""Tests for /diag, the read-only diagnostics credential.

ADMIN_TOKEN can issue licences, send campaigns and export the user table, so
handing it to anyone helping debug a scan risks the whole business on one
leaked string. This is a second, deliberately weak credential: scan telemetry
and nothing else.

What it must NOT do matters more than what it does, so most of these assert
absence.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


class DiagAuthTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()

    def test_it_is_off_until_a_token_is_configured(self):
        with patch.object(ls, "DIAG_TOKEN", ""):
            r = self.client.get("/diag")
        self.assertEqual(r.status_code, 503)

    def test_no_token_is_refused(self):
        with patch.object(ls, "DIAG_TOKEN", "d1ag"):
            r = self.client.get("/diag")
        self.assertEqual(r.status_code, 403)

    def test_a_wrong_token_is_refused(self):
        with patch.object(ls, "DIAG_TOKEN", "d1ag"):
            r = self.client.get("/diag", headers={"X-Diag-Token": "nope"})
        self.assertEqual(r.status_code, 403)

    def test_the_right_token_is_accepted(self):
        with patch.object(ls, "DIAG_TOKEN", "d1ag"):
            r = self.client.get("/diag", headers={"X-Diag-Token": "d1ag"})
        self.assertEqual(r.status_code, 200)

    def test_the_admin_token_is_deliberately_refused(self):
        """If the two were interchangeable, "just use the admin one" would
        become the habit and the separation would buy nothing."""
        with patch.object(ls, "DIAG_TOKEN", "d1ag"), \
             patch.object(ls, "ADMIN_TOKEN", "adm1n"):
            r = self.client.get("/diag", headers={"X-Diag-Token": "adm1n"})
        self.assertEqual(r.status_code, 403)


class DiagPayloadTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()

    def _body(self):
        with patch.object(ls, "DIAG_TOKEN", "d1ag"):
            return self.client.get("/diag",
                                   headers={"X-Diag-Token": "d1ag"}).get_json()

    def test_it_carries_what_a_scan_debug_needs(self):
        b = self._body()
        for key in ("providers", "last_scan", "recent_scans", "feed_audit",
                    "scan_config", "directory", "version"):
            self.assertIn(key, b)

    def test_it_never_carries_user_data(self):
        """The whole point of a weaker credential."""
        blob = str(self._body()).lower()
        for leak in ("licence", "license_key", "issued", "trials", "emails",
                     "supabase", "stripe", "api_key", "secret", "token"):
            self.assertNotIn(leak, blob, f"/diag leaked {leak}")

    def test_there_is_no_way_to_change_anything_through_it(self):
        with patch.object(ls, "DIAG_TOKEN", "d1ag"):
            for method in ("post", "put", "delete", "patch"):
                r = getattr(self.client, method)(
                    "/diag", headers={"X-Diag-Token": "d1ag"})
                self.assertEqual(r.status_code, 405, method)

    def test_provider_bench_times_are_reported(self):
        """Knowing a provider is benched, and for how long, is most of
        diagnosing a scan that came back thin."""
        self.assertIn("benched_until", self._body()["providers"])


if __name__ == "__main__":
    unittest.main()
