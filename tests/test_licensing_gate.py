"""Tests for the endpoints that gate paid access.

Covers three holes:
  * ADMIN_TOKEN falls back to a placeholder published in this public repo, so
    an unconfigured deploy let anyone mint licence keys via /issue;
  * /claim returned a customer's working licence key to anyone who supplied
    their email address, which is not a secret;
  * /mykey resolved by device id only, so a purchase made from the marketing
    site (whose Stripe links carry no device id) never unlocked the app.
"""
import hashlib
import hmac
import json
import os
import sys
import time
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


class StripeWebhookTests(unittest.TestCase):
    """STRIPE_WEBHOOK_SECRET was read in _stripe_verify() but never assigned
    anywhere, so every real webhook POST raised NameError (500) instead of
    verifying or rejecting the signature -- purchases stopped auto-issuing
    keys. These pin the fixed behaviour: a real signature is accepted, a bad
    one is rejected, and neither case raises."""

    def setUp(self):
        self.client = ls.app.test_client()

    def _signed(self, body, secret):
        payload = json.dumps(body).encode("utf-8")
        t = str(int(time.time()))
        signed = f"{t}.{payload.decode('utf-8')}".encode("utf-8")
        v1 = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return payload, f"t={t},v1={v1}"

    def test_unconfigured_secret_rejects_without_crashing(self):
        with patch.object(ls, "STRIPE_WEBHOOK_SECRET", ""):
            r = self.client.post("/stripe/webhook", data=b"{}",
                                  headers={"Stripe-Signature": "t=1,v1=deadbeef"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "bad_signature")

    def test_wrong_signature_is_rejected(self):
        payload, _ = self._signed({"type": "checkout.session.completed"}, "right-secret")
        with patch.object(ls, "STRIPE_WEBHOOK_SECRET", "right-secret"):
            r = self.client.post("/stripe/webhook", data=payload,
                                  headers={"Stripe-Signature": "t=1,v1=deadbeef"})
        self.assertEqual(r.status_code, 400)

    def test_correct_signature_issues_a_key(self):
        body = {"type": "checkout.session.completed",
                 "data": {"object": {"customer_email": BUYER, "amount_total": 5000}}}
        payload, sig = self._signed(body, "right-secret")
        with patch.object(ls, "STRIPE_WEBHOOK_SECRET", "right-secret"), \
             patch.object(ls, "_db", return_value={}), \
             patch.object(ls, "_save_db"), \
             patch.object(ls, "_send_key_email"):
            r = self.client.post("/stripe/webhook", data=payload,
                                  headers={"Stripe-Signature": sig})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])


class ReferralTests(unittest.TestCase):
    """give-a-month-get-a-month: a signed-in user gets a stable code from
    /referral/code, and a checkout carrying that code in client_reference_id
    (as `<device>~ref~<code>`) grants both sides bonus days exactly once."""

    def setUp(self):
        self.client = ls.app.test_client()
        self.db = {}

    def _signed(self, body, secret):
        payload = json.dumps(body).encode("utf-8")
        t = str(int(time.time()))
        signed = f"{t}.{payload.decode('utf-8')}".encode("utf-8")
        v1 = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return payload, f"t={t},v1={v1}"

    def _checkout(self, ref_id, email, amount=5000):
        body = {"type": "checkout.session.completed",
                 "data": {"object": {"customer_email": email, "amount_total": amount,
                                      "client_reference_id": ref_id}}}
        payload, sig = self._signed(body, "right-secret")
        with patch.object(ls, "STRIPE_WEBHOOK_SECRET", "right-secret"), \
             patch.object(ls, "_db", return_value=self.db), \
             patch.object(ls, "_save_db"), \
             patch.object(ls, "_send_key_email"), \
             patch.object(ls, "_send_referral_email"):
            return self.client.post("/stripe/webhook", data=payload,
                                     headers={"Stripe-Signature": sig})

    def test_referral_code_requires_sign_in(self):
        with patch.object(ls, "_verify_supabase_token", return_value=None):
            r = self.client.post("/referral/code", json={"supabase_token": "bad"})
        self.assertEqual(r.status_code, 401)

    def test_referral_code_is_stable_for_the_same_email(self):
        with patch.object(ls, "_db", return_value=self.db), \
             patch.object(ls, "_save_db"), \
             patch.object(ls, "_verify_supabase_token", return_value="ref@example.com"):
            r1 = self.client.post("/referral/code", json={"supabase_token": "t"})
            r2 = self.client.post("/referral/code", json={"supabase_token": "t"})
        self.assertEqual(r1.get_json()["code"], r2.get_json()["code"])

    def test_referred_checkout_grants_both_sides_bonus_days(self):
        self.db["referral_codes"] = {"ABCD1234": {"email": "referrer@example.com", "redemptions": 0}}
        self.db["referral_owner"] = {"referrer@example.com": "ABCD1234"}
        r = self._checkout("web-newbuyer~ref~ABCD1234", "referred@example.com")
        self.assertTrue(r.get_json()["ok"])
        # Referrer got a bonus key even though they never bought anything themselves.
        referrer_key = self.db["emails"].get("referrer@example.com")
        self.assertIsNotNone(referrer_key)
        referrer_exp = self.db["issued"][referrer_key]["expires"]
        # Referred buyer's own key should expire further out than a bare
        # 1-month plan would, since the 14-day bonus stacks on top.
        referred_key = self.db["emails"].get("referred@example.com")
        self.assertIsNotNone(referred_key)
        self.assertGreater(
            self.db["issued"][referred_key]["expires"],
            (ls.datetime.datetime.now() + ls.datetime.timedelta(days=35)).isoformat()[:10])
        self.assertIn("referred@example.com", self.db["referral_redeemed"])
        self.assertGreater(referrer_exp, ls.datetime.datetime.now().isoformat()[:10])

    def test_referral_cannot_be_redeemed_twice_by_the_same_buyer(self):
        self.db["referral_codes"] = {"ABCD1234": {"email": "referrer@example.com", "redemptions": 0}}
        self.db["referral_owner"] = {"referrer@example.com": "ABCD1234"}
        self._checkout("web-newbuyer~ref~ABCD1234", "referred@example.com")
        first_key = self.db["emails"]["referrer@example.com"]
        self._checkout("web-newbuyer2~ref~ABCD1234", "referred@example.com")
        self.assertEqual(self.db["emails"]["referrer@example.com"], first_key)
        self.assertEqual(self.db["referral_codes"]["ABCD1234"]["redemptions"], 1)

    def test_no_self_referral_reward(self):
        self.db["referral_codes"] = {"ABCD1234": {"email": "same@example.com", "redemptions": 0}}
        self.db["referral_owner"] = {"same@example.com": "ABCD1234"}
        self._checkout("web-x~ref~ABCD1234", "same@example.com")
        self.assertEqual(self.db["referral_codes"]["ABCD1234"]["redemptions"], 0)

    def test_checkout_without_a_referral_code_behaves_as_before(self):
        r = self._checkout("web-plainbuyer", "plain@example.com")
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.db["devices"].get("web-plainbuyer"),
                          self.db["emails"].get("plain@example.com"))
        self.assertNotIn("referral_redeemed", self.db)


if __name__ == "__main__":
    unittest.main()


class PaidWithADifferentEmailTests(unittest.TestCase):
    """The case that strands a paying customer on their second device.

    A key is filed under the address used AT STRIPE. /mykey looks it up by the
    signed-in account address. Those are the same string right up until
    somebody pays with a personal card that autofills a different one -- a
    contractor buying business software, which is most of them.

    The device that ran the checkout still works, because the payment link
    carries its id. The next device does not: unknown device, mismatched
    email, both miss, and somebody who has paid is shown "Trial expired,
    subscribe below" -- which invites a second subscription.

    So a device match now also files the key under the verified account
    address. The address comes from the Supabase token, never from the request
    body: claiming a licence by typing somebody else's email is the exact hole
    ClaimTests above exists to keep shut.
    """

    def setUp(self):
        self.db = {"revoked": [], "trials": {}, "issued": {}, "emails": {},
                   "devices": {}}
        self._orig_db, ls._db = ls._db, lambda: self.db
        self._orig_save, ls._save_db = ls._save_db, lambda d: None
        self._orig_verify = ls._verify_supabase_token
        ls._verify_supabase_token = lambda tok: (
            "account@example.com" if tok == "good" else "")
        self.app = ls.app.test_client()
        # Bought on the phone, paying as personal@example.com.
        self.key = ls._issue_for(self.db, "personal@example.com", "phone",
                                 "monthly")

    def tearDown(self):
        ls._db = self._orig_db
        ls._save_db = self._orig_save
        ls._verify_supabase_token = self._orig_verify

    def _ask(self, device, token="good"):
        return self.app.post("/mykey", json={"device_id": device,
                                             "supabase_token": token}).get_json()

    def test_the_device_that_bought_it_still_unlocks(self):
        self.assertTrue(self._ask("phone")["ok"])

    def test_the_purchase_email_is_not_the_account_email(self):
        """The precondition. If this ever stops being true the rest is moot."""
        self.assertIn("personal@example.com", self.db["emails"])
        self.assertNotIn("account@example.com", self.db["emails"])

    def test_a_second_device_was_locked_out_and_now_is_not(self):
        self._ask("phone")                      # the fix files it on this call
        self.assertTrue(self._ask("laptop")["ok"],
                        "a paying customer is locked out on a second device")

    def test_without_visiting_on_the_paying_device_it_still_cannot_guess(self):
        """The fix is a record made from a verified session, not a search."""
        self.assertFalse(self._ask("laptop")["ok"])

    def test_the_account_email_is_taken_from_the_token_not_the_body(self):
        r = self.app.post("/mykey", json={
            "device_id": "phone", "supabase_token": "good",
            "email": "attacker@example.com"}).get_json()
        self.assertTrue(r["ok"])
        self.assertIn("account@example.com", self.db["emails"])
        self.assertNotIn("attacker@example.com", self.db["emails"])

    def test_an_unverified_session_files_nothing(self):
        self.assertTrue(self._ask("phone", token="forged")["ok"])
        self.assertNotIn("account@example.com", self.db["emails"])

    def test_it_does_not_steal_an_address_from_a_live_licence(self):
        """That address may already own a different, working licence."""
        other = ls._issue_for(self.db, "account@example.com", "other-device",
                              "annual")
        self._ask("phone")
        self.assertEqual(self.db["emails"]["account@example.com"], other,
                         "overwrote a live licence with a different one")

    def test_it_does_replace_a_dead_one(self):
        """An expired key is precisely what a new purchase replaces."""
        self.db["emails"]["account@example.com"] = "BCP-NOT-A-REAL-KEY"
        self._ask("phone")
        self.assertEqual(self.db["emails"]["account@example.com"], self.key)

    def test_a_revoked_prior_key_is_also_replaced(self):
        old = ls._issue_for(self.db, "account@example.com", "old", "monthly")
        self.db["revoked"].append(old)
        self._ask("phone")
        self.assertEqual(self.db["emails"]["account@example.com"], self.key)

    def test_a_revoked_key_still_does_not_unlock_anything(self):
        self.db["revoked"].append(self.key)
        self.assertFalse(self._ask("phone")["ok"])
        self.assertNotIn("account@example.com", self.db["emails"])
