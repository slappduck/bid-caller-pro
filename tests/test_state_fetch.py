"""HTTP fetch behavior for state-government sites: status translation,
robots handling, and the per-host rate limiter.

State edge networks (Akamai/Cloudflare) return specific signals this module
has to translate correctly for the report to be honest: a 401/403/406/429
means the site recognized us and turned us away on purpose, so it is
reported as "blocked" -- kept distinct from a plain transport error, because
"blocked" is the signal that a human has to find another way to reach that
state's bids, while a transport error might just mean the site was down for
a minute. A robots.txt that cannot be fetched at all is neither consent nor
refusal, and the module defaults it to allowed (the same default every
crawler uses) rather than to disallowed, because several states' edges
block /robots.txt itself while serving the real bid pages fine -- treating
an unreachable robots.txt as a disallow would silently drop those states.

Every test here stubs urllib.request.urlopen; nothing makes a live request.
The rate limiter is verified against mocked time.time/time.sleep rather than
real sleeping, so the suite doesn't spend 1.5+ real seconds per test.
"""
import os
import sys
import unittest
import urllib.error
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import state_fetch as SF  # noqa: E402


def _response(body=b""):
    """A urlopen() return value usable as `with urlopen(...) as r: r.read()`."""
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = body
    cm.__enter__.return_value.status = 200
    return cm


class _CleanCaches(unittest.TestCase):
    """The rate-limit and robots caches are module-level dicts shared across
    every call; each test gets a clean slate so tests can't leak into each
    other through them."""

    def setUp(self):
        self._saved_hits = dict(SF._last_hit)
        self._saved_robots = dict(SF._robots)
        SF._last_hit.clear()
        SF._robots.clear()
        # These tests aren't about the inter-host gap, and two hits on the
        # same host in one test (robots.txt, then the page) would otherwise
        # burn a real MIN_GAP_SEC-second wait -- _wait_turn loops on the
        # real clock, so patching time.sleep alone doesn't avoid that.
        # RateLimitTests below tests _wait_turn itself and doesn't use this
        # base class.
        wait_patcher = patch("tools.state_fetch._wait_turn", lambda host: None)
        wait_patcher.start()
        self.addCleanup(wait_patcher.stop)

    def tearDown(self):
        SF._last_hit.clear()
        SF._last_hit.update(self._saved_hits)
        SF._robots.clear()
        SF._robots.update(self._saved_robots)


class HostOfTests(unittest.TestCase):
    def test_extracts_a_lowercase_hostname(self):
        self.assertEqual(SF._host_of("https://DOT.Example.GOV/path"),
                          "dot.example.gov")

    def test_a_url_with_no_host_is_the_empty_string(self):
        self.assertEqual(SF._host_of("not-a-url-at-all"), "")

    def test_an_unparsable_url_is_the_empty_string_not_a_raise(self):
        self.assertEqual(SF._host_of("http://[::1"), "")


class BlockedStatusTests(_CleanCaches):
    """The five-state honest-refusal rule and the Akamai/Cloudflare edge
    codes both funnel through this one translation."""

    def test_a_normal_response_is_returned_as_is(self):
        with patch("tools.state_fetch.urllib.request.urlopen",
                    return_value=_response(b"<html>hi</html>")):
            status, text = SF.fetch("https://dot.example.gov/page",
                                    check_robots=False)
        self.assertEqual(status, 200)
        self.assertIn("hi", text)

    def test_401_403_406_429_are_all_reported_as_blocked(self):
        for code in (401, 403, 406, 429):
            err = urllib.error.HTTPError("u", code, "denied", {}, None)
            with patch("tools.state_fetch.urllib.request.urlopen",
                        side_effect=err):
                status, text = SF.fetch("https://dot.example.gov/page",
                                        check_robots=False)
            self.assertEqual(status, "blocked", "code %d" % code)
            self.assertEqual(text, "")

    def test_other_http_errors_keep_their_own_code_distinct_from_blocked(self):
        """A 500 is the site being down, not the site refusing us -- these
        must not collapse into the same "blocked" bucket."""
        err = urllib.error.HTTPError("u", 500, "boom", {}, None)
        with patch("tools.state_fetch.urllib.request.urlopen",
                    side_effect=err):
            status, _ = SF.fetch("https://dot.example.gov/page",
                                 check_robots=False)
        self.assertEqual(status, "http_500")

    def test_a_generic_transport_failure_is_named_by_its_exception_type(self):
        with patch("tools.state_fetch.urllib.request.urlopen",
                    side_effect=TimeoutError("slow")):
            status, text = SF.fetch("https://dot.example.gov/page",
                                    check_robots=False)
        self.assertEqual(status, "TimeoutError")
        self.assertEqual(text, "")

    def test_an_unparsable_url_is_bad_url_and_never_reaches_the_network(self):
        with patch("tools.state_fetch.urllib.request.urlopen") as mock_open:
            status, _ = SF.fetch("http://[::1", check_robots=False)
            mock_open.assert_not_called()
        self.assertEqual(status, "bad_url")

    def test_max_bytes_is_passed_through_to_the_read_call(self):
        cm = _response(b"x" * 10)
        with patch("tools.state_fetch.urllib.request.urlopen",
                    return_value=cm):
            SF.fetch("https://dot.example.gov/page", check_robots=False,
                     max_bytes=10)
        cm.__enter__.return_value.read.assert_called_with(10)


class RobotsAllowsTests(_CleanCaches):
    def test_a_disallowed_path_is_refused(self):
        robots_txt = b"User-agent: *\nDisallow: /private\n"
        with patch("tools.state_fetch.urllib.request.urlopen",
                    return_value=_response(robots_txt)):
            self.assertFalse(
                SF.robots_allows("https://dot.example.gov/private/page"))

    def test_a_path_outside_the_disallow_rule_is_allowed(self):
        robots_txt = b"User-agent: *\nDisallow: /private\n"
        with patch("tools.state_fetch.urllib.request.urlopen",
                    return_value=_response(robots_txt)):
            self.assertTrue(
                SF.robots_allows("https://dot.example.gov/public/page"))

    def test_an_unreachable_robots_txt_defaults_to_allowed(self):
        """Not consent and not refusal -- but the practical default must be
        permission, or the states whose edge blocks /robots.txt itself while
        serving bid pages fine would be wrongly treated as fully closed."""
        with patch("tools.state_fetch.urllib.request.urlopen",
                    side_effect=OSError("connection refused")):
            self.assertTrue(SF.robots_allows("https://dot.example.gov/page"))

    def test_robots_txt_is_fetched_once_per_host_and_then_cached(self):
        robots_txt = b"User-agent: *\nDisallow: /private\n"
        with patch("tools.state_fetch.urllib.request.urlopen",
                    return_value=_response(robots_txt)) as mock_open:
            SF.robots_allows("https://dot.example.gov/a")
            SF.robots_allows("https://dot.example.gov/b")
        self.assertEqual(mock_open.call_count, 1)

    def test_fetch_honors_a_disallow_without_ever_requesting_the_page(self):
        robots_txt = b"User-agent: *\nDisallow: /private\n"
        requested = []

        def fake_urlopen(req, timeout=None):
            requested.append(req.full_url)
            if req.full_url.endswith("/robots.txt"):
                return _response(robots_txt)
            raise AssertionError("the disallowed page must never be requested")

        with patch("tools.state_fetch.urllib.request.urlopen",
                    side_effect=fake_urlopen):
            status, text = SF.fetch("https://dot.example.gov/private/page")
        self.assertEqual(status, "robots_disallow")
        self.assertEqual(text, "")
        self.assertEqual(len(requested), 1)


class RateLimitTests(unittest.TestCase):
    """MIN_GAP_SEC = 1.5s and one request in flight per host at a time --
    the reason state sites (one host serving a whole department) get this
    while the city crawl's larger fan-out does not. Unlike the classes
    above, these test _wait_turn's real logic (with time itself mocked),
    so they don't use the base class that stubs it out."""

    def setUp(self):
        self._saved_hits = dict(SF._last_hit)
        SF._last_hit.clear()

    def tearDown(self):
        SF._last_hit.clear()
        SF._last_hit.update(self._saved_hits)

    def test_a_second_call_for_the_same_host_sleeps_out_the_remaining_gap(self):
        times = iter([1000.0, 1000.2, 1001.7])
        sleeps = []
        with patch("tools.state_fetch.time.time", side_effect=lambda: next(times)), \
             patch("tools.state_fetch.time.sleep", side_effect=sleeps.append):
            SF._wait_turn("dot.example.gov")   # now=1000.0: no prior hit, returns at once
            SF._wait_turn("dot.example.gov")   # now=1000.2: only 0.2s since last hit
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], SF.MIN_GAP_SEC - 0.2, places=6)

    def test_a_call_that_already_cleared_the_gap_does_not_sleep(self):
        times = iter([1000.0, 1002.0])
        with patch("tools.state_fetch.time.time", side_effect=lambda: next(times)), \
             patch("tools.state_fetch.time.sleep") as mock_sleep:
            SF._wait_turn("dot.example.gov")
            SF._wait_turn("dot.example.gov")
        mock_sleep.assert_not_called()

    def test_different_hosts_do_not_share_the_rate_limit(self):
        with patch("tools.state_fetch.time.time", return_value=1000.0), \
             patch("tools.state_fetch.time.sleep") as mock_sleep:
            SF._wait_turn("a.example.gov")
            SF._wait_turn("b.example.gov")
        mock_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
