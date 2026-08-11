"""Tests for /health.

The point of the endpoint is to make silent degradation visible: when a search
backend is unset or DuckDuckGo starts getting blocked, /scan still returns 200
with a thinner set of bids and nothing says why. These check it actually says
so, and that it never echoes a secret's value.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls
import kv_backend

SECRET = "sk-do-not-leak-this-value"

FULLY_CONFIGURED = {
    "OPENAI_API_KEY": SECRET,
    "TAVILY_API_KEY": SECRET,
    "SAM_API_KEY": SECRET,
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_ANON_KEY": SECRET,
    "UPSTASH_URL": "https://example.upstash.io",
    "UPSTASH_TOKEN": SECRET,
    "RESEND_API_KEY": SECRET,
    "SUPABASE_SERVICE_ROLE_KEY": SECRET,
    "CRON_SECRET": SECRET,
}


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()

    def _get(self, **overrides):
        env = dict(FULLY_CONFIGURED)
        env.update(overrides)
        # Storage moved into kv_backend, so durability is read from there —
        # patching only license_server would leave /health reporting the real
        # (undurable) sandbox state and mask what these tests are checking.
        with patch.multiple(ls, **env), \
             patch.multiple(kv_backend,
                            UPSTASH_URL="https://example.upstash.io",
                            UPSTASH_TOKEN=SECRET):
            return self.client.get("/health").get_json()

    def test_plain_root_probe_is_unchanged(self):
        # Uptime monitors point at "/" — its contract must not shift.
        self.assertEqual(self.client.get("/").get_json(),
                         {"service": "Bid Caller Pro License Server", "status": "ok"})

    def test_fully_configured_reports_ok_with_no_problems(self):
        body = self._get()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["problems"], [])
        self.assertTrue(all(body["backends"].values()))

    def test_never_echoes_a_secret_value(self):
        import json
        with patch.object(ls, "_ddg_fail_streak", 0):
            raw = json.dumps(self._get())
        self.assertNotIn(SECRET, raw)

    def test_missing_openai_is_called_out(self):
        body = self._get(OPENAI_API_KEY="")
        self.assertEqual(body["status"], "degraded")
        self.assertFalse(body["backends"]["openai"])
        self.assertTrue(any("OPENAI_API_KEY" in p for p in body["problems"]))

    def test_missing_tavily_is_flagged_as_a_single_point_of_failure(self):
        body = self._get(TAVILY_API_KEY="")
        self.assertTrue(body["local_search"]["is_sole_local_search"])
        self.assertTrue(any("solely on scraping DuckDuckGo" in p for p in body["problems"]))

    def test_blocked_duckduckgo_with_no_tavily_reads_as_search_down(self):
        with patch.object(ls, "_ddg_fail_streak", ls.DDG_TRIP_THRESHOLD):
            body = self._get(TAVILY_API_KEY="")
        self.assertTrue(body["local_search"]["degraded"])
        self.assertTrue(any("effectively down" in p for p in body["problems"]))

    def test_blocked_duckduckgo_is_tolerable_when_tavily_is_configured(self):
        with patch.object(ls, "_ddg_fail_streak", ls.DDG_TRIP_THRESHOLD):
            body = self._get()
        self.assertTrue(body["local_search"]["degraded"])
        self.assertFalse(body["local_search"]["is_sole_local_search"])
        # Tavily is carrying local search, so this is not a search outage.
        self.assertEqual(body["problems"], [])
        self.assertEqual(body["status"], "ok")


if __name__ == "__main__":
    unittest.main()


class TavilyVisibilityTests(unittest.TestCase):
    """A spent Tavily allowance is the most damaging silent failure here:
    /scan keeps returning 200 and just comes back nearly empty, which reads
    as "the area has no bids"."""

    def setUp(self):
        self.client = ls.app.test_client()
        ls._tavily_state.update({"ok": 0, "failed": 0, "last_error": "", "last_status": 0})

    def tearDown(self):
        ls._tavily_state.update({"ok": 0, "failed": 0, "last_error": "", "last_status": 0})

    def _health(self):
        with patch.multiple(ls, **FULLY_CONFIGURED), \
             patch.multiple(kv_backend,
                            UPSTASH_URL="https://example.upstash.io",
                            UPSTASH_TOKEN=SECRET):
            return self.client.get("/health").get_json()

    def test_basic_depth_is_the_default(self):
        # advanced costs double per search and buys nothing here — the results
        # are used as a URL list and the pages are fetched separately.
        self.assertEqual(ls.TAVILY_DEPTH, "basic")

    def test_a_spent_allowance_is_reported_as_a_problem(self):
        for status in (402, 429, 432):
            with self.subTest(status=status):
                ls._tavily_state.update({"ok": 0, "failed": 3, "last_status": status})
                body = self._health()
                self.assertEqual(body["status"], "degraded")
                self.assertTrue(body["tavily"]["quota_or_auth_failure"])
                self.assertTrue(any("Tavily is rejecting searches" in p
                                    for p in body["problems"]))

    def test_a_bad_key_is_reported(self):
        ls._tavily_state.update({"ok": 0, "failed": 1, "last_status": 401})
        self.assertTrue(self._health()["tavily"]["quota_or_auth_failure"])

    def test_healthy_tavily_raises_nothing(self):
        ls._tavily_state.update({"ok": 12, "failed": 0, "last_status": 0})
        body = self._health()
        self.assertEqual(body["problems"], [])
        self.assertFalse(body["tavily"]["failing"])

    def test_some_failures_alongside_successes_is_not_an_outage(self):
        ls._tavily_state.update({"ok": 10, "failed": 2, "last_status": 500})
        self.assertFalse(self._health()["tavily"]["failing"])


class ForceRescanTests(unittest.TestCase):
    """The scan cache is per area per day, so one bad scan used to decide what
    the user saw until midnight."""

    def setUp(self):
        self.client = ls.app.test_client()
        self._orig_gate = ls._license_is_active
        ls._license_is_active = lambda *a, **k: True

    def tearDown(self):
        ls._license_is_active = self._orig_gate

    def test_force_bypasses_the_same_day_cache(self):
        calls = []
        center = {"lat": 37.2, "lon": -93.3, "city": "Springfield", "state": "MO"}
        cache = {}

        def fake_geo(city, state):
            return None

        with patch.object(ls, "_resolve_center", return_value=center), \
             patch.object(ls, "_cache", side_effect=lambda: cache), \
             patch.object(ls, "_save_cache"), \
             patch.object(ls, "OPENAI_API_KEY", ""), \
             patch.object(ls, "SAM_API_KEY", ""), \
             patch.object(ls, "bid_portals") as bp:
            bp.load_directory.return_value = {}
            for force in (False, False, True):
                body = self.client.post("/scan", json={
                    "location": "Springfield, MO", "radius": 50, "force": force}).get_json()
                calls.append(bool(body.get("cached")))
        # first populates, second is served from cache, third forces a re-run
        self.assertEqual(calls, [False, True, False])


class ScanHistoryTests(unittest.TestCase):
    """Only the single most recent scan was ever stored, overwritten each time,
    so there was nothing to compare against and "is search getting better?"
    could not be answered from the data at all."""

    def setUp(self):
        self.client = ls.app.test_client()
        self.store = {}
        self._p = [
            patch.object(kv_backend, "get",
                         side_effect=lambda k, d=None: self.store.get(k, d)),
            patch.object(kv_backend, "set",
                         side_effect=lambda k, v: self.store.__setitem__(k, v)),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def _record(self, kept, at="2026-08-11T12:00:00"):
        return {"at": at, "location": "Springfield, MO", "radius": 25,
                "kept": kept, "raw_local": kept * 3, "anchor_towns": 0,
                "funnel": {"kept": kept}, "sample": [{"title": "x" * 200}],
                "statuses": {"Open": kept}}

    def test_scans_accumulate_instead_of_overwriting(self):
        for n in (1, 4, 9):
            ls._append_scan_history(self._record(n))
        self.assertEqual([r["kept"] for r in self.store[ls.SCAN_HISTORY_KEY]],
                         [1, 4, 9])

    def test_history_is_capped(self):
        for n in range(ls.SCAN_HISTORY_MAX + 15):
            ls._append_scan_history(self._record(n))
        kept = self.store[ls.SCAN_HISTORY_KEY]
        self.assertEqual(len(kept), ls.SCAN_HISTORY_MAX)
        # The cap must drop the OLDEST, not the newest.
        self.assertEqual(kept[-1]["kept"], ls.SCAN_HISTORY_MAX + 14)

    def test_the_log_stays_compact(self):
        # Twenty-five full scan records with samples would make /health
        # unreadable, which is the one thing it must not be.
        ls._append_scan_history(self._record(3))
        row = self.store[ls.SCAN_HISTORY_KEY][0]
        self.assertNotIn("sample", row)
        self.assertNotIn("statuses", row)
        self.assertEqual(row["kept"], 3)
        self.assertIn("funnel", row)

    def test_health_reports_newest_first(self):
        for n in (1, 4, 9):
            ls._append_scan_history(self._record(n))
        with patch.multiple(ls, **FULLY_CONFIGURED):
            body = self.client.get("/health").get_json()
        self.assertEqual([r["kept"] for r in body["recent_scans"]], [9, 4, 1])

    def test_corrupt_history_does_not_break_health_or_the_next_scan(self):
        for junk in ("not a list", 42, {"nope": 1}):
            with self.subTest(junk=junk):
                self.store[ls.SCAN_HISTORY_KEY] = junk
                self.assertEqual(ls._recent_scans(), [])
                ls._append_scan_history(self._record(2))
                self.assertEqual(
                    [r["kept"] for r in self.store[ls.SCAN_HISTORY_KEY]], [2])

    def test_health_answers_even_when_storage_is_the_broken_thing(self):
        with patch.object(kv_backend, "get", side_effect=RuntimeError("down")):
            self.assertEqual(ls._recent_scans(), [])
