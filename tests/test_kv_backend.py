"""Tests for kv_backend — the durable store behind licences, the portal
directory and the geocode cache.

Render's disk is wiped on every deploy and whenever a free instance sleeps, so
"where does this actually get written" is a correctness question, not an
implementation detail. A silent fall back to a local file means licence keys
and trial records evaporate.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kv_backend as kv


class BackendSelectionTests(unittest.TestCase):
    def test_upstash_wins_when_configured(self):
        with patch.multiple(kv, UPSTASH_URL="https://x.upstash.io", UPSTASH_TOKEN="t",
                            SUPABASE_URL="https://x.supabase.co",
                            SUPABASE_SERVICE_ROLE_KEY="k"):
            self.assertEqual(kv.backend_name(), "upstash")
            self.assertTrue(kv.is_durable())

    def test_supabase_is_used_when_upstash_is_absent(self):
        with patch.multiple(kv, UPSTASH_URL="", UPSTASH_TOKEN="",
                            SUPABASE_URL="https://x.supabase.co",
                            SUPABASE_SERVICE_ROLE_KEY="k"):
            self.assertEqual(kv.backend_name(), "supabase")
            self.assertTrue(kv.is_durable())

    def test_a_file_backend_is_reported_as_not_durable(self):
        # This is the state that silently loses licences on Render.
        with patch.multiple(kv, UPSTASH_URL="", UPSTASH_TOKEN="",
                            SUPABASE_URL="", SUPABASE_SERVICE_ROLE_KEY=""):
            self.assertEqual(kv.backend_name(), "file")
            self.assertFalse(kv.is_durable())

    def test_a_supabase_url_without_the_service_key_is_not_enough(self):
        # The anon key cannot write this table by design — RLS has no policies.
        with patch.multiple(kv, UPSTASH_URL="", UPSTASH_TOKEN="",
                            SUPABASE_URL="https://x.supabase.co",
                            SUPABASE_SERVICE_ROLE_KEY=""):
            self.assertFalse(kv.is_durable())


class SupabaseRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.multiple(kv, UPSTASH_URL="", UPSTASH_TOKEN="",
                                  SUPABASE_URL="https://x.supabase.co",
                                  SUPABASE_SERVICE_ROLE_KEY="service-key")
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_a_stored_value_is_returned(self):
        with patch.object(kv, "_supabase_get", return_value=({"a": 1}, True)):
            self.assertEqual(kv.get("bidcaller:license_db"), {"a": 1})

    def test_an_absent_row_yields_the_default_not_an_error(self):
        with patch.object(kv, "_supabase_get", return_value=(None, True)):
            self.assertEqual(kv.get("missing", {"seed": True}), {"seed": True})

    def test_a_successful_write_reports_durable(self):
        with patch.object(kv, "_supabase_set", return_value=True):
            self.assertTrue(kv.set("k", {"v": 1}))

    def test_an_unreachable_backend_still_writes_somewhere_but_says_it_is_not_durable(self):
        # Losing the write entirely would be worse; claiming it was durable
        # would be worse still.
        with patch.object(kv, "_supabase_set", return_value=False):
            self.assertFalse(kv.set("k", {"v": 1}))

    def test_a_read_failure_falls_through_rather_than_raising(self):
        with patch.object(kv, "_supabase_get", return_value=(None, False)):
            self.assertEqual(kv.get("k", "fallback"), "fallback")


class HealthReportTests(unittest.TestCase):
    def test_health_names_the_backend_in_use(self):
        with patch.multiple(kv, UPSTASH_URL="", UPSTASH_TOKEN="",
                            SUPABASE_URL="https://x.supabase.co",
                            SUPABASE_SERVICE_ROLE_KEY="k"):
            h = kv.health()
        self.assertEqual(h["backend"], "supabase")
        self.assertTrue(h["durable"])
        self.assertTrue(h["supabase_configured"])
        self.assertFalse(h["upstash_configured"])


class LocalFallbackTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.multiple(kv, UPSTASH_URL="", UPSTASH_TOKEN="",
                                  SUPABASE_URL="", SUPABASE_SERVICE_ROLE_KEY="")
        self.env.start()
        self.key = "bidcaller:test_roundtrip"

    def tearDown(self):
        self.env.stop()
        try:
            os.unlink(kv._local_path(self.key))
        except OSError:
            pass

    def test_a_value_survives_a_round_trip_through_the_file(self):
        kv.set(self.key, {"hello": "world"})
        self.assertEqual(kv.get(self.key), {"hello": "world"})

    def test_a_key_with_awkward_characters_is_still_a_safe_filename(self):
        path = kv._local_path("bidcaller:portal/directory")
        self.assertNotIn("/", os.path.basename(path))
        self.assertNotIn(":", os.path.basename(path))
