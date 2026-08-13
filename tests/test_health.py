"""Tests for /health.

The point of the endpoint is to make silent degradation visible: when a search
backend is unset or DuckDuckGo starts getting blocked, /scan still returns 200
with a thinner set of bids and nothing says why. These check it actually says
so, and that it never echoes a secret's value.
"""
import io
import json
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


class EmailVisibilityTests(unittest.TestCase):
    """An API key being present (backends.resend_email) says nothing about
    whether Resend will actually accept a send -- their sandbox from-address
    (onboarding@resend.dev, the default FROM_EMAIL) can only deliver to the
    account's own verified email. That's what let /support 500 silently in
    production while /health reported everything as configured. Same shape
    as TavilyVisibilityTests above, applied to _email_state/_email_health."""

    def setUp(self):
        self.client = ls.app.test_client()
        ls._email_state.update({"ok": 0, "failed": 0, "last_error": "", "last_status": 0})

    def tearDown(self):
        ls._email_state.update({"ok": 0, "failed": 0, "last_error": "", "last_status": 0})

    def _health(self):
        with patch.multiple(ls, **FULLY_CONFIGURED), \
             patch.multiple(kv_backend,
                            UPSTASH_URL="https://example.upstash.io",
                            UPSTASH_TOKEN=SECRET):
            return self.client.get("/health").get_json()

    def test_every_send_failing_is_reported_as_a_problem(self):
        ls._email_state.update({"ok": 0, "failed": 3, "last_status": 403,
                                "last_error": "domain not verified"})
        body = self._health()
        self.assertEqual(body["status"], "degraded")
        self.assertTrue(body["email"]["failing"])
        self.assertTrue(any("Every email send this run has failed" in p
                            for p in body["problems"]))
        self.assertTrue(any("domain not verified" in p for p in body["problems"]))

    def test_healthy_email_raises_nothing(self):
        ls._email_state.update({"ok": 5, "failed": 0, "last_status": 0})
        body = self._health()
        self.assertEqual(body["problems"], [])
        self.assertFalse(body["email"]["failing"])

    def test_some_failures_alongside_successes_is_not_an_outage(self):
        ls._email_state.update({"ok": 8, "failed": 1, "last_status": 500})
        self.assertFalse(self._health()["email"]["failing"])

    def test_no_sends_attempted_yet_is_not_an_outage(self):
        # Distinguishes "never tried" from "tried and failed" -- a server
        # that just booted must not immediately report email as broken.
        body = self._health()
        self.assertFalse(body["email"]["failing"])
        self.assertEqual(body["problems"], [])


class SendEmailTests(unittest.TestCase):
    """_send_email is the one place that actually calls Resend -- used by
    /support, license-key delivery, referral notices and admin alerts, so a
    bug here silently breaks all four at once (which is exactly what the
    live production 500 on /support turned out to be)."""

    def setUp(self):
        ls._email_state.update({"ok": 0, "failed": 0, "last_error": "", "last_status": 0})

    def tearDown(self):
        ls._email_state.update({"ok": 0, "failed": 0, "last_error": "", "last_status": 0})

    def test_no_api_key_fails_without_a_network_call(self):
        with patch.object(ls, "RESEND_API_KEY", ""), \
             patch.object(ls.urllib.request, "urlopen") as urlopen:
            ok = ls._send_email("a@example.com", "subj", "body")
        self.assertFalse(ok)
        urlopen.assert_not_called()

    def test_success_is_tracked(self):
        with patch.object(ls, "RESEND_API_KEY", SECRET), \
             patch.object(ls.urllib.request, "urlopen"):
            ok = ls._send_email("a@example.com", "subj", "body")
        self.assertTrue(ok)
        self.assertEqual(ls._email_state["ok"], 1)

    def test_an_http_error_from_resend_is_tracked_with_its_status_and_body(self):
        import urllib.error
        err = urllib.error.HTTPError(
            "https://api.resend.com/emails", 403, "Forbidden", {},
            io.BytesIO(b'{"message":"domain is not verified"}'))
        with patch.object(ls, "RESEND_API_KEY", SECRET), \
             patch.object(ls.urllib.request, "urlopen", side_effect=err):
            ok = ls._send_email("a@example.com", "subj", "body")
        self.assertFalse(ok)
        self.assertEqual(ls._email_state["last_status"], 403)
        self.assertIn("domain is not verified", ls._email_state["last_error"])

    def test_a_real_user_agent_is_sent(self):
        """Resend's API is behind Cloudflare, which answered every send with
        403 'error code: 1010' -- its ban-by-browser-signature response -- to
        urllib's default Python-urllib/3.x agent. Verified against the live
        API: default agent gets 1010, a real one gets through to normal auth.
        That single missing header was the whole production outage."""
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.headers)

        with patch.object(ls, "RESEND_API_KEY", SECRET), \
             patch.object(ls.urllib.request, "urlopen", side_effect=fake_urlopen):
            ls._send_email("a@example.com", "subj", "body")

        # urllib title-cases header names on the Request object.
        ua = captured["headers"].get("User-agent", "")
        self.assertTrue(ua, "no User-Agent sent — Cloudflare will answer 1010")
        self.assertNotIn("urllib", ua.lower())

    def test_reply_to_is_only_set_when_given(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)

        with patch.object(ls, "RESEND_API_KEY", SECRET), \
             patch.object(ls.urllib.request, "urlopen", side_effect=fake_urlopen):
            ls._send_email("a@example.com", "subj", "body")
        self.assertNotIn("reply_to", captured["body"])

        with patch.object(ls, "RESEND_API_KEY", SECRET), \
             patch.object(ls.urllib.request, "urlopen", side_effect=fake_urlopen):
            ls._send_email("a@example.com", "subj", "body", reply_to="buyer@example.com")
        self.assertEqual(captured["body"]["reply_to"], "buyer@example.com")


class SupportRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()

    def test_empty_message_is_rejected_without_calling_resend(self):
        with patch.object(ls, "_send_email") as send:
            r = self.client.post("/support", json={"email": "a@example.com", "message": "  "})
        self.assertEqual(r.get_json(), {"ok": False, "reason": "no_message"})
        send.assert_not_called()

    def test_unconfigured_resend_reports_email_unavailable(self):
        with patch.object(ls, "RESEND_API_KEY", ""):
            r = self.client.post("/support", json={"message": "help"})
        self.assertEqual(r.get_json(), {"ok": False, "reason": "email_unavailable"})

    def test_a_successful_send_reports_ok(self):
        with patch.object(ls, "RESEND_API_KEY", SECRET), \
             patch.object(ls, "_send_email", return_value=True) as send:
            r = self.client.post("/support",
                                 json={"email": "buyer@example.com", "message": "help me"})
        self.assertEqual(r.get_json(), {"ok": True})
        send.assert_called_once()
        args, kwargs = send.call_args
        self.assertEqual(kwargs.get("reply_to"), "buyer@example.com")
        self.assertIn("help me", args[2])

    def test_a_failed_send_reports_send_failed_with_a_500(self):
        with patch.object(ls, "RESEND_API_KEY", SECRET), \
             patch.object(ls, "_send_email", return_value=False):
            r = self.client.post("/support", json={"message": "help"})
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.get_json(), {"ok": False, "reason": "send_failed"})


class CoverageEndpointTests(unittest.TestCase):
    """Public, unauthenticated on purpose: it's what lets someone check
    their own area before paying. Coverage really is uneven -- ~149 verified
    agencies within 50mi of Boston against ~9 around Springfield, MO -- and
    a contractor who finds that out after subscribing is a refund and a bad
    review."""

    SPRINGFIELD = {"lat": 37.2090, "lon": -93.2923, "city": "Springfield", "state": "MO"}

    def setUp(self):
        self.client = ls.app.test_client()

    def _get(self, body, center=None):
        with patch.object(ls, "_resolve_center",
                          return_value=self.SPRINGFIELD if center is None else center):
            return self.client.post("/coverage", json=body)

    def test_it_needs_no_licence_or_login(self):
        # The whole point is that a prospect can run it before paying.
        r = self._get({"location": "65806", "radius": 50})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_a_wider_radius_never_reports_fewer_agencies(self):
        counts = [self._get({"location": "65806", "radius": r}).get_json()["agencies"]
                  for r in (25, 50, 125)]
        self.assertEqual(counts, sorted(counts), counts)

    def test_a_missing_location_is_a_400_not_a_crash(self):
        self.assertEqual(self.client.post("/coverage", json={}).status_code, 400)

    def test_an_unresolvable_location_says_so(self):
        with patch.object(ls, "_resolve_center", return_value=None):
            r = self.client.post("/coverage", json={"location": "zzzz"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["reason"], "unresolved_location")

    def test_the_radius_is_clamped(self):
        # Nobody gets to ask for a 10,000-mile radius and walk the whole file.
        r = self._get({"location": "65806", "radius": 99999}).get_json()
        self.assertLessEqual(r["radius"], 250)
        r2 = self._get({"location": "65806", "radius": -5}).get_json()
        self.assertGreaterEqual(r2["radius"], 5)

    def test_a_junk_radius_falls_back_instead_of_erroring(self):
        r = self._get({"location": "65806", "radius": "abc"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["radius"], 50)

    def test_a_broken_lookup_is_a_500_not_an_exception(self):
        with patch.object(ls.bid_portals, "towns_within_radius",
                          side_effect=RuntimeError("coords file corrupt")):
            r = self._get({"location": "65806", "radius": 50})
        self.assertEqual(r.status_code, 500)
        self.assertFalse(r.get_json()["ok"])

    def test_it_costs_no_search_credits_or_ai_calls(self):
        # A public endpoint that spent money per request would be a way to
        # run up the bill from outside.
        with patch.object(ls, "_tavily_search", side_effect=AssertionError("searched!")), \
             patch.object(ls, "_ddg_search", side_effect=AssertionError("searched!")), \
             patch.object(ls, "_ai_extract", side_effect=AssertionError("AI call!")):
            r = self._get({"location": "65806", "radius": 125})
        self.assertEqual(r.status_code, 200)


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
