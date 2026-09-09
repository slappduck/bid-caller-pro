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


class WinsSummaryTests(unittest.TestCase):
    """Whether the product actually gets contractors work.

    Customers mark a bid submitted, won, lost or passed. That sat per-user in
    Supabase and nothing ever read it in aggregate, so "is this product
    working" had no number behind it -- only opinion. It is also the question
    that predicts retention best: a contractor who wins a job does not cancel.

    The constraint is that row contents never leave Supabase. The only thing
    derived from identities is how many DISTINCT customers have won, and only
    the count is returned.
    """

    def setUp(self):
        self.asked = []
        self._open = ls.urllib.request.urlopen
        self._url, self._key = ls.SUPABASE_URL, ls.SUPABASE_SERVICE_ROLE_KEY
        ls.SUPABASE_URL = "https://project.supabase.co"
        ls.SUPABASE_SERVICE_ROLE_KEY = "svc-key"
        self.counts = {"": 40, "submitted": 9, "won": 3, "lost": 1, "passed": 2}
        self.won_rows = [{"user_id": "u-1"}, {"user_id": "u-1"},
                         {"user_id": "u-2"}]
        outer = self

        class Resp:
            def __init__(self, n=0, body=b"[]"):
                self.headers = {"Content-Range": "0-0/%d" % n}
                self._body = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self._body

        def fake(req, timeout=None):
            url = req.full_url
            outer.asked.append(url)
            if "select=user_id" in url:
                return Resp(body=json.dumps(outer.won_rows).encode())
            for state in ls._PIPELINE_STATES:
                if "pipeline=eq.%s" % state in url:
                    return Resp(outer.counts[state])
            return Resp(outer.counts[""])
        ls.urllib.request.urlopen = fake

    def tearDown(self):
        ls.urllib.request.urlopen = self._open
        ls.SUPABASE_URL, ls.SUPABASE_SERVICE_ROLE_KEY = self._url, self._key

    def test_every_pipeline_state_is_counted(self):
        out = ls._wins_summary()
        for state in ls._PIPELINE_STATES:
            self.assertEqual(out[state], self.counts[state], state)

    def test_the_win_rate_ignores_undecided_jobs(self):
        """3 won, 1 lost -> 75%. The 9 still submitted are not losses."""
        self.assertEqual(ls._wins_summary()["win_rate_pct"], 75.0)

    def test_no_decided_jobs_yields_no_rate_rather_than_zero(self):
        """Zero would read as 'we lose everything'."""
        self.counts["won"] = self.counts["lost"] = 0
        self.assertIsNone(ls._wins_summary()["win_rate_pct"])

    def test_it_counts_people_not_just_jobs(self):
        """One customer winning five is a different business from five
        customers winning one each."""
        self.assertEqual(ls._wins_summary()["customers_who_have_won"], 2)

    def test_no_row_content_is_returned(self):
        blob = json.dumps(ls._wins_summary())
        for leak in ("user_id", "u-1", "title", "@"):
            self.assertNotIn(leak, blob)

    def test_it_asks_for_counts_not_rows(self):
        ls._wins_summary()
        for url in self.asked:
            if "select=user_id" in url:
                continue
            self.assertIn("limit=1", url)

    def test_an_unconfigured_project_says_so(self):
        ls.SUPABASE_SERVICE_ROLE_KEY = ""
        self.assertEqual(ls._wins_summary(), {"configured": False})

    def test_a_failure_reports_the_type_and_not_the_detail(self):
        def boom(req, timeout=None):
            raise OSError("connect to https://project.supabase.co failed")
        ls.urllib.request.urlopen = boom
        self.assertEqual(ls._wins_summary(),
                         {"configured": True, "error": "OSError"})

    def test_it_is_behind_the_diag_token(self):
        import re
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               os.pardir, "license_server.py"),
                  encoding="utf-8") as f:
            src = re.sub(r"^\s*#.*$", "", f.read(), flags=re.M)
        health = src[src.index("def health("):]
        health = health[:health.index("\ndef ")]
        self.assertNotIn("_wins_summary", health)
        diag = src[src.index("def diag("):]
        diag = diag[:diag.index("\ndef ")]
        self.assertIn("_wins_summary()", diag)
