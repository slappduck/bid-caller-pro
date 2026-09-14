"""Noticing that a scheduled job has stopped, which nothing used to do.

The federal refresh failed ten consecutive scheduled runs across three days
and sent no alert. Not because alerting was missing -- this server emails on
unhandled errors, on feed-quality drops, on every search provider going down
-- but because all of that fires from inside a request Flask completed. The
refresh worker was killed at the platform timeout, so no handler ran.

A dead job is precisely the case the error handler cannot report, so the test
that matters here is the one for silence: a job that has never checked in at
all has to read as broken, not as fine.

The heartbeats live in the KV, so these swap in a dict and drive the clock
directly rather than waiting around.
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls  # noqa: E402


def ago(hours):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=hours)).isoformat()


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self.sent = []
        self._kv_get, self._kv_set = ls.kv_backend.get, ls.kv_backend.set
        self._alert = ls._alert_admin
        ls.kv_backend.get = lambda k, d=None: self.store.get(k, d)
        ls.kv_backend.set = lambda k, v, **kw: self.store.__setitem__(k, v)
        ls._alert_admin = lambda subject, detail: self.sent.append(
            (subject, detail))

    def tearDown(self):
        ls.kv_backend.get, ls.kv_backend.set = self._kv_get, self._kv_set
        ls._alert_admin = self._alert

    def beat_all(self, hours=1):
        self.store[ls.CRON_HEARTBEAT_KEY] = {
            job: ago(hours) for job in ls.CRON_EXPECTED}

    def test_a_job_that_never_reported_is_overdue_not_fine(self):
        """An empty record is the ten-silent-runs case. It must not read ok."""
        out = ls._cron_watchdog()
        self.assertEqual(sorted(out["overdue"]), sorted(ls.CRON_EXPECTED))
        self.assertTrue(self.sent)

    def test_all_jobs_fresh_sends_nothing(self):
        """A watchdog that emails daily stops being read within a week."""
        self.beat_all(hours=1)
        out = ls._cron_watchdog()
        self.assertEqual(out["overdue"], [])
        self.assertEqual(self.sent, [])

    def test_one_late_job_is_named(self):
        self.beat_all(hours=1)
        self.store[ls.CRON_HEARTBEAT_KEY]["federal-refresh"] = ago(80)
        out = ls._cron_watchdog()
        self.assertEqual(out["overdue"], ["federal-refresh"])
        self.assertIn("federal-refresh", self.sent[0][0])

    def test_a_weekly_job_is_not_late_after_two_days(self):
        """upcoming-alerts runs weekly; judging it daily would cry wolf."""
        self.beat_all(hours=48)
        self.assertNotIn("upcoming-alerts", ls._cron_watchdog()["overdue"])

    def test_the_weekly_job_is_late_eventually(self):
        self.beat_all(hours=1)
        self.store[ls.CRON_HEARTBEAT_KEY]["upcoming-alerts"] = ago(24 * 9)
        self.assertIn("upcoming-alerts", ls._cron_watchdog()["overdue"])

    def test_the_alert_says_which_job_and_how_long(self):
        self.beat_all(hours=1)
        self.store[ls.CRON_HEARTBEAT_KEY]["bid-audit"] = ago(50)
        ls._cron_watchdog()
        detail = self.sent[0][1]
        self.assertIn("bid-audit", detail)
        self.assertIn("50", detail)

    def test_a_never_run_job_says_so_in_words(self):
        ls._cron_watchdog()
        self.assertIn("never reported a success", self.sent[0][1])

    def test_an_unreadable_timestamp_counts_as_overdue(self):
        """Corrupt beats the same as absent -- both mean 'cannot confirm'."""
        self.beat_all(hours=1)
        self.store[ls.CRON_HEARTBEAT_KEY]["bid-audit"] = "not a date"
        self.assertIn("bid-audit", ls._cron_watchdog()["overdue"])

    def test_a_broken_kv_does_not_take_the_endpoint_down(self):
        def boom(*a, **k):
            raise RuntimeError("kv down")

        ls.kv_backend.get = boom
        out = ls._cron_watchdog()
        self.assertTrue(out["ok"])
        self.assertEqual(sorted(out["overdue"]), sorted(ls.CRON_EXPECTED))


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self._kv_get, self._kv_set = ls.kv_backend.get, ls.kv_backend.set
        ls.kv_backend.get = lambda k, d=None: self.store.get(k, d)
        ls.kv_backend.set = lambda k, v, **kw: self.store.__setitem__(k, v)

    def tearDown(self):
        ls.kv_backend.get, ls.kv_backend.set = self._kv_get, self._kv_set

    def test_a_beat_is_recorded_and_readable(self):
        ls._cron_beat("bid-audit")
        self.assertIn("bid-audit", self.store[ls.CRON_HEARTBEAT_KEY])
        row = [r for r in ls._cron_status() if r["job"] == "bid-audit"][0]
        self.assertFalse(row["overdue"])

    def test_beats_accumulate_rather_than_replace_each_other(self):
        ls._cron_beat("bid-audit")
        ls._cron_beat("federal-refresh")
        self.assertEqual(len(self.store[ls.CRON_HEARTBEAT_KEY]), 2)

    def test_a_failing_kv_never_breaks_the_job_that_just_succeeded(self):
        """The heartbeat is bookkeeping. It must not fail a good run."""
        def boom(*a, **k):
            raise RuntimeError("kv down")

        ls.kv_backend.set = boom
        ls._cron_beat("bid-audit")   # must not raise

    def test_every_expected_job_has_a_beat_call_in_the_server(self):
        """A job in the table with nothing calling _cron_beat is a false alarm
        waiting to happen: it would page forever and never be satisfiable."""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(os.path.dirname(here), "license_server.py"),
                  encoding="utf-8") as f:
            src = f.read()
        for job in ls.CRON_EXPECTED:
            self.assertIn('_cron_beat("%s")' % job, src,
                          "%s is watched but nothing reports it" % job)


if __name__ == "__main__":
    unittest.main()
