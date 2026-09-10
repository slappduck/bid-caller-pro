"""A scan takes over a minute and the bar was a guess.

It was driven by how long a scan usually takes, so it counted up whether
anything was happening or not -- a stalled scan and a working one looked
identical for the whole minute, which is what makes a tool feel broken when
it is fine.

The scan now publishes where it actually is and the app reads it. Entirely
additive on purpose: /scan is unchanged, the record is best-effort, and a
client that cannot read it falls back to the old estimate. Nothing here is
allowed to fail a scan for the sake of a status line.

(The other half of that change -- overlapping the independent phases -- was
reverted after measurement: on half a vCPU it made every phase slower. See
the comment above the state stage in license_server.py.)
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


class ProgressChannelTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self._g, self._s = ls.kv_backend.get, ls.kv_backend.set
        ls.kv_backend.get = lambda k, d=None: self.store.get(k, d)
        ls.kv_backend.set = lambda k, v: self.store.__setitem__(k, v)
        self.app = ls.app.test_client()

    def tearDown(self):
        ls.kv_backend.get, ls.kv_backend.set = self._g, self._s

    def test_a_phase_is_published_and_readable(self):
        ls._progress_note("abcd1234efgh", "reading_towns", 12)
        got = self.app.post("/scan/progress",
                            json={"token": "abcd1234efgh"}).get_json()
        self.assertTrue(got["known"])
        self.assertEqual(got["phase"], "reading_towns")
        self.assertEqual(got["found"], 12)

    def test_an_unknown_token_is_not_an_error(self):
        got = self.app.post("/scan/progress",
                            json={"token": "neverseen1234"}).get_json()
        self.assertTrue(got["ok"])
        self.assertFalse(got["known"])

    def test_a_malformed_token_is_refused(self):
        r = self.app.post("/scan/progress", json={"token": "../etc/passwd"})
        self.assertEqual(r.status_code, 400)

    def test_a_stale_record_reads_as_unknown(self):
        """A scan that died without finishing must not look like one running."""
        ls._progress_note("abcd1234efgh", "searching", 1)
        self.store[ls._progress_key("abcd1234efgh")]["at"] -= (
            ls._PROGRESS_TTL_SEC + 10)
        got = self.app.post("/scan/progress",
                            json={"token": "abcd1234efgh"}).get_json()
        self.assertFalse(got["known"])

    def test_it_carries_no_bid_content(self):
        """Which is why it needs no licence check."""
        ls._progress_note("abcd1234efgh", "searching", 3)
        blob = str(self.app.post("/scan/progress",
                                 json={"token": "abcd1234efgh"}).get_json())
        for leak in ("title", "url", "@", "deadline"):
            self.assertNotIn(leak, blob)

    def test_a_dead_store_never_breaks_a_scan(self):
        def boom(*a, **k):
            raise RuntimeError("kv down")
        ls.kv_backend.set = boom
        ls._progress_note("abcd1234efgh", "searching", 1)   # must not raise

    def test_writing_is_refused_for_a_bad_token(self):
        ls._progress_note("../x", "searching", 1)
        self.assertEqual(self.store, {})

    def test_the_scan_publishes_at_each_phase(self):
        import inspect
        src = inspect.getsource(ls._perform_scan)
        for phase in ("searching", "reading_towns", "checking_details",
                      "finishing"):
            self.assertIn('"%s"' % phase, src)


class AppSideTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               os.pardir, "curbcall_netlify_v4", "app.html"),
                  encoding="utf-8") as f:
            html = f.read()
        body = "\n".join(re.findall(
            r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S))
        self.js = re.sub(r"^\s*//.*$", "", body, flags=re.M)

    def test_the_scan_sends_a_progress_token(self):
        self.assertIn("progress_token:progressToken", self.js)

    def test_every_phase_has_words_a_contractor_would_use(self):
        for phase in ("searching", "reading_towns", "checking_details",
                      "finishing"):
            self.assertIn(phase + ":", self.js)

    def test_the_watcher_is_stopped_when_the_scan_ends(self):
        """A poll loop nobody stops runs until the tab closes."""
        self.assertIn("stopWatching()", self.js)

    def test_only_the_scan_that_starts_it_stops_it(self):
        """runUpcoming and runLeads have their own killer timeout and no
        watcher; calling it there is a ReferenceError that breaks both."""
        for fn in ("runUpcoming", "runLeads"):
            i = self.js.index("function " + fn)
            j = self.js.index("\n}", i)
            self.assertNotIn("stopWatching", self.js[i:j],
                             fn + " calls a watcher it does not have")

    def test_a_failed_poll_is_silent(self):
        i = self.js.index("function watchScanProgress")
        self.assertIn("catch(e)", self.js[i:i + 1600])
