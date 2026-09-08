"""A subscription of a year or more has to warn before it renews.

California's automatic-renewal law has no revenue threshold, so it applies
from the first California customer, and the states that copied it work the
same way. It asks for two things this server was not doing:

  * The renewal terms and the way to cancel, restated in an acknowledgement
    AFTER the purchase -- not only on the page where the buyer clicked. The
    licence-key email said nothing about a card being charged again.

  * A reminder 15 to 45 days before an annual renewal. A $399 charge arriving
    twelve months after the last time anyone thought about it is exactly the
    fact pattern the rule exists for, and it is the one most likely to become
    a chargeback or a complaint.

The reminder rides Stripe's invoice.upcoming webhook, which needs no Stripe
API key -- this server holds only the webhook secret. Stripe sends that event
7 days ahead by DEFAULT, which is OUTSIDE the window the law asks for, so the
timing is a dashboard setting and the code refuses to pretend otherwise: a
reminder outside the window is reported, not quietly counted as compliance.
"""
import datetime
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


def in_days(n):
    return int((datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(days=n, hours=1)).timestamp())


class RenewalReminderTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.store = {}
        self._orig_email = ls._send_email
        self._orig_get = ls.kv_backend.get
        self._orig_set = ls.kv_backend.set
        ls._send_email = lambda to, subj, text, **k: (
            self.sent.append({"to": to, "subject": subj, "text": text}) or True)
        ls.kv_backend.get = lambda k, d=None: self.store.get(k, d)
        ls.kv_backend.set = lambda k, v: self.store.__setitem__(k, v)
        self.db = {"customers": {
            "cus_annual": {"email": "annual@example.com", "device": "d1",
                           "plan": "annual"},
            "cus_monthly": {"email": "monthly@example.com", "device": "d2",
                            "plan": "monthly"},
        }}

    def tearDown(self):
        ls._send_email = self._orig_email
        ls.kv_backend.get = self._orig_get
        ls.kv_backend.set = self._orig_set

    def _upcoming(self, cust="cus_annual", days=30, invoice="in_1"):
        return {"id": invoice, "customer": cust,
                "next_payment_attempt": in_days(days)}

    def test_an_annual_subscriber_is_reminded_inside_the_window(self):
        self.assertEqual(ls._handle_upcoming_invoice(self.db, self._upcoming()),
                         "sent")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["to"], "annual@example.com")

    def test_the_reminder_says_the_amount_and_how_to_cancel(self):
        ls._handle_upcoming_invoice(self.db, self._upcoming())
        body = self.sent[0]["text"]
        self.assertIn("$399", body)
        self.assertIn("renews automatically", body)
        self.assertIn(ls.BILLING_PORTAL, body)

    def test_a_monthly_subscriber_is_not_reminded_every_month(self):
        """The rule covers terms of a year or more, and a monthly notice
        trains people to ignore the one that matters."""
        out = ls._handle_upcoming_invoice(
            self.db, self._upcoming(cust="cus_monthly"))
        self.assertEqual(out, "monthly_no_reminder")
        self.assertEqual(self.sent, [])

    def test_stripes_default_seven_days_is_refused_not_accepted(self):
        """7 days is Stripe's default and outside the 15-45 day window. Sending
        anyway would look like compliance while not being it."""
        out = ls._handle_upcoming_invoice(self.db, self._upcoming(days=7))
        self.assertEqual(out, "outside_window")
        self.assertEqual(self.sent, [])

    def test_a_reminder_far_too_early_is_also_refused(self):
        out = ls._handle_upcoming_invoice(self.db, self._upcoming(days=120))
        self.assertEqual(out, "outside_window")
        self.assertEqual(self.sent, [])

    def test_both_edges_of_the_window_are_inside_it(self):
        for days in (ls.RENEWAL_NOTICE_MIN_DAYS, ls.RENEWAL_NOTICE_MAX_DAYS):
            with self.subTest(days=days):
                self.sent.clear()
                self.store.clear()
                out = ls._handle_upcoming_invoice(
                    self.db, self._upcoming(days=days, invoice="in_%d" % days))
                self.assertEqual(out, "sent")

    def test_a_retried_webhook_does_not_warn_twice(self):
        """Stripe retries. Two identical warnings read as a billing error."""
        self.assertEqual(ls._handle_upcoming_invoice(self.db, self._upcoming()),
                         "sent")
        self.assertEqual(ls._handle_upcoming_invoice(self.db, self._upcoming()),
                         "already_sent")
        self.assertEqual(len(self.sent), 1)

    def test_a_different_invoice_for_the_same_customer_still_sends(self):
        ls._handle_upcoming_invoice(self.db, self._upcoming(invoice="in_1"))
        out = ls._handle_upcoming_invoice(self.db, self._upcoming(invoice="in_2"))
        self.assertEqual(out, "sent")
        self.assertEqual(len(self.sent), 2)

    def test_an_unknown_customer_is_ignored_rather_than_crashing(self):
        out = ls._handle_upcoming_invoice(self.db, self._upcoming(cust="cus_x"))
        self.assertEqual(out, "unknown_customer")
        self.assertEqual(self.sent, [])

    def test_an_invoice_with_no_date_is_ignored(self):
        out = ls._handle_upcoming_invoice(
            self.db, {"id": "in_9", "customer": "cus_annual"})
        self.assertEqual(out, "no_date")
        self.assertEqual(self.sent, [])

    def test_a_dead_kv_store_still_sends_the_reminder(self):
        """Missing the de-duplication is survivable. Missing the notice is
        the thing the law is about."""
        def boom(*a, **k):
            raise RuntimeError("kv down")
        ls.kv_backend.get = boom
        ls.kv_backend.set = boom
        self.assertEqual(ls._handle_upcoming_invoice(self.db, self._upcoming()),
                         "sent")
        self.assertEqual(len(self.sent), 1)


class PurchaseAcknowledgementTests(unittest.TestCase):
    """What the buyer is owed in writing after paying."""

    def setUp(self):
        self.sent = []
        self._orig_email = ls._send_email
        ls._send_email = lambda to, subj, text, **k: (
            self.sent.append({"to": to, "subject": subj, "text": text}) or True)

    def tearDown(self):
        ls._send_email = self._orig_email

    def test_the_key_email_states_the_renewal_terms(self):
        ls._send_key_email("buyer@example.com", "KEY-1", "monthly")
        body = self.sent[0]["text"]
        self.assertIn("renews automatically", body)
        self.assertIn("$49/month", body)

    def test_it_says_how_to_cancel(self):
        ls._send_key_email("buyer@example.com", "KEY-1", "annual")
        self.assertIn(ls.BILLING_PORTAL, self.sent[0]["text"])

    def test_the_annual_price_is_not_the_monthly_one(self):
        ls._send_key_email("buyer@example.com", "KEY-1", "annual")
        body = self.sent[0]["text"]
        self.assertIn("$399/year", body)
        self.assertNotIn("$49/month", body)

    def test_it_still_delivers_the_key(self):
        """The compliance text must not have displaced the useful part."""
        ls._send_key_email("buyer@example.com", "KEY-ABC", "monthly")
        self.assertIn("KEY-ABC", self.sent[0]["text"])

    def test_cancelling_is_described_as_keeping_paid_time(self):
        """Otherwise 'cancel anytime' reads as forfeiting what was paid for."""
        ls._send_key_email("buyer@example.com", "KEY-1", "annual")
        self.assertIn("already paid for", self.sent[0]["text"])


class WebhookRoutingTests(unittest.TestCase):
    def test_the_webhook_routes_upcoming_invoices(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               os.pardir, "license_server.py"),
                  encoding="utf-8") as f:
            src = f.read()
        body = src[src.index("def stripe_webhook("):]
        body = body[:body.index("\n@app.route")]
        self.assertIn('etype == "invoice.upcoming"', body)
        self.assertIn("_handle_upcoming_invoice", body)


if __name__ == "__main__":
    unittest.main()
