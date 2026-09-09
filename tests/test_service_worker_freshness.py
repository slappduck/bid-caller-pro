"""A cached policy is the wrong policy.

terms.html and privacy.html were ordinary shell files, cached
stale-while-revalidate: the installed app served whatever copy it had and
refreshed only afterwards. The cache name stayed "v48" across every policy
edit, so installs kept serving the pre-September text while the server
recorded consent against the September version.

That is not a stale-content annoyance. The whole point of storing a version
with each acceptance -- and of archiving the versions -- is that the document
someone agreed to can be produced. Showing a reader last month's text while
filing consent against this month's breaks it at the source.

So legal documents are network-first with a cache fallback: correct when there
is a connection, still readable with no bars. And the cache name has to move
whenever a published policy does, or existing installs never refetch.
"""
import os
import re
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, os.pardir)
WEB = os.path.join(ROOT, "curbcall_netlify_v4")
SW = os.path.join(WEB, "sw.js")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def sw_source():
    """Comments stripped: they name the very files being discussed."""
    return re.sub(r"//.*$", "", read(SW), flags=re.M)


class LegalPagesAreNotServedStaleTests(unittest.TestCase):
    def setUp(self):
        self.src = sw_source()

    def test_the_policies_are_handled_before_the_shell_rule(self):
        """The shell branch returns; anything after it never runs."""
        legal = self.src.index("matches(LEGAL_FILES)")
        shell = self.src.index("const isShellFile")
        self.assertLess(legal, shell,
                        "the stale-while-revalidate branch would claim them "
                        "first")

    def test_both_documents_are_named(self):
        m = re.search(r"const LEGAL_FILES\s*=\s*\[([^\]]*)\]", self.src)
        self.assertIsNotNone(m, "LEGAL_FILES is gone")
        for page in ("terms.html", "privacy.html"):
            self.assertIn(page, m.group(1))

    def test_the_version_archive_is_covered_too(self):
        """An archived version is the one most likely to be read years later."""
        self.assertIn("/legal/", self.src)

    def test_the_legal_branch_goes_to_the_network_first(self):
        i = self.src.index("matches(LEGAL_FILES)")
        branch = self.src[i:i + 700]
        self.assertLess(branch.index("fetch(event.request)"),
                        branch.index("caches.match"),
                        "cache is consulted before the network")

    def test_it_still_works_with_no_connection(self):
        i = self.src.index("matches(LEGAL_FILES)")
        branch = self.src[i:i + 700]
        self.assertIn(".catch(", branch)
        self.assertIn("caches.match", branch)


class CacheVersionMovesWithThePoliciesTests(unittest.TestCase):
    """An unchanged cache name means existing installs never refetch."""

    def _version(self, text):
        m = re.search(r'curbcall-shell-v(\d+)', text)
        return int(m.group(1)) if m else None

    def test_there_is_a_version(self):
        self.assertIsNotNone(self._version(read(SW)))

    def test_it_moved_the_last_time_a_policy_did(self):
        """Checked against git, because this is only ever wrong in history.

        The failure it guards is silent: edit privacy.html, ship it, and every
        installed app keeps the old one because the cache name did not change.
        """
        def at(ref, path):
            try:
                return subprocess.run(
                    ["git", "show", "%s:%s" % (ref, path)], cwd=ROOT,
                    capture_output=True, text=True, timeout=30).stdout
            except Exception:
                return ""

        head_priv = read(os.path.join(WEB, "privacy.html"))
        prev_priv = at("HEAD", "curbcall_netlify_v4/privacy.html")
        if not prev_priv or prev_priv == head_priv:
            self.skipTest("privacy.html unchanged against HEAD")
        now = self._version(read(SW))
        was = self._version(at("HEAD", "curbcall_netlify_v4/sw.js"))
        self.assertIsNotNone(was)
        self.assertGreater(now, was,
                           "privacy.html changed but the service-worker cache "
                           "name did not, so installed apps keep the old one")


class ApiResponsesAreStillNeverCachedTests(unittest.TestCase):
    """The original promise, which this change must not have loosened."""

    def test_only_same_origin_files_reach_the_shell_cache(self):
        src = sw_source()
        self.assertIn("url.origin === self.location.origin", src)

    def test_non_get_requests_are_left_alone(self):
        self.assertIn('event.request.method !== "GET"', sw_source())
