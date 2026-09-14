"""Honouring "take me off your list" without depending on a good memory.

OUTREACH.md promises an opt-out is honoured immediately and permanently, and
until now that promise was a person typing the right word into the right
column of a CSV while doing something else.

The failure mode is quiet, which is what makes it worth a test. A status
typed "unsubscribe" rather than "unsubscribed" is not an error anywhere -- it
is simply a value outreach_draft.py does not recognise, so the row stays
eligible and the person who asked to be left alone gets a second email. That
is the exact outcome the guard exists to prevent, and it is a CAN-SPAM
violation on top.

So the value written is imported from the guard that reads it, and these
pin the parts that are easy to get subtly wrong: never downgrading a status
that is already final, never silently deciding that one person's request
covers their colleague, and never leaving the one un-backed-up file in the
project half-written.
"""
import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.outreach_draft import DO_NOT_CONTACT  # noqa: E402
from tools import outreach_optout as O  # noqa: E402

FIELDS = ["slug", "company", "greeting", "email", "city", "state", "intro",
          "status", "sent_date"]


def row(slug, email, status="sent", sent_date="2026-09-11"):
    return {"slug": slug, "company": slug.title(), "greeting": "team",
            "email": email, "city": "Columbia", "state": "MO",
            "intro": "x", "status": status, "sent_date": sent_date}


class ApplyTests(unittest.TestCase):
    def test_the_status_written_is_one_the_draft_guard_refuses_on(self):
        """The whole bug class: a near-miss spelling reads as eligible."""
        rows = [row("a", "a@x.com")]
        O.apply_optouts(rows, ["a@x.com"], "unsubscribed", "2026-09-14")
        self.assertIn(rows[0]["status"], DO_NOT_CONTACT)

    def test_the_address_is_matched_regardless_of_case_and_space(self):
        rows = [row("a", "A@X.com")]
        changed, missing, _ = O.apply_optouts(rows, ["  a@x.COM "],
                                              "unsubscribed", "2026-09-14")
        self.assertEqual(len(changed), 1)
        self.assertEqual(missing, [])

    def test_an_address_not_in_the_list_is_reported(self):
        rows = [row("a", "a@x.com")]
        _, missing, _ = O.apply_optouts(rows, ["nobody@y.com"],
                                        "unsubscribed", "2026-09-14")
        self.assertEqual(missing, ["nobody@y.com"])

    def test_other_rows_are_untouched(self):
        rows = [row("a", "a@x.com"), row("b", "b@y.com", status="ready")]
        O.apply_optouts(rows, ["a@x.com"], "unsubscribed", "2026-09-14")
        self.assertEqual(rows[1]["status"], "ready")

    def test_a_bounce_can_be_recorded_separately(self):
        rows = [row("a", "a@x.com")]
        O.apply_optouts(rows, ["a@x.com"], "bounced", "2026-09-14")
        self.assertEqual(rows[0]["status"], "bounced")

    # ── never walk a final status backwards ──

    def test_an_existing_unsubscribe_is_not_rewritten_as_a_bounce(self):
        """Already honoured is already honoured; a later bounce is not news."""
        rows = [row("a", "a@x.com", status="unsubscribed")]
        changed, _, _ = O.apply_optouts(rows, ["a@x.com"], "bounced",
                                        "2026-09-14")
        self.assertEqual(changed, [])
        self.assertEqual(rows[0]["status"], "unsubscribed")

    def test_running_it_twice_changes_nothing_the_second_time(self):
        rows = [row("a", "a@x.com")]
        O.apply_optouts(rows, ["a@x.com"], "unsubscribed", "2026-09-14")
        changed, _, _ = O.apply_optouts(rows, ["a@x.com"], "unsubscribed",
                                        "2026-09-15")
        self.assertEqual(changed, [])

    # ── one person's request may cover their colleague. May. ──

    def test_a_live_colleague_at_the_same_domain_is_reported(self):
        rows = [row("a", "matt@acme.com"), row("b", "sales@acme.com")]
        _, _, siblings = O.apply_optouts(rows, ["matt@acme.com"],
                                         "unsubscribed", "2026-09-14")
        self.assertEqual([r["slug"] for r in siblings], ["b"])

    def test_the_colleague_is_reported_but_never_changed(self):
        """Whose mailboxes a request covers is a judgement, not a match."""
        rows = [row("a", "matt@acme.com"), row("b", "sales@acme.com")]
        O.apply_optouts(rows, ["matt@acme.com"], "unsubscribed", "2026-09-14")
        self.assertEqual(rows[1]["status"], "sent")

    def test_a_free_mail_domain_does_not_drag_in_strangers(self):
        """Two gmail addresses are two people, not one company."""
        rows = [row("a", "one@gmail.com"), row("b", "two@gmail.com")]
        _, _, siblings = O.apply_optouts(rows, ["one@gmail.com"],
                                         "unsubscribed", "2026-09-14")
        self.assertEqual(siblings, [])

    def test_an_already_stopped_colleague_is_not_re_reported(self):
        rows = [row("a", "matt@acme.com"),
                row("b", "sales@acme.com", status="bounced")]
        _, _, siblings = O.apply_optouts(rows, ["matt@acme.com"],
                                         "unsubscribed", "2026-09-14")
        self.assertEqual(siblings, [])


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "outreach_prospects.csv")
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows([row("a", "a@x.com"), row("b", "b@y.com")])

    def read(self):
        with open(self.path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_the_file_survives_a_round_trip_intact(self):
        rows = self.read()
        O.write_rows(self.path, FIELDS, rows)
        self.assertEqual(self.read(), rows)

    def test_the_previous_file_is_kept(self):
        """One copy exists anywhere; it is gitignored on purpose."""
        O.write_rows(self.path, FIELDS, self.read())
        self.assertTrue(os.path.exists(self.path + ".bak"))

    def test_the_columns_keep_their_order(self):
        O.write_rows(self.path, FIELDS, self.read())
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.readline().strip(), ",".join(FIELDS))

    def test_a_failed_write_leaves_no_debris(self):
        before = set(os.listdir(self.dir))
        with self.assertRaises(Exception):
            O.write_rows(self.path, FIELDS, [{"nope": 1}])
        leftover = set(os.listdir(self.dir)) - before - {
            os.path.basename(self.path) + ".bak"}
        self.assertEqual(leftover, set())

    def test_a_failed_write_does_not_corrupt_the_list(self):
        with self.assertRaises(Exception):
            O.write_rows(self.path, FIELDS, [{"nope": 1}])
        self.assertEqual(len(self.read()), 2)


if __name__ == "__main__":
    unittest.main()
