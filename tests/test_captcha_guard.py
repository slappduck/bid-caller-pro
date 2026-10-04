"""Every sign-in call sent its captcha token honestly, and still failed.

Turnstile auto-verifies in the background and the client always passed
captchaToken: turnstileToken() along -- but nothing checked whether that
token had actually arrived before the request went out. When the
challenges.cloudflare.com script was blocked (an ad blocker, a privacy
browser, a restrictive jobsite network -- the same class of "different
browser context" problem that broke the password-reset link), the widget
silently never completed, turnstileToken() stayed empty forever, and
Supabase rejected the request with its own raw text: "captcha protection:
request disallowed (no captcha_token found)". That string reached the
screen verbatim.

There is no JS test runner in this project (see test_frontend_structure.py),
so these assert the wiring is present rather than exercising a real browser.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP_JS = os.path.join(HERE, os.pardir, "curbcall_netlify_v4", "app.js")


def source():
    with open(APP_JS, encoding="utf-8") as f:
        body = f.read()
    # Comments narrate this exact bug and name the thing being asserted, so a
    # naive scan would match the prose instead of the code.
    return re.sub(r"^\s*//.*$", "", body, flags=re.M)


NEXT_TOP_LEVEL = re.compile(r'\n(?:(?:async )?function |document\.getElementById\()')


def fn_body(src, signature):
    """The source of one function or click handler, to the start of the
    next top-level one. Handlers in this file are usually
    document.getElementById("x").onclick=async()=>{...}, not named function
    declarations, so the boundary has to recognize both shapes -- stopping
    only at "function" swallowed every handler after the one being read."""
    start = src.index(signature)
    after = NEXT_TOP_LEVEL.search(src[start + 1:])
    end = start + 1 + after.start() if after else len(src)
    return src[start:end]


class EveryCaptchaCallIsGuardedTests(unittest.TestCase):
    """Each captchaToken: turnstileToken() call is Supabase's half of the
    contract; captchaBlocking() is the client's half, and one without the
    other is exactly how this bug shipped the first time."""

    def setUp(self):
        self.src = source()

    def test_there_are_exactly_the_three_known_entry_points(self):
        """Sign-in/up, magic link, password reset -- the only anonymous
        actions that both need a human check and can be bot-farmed. OAuth
        is deliberately excluded: Google gates its own bot traffic."""
        self.assertEqual(self.src.count("captchaToken:turnstileToken()"), 4,
                         "signUp and signInWithPassword share one call site; "
                         "a count change here means an entry point was added "
                         "or removed without updating this test")

    def test_each_captcha_call_site_is_preceded_by_the_guard(self):
        for call in re.finditer(r"captchaToken:turnstileToken\(\)", self.src):
            # The guard sits in the same click handler, a few lines above the
            # call it protects -- never more than one screen's worth of code.
            window = self.src[max(0, call.start() - 600):call.start()]
            self.assertIn("await captchaBlocking()", window,
                         "a captchaToken call with no captchaBlocking() guard "
                         "above it: " + self.src[call.start():call.start() + 60])

    def test_signinwithoauth_does_not_need_the_guard(self):
        """Confirms the omission above is deliberate, not an oversight --
        if Google sign-in ever starts sending a captchaToken, it needs the
        same guard as everything else."""
        fn = fn_body(self.src, 'signInWithOAuth({')
        self.assertNotIn("captchaToken", fn)


class CaptchaBlockingWaitsOutTheCommonCaseTests(unittest.TestCase):
    """The widget usually finishes within a second of page load -- well
    before a person finishes typing their email and password. Failing
    instantly on a blank token would make that normal timing gap look like
    a real error and force a pointless second tap."""

    def setUp(self):
        self.fn = fn_body(source(), "async function captchaBlocking()")

    def test_it_is_async_so_callers_can_wait_on_it(self):
        self.assertIn("async function captchaBlocking()", source())

    def test_it_polls_rather_than_checking_once(self):
        self.assertIn("for(", self.fn)
        self.assertIn("await new Promise", self.fn)

    def test_it_gives_up_eventually_rather_than_hanging_forever(self):
        """A script that is genuinely blocked never produces a token --
        the wait must have a ceiling, or the button would spin forever."""
        self.assertRegex(self.fn, r"waited\s*<\s*\d+")

    def test_a_token_that_arrives_during_the_wait_is_not_blocked(self):
        self.assertIn("if(turnstileToken())return false;", self.fn)

    def test_the_message_explains_the_actual_fix(self):
        """Not "try again" alone -- the one message a genuinely-blocked
        visitor sees has to name what to unblock, or they're stuck."""
        self.assertIn("challenges.cloudflare.com", self.fn)


class SupabaseRawCaptchaErrorNeverReachesTheScreenTests(unittest.TestCase):
    """Safety net for the request that slips past captchaBlocking() anyway
    (a token that goes stale in the moment between the check and Supabase
    receiving it) -- every captchaToken call's error path must run through
    the same translator, not show Supabase's own text."""

    def setUp(self):
        self.src = source()

    def test_the_translator_exists_and_catches_the_known_message(self):
        fn = fn_body(self.src, "function friendlyAuthError(")
        self.assertIn("captcha", fn.lower())
        self.assertNotIn("request disallowed", fn.lower())

    def test_every_guarded_call_sites_error_goes_through_it(self):
        for call in re.finditer(r"captchaToken:turnstileToken\(\)", self.src):
            # The error handling for a call sits below it, not above --
            # widen the window forward instead of backward.
            window = self.src[call.start():call.start() + 500]
            self.assertIn("friendlyAuthError(", window,
                         "a captcha-guarded call whose error is shown raw: "
                         + self.src[call.start():call.start() + 60])


if __name__ == "__main__":
    unittest.main()
