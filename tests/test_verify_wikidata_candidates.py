"""Probing a Wikidata candidate domain end to end.

tests/test_verify_wikidata_resume.py already covers the resume/append path
and the two building blocks -- _owns() and _bid_page_at() -- in isolation.
What it does not cover is probe() itself: the function that wires those
pieces together for every row is entirely replaced there with a fake. These
tests exercise the real probe(), because that is where the file's own
priority order actually lives -- try https before http, try every guessed
CANDIDATE_BID_PATH before ever following a link off the homepage, and stop
at the first hit rather than reporting the last one.

The network is stubbed by replacing verify._get, matching the pattern the
existing wikidata tests already use for this module.
"""
import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import verify_wikidata_candidates as V  # noqa: E402


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.served = {}
        self.requested = []
        self._orig_get = V._get

        def fake_get(url, **kw):
            self.requested.append(url)
            return self.served.get(url)

        V._get = fake_get
        self.addCleanup(lambda: setattr(V, "_get", self._orig_get))

    def row(self, **over):
        base = {"state": "MO", "place": "Boone", "domain": "boone.mo.gov"}
        base.update(over)
        return base

    def test_https_is_tried_before_http(self):
        self.served["https://boone.mo.gov"] = "<h1>Boone</h1>"
        got = V.probe(self.row())
        self.assertEqual(got["status"], "no_bid_page")
        self.assertIn("https://boone.mo.gov", self.requested)
        self.assertNotIn("http://boone.mo.gov", self.requested)

    def test_http_is_tried_when_https_fails(self):
        self.served["http://boone.mo.gov"] = "<h1>Boone</h1>"
        got = V.probe(self.row())
        self.assertEqual(got["status"], "no_bid_page")
        self.assertIn("http://boone.mo.gov", self.requested)

    def test_neither_scheme_reachable_is_reported_unreachable(self):
        got = V.probe(self.row())
        self.assertEqual(got["status"], "unreachable")
        self.assertEqual(got["owns"], "")
        self.assertEqual(got["bid_url"], "")

    def test_a_reachable_site_that_is_not_this_town_is_never_promoted(self):
        """A Kentucky town pointing at an Ohio city's site: reachable, but
        nothing ties it to the place this row claims it is."""
        row = self.row(domain="acmegov.com")
        self.served["https://acmegov.com"] = "<h1>Springfield</h1>"
        got = V.probe(row)
        self.assertEqual(got["status"], "not_this_town")
        self.assertEqual(got["owns"], "")
        self.assertEqual(got["bid_url"], "")

    def test_a_candidate_path_hit_stops_the_search_there(self):
        """The first CANDIDATE_BID_PATHS hit wins; later paths (and the
        homepage-link fallback) must never even be requested."""
        self.served["https://boone.mo.gov"] = "<h1>Boone</h1>"
        first_path = V.bid_sources.CANDIDATE_BID_PATHS[0]
        self.served["https://boone.mo.gov" + first_path] = \
            "Invitation to Bid -- sidewalk program"
        got = V.probe(self.row())
        self.assertEqual(got["status"], "found")
        self.assertEqual(got["bid_url"], "https://boone.mo.gov" + first_path)
        later_path = "https://boone.mo.gov" + V.bid_sources.CANDIDATE_BID_PATHS[-1]
        self.assertNotIn(later_path, self.requested)

    def test_a_later_candidate_path_is_reached_when_earlier_ones_miss(self):
        self.served["https://boone.mo.gov"] = "<h1>Boone</h1>"
        last_path = V.bid_sources.CANDIDATE_BID_PATHS[-1]
        self.served["https://boone.mo.gov" + last_path] = \
            "Request for proposal: paving"
        got = V.probe(self.row())
        self.assertEqual(got["status"], "found")
        self.assertEqual(got["bid_url"], "https://boone.mo.gov" + last_path)

    def test_no_guessed_path_hits_falls_through_to_a_homepage_link(self):
        """Every guessed path misses, so the tool follows a link a real
        visitor would click -- found off the homepage itself."""
        home = '<a href="/procurement/rfps">Current Bids</a>'
        self.served["https://boone.mo.gov"] = home
        self.served["https://boone.mo.gov/procurement/rfps"] = \
            "Invitation to Bid -- 2026 sidewalk program"
        got = V.probe(self.row())
        self.assertEqual(got["status"], "found")
        self.assertEqual(got["bid_url"], "https://boone.mo.gov/procurement/rfps")

    def test_nothing_found_anywhere_is_no_bid_page_not_unreachable(self):
        """The site is real and belongs to the town; it simply has no bid
        page right now. That must be reported differently from a site that
        never answered at all."""
        self.served["https://boone.mo.gov"] = "<h1>Boone</h1>"
        got = V.probe(self.row())
        self.assertEqual(got["status"], "no_bid_page")
        self.assertEqual(got["owns"], "domain")
        self.assertEqual(got["bid_url"], "")

    def test_relevance_is_carried_through_from_the_found_page(self):
        self.served["https://boone.mo.gov"] = "<h1>Boone</h1>"
        first_path = V.bid_sources.CANDIDATE_BID_PATHS[0]
        self.served["https://boone.mo.gov" + first_path] = (
            "Invitation to Bid -- sidewalk and curb ramp replacement")
        got = V.probe(self.row())
        self.assertEqual(got["relevant"], "yes")


class AlreadyDoneTests(unittest.TestCase):
    def test_a_missing_output_file_means_nothing_is_done_yet(self):
        self.assertEqual(V._already_done("/no/such/file.csv"), {})

    def test_domains_with_no_output_row_are_skipped_not_keyed_on_empty(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "verified.csv")
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(V.FIELDS)
            w.writerow(["MO", "Boone", "boone.mo.gov", "found", "domain",
                        "https://boone.mo.gov/bids", "no"])
            w.writerow(["MO", "", "", "unreachable", "", "", ""])
        done = V._already_done(path)
        self.assertEqual(set(done), {"boone.mo.gov"})
        self.assertEqual(done["boone.mo.gov"]["status"], "found")


class MainCliTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.src = os.path.join(self.dir, "candidates.csv")
        self.dst = os.path.join(self.dir, "verified.csv")
        self._orig_probe = V.probe
        self.addCleanup(lambda: setattr(V, "probe", self._orig_probe))

    def _write_src(self, domains):
        with open(self.src, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["state", "place", "domain"])
            for d in domains:
                w.writerow(["MO", d, d])

    def _run(self, *extra):
        old = sys.argv
        sys.argv = ["verify", "--in", self.src, "--out", self.dst,
                    "--workers", "2", *extra]
        try:
            V.main()
        finally:
            sys.argv = old

    def test_limit_caps_how_many_rows_are_probed(self):
        seen = []
        V.probe = lambda row: (seen.append(row["domain"]) or
                                dict(row, status="found", owns="domain",
                                     bid_url="x", relevant="no"))
        self._write_src(["a.org", "b.org", "c.org"])
        self._run("--limit", "1")
        self.assertEqual(len(seen), 1)

    def test_no_rows_left_after_resume_writes_nothing_new(self):
        V.probe = lambda row: dict(row, status="found", owns="domain",
                                    bid_url="x", relevant="no")
        self._write_src(["a.org"])
        self._run()
        with open(self.dst) as f:
            before = f.read()
        self._run("--resume")
        with open(self.dst) as f:
            self.assertEqual(f.read(), before)


if __name__ == "__main__":
    unittest.main()
