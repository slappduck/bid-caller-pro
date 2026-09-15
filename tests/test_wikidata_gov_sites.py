"""Harvesting county/municipal candidate domains from Wikidata for the
nine-state sales region.

Everything this writes is a CANDIDATE, never a directory entry -- Wikidata
carried a Kentucky town pointing at an Ohio city's website in the very first
sample pulled for this tool, which is why data/wikidata_candidates.csv exists
as a separate, unverified file that discover_bid_portals.py has to probe
before anything in it is trusted.

Three behaviors matter enough to pin here:
  * obvious non-government hosts (facebook, wikipedia, ...) never make it
    into the candidate list at all, even though Wikidata will happily hand
    them back as a "website";
  * a domain already known (in the live directory, or already promoted from
    an earlier Wikidata run) is not re-emitted, so the verifier is never
    asked to re-probe several thousand pages it has already seen;
  * a run scoped to one state must not truncate a national candidate file
    down to just that state -- rows for the states not being touched have to
    survive the rewrite.

The SPARQL endpoint is called through `curl` via subprocess in this file
(not urllib, unlike its sibling tools), so subprocess.run is what gets
stubbed here -- nothing makes a real request to query.wikidata.org.
"""
import csv
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import wikidata_gov_sites as W  # noqa: E402


def _binding(place, url):
    return {"placeLabel": {"value": place}, "website": {"value": url}}


def _curl_result(bindings):
    proc = MagicMock()
    proc.stdout = ('{"results": {"bindings": %s}}' %
                   __import__("json").dumps(bindings))
    return proc


class SparqlTests(unittest.TestCase):
    def test_a_successful_query_returns_its_bindings(self):
        rows = [_binding("Rocheport", "https://rocheportmo.us")]
        with patch.object(W.subprocess, "run", return_value=_curl_result(rows)):
            out = W._sparql("SELECT * WHERE {}")
        self.assertEqual(out, rows)

    def test_a_malformed_response_is_retried_then_gives_up_empty(self):
        bad = MagicMock()
        bad.stdout = "502 Bad Gateway (not JSON)"
        with patch.object(W.subprocess, "run", return_value=bad) as run, \
             patch.object(W.time, "sleep"):
            out = W._sparql("SELECT * WHERE {}", tries=3)
        self.assertEqual(out, [])
        self.assertEqual(run.call_count, 3)


class HarvestTests(unittest.TestCase):
    def test_junk_hosts_are_filtered_out(self):
        rows = [
            _binding("Springfield", "https://www.facebook.com/springfieldmo"),
            _binding("Springfield", "https://en.wikipedia.org/wiki/Springfield"),
            _binding("Springfield", "https://springfieldmo.gov"),
        ]
        with patch.object(W, "_sparql", return_value=rows):
            found = W._harvest("MO", "Q1581")
        self.assertEqual(found, {"springfieldmo.gov": "Springfield"})

    def test_www_is_stripped_from_the_host(self):
        rows = [_binding("Aurora", "https://www.aurora-cityhall.org")]
        with patch.object(W, "_sparql", return_value=rows):
            found = W._harvest("MO", "Q1581")
        self.assertEqual(found, {"aurora-cityhall.org": "Aurora"})

    def test_an_empty_combined_query_falls_back_to_the_two_halves(self):
        """The combined P131 UNION P131/P131 query can time out in the
        biggest states; an empty result means try the two halves apart
        instead of silently harvesting nothing."""
        half_rows = [_binding("Houston", "https://houstontx.gov")]
        calls = []

        def fake_sparql(query):
            calls.append(query)
            # combined query (call 1) times out empty; both halves (calls
            # 2-3) are tried and only the first turns up a row.
            return [] if len(calls) != 2 else half_rows
        with patch.object(W, "_sparql", side_effect=fake_sparql):
            found = W._harvest("TX", "Q1439")
        self.assertEqual(found, {"houstontx.gov": "Houston"})
        self.assertEqual(len(calls), 3)  # combined + both halves


class KnownDomainsTests(unittest.TestCase):
    def test_a_missing_file_contributes_nothing(self):
        self.assertEqual(W._known_domains("/no/such/file.csv"), set())

    def test_domains_are_read_and_www_stripped(self, ):
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "directory.csv")
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["domain", "city"])
            w.writerow(["www.example.gov", "Example"])
            w.writerow(["plainsite.org", "Plains"])
        self.assertEqual(W._known_domains(path),
                         {"example.gov", "plainsite.org"})


class ExistingTests(unittest.TestCase):
    def test_rows_for_states_not_in_this_run_are_kept(self):
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "candidates.csv")
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["state", "place", "domain"])
            w.writerow(["KS", "Wichita", "wichita.gov"])
            w.writerow(["MO", "Rocheport", "rocheportmo.us"])
        kept = W._existing(path, skip_states={"MO"})
        self.assertEqual(kept, [("KS", "Wichita", "wichita.gov")])

    def test_a_missing_file_returns_an_empty_list(self):
        self.assertEqual(W._existing("/no/such/file.csv", {"MO"}), [])


class MainRegionRunTests(unittest.TestCase):
    """A run scoped to one state must not truncate the national candidate
    file down to just that state."""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.out = os.path.join(self.tmpdir, "candidates.csv")
        with open(self.out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["state", "place", "domain"])
            w.writerow(["KS", "Wichita", "wichita.gov"])

    def test_other_states_rows_survive_a_single_state_run(self):
        with patch.object(W, "_known_domains", return_value=set()), \
             patch.object(W, "_harvest",
                          return_value={"rocheportmo.us": "Rocheport"}):
            W.main(["MO", "--out", self.out])
        with open(self.out, newline="") as fh:
            rows = {(r["state"], r["domain"]) for r in csv.DictReader(fh)}
        self.assertIn(("KS", "wichita.gov"), rows)
        self.assertIn(("MO", "rocheportmo.us"), rows)

    def test_an_already_known_domain_is_not_re_emitted_as_new(self):
        with patch.object(W, "_known_domains",
                          return_value={"rocheportmo.us"}), \
             patch.object(W, "_harvest",
                          return_value={"rocheportmo.us": "Rocheport",
                                        "newtownmo.us": "New Town"}):
            W.main(["MO", "--out", self.out])
        with open(self.out, newline="") as fh:
            domains = {r["domain"] for r in csv.DictReader(fh)}
        self.assertIn("newtownmo.us", domains)
        self.assertNotIn("rocheportmo.us", domains)


if __name__ == "__main__":
    unittest.main()
