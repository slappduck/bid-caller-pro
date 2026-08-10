"""Tests for the endpoints that gate paid access.

Covers three holes:
  * ADMIN_TOKEN falls back to a placeholder published in this public repo, so
    an unconfigured deploy let anyone mint licence keys via /issue;
  * /claim returned a customer's working licence key to anyone who supplied
    their email address, which is not a secret;
  * /mykey resolved by device id only, so a purchase made from the marketing
    site (whose Stripe links carry no device id) never unlocked the app.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls

REAL_TOKEN = "a-real-admin-token"
BUYER = "buyer@example.com"


class AdminTokenTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()

    def _post(self, path, body, token=REAL_TOKEN):
        with patch.object(ls, "ADMIN_TOKEN", token):
            return self.client.post(path, json=body)

    def test_placeholder_token_disables_admin_endpoints(self):
        # The default is in the public repo — it must never be a live password.
        for path in ("/issue", "/revoke", "/admin/list"):
            with self.subTest(path=path):
                r = self._post(path, {"admin_token": ls._ADMIN_TOKEN_PLACEHOLDER},
                               token=ls._ADMIN_TOKEN_PLACEHOLDER)
                self.assertEqual(r.status_code, 503)
                self.assertEqual(r.get_json()["reason"], "admin_not_configured")

    def test_empty_token_disables_admin_endpoints(self):
        r = self._post("/admin/list", {"admin_token": ""}, token="")
        self.assertEqual(r.status_code, 503)

    def test_wrong_token_is_rejected_when_configured(self):
        for path in ("/issue", "/revoke", "/admin/list"):
            with self.subTest(path=path):
                r = self._post(path, {"admin_token": "wrong"})
                self.assertIn(r.status_code, (401, 403))

    def test_missing_token_is_rejected(self):
        r = self._post("/admin/list", {})
        self.assertIn(r.status_code, (401, 403))

    def test_correct_token_is_accepted(self):
        with patch.object(ls, "_db", return_value={}), \
             patch.object(ls, "_save_db"):
            r = self._post("/admin/list", {"admin_token": REAL_TOKEN})
        self.assertEqual(r.status_code, 200)


class ClaimTests(unittest.TestCase):
    """Restoring a purchase must prove who you are, not just name an email."""

    def setUp(self):
        self.client = ls.app.test_client()
        self.db = {"emails": {BUYER: "BCP-REAL-KEY"}, "devices": {}, "revoked": []}

    def test_bare_email_no_longer_yields_a_key(self):
        with patch.object(ls, "_db", return_value=self.db), \
             patch.object(ls, "_verify_supabase_token", return_value=None):
            r = self.client.post("/claim", json={"email": BUYER, "device_id": "attacker"})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("key", r.get_json())
        self.assertEqual(self.db["devices"], {})  # attacker's device not bound

    def test_a_body_email_cannot_override_the_verified_one(self):
        # Signed in as someone else, but naming the buyer's address in the body.
        with patch.object(ls, "_db", return_value=self.db), \
             patch.object(ls, "_verify_supabase_token", return_value="someone@else.com"):
            r = self.client.post("/claim", json={"email": BUYER, "device_id": "attacker"})
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual(r.get_json()["reason"], "no_purchase")

    def test_the_real_buyer_can_still_restore(self):
        with patch.object(ls, "_db", return_value=self.db), \
             patch.object(ls, "_save_db"), \
             patch.object(ls, "_verify_supabase_token", return_value=BUYER), \
             patch.object(ls, "verify_key", return_value=(True, "monthly", "2027-01-01", None)):
            r = self.client.post("/claim", json={"device_id": "buyers-phone"})
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["key"], "BCP-REAL-KEY")


class MyKeyTests(unittest.TestCase):
    """A purchase from the marketing site has no device id attached."""

    def setUp(self):
        self.client = ls.app.test_client()
        self.db = {"emails": {BUYER: "BCP-REAL-KEY"}, "devices": {}, "revoked": []}

    def test_unknown_device_alone_still_reports_no_key(self):
        with patch.object(ls, "_db", return_value=self.db), \
             patch.object(ls, "_verify_supabase_token", return_value=None):
            r = self.client.post("/mykey", json={"device_id": "web-abc"})
        self.assertFalse(r.get_json()["ok"])

    def test_signed_in_buyer_unlocks_even_with_an_unknown_device(self):
        with patch.object(ls, "_db", return_value=self.db), \
             patch.object(ls, "_save_db"), \
             patch.object(ls, "_verify_supabase_token", return_value=BUYER), \
             patch.object(ls, "verify_key", return_value=(True, "monthly", "2027-01-01", None)):
            r = self.client.post("/mykey", json={"device_id": "web-abc", "supabase_token": "t"})
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["key"], "BCP-REAL-KEY")

    def test_the_device_is_remembered_after_an_email_match(self):
        with patch.object(ls, "_db", return_value=self.db), \
             patch.object(ls, "_save_db"), \
             patch.object(ls, "_verify_supabase_token", return_value=BUYER), \
             patch.object(ls, "verify_key", return_value=(True, "monthly", "2027-01-01", None)):
            self.client.post("/mykey", json={"device_id": "web-abc", "supabase_token": "t"})
        self.assertEqual(self.db["devices"].get("web-abc"), "BCP-REAL-KEY")

    def test_a_revoked_key_does_not_unlock(self):
        self.db["revoked"] = ["BCP-REAL-KEY"]
        with patch.object(ls, "_db", return_value=self.db), \
             patch.object(ls, "_verify_supabase_token", return_value=BUYER), \
             patch.object(ls, "verify_key", return_value=(True, "monthly", "2027-01-01", None)):
            r = self.client.post("/mykey", json={"device_id": "web-abc", "supabase_token": "t"})
        self.assertEqual(r.get_json()["reason"], "inactive")


if __name__ == "__main__":
    unittest.main()
