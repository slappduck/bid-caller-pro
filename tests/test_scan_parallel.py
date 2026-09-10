"""A scan took 79 seconds median because its sources waited in line.

Measured across ten real scans: towns 41%, search 34%, state 12%, federal 12%.
State letting pages and SAM talk to entirely different hosts from the search
backends, so nothing about them required waiting their turn -- their seconds
were being added to the wall clock instead of overlapping it.

They now start before the search-and-read phase and are collected after it.
Each fills its OWN dict: _place_bid() dedupes against the bucket it writes
into, and making every source thread-safe by inspection is the kind of change
that looks fine and corrupts a feed at three in the morning. Separate dicts
cost one merge and are correct by construction.

The merge has to redo the dedupe, because a state letting page and a municipal
portal genuinely do carry the same job.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


def source():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           os.pardir, "license_server.py"), encoding="utf-8") as f:
        return re.sub(r"^\s*#.*$", "", f.read(), flags=re.M)


class MergeTests(unittest.TestCase):
    def bid(self, title, deadline="2026-10-01", city="Aurora"):
        return {"title": title, "deadline": deadline, "city": city}

    def test_new_bids_are_added(self):
        into = {"Aurora, MO": [self.bid("Sidewalk")]}
        ls._merge_grouped(into, {"Aurora, MO": [self.bid("ADA Ramps", "2026-11-01")]})
        self.assertEqual(len(into["Aurora, MO"]), 2)

    def test_the_same_job_from_two_sources_is_not_shown_twice(self):
        """A state letting page and a municipal portal carry the same job."""
        into = {"Aurora, MO": [self.bid("Sidewalk Replacement")]}
        stats = {}
        ls._merge_grouped(into, {"Aurora, MO": [self.bid("Sidewalk Replacement")]},
                          stats)
        self.assertEqual(len(into["Aurora, MO"]), 1)
        self.assertEqual(stats["merge_duplicate"], 1)

    def test_a_new_town_creates_its_bucket(self):
        into = {}
        ls._merge_grouped(into, {"Joplin, MO": [self.bid("Curb", city="Joplin")]})
        self.assertEqual(list(into), ["Joplin, MO"])

    def test_duplicates_inside_the_incoming_batch_collapse_too(self):
        into = {}
        ls._merge_grouped(into, {"Aurora, MO": [self.bid("Curb"), self.bid("Curb")]})
        self.assertEqual(len(into["Aurora, MO"]), 1)

    def test_an_empty_or_missing_batch_is_harmless(self):
        into = {"Aurora, MO": [self.bid("Sidewalk")]}
        ls._merge_grouped(into, None)
        ls._merge_grouped(into, {})
        self.assertEqual(len(into["Aurora, MO"]), 1)


class StagesRunConcurrentlyTests(unittest.TestCase):
    def setUp(self):
        self.src = source()

    def test_state_and_federal_start_before_the_search_phase(self):
        i_state = self.src.index('_stage_async(\n            drop_stats, "state"')
        i_search = self.src.index("_t_search = time.time()")
        self.assertLess(i_state, i_search,
                        "the independent sources still wait their turn")

    def test_they_are_collected_before_enrichment(self):
        """Their rows must go through the same deadline and enrichment passes
        as everything else -- only the waiting was supposed to change."""
        i_merge = self.src.index("_merge_grouped(grouped, own, drop_stats)")
        i_enrich = self.src.index('"enrich", scan_deadline')
        self.assertLess(i_merge, i_enrich)

    def test_each_writes_to_its_own_dict(self):
        self.assertIn("_state_grouped, _federal_grouped = {}, {}", self.src)
        self.assertIn("center, radius, _state_grouped, city_coords", self.src)

    def test_a_failing_source_does_not_take_the_scan_with_it(self):
        i = self.src.index("for fut, own in ((_state_fut")
        block = self.src[i:i + 900]
        self.assertIn("except Exception", block)
        self.assertIn("stage_failed", block)

    def test_it_still_times_each_stage(self):
        """The timings are what justified this change; losing them would make
        the next one guesswork."""
        i = self.src.index("def _stage_async(")
        self.assertIn('stats["ms_" + name]', self.src[i:i + 1400])


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
