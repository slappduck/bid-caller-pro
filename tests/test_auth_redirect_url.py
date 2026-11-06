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
APP_JS = os.path.join(HERE, os.pardir, "curbcall_netlify_v4", "app.js")


def source():
    with open(APP_JS, encoding="utf-8") as f:
        body = f.read()
    # Comments explain this exact bug and name the thing being banned, so a
    # naive scan matches the prose instead of the code. Strip them first.
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


class ConfirmLinksWorkInAnyBrowserTests(unittest.TestCase):
    """Someone signed up in a private tab, tapped the confirmation email, and
    the link opened in a normal tab: confirmed, but signed out and looking at
    the create-account form, because a PKCE link only works in the browser
    that asked for it."""

    def setUp(self):
        self.js = source()
        with open(os.path.join(HERE, os.pardir, "curbcall_netlify_v4", "index.html"), encoding="utf-8") as f:
            self.landing = f.read()

    def test_signup_tells_supabase_to_come_back_to_the_app(self):
        m = re.search(r"sb\.auth\.signUp\(\{[^\n]*", self.js)
        self.assertIsNotNone(m)
        self.assertIn("emailRedirectTo:authReturnUrl()", m.group(0))

    def test_token_hash_links_sign_in_whichever_browser_opens_them(self):
        self.assertRegex(self.js, r"\[\s*\"email\"\s*,\s*\"signup\"\s*,\s*\"magiclink\"")
        self.assertIn("sb.auth.verifyOtp({type:EMAIL_LINK.type,token_hash:EMAIL_LINK.hash})", self.js)

    def test_a_link_this_browser_cannot_use_says_sign_in_not_sign_up(self):
        m = re.search(r"function showSignInAfterLink\(ok\)\{(.*?)\n\}", self.js, re.S)
        self.assertIsNotNone(m)
        self.assertIn('setAuthMode("signin")', m.group(1))
        self.assertIn("CAME_WITH_CODE&&!CAME_FROM_RESET_LINK", self.js)

    def test_the_landing_page_passes_sign_in_links_to_the_app(self):
        self.assertIn('location.replace("/app.html" + s + h)', self.landing)
        head = self.landing.split("</head>")[0]
        self.assertIn("token_hash", head, "must run in <head>, before anything else on the page")


if __name__ == "__main__":
    unittest.main()
