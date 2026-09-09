"""What the system knew about a departing customer: nothing but that they left.

A cancellation appended a licence key to a flat list -- db["revoked"] -- with
no date, no plan and no reason. So "how long does a customer last", "did churn
move after a price change" and "what share of trials convert" were all
unanswerable, and unanswerable PERMANENTLY: a date nobody wrote down at the
moment it happened cannot be recovered later.

That is the whole argument for instrumenting before there is enough data to be
interesting. The first ten customers decide whether this business works, and
they are the ones whose behaviour is most easily lost.

The constraint is that none of this may become a way to read off who the
customers are. Each event carries a short salted hash instead of an address:
enough to follow one person's trial -> paid -> churn arc, useless for
enumerating anybody.
"""
import datetime
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


def ago(days):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=days)).isoformat()


class IdentityIsNotStoredTests(unittest.TestCase):
    def test_the_handle_is_not_the_address(self):
        h = ls._bi_id("josh@example.com")
        self.assertTrue(h)
        self.assertNotIn("josh", h)
        self.assertNotIn("example.com", h)

    def test_the_same_person_gets_the_same_handle(self):
        """Otherwise no arc can be followed and every metric is per-event."""
        self.assertEqual(ls._bi_id("josh@example.com"),
                         ls._bi_id("Josh@Example.com "))

    def test_plus_tags_are_one_person(self):
        """Same rule trial eligibility already uses."""
        self.assertEqual(ls._bi_id("josh+a@gmail.com"),
                         ls._bi_id("josh+b@gmail.com"))

    def test_different_people_differ(self):
        self.assertNotEqual(ls._bi_id("a@x.com"), ls._bi_id("b@x.com"))

    def test_no_address_reaches_the_log(self):
        db = {}
        ls._bi_note(db, "subscribed", "josh@example.com", "monthly")
        self.assertNotIn("josh", json.dumps(db["lifecycle"]))


class LoggingNeverBreaksBillingTests(unittest.TestCase):
    """A metric is never worth failing a payment over."""

    def test_a_broken_db_object_is_swallowed(self):
        class Bad(dict):
            def setdefault(self, *a, **k):
                raise RuntimeError("boom")
        ls._bi_note(Bad(), "subscribed", "a@x.com", "monthly")

    def test_the_log_is_bounded(self):
        db = {"lifecycle": [{"at": ago(1), "event": "x", "id": "i"}
                            for _ in range(ls.LIFECYCLE_MAX + 50)]}
        ls._bi_note(db, "subscribed", "a@x.com")
        self.assertLessEqual(len(db["lifecycle"]), ls.LIFECYCLE_MAX)


class SummaryTests(unittest.TestCase):
    def _db(self):
        rows = [
            {"at": ago(40), "event": "trial_started", "id": "A"},
            {"at": ago(35), "event": "subscribed", "id": "A", "plan": "monthly"},
            {"at": ago(5), "event": "churned", "id": "A", "plan": "monthly",
             "reason": "deleted"},
            {"at": ago(30), "event": "trial_started", "id": "B"},
            {"at": ago(20), "event": "subscribed", "id": "B", "plan": "annual"},
            {"at": ago(10), "event": "trial_started", "id": "C"},
        ]
        return {"lifecycle": rows}

    def test_the_number_that_decides_the_funnel(self):
        """Two of three trials paid."""
        self.assertEqual(ls._bi_summary(self._db())["trial_to_paid_pct"], 66.7)

    def test_how_long_a_trial_takes_to_convert(self):
        """5 days and 10 days -> 7.5."""
        self.assertEqual(
            ls._bi_summary(self._db())["median_days_trial_to_paid"], 7.5)

    def test_how_long_a_customer_lasted(self):
        """Subscribed 35 days ago, churned 5 days ago."""
        self.assertEqual(
            ls._bi_summary(self._db())["median_days_subscribed_before_churn"],
            30)

    def test_who_is_actually_paying_right_now(self):
        p = ls._bi_summary(self._db())["people"]
        self.assertEqual(p, {"trialled": 3, "paid": 2, "churned": 1,
                             "paying_now": 1})

    def test_which_plans_sell(self):
        self.assertEqual(ls._bi_summary(self._db())["plans_sold"],
                         {"monthly": 1, "annual": 1})

    def test_an_empty_log_does_not_divide_by_zero(self):
        out = ls._bi_summary({"lifecycle": []})
        self.assertEqual(out["events"], 0)
        self.assertIsNone(out["trial_to_paid_pct"])

    def test_a_corrupt_row_does_not_sink_the_report(self):
        db = self._db()
        db["lifecycle"].append({"at": "not-a-date", "event": "churned",
                                "id": "D"})
        self.assertEqual(ls._bi_summary(db)["people"]["trialled"], 3)

    def test_a_repeated_event_counts_the_person_once(self):
        """Renewals must not look like new customers."""
        db = self._db()
        db["lifecycle"].append({"at": ago(1), "event": "subscribed", "id": "A",
                                "plan": "monthly"})
        self.assertEqual(ls._bi_summary(db)["people"]["paid"], 2)

    def test_the_summary_names_nobody(self):
        blob = json.dumps(ls._bi_summary(self._db()))
        self.assertNotIn("@", blob)


class EventsAreRecordedWhereTheyHappenTests(unittest.TestCase):
    """Grepped deliberately: these are one-line calls inside billing paths
    that are easy to drop in a refactor, and their absence is silent."""

    def setUp(self):
        import re
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               os.pardir, "license_server.py"),
                  encoding="utf-8") as f:
            self.src = re.sub(r"^\s*#.*$", "", f.read(), flags=re.M)

    def test_a_trial_start_is_recorded(self):
        self.assertIn('_bi_note(db, "trial_started"', self.src)

    def test_a_purchase_is_recorded(self):
        self.assertIn('_bi_note(db, "subscribed"', self.src)

    def test_a_renewal_is_recorded(self):
        self.assertIn('_bi_note(db, "renewed"', self.src)

    def test_a_cancellation_is_recorded_with_its_reason(self):
        i = self.src.index('_bi_note(db, "churned"')
        self.assertIn("reason", self.src[i:i + 200])

    def test_the_rollup_is_behind_the_diag_token(self):
        diag = self.src[self.src.index("def diag("):]
        diag = diag[:diag.index("\ndef ")]
        self.assertIn("_bi_summary()", diag)
        health = self.src[self.src.index("def health("):]
        health = health[:health.index("\ndef ")]
        self.assertNotIn("_bi_summary", health)


if __name__ == "__main__":
    unittest.main()
