"""Outbound campaign email: nothing sends without explicit approval, and
CAN-SPAM compliance is enforced in code.

A cold-email blast cannot be recalled, so drafting and sending are two
separate calls: /campaign/send only ever builds a draft and returns the
exact message for review, and /campaign/approve -- naming that draft, with
confirm: true -- is the only thing in the app that mails anybody.

The legal requirements that can be checked mechanically are checked here
too rather than left to whoever writes the campaign text: a physical postal
address in every message, a working one-click unsubscribe, and an
unsubscribe honoured permanently and re-checked at approval time.
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

    def _draft(self, **over):
        body = {"admin_token": TOKEN, "subject": "Try CurbCall Pro",
                "body": "Free 7-day trial.", "recipients": ["a@x.com"]}
        body.update(over)
        return self.client.post("/campaign/send", json=body)

    def _approve(self, draft_id, **over):
        body = {"admin_token": TOKEN, "draft_id": draft_id, "confirm": True}
        body.update(over)
        return self.client.post("/campaign/approve", json=body)

    def _send(self, **over):
        """Draft then approve — the full path, for tests about what goes out."""
        r = self._draft(**over)
        if r.status_code != 200 or not r.get_json().get("draft_id"):
            return r
        return self._approve(r.get_json()["draft_id"])

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
        drafted = self._draft(recipients=["gone@x.com", "stays@x.com"]).get_json()
        self.assertEqual(drafted["skipped_unsubscribed"], 1)
        self._approve(drafted["draft_id"])
        self.assertEqual([s["to"] for s in self.sent], ["stays@x.com"])

    def test_unsubscribing_between_draft_and_approval_still_stops_the_send(self):
        """The whole point of re-checking at approval: a draft can sit for
        hours, and somebody who opts out in the meantime must not be mailed
        by an approval of the older list."""
        drafted = self._draft(recipients=["late@x.com", "stays@x.com"]).get_json()
        self.assertEqual(drafted["would_send"], 2)
        ls._suppress("late@x.com")
        self._approve(drafted["draft_id"])
        self.assertEqual([s["to"] for s in self.sent], ["stays@x.com"])

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
            d = self._draft(recipients=[f"u{i}@x.com" for i in range(10)]).get_json()
            self.assertEqual(d["would_send"], 3)
            self.assertEqual(d["over_limit_not_sent"], 7)
            self._approve(d["draft_id"])
        self.assertEqual(len(self.sent), 3)

    # ── the approval gate ──
    def test_drafting_sends_nothing_at_all(self):
        """There is no path named "send" that actually mails anybody."""
        r = self._draft(recipients=["a@x.com", "b@x.com"]).get_json()
        self.assertEqual(self.sent, [])
        self.assertEqual(r["sent"], 0)
        self.assertEqual(r["status"], "awaiting_approval")
        self.assertEqual(r["would_send"], 2)

    def test_the_preview_is_exactly_what_gets_sent(self):
        d = self._draft(recipients=["a@x.com"]).get_json()
        self._approve(d["draft_id"])
        self.assertEqual(self.sent[0]["text"], d["preview"])

    def test_approval_needs_an_explicit_confirm(self):
        d = self._draft().get_json()
        r = self._approve(d["draft_id"], confirm=False)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "confirmation_required")
        self.assertEqual(self.sent, [])

    def test_approval_needs_the_admin_token(self):
        d = self._draft().get_json()
        r = self.client.post("/campaign/approve",
                             json={"draft_id": d["draft_id"], "confirm": True})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.sent, [])

    def test_an_unknown_draft_sends_nothing(self):
        r = self._approve("nosuchdraft")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.sent, [])

    def test_a_draft_cannot_be_approved_twice(self):
        """Otherwise a retried request puts a second copy of the campaign in
        everyone's inbox."""
        d = self._draft(recipients=["a@x.com"]).get_json()
        self._approve(d["draft_id"])
        self.assertEqual(self._approve(d["draft_id"]).status_code, 404)
        self.assertEqual(len(self.sent), 1)

    def test_an_expired_draft_cannot_be_approved(self):
        d = self._draft().get_json()
        with patch.object(ls, "DRAFT_TTL_HOURS", 0):
            r = self._approve(d["draft_id"])
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.sent, [])

    def test_pending_drafts_are_listable_and_discardable(self):
        d = self._draft().get_json()
        listed = self.client.post("/campaign/drafts",
                                  json={"admin_token": TOKEN}).get_json()
        self.assertEqual([x["draft_id"] for x in listed["drafts"]], [d["draft_id"]])
        self.client.post("/campaign/drafts",
                         json={"admin_token": TOKEN, "discard": d["draft_id"]})
        self.assertEqual(self._approve(d["draft_id"]).status_code, 404)
        self.assertEqual(self.sent, [])

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


class MergeFieldTests(CampaignTests):
    """Per-recipient personalisation.

    A campaign that cannot say "3 open jobs within 125 miles of Grimes, the
    nearest 8 miles out and closing September 2" is a generic pitch. One that
    can is a demonstration the reader can check. The numbers come from a real
    scan of their own market, so the whole value of the feature depends on
    each recipient getting THEIR numbers and never someone else's -- or,
    worse, the raw placeholder.
    """

    RICH = ("Hi {{company}} -- there are {{bids}} open concrete jobs within "
            "125 miles of {{city}} right now. Nearest is {{nearest}}.")

    def _people(self):
        return [
            {"email": "a@x.com", "vars": {"company": "Ford Concrete",
                                          "bids": "5", "city": "Kansas City",
                                          "nearest": "51 miles out"}},
            {"email": "b@x.com", "vars": {"company": "Kain's Concrete",
                                          "bids": "7", "city": "Springfield",
                                          "nearest": "38 miles out"}},
        ]

    def test_each_recipient_gets_their_own_numbers(self):
        r = self._send(body=self.RICH, recipients=self._people())
        self.assertEqual(r.get_json().get("sent"), 2, r.get_json())
        by = {m["to"]: m["text"] for m in self.sent}
        self.assertIn("Ford Concrete", by["a@x.com"])
        self.assertIn("5 open concrete jobs", by["a@x.com"])
        self.assertIn("Kansas City", by["a@x.com"])
        self.assertIn("Kain's Concrete", by["b@x.com"])
        self.assertIn("7 open concrete jobs", by["b@x.com"])
        self.assertIn("Springfield", by["b@x.com"])

    def test_nobody_gets_another_recipients_numbers(self):
        self._send(body=self.RICH, recipients=self._people())
        by = {m["to"]: m["text"] for m in self.sent}
        self.assertNotIn("Springfield", by["a@x.com"])
        self.assertNotIn("Kansas City", by["b@x.com"])

    def test_no_raw_placeholder_ever_goes_out(self):
        self._send(body=self.RICH, recipients=self._people())
        for m in self.sent:
            self.assertNotIn("{{", m["text"], m["to"])

    def test_a_missing_field_refuses_the_whole_draft(self):
        """Not just that recipient. The usual cause is a column named
        differently from the placeholder, which would silently halve a send
        that cannot be recalled."""
        people = self._people()
        del people[1]["vars"]["city"]
        r = self._draft(body=self.RICH, recipients=people)
        self.assertEqual(r.status_code, 400)
        j = r.get_json()
        self.assertEqual(j["reason"], "missing_merge_fields")
        self.assertEqual(j["sent"], 0)
        self.assertEqual(j["recipients_missing"][0]["missing"], ["city"])
        self.assertEqual(self.sent, [], "nothing may be sent")

    def test_a_blank_field_counts_as_missing(self):
        people = self._people()
        people[0]["vars"]["bids"] = "   "
        r = self._draft(body=self.RICH, recipients=people)
        self.assertEqual(r.status_code, 400)

    def test_the_preview_shows_merged_text_not_placeholders(self):
        j = self._draft(body=self.RICH, recipients=self._people()).get_json()
        self.assertNotIn("{{", j["preview"])
        self.assertIn("Ford Concrete", j["preview"])
        self.assertEqual(j["preview_for"], "a@x.com")
        self.assertEqual(j["merge_fields"],
                         ["bids", "city", "company", "nearest"])

    def test_the_preview_is_exactly_what_that_person_receives(self):
        """The reason draft and send share one renderer."""
        j = self._draft(body=self.RICH, recipients=self._people()).get_json()
        self._approve(j["draft_id"])
        first = [m for m in self.sent if m["to"] == "a@x.com"][0]
        self.assertEqual(j["preview"], first["text"])

    def test_a_plain_list_of_addresses_still_works(self):
        """The existing caller shape, unchanged."""
        r = self._send(recipients=["a@x.com", "b@x.com"])
        self.assertEqual(r.get_json().get("sent"), 2)

    def test_the_footer_and_unsubscribe_survive_personalisation(self):
        self._send(body=self.RICH, recipients=self._people())
        for m in self.sent:
            self.assertIn(ADDRESS, m["text"])
            self.assertIn("Unsubscribe:", m["text"])
            self.assertIn("List-Unsubscribe", m.get("headers", {}))

    def test_an_unsubscribed_recipient_is_still_dropped(self):
        ls._suppress("b@x.com")
        r = self._send(body=self.RICH, recipients=self._people())
        self.assertEqual(r.get_json().get("sent"), 1)
        self.assertEqual([m["to"] for m in self.sent], ["a@x.com"])

    def test_a_merge_value_cannot_carry_an_essay(self):
        people = self._people()
        people[0]["vars"]["company"] = "A" * 5000
        self._send(body=self.RICH, recipients=people)
        first = [m for m in self.sent if m["to"] == "a@x.com"][0]
        self.assertLess(len(first["text"]), 1500)


if __name__ == "__main__":
    unittest.main()
