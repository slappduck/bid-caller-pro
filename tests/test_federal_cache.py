"""Federal bids stopped working, and no timeout value could fix it.

Measured against the live service: a REJECTED SAM request comes back in 0.6
seconds, while an authenticated one does not return in forty -- even asking
for a two-day window and a single row. Production said the same thing in its
own numbers: nine consecutive failures, and exactly two timeouts per scan at
six seconds each, which is the entire twelve-second federal budget spent to
learn nothing. `federal_kept` never appeared in a single recent scan.

SAM_TIMEOUT sits inside FEDERAL_BUDGET_SEC inside a scan somebody is watching.
Raising either only makes the scan slower while still failing. So the asking
moved out of the scan: a scheduled job fills a cache, and scans read it.

Two things this must get right, and both are tested here:

  * A failed refresh must never replace good data. Yesterday's federal bids
    are fine; none because the refresh had a bad night is the outage this
    exists to prevent.

  * A stale cache must fall back rather than be served. A solicitation that
    closed last week shown as open is worse than showing nothing.
"""
import datetime
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


def hours_ago(n):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=n)).isoformat()


class CacheReadTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self._g, self._s = ls.kv_backend.get, ls.kv_backend.set
        ls.kv_backend.get = lambda k, d=None: self.store.get(k, d)
        ls.kv_backend.set = lambda k, v: self.store.__setitem__(k, v)

    def tearDown(self):
        ls.kv_backend.get, ls.kv_backend.set = self._g, self._s

    def _fill(self, rows, age_h=1):
        self.store[ls.FEDERAL_CACHE_KEY] = {"at": hours_ago(age_h), "rows": rows}

    def test_rows_are_filtered_to_the_states_in_range(self):
        self._fill([{"title": "MO job", "state": "MO"},
                    {"title": "KS job", "state": "KS"},
                    {"title": "CA job", "state": "CA"}])
        stats = {}
        got = ls._federal_cached(["MO", "KS"], stats)
        self.assertEqual([b["title"] for b in got], ["MO job", "KS job"])
        self.assertEqual(stats["federal_from_cache"], 2)

    def test_state_matching_ignores_case(self):
        self._fill([{"title": "x", "state": "mo"}])
        self.assertEqual(len(ls._federal_cached(["MO"], {})), 1)

    def test_an_empty_but_fresh_cache_is_an_answer_not_a_miss(self):
        """A genuinely quiet radius must not send the scan to the live path."""
        self._fill([])
        got = ls._federal_cached(["MO"], {})
        self.assertEqual(got, [], "returned None, so the scan would call SAM")

    def test_no_cache_at_all_returns_none_so_the_caller_falls_back(self):
        stats = {}
        self.assertIsNone(ls._federal_cached(["MO"], stats))
        self.assertEqual(stats.get("federal_cache_missing"), 1)

    def test_a_stale_cache_is_refused(self):
        """A solicitation that closed last week must not be shown as open."""
        self._fill([{"title": "old", "state": "MO"}],
                   age_h=ls.FEDERAL_CACHE_MAX_AGE_H + 1)
        stats = {}
        self.assertIsNone(ls._federal_cached(["MO"], stats))
        self.assertEqual(stats.get("federal_cache_stale"), 1)

    def test_a_cache_just_inside_the_window_is_still_used(self):
        self._fill([{"title": "ok", "state": "MO"}],
                   age_h=ls.FEDERAL_CACHE_MAX_AGE_H - 1)
        self.assertEqual(len(ls._federal_cached(["MO"], {})), 1)

    def test_a_corrupt_timestamp_does_not_raise(self):
        self.store[ls.FEDERAL_CACHE_KEY] = {"at": "not-a-date", "rows": []}
        self.assertIsNone(ls._federal_cached(["MO"], {}))


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self.calls = []
        self._g, self._s = ls.kv_backend.get, ls.kv_backend.set
        self._fetch = ls._sam_fetch
        self._key = ls.SAM_API_KEY
        ls.SAM_API_KEY = "test-key"
        ls.kv_backend.get = lambda k, d=None: self.store.get(k, d)
        ls.kv_backend.set = lambda k, v: self.store.__setitem__(k, v)

    def tearDown(self):
        ls.kv_backend.get, ls.kv_backend.set = self._g, self._s
        ls._sam_fetch = self._fetch
        ls.SAM_API_KEY = self._key

    def _opp(self, title="Concrete sidewalk repair", nid="n1"):
        return {"title": title, "noticeId": nid, "solicitationNumber": nid,
                "naicsCode": "237310", "type": "Solicitation",
                "placeOfPerformance": {"city": {"name": "Aurora"},
                                       "state": {"code": "MO"}},
                "responseDeadLine": "2099-01-01T00:00:00-06:00"}

    def test_it_queries_nationally_once_per_trade_code(self):
        """Six requests for the whole country, not six per state."""
        def fake(state, ncode=None, ccode=None, timeout=None, limit=None):
            self.calls.append((state, ncode, ccode, timeout))
            return []
        ls._sam_fetch = fake
        ls._federal_refresh()
        self.assertEqual(len(self.calls),
                         len(ls.federal_bids.CONCRETE_NAICS)
                         + len(ls.federal_bids.CONCRETE_PSC))
        for state, _, _, _ in self.calls:
            self.assertIsNone(state, "a state filter costs six requests each")

    def test_it_asks_for_far_more_patience_than_a_scan_would(self):
        """The whole point: slowness is free here."""
        def fake(state, ncode=None, ccode=None, timeout=None, limit=None):
            self.calls.append(timeout)
            return []
        ls._sam_fetch = fake
        ls._federal_refresh()
        self.assertTrue(all(t and t > ls.SAM_TIMEOUT for t in self.calls),
                        "refresh is using the scan's timeout")

    def test_a_successful_run_stores_rows(self):
        ls._sam_fetch = lambda *a, **k: [self._opp()]
        out = ls._federal_refresh()
        self.assertTrue(out["ok"])
        self.assertGreaterEqual(out["rows"], 1)
        self.assertIn(ls.FEDERAL_CACHE_KEY, self.store)

    def test_amendments_of_one_solicitation_are_collapsed(self):
        ls._sam_fetch = lambda *a, **k: [self._opp(nid="same"),
                                         self._opp(nid="same")]
        out = ls._federal_refresh()
        self.assertEqual(out["rows"], 1)

    def test_a_totally_failed_run_leaves_the_previous_cache_alone(self):
        """Yesterday's bids beat none because the refresh had a bad night."""
        self.store[ls.FEDERAL_CACHE_KEY] = {"at": hours_ago(2),
                                            "rows": [{"title": "kept",
                                                      "state": "MO"}]}
        ls._sam_fetch = lambda *a, **k: None
        out = ls._federal_refresh()
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "all_queries_failed")
        self.assertEqual(self.store[ls.FEDERAL_CACHE_KEY]["rows"][0]["title"],
                         "kept")

    def test_a_partial_failure_still_stores_what_it_got(self):
        seq = [None, [self._opp()], None, [], None, []]
        ls._sam_fetch = lambda *a, **k: seq.pop(0)
        out = ls._federal_refresh()
        self.assertTrue(out["ok"])
        self.assertEqual(out["failed"], 3)

    def test_no_key_is_reported_rather_than_stored_as_empty(self):
        ls.SAM_API_KEY = ""
        out = ls._federal_refresh()
        self.assertFalse(out["ok"])
        self.assertNotIn(ls.FEDERAL_CACHE_KEY, self.store)


class RefreshEndpointTests(unittest.TestCase):
    def setUp(self):
        self._cron = ls.CRON_SECRET
        self._refresh = ls._federal_refresh
        ls.CRON_SECRET = "cron-secret"
        self.ran = []
        ls._federal_refresh = lambda: (self.ran.append(1) or
                                       {"ok": True, "rows": 3})
        self.app = ls.app.test_client()

    def tearDown(self):
        ls.CRON_SECRET = self._cron
        ls._federal_refresh = self._refresh

    def test_the_right_secret_runs_it(self):
        r = self.app.post("/run-federal-refresh", json={},
                          headers={"X-Cron-Secret": "cron-secret"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.ran), 1)

    def test_a_wrong_secret_does_not(self):
        r = self.app.post("/run-federal-refresh", json={},
                          headers={"X-Cron-Secret": "nope"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.ran, [])

    def test_an_unset_secret_does_not_open_the_door(self):
        ls.CRON_SECRET = ""
        r = self.app.post("/run-federal-refresh", json={},
                          headers={"X-Cron-Secret": ""})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.ran, [])

    def test_a_failed_refresh_is_not_reported_as_success(self):
        ls._federal_refresh = lambda: {"ok": False, "reason": "no_key"}
        r = self.app.post("/run-federal-refresh", json={},
                          headers={"X-Cron-Secret": "cron-secret"})
        self.assertEqual(r.status_code, 503)


class ScheduledJobExistsTests(unittest.TestCase):
    """A cache nothing refills goes stale, and scans fall back to the path
    that does not work. The schedule is part of the fix, not packaging."""

    def test_there_is_a_workflow_that_calls_it(self):
        wf = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          os.pardir, ".github", "workflows",
                          "federal-refresh.yml")
        self.assertTrue(os.path.exists(wf))
        with open(wf, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("/run-federal-refresh", text)
        self.assertIn("schedule:", text)

    def test_it_runs_more_than_once_a_day(self):
        """One run a day means one bad night leaves the cache too old to use."""
        wf = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          os.pardir, ".github", "workflows",
                          "federal-refresh.yml")
        with open(wf, encoding="utf-8") as f:
            text = f.read()
        import re
        m = re.search(r'cron:\s*"([^"]+)"', text)
        self.assertIsNotNone(m)
        hours = m.group(1).split()[1]
        self.assertIn(",", hours, "a single daily run is one bad night from "
                                  "a stale cache")


if __name__ == "__main__":
    unittest.main()
