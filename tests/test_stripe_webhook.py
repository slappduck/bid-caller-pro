"""The plan a customer is issued decided by what they paid, not what they bought.

checkout.session.completed classified "annual vs monthly" purely by checking
amount_total >= $100. Any discount, coupon, or future price change that pushed
a real annual sale under that line silently issued a monthly key instead --
the customer's access would then quietly expire a year early, with no error
anywhere, for someone who paid for twelve months. `metadata.plan`, set on
each of the two Stripe Payment Links themselves, is what actually reflects
which plan was bought; the amount is now only a fallback for a session with
no such metadata.

This endpoint had no test coverage at all before this file, despite being
the one place in the app that touches money directly.
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
import kv_backend
import license_server as ls

SECRET = "whsec_test_secret_0123456789abcdef"


def sign(raw, timestamp=None):
    ts = str(int(time.time()) if timestamp is None else timestamp)
    signed = f"{ts}.".encode() + raw
    v1 = hmac.new(SECRET.encode(), signed, hashlib.sha256).hexdigest()
    return ts, f"t={ts},v1={v1}"


class StripeWebhookTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()
        self.store = {}
        self._p = [
            patch.object(ls, "STRIPE_WEBHOOK_SECRET", SECRET),
            patch.object(kv_backend, "get",
                         side_effect=lambda k, d=None: self.store.get(k, d)),
            patch.object(kv_backend, "set",
                         side_effect=lambda k, v: self.store.__setitem__(k, v)),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def _post(self, event, *, secret_ok=True, event_id=None):
        event = dict(event)
        event.setdefault("id", event_id or ("evt_" + hashlib.sha1(
            json.dumps(event, sort_keys=True).encode()).hexdigest()[:16]))
        raw = json.dumps(event).encode()
        _, sig = sign(raw)
        if not secret_ok:
            sig = "t=0,v1=" + "0" * 64
        return self.client.post("/stripe/webhook", data=raw,
                                content_type="application/json",
                                headers={"Stripe-Signature": sig})

    @staticmethod
    def _checkout(email="buyer@x.com", amount_total=4900, metadata=None,
                  customer="cus_1", client_reference_id=""):
        obj = {
            "customer_details": {"email": email},
            "customer": customer,
            "amount_total": amount_total,
            "client_reference_id": client_reference_id,
        }
        if metadata is not None:
            obj["metadata"] = metadata
        return {"type": "checkout.session.completed", "data": {"object": obj}}

    def _db(self):
        return self.store.get(ls._LIC_KEY) or {}

    # ── who may write ────────────────────────────────────────────────────
    def test_a_forged_signature_is_refused(self):
        r = self._post(self._checkout(), secret_ok=False)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._db().get("issued", {}), {})

    def test_the_endpoint_refuses_entirely_when_no_secret_is_set(self):
        with patch.object(ls, "STRIPE_WEBHOOK_SECRET", ""):
            r = self._post(self._checkout())
        self.assertEqual(r.status_code, 400)

    # ── the classification bug itself ───────────────────────────────────
    def test_a_discounted_annual_sale_is_still_classified_annual(self):
        """The bug: a coupon or promo pushing the charge under $100 used to
        silently issue a monthly key for a real annual purchase."""
        r = self._post(self._checkout(amount_total=5000,
                                      metadata={"plan": "annual"}))
        self.assertEqual(r.status_code, 200)
        issued = self._db().get("issued", {})
        self.assertEqual(len(issued), 1)
        self.assertEqual(next(iter(issued.values()))["plan"], "annual")

    def test_a_full_price_monthly_sale_with_metadata_stays_monthly(self):
        r = self._post(self._checkout(amount_total=4900,
                                      metadata={"plan": "monthly"}))
        issued = self._db().get("issued", {})
        self.assertEqual(next(iter(issued.values()))["plan"], "monthly")

    def test_a_session_with_no_metadata_falls_back_to_the_amount_guess(self):
        """Backward compatible with a Payment Link that hasn't been tagged
        with metadata yet -- not a regression for sessions already live."""
        r = self._post(self._checkout(amount_total=39900, metadata=None))
        issued = self._db().get("issued", {})
        self.assertEqual(next(iter(issued.values()))["plan"], "annual")

    def test_garbage_metadata_does_not_override_the_amount_guess(self):
        """Only the two real plan names are trusted from metadata; anything
        else (a typo, an unrelated key reused by mistake) falls through to
        the amount-based guess rather than silently doing nothing."""
        r = self._post(self._checkout(amount_total=4900,
                                      metadata={"plan": "yearly"}))
        issued = self._db().get("issued", {})
        self.assertEqual(next(iter(issued.values()))["plan"], "monthly")

    # ── idempotency ──────────────────────────────────────────────────────
    def test_a_redelivered_event_is_not_processed_twice(self):
        """Stripe's own docs guarantee at-least-once delivery. A retried
        checkout.session.completed must not mint a second key."""
        event = self._checkout(metadata={"plan": "monthly"})
        r1 = self._post(event, event_id="evt_dup_1")
        r2 = self._post(event, event_id="evt_dup_1")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.get_json().get("duplicate"))
        self.assertEqual(len(self._db().get("issued", {})), 1)

    def test_a_different_event_for_the_same_purchase_still_runs(self):
        """Different event ids (e.g. the later invoice.paid for this same
        subscription) are not the same event and must not be blocked by the
        dedupe guard, which keys off event id -- never off customer or
        session, which legitimately repeat across a subscription's life."""
        self._post(self._checkout(customer="cus_dup", metadata={"plan": "monthly"}),
                   event_id="evt_a")
        r = self._post({"type": "invoice.paid",
                        "data": {"object": {"customer": "cus_dup"}}},
                       event_id="evt_b")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json().get("duplicate"))

    # ── events that reference an unknown customer ───────────────────────
    def test_an_invoice_for_an_unrecorded_customer_does_not_crash(self):
        """No checkout.session.completed was ever seen for this customer id
        (a webhook misconfiguration, a manually-created Stripe customer,
        test-mode data) -- must be a no-op, not a 500."""
        r = self._post({"type": "invoice.paid",
                        "data": {"object": {"customer": "cus_never_seen"}}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._db().get("issued", {}), {})

    def test_a_cancellation_for_an_unrecorded_customer_does_not_crash(self):
        r = self._post({"type": "customer.subscription.deleted",
                        "data": {"object": {"customer": "cus_never_seen"}}})
        self.assertEqual(r.status_code, 200)

    # ── revocation ───────────────────────────────────────────────────────
    def test_a_cancelled_subscription_revokes_the_issued_key(self):
        self._post(self._checkout(email="churn@x.com", customer="cus_churn",
                                  metadata={"plan": "monthly"}))
        key = self._db()["emails"]["churn@x.com"]
        r = self._post({"type": "customer.subscription.deleted",
                        "data": {"object": {"customer": "cus_churn"}}})
        self.assertEqual(r.status_code, 200)
        self.assertIn(key, self._db().get("revoked", []))


if __name__ == "__main__":
    unittest.main()
