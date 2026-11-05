"""Security hardening: things that fail safe when a setting goes missing.

1. LICENSE_SECRET's fallback is printed in this public repo. On Render a
   missing variable must not mean keys anyone can forge.
2. A Stripe webhook signed long ago is refused, so a captured one can't be
   replayed.
3. The site sends browser security headers (curbcall_netlify_v4/_headers).
4. supabase_security_fixes.sql covers what Supabase's Security Advisor flags.
"""
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import license_server as ls  # noqa: E402

PLACEHOLDER = "CHANGE_THIS_LONG_RANDOM_SECRET"


def _forged_key():
    date = time.strftime("%Y%m%d", time.localtime(time.time() + 30 * 86400))
    sig = hmac.new(PLACEHOLDER.encode(), f"monthly|{date}".encode(), hashlib.sha256).hexdigest()[:16].upper()
    return f"BCP-MON-{date}-{sig}"


class LicenseSecretTests(unittest.TestCase):
    def _run(self, env):
        code = ("import license_server as ls, json, sys; key = sys.argv[1]; "
                "print(json.dumps([ls.LICENSE_SECRET_MISSING, ls.verify_key(key)[0]]))")
        full = {k: v for k, v in os.environ.items() if k not in ("LICENSE_SECRET", "RENDER")}
        full.update(env)
        out = subprocess.run([sys.executable, "-c", code, _forged_key()], cwd=ROOT, env=full,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_on_render_a_missing_secret_does_not_accept_forged_keys(self):
        missing, forged_ok = self._run({"RENDER": "true"})
        self.assertTrue(missing)
        self.assertFalse(forged_ok, "a key signed with the public placeholder must not validate on Render")

    def test_a_real_secret_is_used_as_given(self):
        missing, forged_ok = self._run({"RENDER": "true", "LICENSE_SECRET": "a-real-private-value"})
        self.assertFalse(missing)
        self.assertFalse(forged_ok)

    def test_off_render_the_placeholder_is_not_a_problem(self):
        """Tests and local runs sign with it on purpose."""
        missing, forged_ok = self._run({})
        self.assertFalse(missing)
        self.assertTrue(forged_ok)

    def test_health_reports_a_missing_secret(self):
        with patch.object(ls, "LICENSE_SECRET_MISSING", True):
            body = ls.app.test_client().get("/health").get_json()
        self.assertTrue(any("LICENSE_SECRET" in p for p in body["problems"]))
        self.assertEqual(body["status"], "degraded")


class StripeReplayTests(unittest.TestCase):
    def _sig(self, payload, t):
        v1 = hmac.new(b"whsec_x", f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
        return f"t={t},v1={v1}"

    def test_a_fresh_signature_is_accepted_and_an_old_one_is_not(self):
        payload = b'{"type":"ping"}'
        with patch.object(ls, "STRIPE_WEBHOOK_SECRET", "whsec_x"):
            self.assertTrue(ls._stripe_verify(payload, self._sig(payload, int(time.time()))))
            self.assertFalse(ls._stripe_verify(payload, self._sig(payload, int(time.time()) - 3600)))
            self.assertFalse(ls._stripe_verify(payload, self._sig(payload, int(time.time()) + 3600)))
            self.assertFalse(ls._stripe_verify(payload, "t=abc,v1=00"))


class SiteHeadersTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "curbcall_netlify_v4", "_headers"), encoding="utf-8") as f:
            self.text = f.read()

    def test_every_page_gets_the_headers(self):
        lines = [l for l in self.text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        self.assertEqual(lines[0], "/*")
        for h in ("Strict-Transport-Security: max-age=", "X-Content-Type-Options: nosniff",
                  "X-Frame-Options: DENY", "frame-ancestors 'none'", "Referrer-Policy:"):
            self.assertIn(h, self.text)

    def test_location_is_still_allowed(self):
        """'Use My Location' needs geolocation; blocking it would break search."""
        self.assertNotIn("geolocation=()", self.text)


class SupabaseFixesTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "supabase_security_fixes.sql"), encoding="utf-8") as f:
            self.sql = f.read().lower()

    def test_each_advisor_warning_is_handled(self):
        self.assertIn("alter function public.kv_store_touch() set search_path", self.sql)
        self.assertIn('drop policy if exists "public read access for avatars"', self.sql)
        for role in ("public", "anon", "authenticated"):
            self.assertIn(f"revoke execute on function public.reviews_reset_approval() from {role}", self.sql)

    def test_the_schema_files_match_so_a_new_project_starts_fixed(self):
        with open(os.path.join(ROOT, "supabase_sync_schema.sql"), encoding="utf-8") as f:
            sync = f.read().lower()
        with open(os.path.join(ROOT, "supabase_kv_schema.sql"), encoding="utf-8") as f:
            kv = f.read().lower()
        self.assertNotIn('create policy "public read access for avatars"', sync)
        self.assertIn("revoke execute on function public.reviews_reset_approval()", sync)
        self.assertIn("set search_path = ''", kv)


if __name__ == "__main__":
    unittest.main()
