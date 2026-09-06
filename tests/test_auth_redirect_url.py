"""Emailed auth links have to come back to a URL Supabase can append to.

A real password-reset link arrived as

    .../verify?token=...&type=recovery&redirect_to=https%3A%2F%2F...%2Fapp.html%23

-- note the trailing %23, an empty fragment. Supabase appends its own
"#access_token=..." to whatever redirect_to it is given, so the browser landed
on app.html##access_token=... The doubled hash makes the first key parse as
"#access_token" instead of "access_token", so supabase-js found no token,
created no session and never fired PASSWORD_RECOVERY. The app fell through to
the auth screen, which is written in its signup state, and the user was
staring at a create-account form after clicking a reset link.

Nothing errored anywhere. That is what makes it worth a test.

The cause was passing window.location.href as the return address. It carries
whatever fragment is in the address bar -- and supabase-js itself leaves a
bare "#" behind after it processes an auth callback, so simply confirming an
email and then asking for a reset was enough to trigger it.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, os.pardir, "curbcall_netlify_v4", "app.html")


def source():
    with open(APP, encoding="utf-8") as f:
        html = f.read()
    # Comments explain this exact bug and name the thing being banned, so a
    # naive scan matches the prose instead of the code. Strip them first.
    body = "\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                                html, re.S))
    return re.sub(r"^\s*//.*$", "", body, flags=re.M)


class RedirectTargetTests(unittest.TestCase):
    def setUp(self):
        self.js = source()

    def test_no_emailed_link_returns_to_the_live_url(self):
        """window.location.href brings its fragment along. Nothing may use it."""
        offenders = [m.group(0) for m in re.finditer(
            r"(?:emailRedirectTo|redirectTo)\s*:\s*([^,}\n]+)", self.js)
            if "location.href" in m.group(1)]
        self.assertEqual(offenders, [],
                         "redirect target carries the current fragment: "
                         + "; ".join(offenders))

    def test_every_redirect_goes_through_the_one_helper(self):
        targets = re.findall(r"(?:emailRedirectTo|redirectTo)\s*:\s*([^,}\n]+)",
                             self.js)
        self.assertGreaterEqual(len(targets), 3, "expected reset, magic link "
                                "and OAuth to all set a return address")
        for t in targets:
            self.assertIn("authReturnUrl()", t)

    def test_the_helper_keeps_only_origin_and_path(self):
        m = re.search(r"function authReturnUrl\(\)\{(.*?)\}", self.js, re.S)
        self.assertIsNotNone(m, "authReturnUrl() is gone")
        body = m.group(1)
        self.assertIn("origin", body)
        self.assertIn("pathname", body)
        for banned in ("location.href", "location.hash", "location.search"):
            self.assertNotIn(banned, body)


class DoubledHashTests(unittest.TestCase):
    """Why the trailing "#" was fatal rather than merely untidy."""

    def setUp(self):
        self.node = shutil.which("node")
        if not self.node:
            raise unittest.SkipTest("node not available")

    def _parse(self, url):
        js = ("const h=new URL(%s).hash;"
              "const p=new URLSearchParams(h.replace(/^#/,''));"
              "console.log(JSON.stringify(p.get('access_token')));"
              % json.dumps(url))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(js)
            out = subprocess.run([self.node, path], capture_output=True,
                                 text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr[-800:])
        return json.loads(out.stdout.strip())

    def test_a_clean_return_url_yields_a_token(self):
        self.assertEqual(
            self._parse("https://curbcallpro.com/app.html"
                        "#access_token=abc&type=recovery"), "abc")

    def test_a_trailing_hash_loses_the_token_silently(self):
        self.assertIsNone(
            self._parse("https://curbcallpro.com/app.html"
                        "##access_token=abc&type=recovery"),
            "a doubled hash must be understood to break token parsing -- "
            "if this ever passes, the reason for authReturnUrl() has changed")


if __name__ == "__main__":
    unittest.main()
