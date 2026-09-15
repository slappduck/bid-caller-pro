"""Wikidata municipal-website harvest: domain parsing, the .gov carve-out,
and live-checking before anything gets written.

Wikidata's P856 ("official website") on municipality entities catches towns
the CISA .gov registry cannot see at all (a real .gov entry can point at a
domain that no longer resolves; a real town can run its whole site on
.org/.com/.us). But Wikidata is crowd-sourced and stale links happen, so
nothing here is trusted on the strength of the query alone: every domain is
supposed to be probed with a real GET before it is kept, and any domain that
already ends in .gov is dropped rather than duplicated, because that's
already the CISA registry's job.

The SPARQL endpoint and the liveness GETs are both stubbed -- nothing here
makes a real request to query.wikidata.org or to any town's website.
"""
import os
import sys
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import discover_wikidata_domains as D  # noqa: E402


def _binding(**kv):
    """One SPARQL result row, in the {"var": {"value": ...}} shape the real
    endpoint returns."""
    return {k: {"value": v} for k, v in kv.items()}


class DomainFromUrlTests(unittest.TestCase):
    def test_strips_the_www_prefix_and_lowercases(self):
        self.assertEqual(D._domain_from_url("https://WWW.Aurora-CityHall.ORG/"),
                          "aurora-cityhall.org")

    def test_strips_userinfo_and_port(self):
        self.assertEqual(
            D._domain_from_url("https://user:pass@example.com:8080/path"),
            "example.com")

    def test_a_bare_domain_with_no_www_is_left_alone(self):
        self.assertEqual(D._domain_from_url("https://republicmo.com"),
                          "republicmo.com")

    def test_an_unparsable_url_returns_empty_string_not_a_raise(self):
        self.assertEqual(D._domain_from_url("http://[::1"), "")


class StatesTests(unittest.TestCase):
    def test_only_us_iso_codes_are_kept(self):
        rows = [
            _binding(state="http://www.wikidata.org/entity/Q1581",
                     stateLabel="Missouri", iso="US-MO"),
            _binding(state="http://www.wikidata.org/entity/Q142",
                     stateLabel="France", iso="FR"),
        ]
        with patch.object(D, "_sparql", return_value=rows):
            out = D._states()
        self.assertEqual(out, {"MO": "Q1581"})

    def test_a_state_missing_the_iso_property_is_skipped_not_raising(self):
        rows = [_binding(state="http://www.wikidata.org/entity/Q99",
                         stateLabel="California")]
        with patch.object(D, "_sparql", return_value=rows):
            out = D._states()
        self.assertEqual(out, {})


class CitiesForStateTests(unittest.TestCase):
    def test_a_gov_domain_is_excluded_because_the_registry_already_covers_it(self):
        rows = [_binding(itemLabel="Jefferson City", website="https://www.jeffersoncitymo.gov")]
        with patch.object(D, "_sparql", return_value=rows):
            out = D._cities_for_state("Q1581")
        self.assertEqual(out, [])

    def test_a_non_gov_domain_survives_with_www_stripped(self):
        rows = [_binding(itemLabel="Rocheport", website="https://www.rocheportmo.us")]
        with patch.object(D, "_sparql", return_value=rows):
            out = D._cities_for_state("Q1581")
        self.assertEqual(out, [{"city": "Rocheport", "domain": "rocheportmo.us"}])

    def test_rows_missing_a_city_or_a_domain_are_dropped(self):
        rows = [
            _binding(itemLabel="", website="https://example.us"),
            _binding(itemLabel="Nowhere"),  # no website key at all
        ]
        with patch.object(D, "_sparql", return_value=rows):
            out = D._cities_for_state("Q1581")
        self.assertEqual(out, [])

    def test_a_query_failure_returns_an_empty_list_rather_than_raising(self):
        with patch.object(D, "_sparql", side_effect=urllib.error.URLError("timeout")):
            out = D._cities_for_state("Q1581")
        self.assertEqual(out, [])


class IsLiveTests(unittest.TestCase):
    def test_a_normal_200_is_live(self):
        with patch("tools.discover_wikidata_domains.urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value.status = 200
            self.assertTrue(D._is_live("example.us"))

    def test_a_server_that_answers_with_an_error_under_500_is_still_live(self):
        """A 403 or 404 means a real server is there, unlike a connection
        failure -- so it still counts, even though the request was refused."""
        err = urllib.error.HTTPError("u", 404, "not found", {}, None)
        with patch("tools.discover_wikidata_domains.urllib.request.urlopen",
                    side_effect=err):
            self.assertTrue(D._is_live("example.us"))

    def test_https_failing_falls_back_to_http(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            if req.full_url.startswith("https://"):
                raise OSError("connection refused")
            cm = MagicMock()
            cm.__enter__.return_value.status = 200
            return cm

        with patch("tools.discover_wikidata_domains.urllib.request.urlopen",
                    side_effect=fake_urlopen):
            self.assertTrue(D._is_live("oldtownsite.us"))
        self.assertEqual(calls, ["https://oldtownsite.us/", "http://oldtownsite.us/"])

    def test_both_schemes_failing_is_reported_as_dead(self):
        with patch("tools.discover_wikidata_domains.urllib.request.urlopen",
                    side_effect=OSError("no route to host")):
            self.assertFalse(D._is_live("deadtown.us"))

    def test_a_5xx_error_does_not_count_as_live_on_its_own(self):
        """A 500 is not evidence the domain is a live town site; it should
        still try the http fallback rather than accepting immediately."""
        err = urllib.error.HTTPError("u", 500, "boom", {}, None)
        with patch("tools.discover_wikidata_domains.urllib.request.urlopen",
                    side_effect=err):
            self.assertFalse(D._is_live("brokentown.us"))


class ProcessStateTests(unittest.TestCase):
    def test_only_live_candidates_are_kept_and_shaped_for_the_csv(self):
        candidates = [{"city": "Rocheport", "domain": "rocheportmo.us"},
                      {"city": "Ghost Town", "domain": "ghosttown.example"}]
        with patch.object(D, "_cities_for_state", return_value=candidates), \
             patch.object(D, "_is_live",
                          side_effect=lambda d: d == "rocheportmo.us"):
            out = D._process_state("MO", "Q1581", "2026-09-15", workers=2)
        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertEqual(row["domain"], "rocheportmo.us")
        self.assertEqual(row["state"], "MO")
        self.assertEqual(row["source"], "wikidata")
        self.assertEqual(row["checked_date"], "2026-09-15")

    def test_no_candidates_short_circuits_without_checking_liveness(self):
        with patch.object(D, "_cities_for_state", return_value=[]), \
             patch.object(D, "_is_live") as is_live:
            out = D._process_state("WY", "Q1214", "2026-09-15", workers=2)
        self.assertEqual(out, [])
        is_live.assert_not_called()


if __name__ == "__main__":
    unittest.main()
