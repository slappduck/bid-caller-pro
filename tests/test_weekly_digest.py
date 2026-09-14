"""Delivering the numbers, and surviving the weeks worth reading about.

Four summaries were built and left where somebody had to remember to go and
look at them. Numbers nobody reads do not inform anything, so this mails them.

The design constraint that matters is failure. A summary that throws is not
an edge case here -- the weeks something breaks are exactly the weeks the
mail needs to arrive, so one bad section has to degrade to a line saying so
rather than costing the whole digest. The old behaviour, where the numbers
simply sat unread, was itself the failure mode being fixed.

It doubles as the outer dead-man's-switch: the watchdog only speaks when
something is wrong, so a dead watchdog reads as a quiet week. A mail expected
every Monday fails loudly by not arriving.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls  # noqa: E402


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.mail = []
        self.saved = {n: getattr(ls, n) for n in
                      ("_funnel_summary", "_bi_summary", "_engagement",
                       "_wins_summary", "_cron_status", "_send_email")}
        ls._funnel_summary = lambda *a, **k: {"site_visits": 40,
                                              "signups_started": 3,
                                              "visit_to_trial_pct": 7.5}
        ls._bi_summary = lambda *a, **k: {"people": {"trialled": 3, "paid": 0},
                                          "trial_to_paid_pct": None}
        ls._engagement = lambda *a, **k: {"active_people": 2,
                                          "silent_14d_or_more": 1}
        ls._wins_summary = lambda *a, **k: {"submitted": 4, "won": 1}
        ls._cron_status = lambda: [
            {"job": "bid-audit", "last": "x", "hours": 2.0, "max_hours": 36,
             "overdue": False}]
        ls._send_email = lambda to, subject, text, **kw: self.mail.append(
            (to, subject, text))

    def tearDown(self):
        for name, fn in self.saved.items():
            setattr(ls, name, fn)

    def body(self):
        ls._run_weekly_digest()
        return self.mail[0][2]

    def test_it_sends_to_the_support_address(self):
        out = ls._run_weekly_digest()
        self.assertTrue(out["ok"])
        self.assertEqual(self.mail[0][0], ls.SUPPORT_EMAIL)

    def test_every_section_appears(self):
        body = self.body()
        for heading in ("Funnel", "Lifecycle", "Engagement", "Outcomes"):
            self.assertIn(heading, body)

    def test_the_numbers_are_in_it(self):
        body = self.body()
        self.assertIn("40", body)
        self.assertIn("site_visits", body)

    def test_a_none_reads_as_a_dash_not_as_zero(self):
        """None means "nobody came", zero means "converts nobody"."""
        body = self.body()
        line = [ln for ln in body.splitlines()
                if "trial_to_paid_pct" in ln][0]
        self.assertIn("-", line)
        self.assertNotIn("0", line.split("trial_to_paid_pct")[1])

    # ── the weeks worth reading about ──

    def test_one_broken_summary_does_not_cost_the_digest(self):
        def boom(*a, **k):
            raise RuntimeError("supabase down")

        ls._wins_summary = boom
        out = ls._run_weekly_digest()
        self.assertTrue(out["ok"])
        self.assertIn("Funnel", self.mail[0][2])

    def test_a_broken_summary_says_so_rather_than_vanishing(self):
        def boom(*a, **k):
            raise RuntimeError("supabase down")

        ls._wins_summary = boom
        self.assertIn("unavailable", self.body())

    def test_every_summary_broken_still_sends(self):
        def boom(*a, **k):
            raise RuntimeError("everything is on fire")

        for n in ("_funnel_summary", "_bi_summary", "_engagement",
                  "_wins_summary"):
            setattr(ls, n, boom)
        self.assertTrue(ls._run_weekly_digest()["ok"])
        self.assertEqual(len(self.mail), 1)

    # ── it carries the scheduler's pulse ──

    def test_healthy_crons_are_summarised_not_listed(self):
        self.assertIn("all 1 reporting", self.body())

    def test_an_overdue_job_is_named_in_the_digest(self):
        ls._cron_status = lambda: [
            {"job": "federal-refresh", "last": None, "hours": None,
             "max_hours": 36, "overdue": True}]
        body = self.body()
        self.assertIn("OVERDUE", body)
        self.assertIn("federal-refresh", body)
        self.assertIn("never", body)

    def test_the_overdue_jobs_come_back_to_the_caller_too(self):
        ls._cron_status = lambda: [
            {"job": "bid-audit", "last": None, "hours": None,
             "max_hours": 36, "overdue": True}]
        self.assertEqual(ls._run_weekly_digest()["overdue"], ["bid-audit"])

    def test_it_tells_the_reader_what_silence_means(self):
        """The whole point of a dead-man's-switch is knowing it is one."""
        self.assertIn("If it stops", self.body())

    def test_no_support_address_is_reported_not_swallowed(self):
        original = ls.SUPPORT_EMAIL
        ls.SUPPORT_EMAIL = ""
        try:
            out = ls._run_weekly_digest()
        finally:
            ls.SUPPORT_EMAIL = original
        self.assertFalse(out["ok"])
        self.assertEqual(self.mail, [])


if __name__ == "__main__":
    unittest.main()
