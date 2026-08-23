"""Bounces and spam complaints must clean the list, and only Resend may do it.

Sending to addresses that no longer exist is how a sending domain dies:
mailbox providers read repeated hard bounces and complaints as evidence the
sender does not maintain a list, and start filing everything from that domain
in junk -- including the trial keys and bid alerts real customers are waiting
on. So the list has to clean itself from delivery feedback.

That makes this endpoint a write to the suppression list from the open
internet. Unsigned, it would let anyone permanently silence any address the
app mails -- an entire prospect list, quietly, with nothing logged as an
error. Signature verification is the feature, not a formality.
"""
import base64
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

SECRET_BYTES = b"0123456789abcdef0123456789abcdef"
SECRET = "whsec_" + base64.b64encode(SECRET_BYTES).decode()


def sign(msg_id, timestamp, raw):
    signed = f"{msg_id}.{timestamp}.".encode() + raw
    return "v1," + base64.b64encode(
        hmac.new(SECRET_BYTES, signed, hashlib.sha256).digest()).decode()


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()
        self.store = {}
        self._p = [
            patch.object(ls, "RESEND_WEBHOOK_SECRET", SECRET),
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

    def _post(self, event, *, secret_ok=True, timestamp=None, msg_id="msg_1"):
        raw = json.dumps(event).encode()
        ts = str(int(time.time()) if timestamp is None else timestamp)
        sig = sign(msg_id, ts, raw) if secret_ok else "v1," + base64.b64encode(
            b"wrong-signature-entirely-xxxxxx").decode()
        return self.client.post("/webhooks/resend", data=raw,
                                content_type="application/json",
                                headers={"svix-id": msg_id,
                                         "svix-timestamp": ts,
                                         "svix-signature": sig})

    @staticmethod
    def _event(kind, addr="gone@x.com", bounce_type=None):
        data = {"to": [addr], "email_id": "e_1"}
        if bounce_type:
            data["bounce"] = {"type": bounce_type, "subType": "General"}
        return {"type": kind, "data": data}

    # ── who is allowed to write ──────────────────────────────────────────
    def test_a_forged_signature_is_refused(self):
        r = self._post(self._event("email.complained"), secret_ok=False)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(ls._suppression(), set(), "nothing may be suppressed")

    def test_an_unsigned_request_is_refused(self):
        r = self.client.post("/webhooks/resend", json=self._event("email.complained"))
        self.assertEqual(r.status_code, 401)
        self.assertEqual(ls._suppression(), set())

    def test_a_replayed_old_delivery_is_refused(self):
        """Correctly signed, but hours old."""
        r = self._post(self._event("email.complained"),
                       timestamp=int(time.time()) - 7200)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(ls._suppression(), set())

    def test_the_endpoint_refuses_entirely_when_no_secret_is_set(self):
        """Better a 503 than accepting unsigned writes to the deny list."""
        with patch.object(ls, "RESEND_WEBHOOK_SECRET", ""):
            r = self.client.post("/webhooks/resend",
                                 json=self._event("email.complained"))
        self.assertEqual(r.status_code, 503)

    def test_a_signature_for_a_different_body_is_refused(self):
        """The body is part of what is signed, so it cannot be swapped."""
        raw = json.dumps(self._event("email.complained")).encode()
        ts = str(int(time.time()))
        sig = sign("msg_1", ts, raw)
        tampered = json.dumps(self._event("email.complained",
                                          addr="someone-else@x.com")).encode()
        r = self.client.post("/webhooks/resend", data=tampered,
                             content_type="application/json",
                             headers={"svix-id": "msg_1", "svix-timestamp": ts,
                                      "svix-signature": sig})
        self.assertEqual(r.status_code, 401)

    def test_a_rotated_secret_header_with_several_signatures_works(self):
        """Svix sends space-separated signatures during a rotation."""
        raw = json.dumps(self._event("email.complained")).encode()
        ts = str(int(time.time()))
        good = sign("msg_1", ts, raw)
        header = "v1,b3RoZXJzZWNyZXRzaWduYXR1cmU= " + good
        r = self.client.post("/webhooks/resend", data=raw,
                             content_type="application/json",
                             headers={"svix-id": "msg_1", "svix-timestamp": ts,
                                      "svix-signature": header})
        self.assertEqual(r.status_code, 200)
        self.assertIn("gone@x.com", ls._suppression())

    # ── what each event does ─────────────────────────────────────────────
    def test_a_complaint_suppresses_immediately(self):
        """Somebody pressed "this is spam". There is no reading of that
        which permits another message."""
        r = self._post(self._event("email.complained", "angry@x.com"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("angry@x.com", ls._suppression())

    def test_a_permanent_bounce_suppresses(self):
        self._post(self._event("email.bounced", "gone@x.com", "Permanent"))
        self.assertIn("gone@x.com", ls._suppression())

    def test_a_transient_bounce_does_not(self):
        """A full mailbox empties. Retiring a good prospect over a temporary
        condition loses a real customer."""
        self._post(self._event("email.bounced", "busy@x.com", "Transient"))
        self.assertNotIn("busy@x.com", ls._suppression())

    def test_a_bounce_with_no_stated_class_does_not_suppress(self):
        """Unknown is not permanent. Err towards keeping the address."""
        self._post(self._event("email.bounced", "vague@x.com"))
        self.assertNotIn("vague@x.com", ls._suppression())

    def test_a_delivery_is_recorded_but_suppresses_nobody(self):
        r = self._post(self._event("email.delivered", "fine@x.com"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ls._suppression(), set())

    def test_an_unknown_event_still_answers_200(self):
        """A non-2xx tells Resend to retry, and retrying an event we do not
        act on accomplishes nothing but noise."""
        r = self._post({"type": "email.something_new", "data": {}})
        self.assertEqual(r.status_code, 200)

    def test_several_recipients_on_one_event_are_all_suppressed(self):
        ev = {"type": "email.complained",
              "data": {"to": ["a@x.com", "b@x.com"]}}
        self._post(ev)
        self.assertTrue({"a@x.com", "b@x.com"} <= ls._suppression())

    def test_addresses_are_normalised_before_suppressing(self):
        """The sender lowercases on the way out; this must match, or a
        suppressed address is silently re-mailed."""
        self._post(self._event("email.complained", "  Angry@X.COM  "))
        self.assertIn("angry@x.com", ls._suppression())

    def test_events_are_counted_for_visibility(self):
        self._post(self._event("email.complained", "a@x.com"))
        self._post(self._event("email.delivered", "b@x.com"), msg_id="msg_2")
        counts = self.store.get(ls._EMAIL_EVENTS_KEY) or {}
        self.assertEqual(counts.get("email.complained"), 1)
        self.assertEqual(counts.get("email.delivered"), 1)
        self.assertEqual(counts.get("suppressed"), 1)

    def test_a_rejected_signature_is_counted(self):
        """A mistyped secret and a webhook nobody has configured yet both
        leave the counters empty, and only one of them needs fixing. The
        rejection has to be visible or the difference is unknowable."""
        self._post(self._event("email.complained"), secret_ok=False)
        counts = self.store.get(ls._EMAIL_EVENTS_KEY) or {}
        self.assertEqual(counts.get("rejected_bad_signature"), 1)
        self.assertEqual(ls._suppression(), set(), "still suppresses nobody")


class SenderRespectsItTests(unittest.TestCase):
    """The whole point: a bounced address never gets mailed again."""

    def test_a_suppressed_address_is_dropped_from_a_later_campaign(self):
        store = {}
        with patch.object(kv_backend, "get",
                          side_effect=lambda k, d=None: store.get(k, d)), \
             patch.object(kv_backend, "set",
                          side_effect=lambda k, v: store.__setitem__(k, v)):
            ls._suppress("gone@x.com")
            queue, skipped, _ = ls._clean_recipients(["gone@x.com", "ok@x.com"])
        self.assertEqual([a for a, _ in queue], ["ok@x.com"])
        self.assertEqual(skipped, 1)


if __name__ == "__main__":
    unittest.main()
