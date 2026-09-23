"""One follow-up, a few days out, never sooner.

A joint study of 12 million outreach emails found a single follow-up lifts
replies substantially, and the first email alone never captures the whole
answer -- but sending it within a day of the first reads as impatient rather
than persistent, so this only ever fires after FOLLOWUP_AFTER_DAYS, and only
once per prospect.
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.outreach_followup_draft import due_for_followup, FOLLOWUP_AFTER_DAYS


def row(slug, status="sent", sent_date="", followup_date="", intro="the angle"):
    return {"slug": slug, "company": slug.title(), "greeting": "team",
            "email": f"{slug}@example.com", "city": "Columbia", "state": "MO",
            "intro": intro, "status": status, "sent_date": sent_date,
            "followup_date": followup_date}


TODAY = datetime.date(2026, 9, 20)


def days_ago(n):
    return (TODAY - datetime.timedelta(days=n)).isoformat()


class DueTests(unittest.TestCase):
    def test_a_row_old_enough_is_due(self):
        due, _ = due_for_followup([row("a", sent_date=days_ago(FOLLOWUP_AFTER_DAYS))], TODAY)
        self.assertEqual([r["slug"] for r, _ in due], ["a"])

    def test_a_row_sent_yesterday_is_not_due_yet(self):
        due, skipped = due_for_followup([row("a", sent_date=days_ago(1))], TODAY)
        self.assertEqual(due, [])
        self.assertEqual(len(skipped), 1)

    def test_the_boundary_is_inclusive(self):
        due, _ = due_for_followup(
            [row("a", sent_date=days_ago(FOLLOWUP_AFTER_DAYS))], TODAY)
        self.assertTrue(due)
        due, _ = due_for_followup(
            [row("a", sent_date=days_ago(FOLLOWUP_AFTER_DAYS - 1))], TODAY)
        self.assertFalse(due)

    def test_a_row_already_followed_up_is_never_listed_again(self):
        due, skipped = due_for_followup(
            [row("a", sent_date=days_ago(10), followup_date=days_ago(2))], TODAY)
        self.assertEqual(due, [])
        self.assertEqual(len(skipped), 0,
            "already handled, not even worth reporting as skipped")

    def test_a_row_that_is_not_sent_yet_is_ignored(self):
        due, skipped = due_for_followup([row("a", status="ready", sent_date="")], TODAY)
        self.assertEqual(due, [])

    def test_a_missing_sent_date_does_not_crash(self):
        due, skipped = due_for_followup([row("a", sent_date="")], TODAY)
        self.assertEqual(due, [])
        self.assertIn("missing", skipped[0][1])

    def test_a_garbled_sent_date_does_not_crash(self):
        due, skipped = due_for_followup([row("a", sent_date="not-a-date")], TODAY)
        self.assertEqual(due, [])
        self.assertIn("unreadable", skipped[0][1])

    def test_do_not_contact_statuses_are_never_listed(self):
        for status in ("unsubscribed", "bounced", "do-not-contact"):
            due, _ = due_for_followup(
                [row("a", status=status, sent_date=days_ago(30))], TODAY)
            self.assertEqual(due, [], status)

    def test_oldest_sends_come_first(self):
        rows = [row("newer", sent_date=days_ago(FOLLOWUP_AFTER_DAYS)),
                row("older", sent_date=days_ago(FOLLOWUP_AFTER_DAYS + 10))]
        due, _ = due_for_followup(rows, TODAY)
        self.assertEqual([r["slug"] for r, _ in due], ["older", "newer"])


class NoNetworkNoIntroCheckTests(unittest.TestCase):
    """A follow-up isn't a new pitch -- it references one that already
    passed every guard outreach_draft.py has. Re-running those here would
    just cost a live /coverage call for a fact nobody asked to re-verify."""

    def test_a_followup_needs_no_coverage_lookup(self):
        import inspect
        from tools import outreach_followup_draft as fd
        self.assertNotIn("coverage(", inspect.getsource(fd.due_for_followup))

    def test_a_followup_needs_no_location_evidence_check(self):
        import inspect
        from tools import outreach_followup_draft as fd
        self.assertNotIn("_location_evidence", inspect.getsource(fd))


if __name__ == "__main__":
    unittest.main()
