"""Telling somebody their trial is about to end, exactly once.

There was a reminder for people already paying and none for people who had
not started. A seven-day trial that ends in silence is a cancellation nobody
had to decide on, and the funnel to date has converted zero.

The two ways this goes wrong are opposite and both bad. Send twice and the
one genuinely useful email of the relationship reads as a drip campaign.
Send to somebody who already subscribed and you have told a paying customer
their access is ending, which is worse than saying nothing at all -- and it
is the easy mistake, because the obvious helper for "are they active"
counts a running trial as active and would suppress the email for precisely
the people it exists for.

So the record is stamped before anything else can go wrong, and paid status
is read from the licence rather than from activity.
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls  # noqa: E402


def started_days_ago(days):
    return (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()


class TrialReminderTests(unittest.TestCase):
    def setUp(self):
        self.db = {"trials": {}, "emails": {}, "revoked": []}
        self.mail = []
        self._db, self._save = ls._db, ls._save_db
        self._send, self._verify = ls._send_email, ls.verify_key
        ls._db = lambda: self.db
        ls._save_db = lambda db: None
        ls._send_email = lambda to, subject, text, **kw: self.mail.append(
            (to, subject, text))
        ls.verify_key = lambda key: (True, "monthly", "2030-01-01", "ok")

    def tearDown(self):
        ls._db, ls._save_db = self._db, self._save
        ls._send_email, ls.verify_key = self._send, self._verify

    def add(self, email, days, **extra):
        rec = {"started": started_days_ago(days), "email": email}
        rec.update(extra)
        self.db["trials"]["email:" + email] = rec
        return rec

    # ── the window ──

    def test_a_trial_inside_the_window_is_emailed(self):
        self.add("a@x.com", ls.TRIAL_DAYS - 1)
        out = ls._run_trial_reminders()
        self.assertEqual(out["sent"], 1)
        self.assertEqual(self.mail[0][0], "a@x.com")

    def test_a_fresh_trial_is_left_alone(self):
        self.add("a@x.com", 1)
        self.assertEqual(ls._run_trial_reminders()["sent"], 0)

    def test_an_expired_trial_is_not_chased(self):
        """After it lapses the moment has passed; this is not a win-back."""
        self.add("a@x.com", ls.TRIAL_DAYS + 3)
        self.assertEqual(ls._run_trial_reminders()["sent"], 0)

    # ── exactly once ──

    def test_the_second_run_sends_nothing(self):
        self.add("a@x.com", ls.TRIAL_DAYS - 1)
        ls._run_trial_reminders()
        ls._run_trial_reminders()
        self.assertEqual(len(self.mail), 1)

    def test_the_record_is_stamped(self):
        rec = self.add("a@x.com", ls.TRIAL_DAYS - 1)
        ls._run_trial_reminders()
        self.assertTrue(rec.get("ending_notice_at"))

    def test_an_already_stamped_trial_is_skipped(self):
        self.add("a@x.com", ls.TRIAL_DAYS - 1,
                 ending_notice_at="2026-01-01T00:00:00")
        self.assertEqual(ls._run_trial_reminders()["sent"], 0)

    # ── never tell a paying customer their access is ending ──

    def test_a_subscriber_is_not_told_their_trial_is_ending(self):
        self.add("a@x.com", ls.TRIAL_DAYS - 1)
        self.db["emails"]["a@x.com"] = "LIVE-KEY"
        self.assertEqual(ls._run_trial_reminders()["sent"], 0)

    def test_a_revoked_key_does_not_count_as_paid(self):
        self.add("a@x.com", ls.TRIAL_DAYS - 1)
        self.db["emails"]["a@x.com"] = "DEAD-KEY"
        self.db["revoked"] = ["DEAD-KEY"]
        self.assertEqual(ls._run_trial_reminders()["sent"], 1)

    def test_an_active_trial_alone_does_not_read_as_paid(self):
        """The trap: _license_is_active counts a trial, so it cannot be used."""
        self.add("a@x.com", ls.TRIAL_DAYS - 1)
        self.assertFalse(ls._has_paid_licence(self.db, "a@x.com"))

    # ── bad data must not stop the run ──

    def test_a_trial_with_no_email_is_skipped_not_crashed(self):
        self.db["trials"]["device-only"] = {"started": started_days_ago(6)}
        self.assertEqual(ls._run_trial_reminders()["sent"], 0)

    def test_a_corrupt_start_date_is_skipped(self):
        self.db["trials"]["email:a@x.com"] = {"started": "yesterday",
                                              "email": "a@x.com"}
        self.assertEqual(ls._run_trial_reminders()["sent"], 0)

    def test_one_failing_send_does_not_stop_the_others(self):
        self.add("bad@x.com", ls.TRIAL_DAYS - 1)
        self.add("good@x.com", ls.TRIAL_DAYS - 1)

        def flaky(to, subject, text, **kw):
            if to == "bad@x.com":
                raise RuntimeError("resend down")
            self.mail.append((to, subject, text))

        ls._send_email = flaky
        out = ls._run_trial_reminders()
        self.assertEqual(out["sent"], 1)
        self.assertEqual(self.mail[0][0], "good@x.com")

    def test_a_failed_send_is_not_stamped_so_it_retries_tomorrow(self):
        rec = self.add("bad@x.com", ls.TRIAL_DAYS - 1)

        def boom(*a, **kw):
            raise RuntimeError("resend down")

        ls._send_email = boom
        ls._run_trial_reminders()
        self.assertIsNone(rec.get("ending_notice_at"))

    # ── what the email says ──

    def test_it_says_no_card_is_on_file(self):
        """The trial takes no payment details, so nothing auto-charges."""
        self.add("a@x.com", ls.TRIAL_DAYS - 1)
        ls._run_trial_reminders()
        self.assertIn("no card on file", self.mail[0][2])

    def test_it_links_somewhere_they_can_actually_subscribe(self):
        self.add("a@x.com", ls.TRIAL_DAYS - 1)
        ls._run_trial_reminders()
        self.assertIn(ls.APP_URL, self.mail[0][2])

    def test_the_subject_names_the_timing(self):
        self.add("a@x.com", ls.TRIAL_DAYS - 0.5)
        ls._run_trial_reminders()
        self.assertIn("trial ends", self.mail[0][1].lower())


if __name__ == "__main__":
    unittest.main()
