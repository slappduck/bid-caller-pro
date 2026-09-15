"""The standalone recall-check CLI: does the real funnel find real bids?

SEARCH_PLAN.md's original recall benchmark was a fixed list of "bids known to
be open right now" -- checked live against springfieldmo.gov/Bids.aspx while
building this, and the two documented ground-truth bids had already closed.
A recall test tied to bids staying open goes stale within weeks for reasons
that have nothing to do with whether the scanner works, which is why
tools/recall_check.py runs the real parser and relevance filter against a
saved fixture (or any live page on demand) instead.

tests/test_recall_fixtures.py already pins bid_sources.parse_civicplus_html
and looks_relevant() against the bundled fixture directly. What is untested
is recall_check.py's own layer on top: the pass/fail verdict run() computes
from --expect, the AI-stage gating (skipped without an API key, reported
either way), and main()'s CLI error handling for a live --url fetch. These
reuse the same bundled fixture rather than inventing a second one, since a
CivicPlus page's exact markup is itself part of what is being trusted.

The one real network call this file makes (--url) is stubbed by replacing
recall_check._fetch.
"""
import contextlib
import io
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tools.recall_check as RC  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "recall_fixtures", "springfield_civicplus.html")
with open(FIXTURE, encoding="utf-8") as _f:
    SPRINGFIELD_HTML = _f.read()
BASE_URL = "https://www.springfieldmo.gov"
KNOWN_BIDS = ["ADA IMPROVEMENT PROJECT", "MT. VERNON & MILLER SIDEWALKS"]


class RunVerdictTests(unittest.TestCase):
    def test_both_known_bids_found_is_a_pass(self):
        ok = RC.run(SPRINGFIELD_HTML, BASE_URL, expect=KNOWN_BIDS)
        self.assertTrue(ok)

    def test_a_missing_expected_bid_is_a_fail(self):
        ok = RC.run(SPRINGFIELD_HTML, BASE_URL,
                    expect=KNOWN_BIDS + ["A BID THAT DOES NOT EXIST"])
        self.assertFalse(ok)

    def test_recall_is_reported_as_a_fraction_of_the_expected_list(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            RC.run(SPRINGFIELD_HTML, BASE_URL,
                  expect=[KNOWN_BIDS[0], "A BID THAT DOES NOT EXIST"])
        self.assertIn("recall: 50%", out.getvalue())

    def test_no_expect_list_means_no_verdict_is_computed(self):
        """Just running against a page (no --expect) still reports what was
        found; it should not fabricate a pass/fail."""
        ok = RC.run(SPRINGFIELD_HTML, BASE_URL, expect=None)
        self.assertTrue(ok)  # ok defaults True and is never flipped

    def test_unrelated_postings_are_reported_as_dropped_with_a_reason(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            RC.run(SPRINGFIELD_HTML, BASE_URL, expect=None)
        self.assertIn("RENTAL OF ICE MACHINES", out.getvalue())


class AiStageGatingTests(unittest.TestCase):
    def setUp(self):
        self._had_key = os.environ.pop("OPENAI_API_KEY", None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._had_key is not None:
            os.environ["OPENAI_API_KEY"] = self._had_key

    def test_the_ai_stage_is_skipped_without_a_key(self):
        self.assertIsNone(RC._ai_stage("Springfield, MO", "some bid text"))

    def test_run_reports_the_ai_stage_as_skipped_when_no_key_is_set(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            RC.run(SPRINGFIELD_HTML, BASE_URL, expect=None, try_ai=True)
        self.assertIn("AI stage: skipped", out.getvalue())

    def test_run_reports_the_ai_stage_count_when_stubbed(self):
        orig = RC._ai_stage
        RC._ai_stage = lambda area, text: [{"title": "ADA IMPROVEMENT PROJECT"},
                                            {"title": "MT. VERNON & MILLER SIDEWALKS"}]
        self.addCleanup(lambda: setattr(RC, "_ai_stage", orig))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            RC.run(SPRINGFIELD_HTML, BASE_URL, expect=None, try_ai=True)
        self.assertIn("AI stage: extraction confirmed 2 bid(s)", out.getvalue())


class MainCliTests(unittest.TestCase):
    def _argv(self, *extra):
        return ["recall_check.py"] + list(extra)

    def test_the_bundled_fixture_passes_its_own_bundled_expectations(self):
        old = sys.argv
        sys.argv = self._argv()
        try:
            RC.main()  # would sys.exit(1) on a regression
        finally:
            sys.argv = old

    def test_a_fetch_failure_exits_with_a_message_not_a_traceback(self):
        orig = RC._fetch
        RC._fetch = lambda url: (_ for _ in ()).throw(
            urllib.error.URLError("no route to host"))
        self.addCleanup(lambda: setattr(RC, "_fetch", orig))
        old = sys.argv
        sys.argv = self._argv("--url", "https://city.example.gov/Bids.aspx")
        try:
            with self.assertRaises(SystemExit) as cm:
                RC.main()
            self.assertIn("fetch failed", str(cm.exception))
        finally:
            sys.argv = old

    def test_a_url_missing_an_expected_bid_exits_nonzero(self):
        orig = RC._fetch
        RC._fetch = lambda url: SPRINGFIELD_HTML
        self.addCleanup(lambda: setattr(RC, "_fetch", orig))
        old = sys.argv
        sys.argv = self._argv("--url", "https://city.example.gov/Bids.aspx",
                              "--expect", "A BID THAT DOES NOT EXIST")
        try:
            with self.assertRaises(SystemExit) as cm:
                RC.main()
            self.assertEqual(cm.exception.code, 1)
        finally:
            sys.argv = old

    def test_a_fixture_override_does_not_apply_the_bundled_expectations(self):
        """--fixture with no --expect must not silently reuse the bundled
        fixture's own ground truth against an unrelated page."""
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "other.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write('<a href="x">SOME OTHER BID</a>')
        old = sys.argv
        sys.argv = self._argv("--fixture", path)
        try:
            RC.main()  # no --expect applied: nothing to fail on
        finally:
            sys.argv = old


if __name__ == "__main__":
    unittest.main()
