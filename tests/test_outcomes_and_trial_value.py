"""Two things that turn the product into money.

1. Wins reported in the app ("how did it go?") are recorded by account, and
   /proof totals them for the landing page, held back until there are
   enough from more than one customer.
2. On day 4 of a trial, one email lists the open jobs scans have found in
   the trial's area, so the trial is used while there is time to bid.
"""
import datetime
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls  # noqa: E402


class _KV:
    def __init__(self):
        self.d = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.kv = _KV()
        self.p = [patch.object(ls.kv_backend, "get", side_effect=self.kv.get),
                  patch.object(ls.kv_backend, "set", side_effect=self.kv.set)]
        for p in self.p:
            p.start()
        self.client = ls.app.test_client()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_a_win_is_recorded_with_its_amount(self):
        self.assertTrue(ls._record_outcome("A@x.com", "b1", "won", 48500, "mo"))
        row = self.kv.d[ls._OUTCOMES_KEY]["a@x.com|b1"]
        self.assertEqual((row["result"], row["amount"], row["state"]), ("won", 48500.0, "MO"))

    def test_an_absurd_amount_keeps_the_win_but_not_the_amount(self):
        ls._record_outcome("a@x.com", "b1", "won", 9_000_000_000)
        self.assertIsNone(self.kv.d[ls._OUTCOMES_KEY]["a@x.com|b1"]["amount"])

    def test_answering_again_replaces_the_answer(self):
        ls._record_outcome("a@x.com", "b1", "lost")
        ls._record_outcome("a@x.com", "b1", "won", 1000)
        self.assertEqual(len(self.kv.d[ls._OUTCOMES_KEY]), 1)
        self.assertEqual(ls._outcome_summary()["won"], 1)

    def test_junk_is_refused(self):
        self.assertFalse(ls._record_outcome("a@x.com", "b1", "maybe"))
        self.assertFalse(ls._record_outcome("", "b1", "won"))

    def test_one_account_cannot_grow_the_store_without_bound(self):
        for i in range(ls.OUTCOMES_PER_ACCOUNT):
            ls._record_outcome("a@x.com", "b%d" % i, "passed")
        self.assertFalse(ls._record_outcome("a@x.com", "one-more", "won", 5))

    def test_the_endpoint_needs_a_signed_in_user(self):
        r = self.client.post("/outcome", json={"bid_id": "b1", "result": "won"})
        self.assertEqual(r.status_code, 401)
        with patch.object(ls, "_verify_supabase_token", return_value="a@x.com"):
            r = self.client.post("/outcome", json={"supabase_token": "t", "bid_id": "b1",
                                                   "result": "won", "amount": 20000})
        self.assertTrue(r.get_json()["ok"])

    def test_proof_waits_for_enough_wins_from_more_than_one_customer(self):
        ls._record_outcome("a@x.com", "b1", "won", 30000)
        ls._record_outcome("a@x.com", "b2", "won", 30000)
        ls._record_outcome("a@x.com", "b3", "won", 30000)
        self.assertFalse(self.client.get("/proof").get_json()["show"],
                         "three wins from one account must not be the public claim")
        ls._record_outcome("b@x.com", "b9", "won", 15000)
        d = self.client.get("/proof").get_json()
        self.assertTrue(d["show"])
        self.assertEqual((d["wins"], d["won_total"]), (4, 105000))

    def test_proof_never_shows_identities(self):
        ls._record_outcome("a@x.com", "b1", "won", 1)
        ls._record_outcome("b@x.com", "b2", "won", 1)
        ls._record_outcome("c@x.com", "b3", "won", 1)
        body = self.client.get("/proof").get_data(as_text=True)
        self.assertNotIn("@", body)


class TrialValueNoticeTests(unittest.TestCase):
    NOW = datetime.datetime(2026, 11, 10, 9, 0)
    CENTER = {"lat": 37.0, "lon": -93.7, "city": "Aurora", "state": "MO"}
    BIDS = {"k1": {"title": "Sidewalk Phase 2", "town": "Monett", "lat": 36.93, "lon": -93.93,
                   "due": "2026-11-20", "seen": "2026-11-09"},
            "k2": {"title": "ADA Ramps", "town": "Mt. Vernon", "lat": 37.10, "lon": -93.82,
                   "due": "2026-11-18", "seen": "2026-11-09"},
            "far": {"title": "Far away", "town": "Denver", "lat": 39.7, "lon": -104.9,
                    "due": "2026-11-30", "seen": "2026-11-09"}}

    def run_with(self, trials, areas=None, bids=None, paid=False):
        db = {"trials": trials}
        sent = []
        with patch.object(ls, "_trial_areas", return_value=areas if areas is not None
                          else {"t@x.com": ("Aurora, MO", 50.0)}), \
             patch.object(ls, "_open_bid_rows", return_value=self.BIDS if bids is None else bids), \
             patch.object(ls, "_resolve_center", return_value=self.CENTER), \
             patch.object(ls, "_has_paid_licence", return_value=paid), \
             patch.object(ls, "_send_email", side_effect=lambda to, subj, body, **k:
                          sent.append((to, subj, body)) or True):
            n = ls._run_trial_value_notices(db, self.NOW)
        return n, sent, db

    def trial(self, days_ago):
        return {"email:t@x.com": {"email": "t@x.com",
                                  "started": (self.NOW - datetime.timedelta(days=days_ago)).isoformat()}}

    def test_day_four_lists_what_was_found_nearby(self):
        n, sent, db = self.run_with(self.trial(4))
        self.assertEqual(n, 1)
        to, subj, body = sent[0]
        self.assertEqual(to, "t@x.com")
        self.assertEqual(subj, "2 open jobs near Aurora")
        self.assertIn("ADA Ramps, Mt. Vernon, due Nov 18", body)
        self.assertIn("Sidewalk Phase 2, Monett, due Nov 20", body)
        self.assertNotIn("Denver", body)
        self.assertIn("items ready", body)
        self.assertIn("days left", body)
        self.assertTrue(db["trials"]["email:t@x.com"].get("value_notice_at"))

    def test_sent_once(self):
        trials = self.trial(4)
        trials["email:t@x.com"]["value_notice_at"] = "2026-11-09T09:00:00"
        self.assertEqual(self.run_with(trials)[0], 0)

    def test_not_before_day_four(self):
        self.assertEqual(self.run_with(self.trial(2))[0], 0)

    def test_nothing_found_means_no_email_and_another_try_tomorrow(self):
        n, sent, db = self.run_with(self.trial(5), bids={})
        self.assertEqual((n, sent), (0, []))
        self.assertNotIn("value_notice_at", db["trials"]["email:t@x.com"])

    def test_a_paying_customer_gets_no_trial_email(self):
        self.assertEqual(self.run_with(self.trial(4), paid=True)[0], 0)

    def test_a_trial_with_no_area_is_skipped(self):
        self.assertEqual(self.run_with(self.trial(4), areas={})[0], 0)

    def test_it_runs_inside_the_daily_trial_job(self):
        """No new scheduled job: the trial-reminders cron already runs daily."""
        with patch.object(ls, "_db", return_value={"trials": {}}), \
             patch.object(ls, "_run_trial_value_notices", return_value=2) as v, \
             patch.object(ls, "_save_db"):
            r = ls._run_trial_reminders(now=self.NOW)
        v.assert_called_once()
        self.assertEqual(r["value_sent"], 2)


if __name__ == "__main__":
    unittest.main()


class OutcomeDeletionTests(OutcomeTests):
    def test_deleting_the_account_removes_its_outcomes(self):
        ls._record_outcome("a@x.com", "b1", "won", 100)
        ls._record_outcome("b@x.com", "b2", "won", 100)
        ls._forget_outcomes("A@x.com")
        self.assertEqual(list(self.kv.d[ls._OUTCOMES_KEY]), ["b@x.com|b2"])

    def test_the_delete_endpoint_calls_it(self):
        import inspect
        self.assertIn("_forget_outcomes(email)", inspect.getsource(ls.account_delete))
