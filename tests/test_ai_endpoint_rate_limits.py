"""/scan (force=true) and /draft-proposal both already require an active
licence, unlike the public forms /support and /claim -- but neither had any
cap past that, unlike everything else that spends real money or quota here.
A forced re-scan burns live search-API quota re-running the whole pipeline
instead of reading the same-day cache; every proposal draft is a paid OpenAI
call with nothing cached about it at all. A scripted client, a buggy retry
loop, or one shared/leaked licence key hammering either in a loop had
nothing to stop it.

Keyed by the licence key (see FORCE_SCAN_MAX_PER_KEY_PER_DAY's own comment
in community_submissions.py), not the IP -- these are authenticated
endpoints, so the credential being spent is the thing worth capping.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kv_backend
import license_server as ls


class ForceScanRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()
        self.store = {}
        self._p = [
            patch.object(ls, "_license_is_active", return_value=True),
            patch.object(ls, "_perform_scan",
                         return_value={"location": "Mentor, OH", "bids": [],
                                      "total_bids": 0, "city_coords": {},
                                      "center": {}}),
            patch.object(ls, "_verify_supabase_token", return_value=""),
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

    def _post(self, force=False, key="BCP-TEST-KEY"):
        return self.client.post("/scan", json={
            "key": key, "device_id": "dev1", "location": "Mentor, OH",
            "radius": 25, "force": force})

    def test_an_ordinary_scan_is_never_rate_limited(self):
        """Only force=true spends extra quota -- a plain scan reads the
        cache and must never be capped by this."""
        for _ in range(ls.FORCE_SCAN_MAX_PER_KEY_PER_DAY + 5):
            r = self._post(force=False)
            self.assertEqual(r.status_code, 200)

    def test_forced_rescans_are_capped_per_key(self):
        for _ in range(ls.FORCE_SCAN_MAX_PER_KEY_PER_DAY):
            r = self._post(force=True)
            self.assertEqual(r.status_code, 200)
        r = self._post(force=True)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.get_json()["reason"], "rate_limited")

    def test_the_cap_is_per_key_not_global(self):
        """A different customer's key must not be blocked by someone else's
        forced re-scans."""
        for _ in range(ls.FORCE_SCAN_MAX_PER_KEY_PER_DAY):
            self._post(force=True, key="BCP-KEY-A")
        r = self._post(force=True, key="BCP-KEY-B")
        self.assertEqual(r.status_code, 200)

    def test_an_unlicensed_request_is_still_refused_before_any_of_this(self):
        with patch.object(ls, "_license_is_active", return_value=False):
            r = self._post(force=True)
        self.assertEqual(r.status_code, 403)


class DraftProposalRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()
        self.store = {}
        self._p = [
            patch.object(ls, "_license_is_active", return_value=True),
            patch.object(ls, "OPENAI_API_KEY", "test-key"),
            patch.object(ls, "_ai_draft_proposal", return_value="Dear buyer..."),
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

    def _post(self, key="BCP-TEST-KEY"):
        return self.client.post("/draft-proposal", json={
            "key": key, "device_id": "dev1",
            "bid": {"title": "Sidewalk replacement"}, "company": {}})

    def test_an_ordinary_draft_goes_through(self):
        self.assertTrue(self._post().get_json()["ok"])

    def test_one_key_cannot_run_up_unlimited_openai_calls(self):
        for _ in range(ls.DRAFT_PROPOSAL_MAX_PER_KEY_PER_DAY):
            r = self._post()
            self.assertEqual(r.status_code, 200)
        r = self._post()
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.get_json()["reason"], "rate_limited")

    def test_the_cap_is_per_key_not_global(self):
        for _ in range(ls.DRAFT_PROPOSAL_MAX_PER_KEY_PER_DAY):
            self._post(key="BCP-KEY-A")
        r = self._post(key="BCP-KEY-B")
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
