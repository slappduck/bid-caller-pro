"""Outbound campaign email: CAN-SPAM compliance, enforced in code.

Commercial email has legal requirements and a blast cannot be un-sent, so
the ones that can be checked mechanically are checked here rather than left
to whoever writes the campaign text: a physical postal address in every
message, a working one-click unsubscribe, and an unsubscribe that is
honoured permanently and checked before every later send.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls
import kv_backend

TOKEN = "a-real-admin-token"
ADDRESS = "CurbCall Pro, 123 Main St, Aurora MO 65605"


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()
        self.store = {}
        self.sent = []
        self._p = [
            patch.object(ls, "ADMIN_TOKEN", TOKEN),
            patch.object(ls, "RESEND_API_KEY", "resend-key"),
            patch.object(ls, "MAILING_ADDRESS", ADDRESS),
            patch.object(ls, "CAMPAIGN_PAUSE_SEC", 0),
            patch.object(kv_backend, "get",
                         side_effect=lambda k, d=None: self.store.get(k, d)),
            patch.object(kv_backend, "set",
                         side_effect=lambda k, v: self.store.__setitem__(k, v)),
            patch.object(ls, "_send_email",
                         side_effect=lambda to, subj, text, **kw:
                             (self.sent.append({"to": to, "text": text, **kw}), True)[1]),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def _send(self, **over):
        body = {"admin_token": TOKEN, "subject": "Try CurbCall Pro",
                "body": "Free 7-day trial.", "recipients": ["a@x.com"]}
        body.update(over)
        return self.client.post("/campaign/send", json=body)

    # ── the compliance guards ──
    def test_it_refuses_to_send_without_a_postal_address(self):
        """CAN-SPAM requires one in every message and a blast can't be
        recalled, so this fails closed rather than sending."""
        with patch.object(ls, "MAILING_ADDRESS", "   "):
            r = self._send()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()["reason"], "mailing_address_not_configured")
        self.assertEqual(self.sent, [])

    def test_every_message_carries_the_address_and_an_unsubscribe_link(self):
        self._send(recipients=["a@x.com"])
        text = self.sent[0]["text"]
        self.assertIn(ADDRESS, text)
        self.assertIn("/unsubscribe?", text)

    def test_every_message_carries_the_one_click_unsubscribe_headers(self):
        # What mail clients surface as their own Unsubscribe button.
        self._send(recipients=["a@x.com"])
        h = self.sent[0]["headers"]
        self.assertIn("/unsubscribe?", h["List-Unsubscribe"])
        self.assertEqual(h["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")

    def test_an_unsubscribe_is_permanent_and_checked_before_later_sends(self):
        self._send(recipients=["gone@x.com", "stays@x.com"])
        link = [w for w in self.sent[0]["text"].split() if "/unsubscribe?" in w][0]
        self.assertEqual(self.client.get(link.replace(ls.PUBLIC_BASE_URL, "")).status_code, 200)
        self.sent.clear()
        r = self._send(recipients=["gone@x.com", "stays@x.com"]).get_json()
        self.assertEqual([s["to"] for s in self.sent], ["stays@x.com"])
        self.assertEqual(r["skipped_unsubscribed"], 1)

    def test_an_unsubscribe_link_cannot_be_edited_to_target_someone_else(self):
        r = self.client.get("/unsubscribe?e=victim@x.com&t=deadbeef")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(ls._suppression(), set())

    # ── access ──
    def test_sending_requires_the_admin_token(self):
        self.assertEqual(self._send(admin_token="wrong").status_code, 403)
        self.assertEqual(self.sent, [])

    def test_sending_is_disabled_when_no_admin_token_is_configured(self):
        with patch.object(ls, "ADMIN_TOKEN", ls._ADMIN_TOKEN_PLACEHOLDER):
            self.assertEqual(self._send().status_code, 503)

    # ── hygiene ──
    def test_recipients_are_deduped_case_insensitively(self):
        self._send(recipients=["A@x.com", "a@x.com", " a@X.com "])
        self.assertEqual(len(self.sent), 1)

    def test_junk_addresses_are_dropped(self):
        self._send(recipients=["notanemail", "", None, "ok@x.com"])
        self.assertEqual([s["to"] for s in self.sent], ["ok@x.com"])

    def test_a_batch_is_capped(self):
        with patch.object(ls, "CAMPAIGN_MAX_PER_REQUEST", 3):
            r = self._send(recipients=[f"u{i}@x.com" for i in range(10)]).get_json()
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(r["over_limit_not_sent"], 7)

    def test_a_dry_run_sends_nothing(self):
        r = self._send(recipients=["a@x.com", "b@x.com"], dry_run=True).get_json()
        self.assertEqual(self.sent, [])
        self.assertEqual(r["would_send"], 2)

    def test_subject_and_body_are_required(self):
        self.assertEqual(self._send(subject="  ").status_code, 400)
        self.assertEqual(self._send(body="  ").status_code, 400)
        self.assertEqual(self.sent, [])

    def test_suppression_list_is_readable_and_addable_by_admin(self):
        r = self.client.post("/campaign/suppression",
                             json={"admin_token": TOKEN, "add": ["X@x.com"]}).get_json()
        self.assertEqual(r["suppressed"], ["x@x.com"])
        self.assertEqual(self.client.post("/campaign/suppression",
                                          json={"admin_token": "no"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
