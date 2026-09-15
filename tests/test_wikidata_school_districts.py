"""Harvesting US school-district websites from Wikidata.

School districts are almost never on a .gov domain (.k12.xx.us, .org, and
vanity domains instead), so the CISA registry this product's directory is
otherwise built from has essentially none of them -- 65 of roughly 13,000
nationally. Wikidata's P856 covers 808, skewed toward the larger districts,
which is the right bias for concrete-bid prospecting: a fifty-school
district lets far more work than a two-school one.

Two things this file gets right matter enough to pin:
  * WDQS throttles hard during load ("Aggressively rate-limiting to 1
    req/min") and a one-off registry build should wait that out rather than
    giving up, but a plain 404/permanent error should not be retried forever;
  * a district is unusable to the rest of the pipeline without a state code
    (it keys placement on (city, state)), so a row with no resolvable state
    is dropped rather than written with a blank one that would silently
    never match anything downstream.

The SPARQL endpoint is stubbed throughout via urllib.request.urlopen --
nothing here makes a live request to query.wikidata.org.
"""
import csv
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import wikidata_school_districts as W  # noqa: E402


def _row(district, qid, site, state=""):
    r = {"d": {"value": "http://www.wikidata.org/entity/%s" % qid},
         "dLabel": {"value": district},
         "site": {"value": site}}
    if state:
        r["stateLabel"] = {"value": state}
    return r


class _JsonResponse:
    """json.load(r) calls r.read(); a plain object with .read is enough and
    doesn't need the context-manager dance `with ... as r:` uses."""

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class DomainTests(unittest.TestCase):
    def test_extracts_and_lowercases_the_host(self):
        self.assertEqual(W._domain("https://WWW.Rockwood.K12.MO.US/"),
                          "rockwood.k12.mo.us")

    def test_strips_the_www_prefix_only(self):
        self.assertEqual(W._domain("https://rockwood.k12.mo.us"),
                          "rockwood.k12.mo.us")

    def test_an_unparsable_url_returns_empty_string_not_a_raise(self):
        self.assertEqual(W._domain("http://[::1"), "")


class SparqlRetryTests(unittest.TestCase):
    def test_a_429_waits_out_the_retry_after_header_then_succeeds(self):
        payload = {"results": {"bindings": [_row("Rockwood", "Q1", "https://rockwood.k12.mo.us")]}}
        err = urllib.error.HTTPError("u", 429, "throttled", {"Retry-After": "5"}, None)
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise err
            return _JsonResponse(payload)

        with patch("tools.wikidata_school_districts.urllib.request.urlopen",
                    side_effect=fake_urlopen), \
             patch("tools.wikidata_school_districts.time.sleep") as sleep:
            rows = W._sparql("SELECT * WHERE {}")
        self.assertEqual(len(rows), 1)
        sleep.assert_called_once_with(5)

    def test_a_4xx_that_is_not_429_is_not_retried(self):
        err = urllib.error.HTTPError("u", 404, "gone", {}, None)
        with patch("tools.wikidata_school_districts.urllib.request.urlopen",
                    side_effect=err), \
             patch("tools.wikidata_school_districts.time.sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                W._sparql("SELECT * WHERE {}", tries=6)
        sleep.assert_not_called()

    def test_exhausting_every_retry_reraises_the_last_error(self):
        err = urllib.error.HTTPError("u", 503, "unavailable", {}, None)
        with patch("tools.wikidata_school_districts.urllib.request.urlopen",
                    side_effect=err), \
             patch("tools.wikidata_school_districts.time.sleep"):
            with self.assertRaises(urllib.error.HTTPError):
                W._sparql("SELECT * WHERE {}", tries=2)


class MainHarvestTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.out = os.path.join(self.tmpdir, "school_district_candidates.csv")
        self._orig_out = W.OUT_CSV
        W.OUT_CSV = self.out
        self.addCleanup(setattr, W, "OUT_CSV", self._orig_out)

    def _run(self, rows, argv=None):
        with patch.object(W, "_sparql", return_value=rows), \
             patch.object(sys, "argv", ["wikidata_school_districts.py"] + (argv or [])):
            W.main()
        with open(self.out, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def test_a_district_with_no_resolvable_state_is_dropped(self):
        """The pipeline keys placement on (city, state); a blank state would
        never match anything downstream, so it's excluded rather than
        written as junk."""
        rows = [_row("Mystery District", "Q1", "https://mystery.example.org")]
        out = self._run(rows)
        self.assertEqual(out, [])

    def test_a_district_with_a_resolvable_state_is_kept_and_abbreviated(self):
        rows = [_row("Rockwood R-VI School District", "Q1",
                     "https://rockwood.k12.mo.us", state="Missouri")]
        out = self._run(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["state"], "MO")
        self.assertEqual(out[0]["domain"], "rockwood.k12.mo.us")
        self.assertEqual(out[0]["type"], "School district")

    def test_the_same_domain_twice_is_only_one_candidate(self):
        """A district can carry more than one P856 value (a portal and a
        homepage) -- the second occurrence must not duplicate the row."""
        rows = [
            _row("Rockwood", "Q1", "https://rockwood.k12.mo.us", state="Missouri"),
            _row("Rockwood", "Q1", "https://www.rockwood.k12.mo.us", state="Missouri"),
        ]
        out = self._run(rows)
        self.assertEqual(len(out), 1)

    def test_the_limit_flag_caps_the_output(self):
        rows = [_row("D%d" % i, "Q%d" % i, "https://d%d.example.k12.mo.us" % i,
                     state="Missouri") for i in range(5)]
        out = self._run(rows, argv=["--limit", "2"])
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
