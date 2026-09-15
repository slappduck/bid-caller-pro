"""Pulling local-government candidates out of Wikidata.

Indiana looked almost empty in the .gov registry because the state routes its
cities through *.in.gov subdomains the registry never sees -- this script's
whole reason to exist is reaching those. Two things can quietly wreck that:

  * a district's name has to turn into the TOWN it sits in ("Chester School
    District" -> Chester), and the suffix list has to be checked longest
    first or "Unified School District" strips to "Unified", not the town;
  * the two ways of resolving a place's state and parent -- SPARQL in
    bounded batches, REST as a throttle-proof fallback -- have to agree
    closely enough that neither silently produces worse data than the other.

That second point is where a real gap turned up while writing these tests:
DETAIL_QUERY (the default, non-`--rest` path) never selects a parent label at
all, so every place `_details_for` resolves carries an empty parent -- the
"fall back to the parent from P131" behaviour `_place_name` documents is
dead code unless `--rest` is used. See
test_the_sparql_path_never_actually_resolves_a_parent_name below; flagged in
the task report rather than fixed here.

The network is stubbed throughout -- no test here talks to WDQS or Wikidata's
REST API.
"""
import csv
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import wikidata_municipalities as W  # noqa: E402


class PlaceNameTests(unittest.TestCase):
    def test_a_role_suffix_is_stripped_to_the_town(self):
        self.assertEqual(W._place_name("Chester School District", ""), "Chester")

    def test_the_longest_suffix_wins_so_unified_is_not_left_behind(self):
        """"Unified School District" must strip whole, not leave "Unified"
        dangling the way it would if "School District" matched first."""
        self.assertEqual(
            W._place_name("Springfield Unified School District", ""),
            "Springfield")

    def test_a_digit_only_stem_falls_back_to_the_parent(self):
        """"14 School District" strips to "14", which is not a town."""
        self.assertEqual(
            W._place_name("14 School District", "Boone County"),
            "Boone County")

    def test_a_two_character_stem_falls_back_to_the_parent(self):
        self.assertEqual(W._place_name("Co Water District", "Boone County"),
                         "Boone County")

    def test_a_name_with_no_role_suffix_is_returned_as_is(self):
        self.assertEqual(W._place_name("Springfield", ""), "Springfield")

    def test_a_county_suffix_strips_to_the_county_seat_name(self):
        self.assertEqual(W._place_name("Boone County", ""), "Boone")

    def test_no_name_and_no_parent_is_empty_not_none(self):
        self.assertEqual(W._place_name("", ""), "")

    def test_a_name_with_nothing_useful_and_no_parent_falls_back_to_itself(self):
        """"Region 14" carries no town at all; absent a parent, the name is
        all there is to return."""
        self.assertEqual(W._place_name("Region 14", ""), "Region 14")


class DomainTests(unittest.TestCase):
    def test_www_is_stripped(self):
        self.assertEqual(W._domain("https://www.example.gov/bids"),
                         "example.gov")

    def test_a_bare_host_keeps_its_case_folded_down(self):
        self.assertEqual(W._domain("https://City.Example.US/"),
                         "city.example.us")

    def test_an_unparseable_url_yields_an_empty_domain_not_a_crash(self):
        self.assertEqual(W._domain(""), "")


class KnownDomainsTests(unittest.TestCase):
    """Domains already probed by any prior stage must not be re-probed."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self._root = W._ROOT
        W._ROOT = self.tmp
        self.addCleanup(lambda: setattr(W, "_ROOT", self._root))

    def _write(self, rel, rows, header):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)

    def test_domains_are_collected_across_every_known_file_present(self):
        self._write("data/bid_portal_directory.csv",
                    [["Springfield.gov"]], ["domain"])
        self._write("data/gov_domains.csv", [["boone.mo.gov"]], ["domain"])
        seen = W._known_domains()
        # case-folded, matching the lookup this feeds later
        self.assertEqual(seen, {"springfield.gov", "boone.mo.gov"})

    def test_a_missing_known_file_is_not_an_error(self):
        # Nothing written at all -- every file in KNOWN is absent.
        self.assertEqual(W._known_domains(), set())

    def test_blank_domain_cells_are_not_counted_as_known(self):
        self._write("data/bid_portal_directory.csv", [[""]], ["domain"])
        self.assertEqual(W._known_domains(), set())


class SparqlRetryTests(unittest.TestCase):
    """WDQS throttles and says so; the tool waits rather than giving up on a
    one-off registry build where waiting is free."""

    def setUp(self):
        self._sleep = W.time.sleep
        W.time.sleep = lambda s: None  # don't actually wait in tests
        self.addCleanup(lambda: setattr(W.time, "sleep", self._sleep))

    def test_a_throttle_response_is_retried_until_it_succeeds(self):
        calls = {"n": 0}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"results": {"bindings": [{"x": {"value": "Q1"}}]}}'

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.HTTPError(
                    "url", 429, "throttled", {"Retry-After": "0"}, None)
            return FakeResp()

        orig = W.urllib.request.urlopen
        W.urllib.request.urlopen = fake_urlopen
        self.addCleanup(lambda: setattr(W.urllib.request, "urlopen", orig))

        got = W._sparql("SELECT * WHERE {}")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(got[0]["x"]["value"], "Q1")

    def test_a_non_retryable_error_is_raised_immediately(self):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError("url", 400, "bad query", {}, None)

        orig = W.urllib.request.urlopen
        W.urllib.request.urlopen = fake_urlopen
        self.addCleanup(lambda: setattr(W.urllib.request, "urlopen", orig))

        with self.assertRaises(urllib.error.HTTPError):
            W._sparql("SELECT * WHERE {}", tries=5)

    def test_exhausting_every_retry_still_raises(self):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError("url", 503, "unavailable", {}, None)

        orig = W.urllib.request.urlopen
        W.urllib.request.urlopen = fake_urlopen
        self.addCleanup(lambda: setattr(W.urllib.request, "urlopen", orig))

        with self.assertRaises(urllib.error.HTTPError):
            W._sparql("SELECT * WHERE {}", tries=2)


def _binding(qid, name="", state=""):
    row = {"x": {"value": "http://www.wikidata.org/entity/" + qid}}
    if name:
        row["xLabel"] = {"value": name}
    if state:
        row["stLabel"] = {"value": state}
    return row


class DetailsForTests(unittest.TestCase):
    """Resolving names and states in bounded VALUES batches."""

    def test_a_batch_that_fails_is_skipped_not_fatal(self):
        def boom(query):
            raise urllib.error.HTTPError("url", 500, "x", {}, None)
        orig = W._sparql
        W._sparql = boom
        self.addCleanup(lambda: setattr(W, "_sparql", orig))
        self.assertEqual(W._details_for(["Q1", "Q2"]), {})

    def test_a_row_that_resolved_no_state_never_overwrites_one_that_did(self):
        """P131* reaches a state by more than one path; a later row that came
        back empty is a dead end, not a correction."""
        calls = []

        def fake_sparql(query):
            calls.append(query)
            if len(calls) == 1:
                return [_binding("Q1", "Boone", "Missouri")]
            return [_binding("Q1", "Boone", "")]
        orig = W._sparql
        W._sparql = fake_sparql
        self.addCleanup(lambda: setattr(W, "_sparql", orig))

        # Force two batches so both fake responses are consumed.
        qids = ["Q1"] * (W.BATCH + 1)
        found = W._details_for(qids)
        self.assertEqual(found["Q1"][:2], ("Boone", "MO"))

    def test_the_sparql_path_never_actually_resolves_a_parent_name(self):
        """SUSPECTED BUG, flagged rather than fixed: DETAIL_QUERY selects
        only ?x, ?xLabel and ?stLabel -- no parent label -- so the third
        tuple element _details_for returns is always "". _place_name's
        documented "falls back to the parent from P131" behaviour is
        therefore unreachable on the default (non --rest) code path; only
        _details_via_rest actually resolves a parent. See
        tools/wikidata_municipalities.py DETAIL_QUERY (~L137) and
        _details_for (~L314-340)."""
        orig = W._sparql
        W._sparql = lambda query: [_binding("Q1", "Region 14", "Missouri")]
        self.addCleanup(lambda: setattr(W, "_sparql", orig))
        found = W._details_for(["Q1"])
        self.assertEqual(found["Q1"], ("Region 14", "MO", ""))


class MainIntegrationTests(unittest.TestCase):
    """The end-to-end CSV a run actually produces."""

    def setUp(self):
        self._sparql = W._sparql
        self._known = W._known_domains
        W._known_domains = lambda: set()
        self.addCleanup(lambda: setattr(W, "_known_domains", self._known))
        self.addCleanup(lambda: setattr(W, "_sparql", self._sparql))

    def _run(self, places_rows, detail_rows, argv):
        def fake_sparql(query):
            if "wdt:P856" in query:
                return places_rows
            return detail_rows
        W._sparql = fake_sparql
        old_argv = sys.argv
        sys.argv = ["wikidata_municipalities.py"] + argv
        try:
            W.main()
        finally:
            sys.argv = old_argv

    def test_a_state_filter_drops_everything_else(self):
        import tempfile
        out = os.path.join(tempfile.mkdtemp(), "out.csv")
        places = [{"x": {"value": "http://www.wikidata.org/entity/Q1"},
                   "site": {"value": "https://boone.mo.gov"}},
                  {"x": {"value": "http://www.wikidata.org/entity/Q2"},
                   "site": {"value": "https://reno.nv.gov"}}]
        detail = [_binding("Q1", "Boone", "Missouri"),
                  _binding("Q2", "Reno", "Nevada")]
        self._run(places, detail,
                   ["--kind", "city", "--out", out, "--state", "MO"])
        with open(out, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r["domain"] for r in rows], ["boone.mo.gov"])
        self.assertEqual(rows[0]["state"], "MO")

    def test_a_place_with_no_resolvable_state_is_dropped(self):
        """The directory keys on state; a row without one can never be
        placed, so it must not be written at all."""
        import tempfile
        out = os.path.join(tempfile.mkdtemp(), "out.csv")
        places = [{"x": {"value": "http://www.wikidata.org/entity/Q1"},
                   "site": {"value": "https://mystery.example.org"}}]
        detail = [_binding("Q1", "Mystery Town", "")]
        self._run(places, detail, ["--kind", "city", "--out", out])
        with open(out, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows, [])

    def test_school_kind_derives_the_city_from_the_district_name(self):
        import tempfile
        out = os.path.join(tempfile.mkdtemp(), "out.csv")
        places = [{"x": {"value": "http://www.wikidata.org/entity/Q1"},
                   "site": {"value": "https://chester.k12.mo.us"}}]
        detail = [_binding("Q1", "Chester School District", "Missouri")]
        self._run(places, detail, ["--kind", "school", "--out", out])
        with open(out, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["city"], "Chester")
        self.assertEqual(rows[0]["org"], "Chester School District")

    def test_a_domain_seen_more_than_once_is_only_written_once(self):
        """Neighbouring towns commonly share a county-run site; P856 also
        repeats across rows for the same place."""
        import tempfile
        out = os.path.join(tempfile.mkdtemp(), "out.csv")
        places = [{"x": {"value": "http://www.wikidata.org/entity/Q1"},
                   "site": {"value": "https://boone.mo.gov"}},
                  {"x": {"value": "http://www.wikidata.org/entity/Q1"},
                   "site": {"value": "https://boone.mo.gov"}}]
        detail = [_binding("Q1", "Boone", "Missouri")]
        self._run(places, detail, ["--kind", "city", "--out", out])
        with open(out, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
