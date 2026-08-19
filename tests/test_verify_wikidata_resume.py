"""Tests for the resume path in tools/verify_wikidata_candidates.py.

A national candidate set is thousands of domains and every one costs real
network round trips, so an interrupted run must not start over. Two things
have to hold: already-probed domains are skipped, and the rows they produced
are still in the file afterwards -- an append that dropped the header, or a
resume that reopened in "w", would silently throw away hours of probing.
"""
import csv
import importlib.util
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location(
    "verify_wikidata_candidates",
    os.path.join(ROOT, "tools", "verify_wikidata_candidates.py"))
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.src = os.path.join(self.dir.name, "candidates.csv")
        self.dst = os.path.join(self.dir.name, "verified.csv")
        self.probed = []

        def fake_probe(row):
            self.probed.append(row["domain"])
            return dict(row, status="found", owns="domain",
                        bid_url="https://" + row["domain"] + "/bids",
                        relevant="no")

        self._real_probe = verify.probe
        verify.probe = fake_probe
        self.addCleanup(lambda: setattr(verify, "probe", self._real_probe))

    def _candidates(self, domains):
        with open(self.src, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["state", "place", "domain"])
            for d in domains:
                w.writerow(["MO", d.split(".")[0].title(), d])

    def _run(self, *extra):
        argv = sys.argv
        sys.argv = ["verify", "--in", self.src, "--out", self.dst,
                    "--workers", "2", *extra]
        try:
            verify.main()
        finally:
            sys.argv = argv

    def _rows(self):
        with open(self.dst, newline="") as fh:
            return list(csv.DictReader(fh))

    def test_a_fresh_run_probes_everything(self):
        self._candidates(["a.org", "b.org", "c.org"])
        self._run()
        self.assertEqual(sorted(self.probed), ["a.org", "b.org", "c.org"])
        self.assertEqual(len(self._rows()), 3)

    def test_resume_skips_what_was_already_probed(self):
        self._candidates(["a.org", "b.org"])
        self._run()
        self.probed.clear()
        self._candidates(["a.org", "b.org", "c.org"])
        self._run("--resume")
        self.assertEqual(self.probed, ["c.org"])

    def test_resume_keeps_the_earlier_rows(self):
        self._candidates(["a.org", "b.org"])
        self._run()
        self._candidates(["a.org", "b.org", "c.org"])
        self._run("--resume")
        domains = sorted(r["domain"] for r in self._rows())
        self.assertEqual(domains, ["a.org", "b.org", "c.org"])

    def test_resume_does_not_write_a_second_header(self):
        self._candidates(["a.org"])
        self._run()
        self._candidates(["a.org", "b.org"])
        self._run("--resume")
        with open(self.dst) as fh:
            body = fh.read()
        self.assertEqual(body.count("state,place,domain"), 1)

    def test_resume_with_nothing_left_leaves_the_file_alone(self):
        self._candidates(["a.org"])
        self._run()
        with open(self.dst) as fh:
            before = fh.read()
        self._run("--resume")
        with open(self.dst) as fh:
            self.assertEqual(fh.read(), before)

    def test_without_resume_the_file_is_rewritten_not_appended(self):
        """A plain re-run is a fresh start; it must not stack duplicate rows
        on top of the previous run's output."""
        self._candidates(["a.org"])
        self._run()
        self._run()
        self.assertEqual(len(self._rows()), 1)


class OwnershipTests(unittest.TestCase):
    """The check that keeps a crowd-sourced wrong website out of the
    directory -- a Kentucky town pointing at an Ohio city's site turned up in
    the first sample."""

    def test_name_in_the_domain_counts(self):
        self.assertEqual(verify._owns("Aurora", "aurora-cityhall.org", ""),
                         "domain")

    def test_name_on_the_homepage_counts_when_the_domain_hides_it(self):
        self.assertEqual(
            verify._owns("Appleton City", "acmogov.com",
                         "<h1>Welcome to Appleton City</h1>"),
            "homepage")

    def test_neither_means_we_cannot_claim_it(self):
        self.assertEqual(verify._owns("Aurora", "someothertown.org",
                                      "<h1>Springfield</h1>"), "")


class BidPageRecognitionTests(unittest.TestCase):
    """What counts as a bid page, and why the bar differs by how the URL was
    reached. A guessed path like /Bids.aspx is itself evidence; a link
    followed off a homepage is not, so it has to say something only a real
    solicitation page says."""

    def setUp(self):
        self.served = {}
        self._orig_get = verify._get
        verify._get = lambda url, **kw: self.served.get(url)
        self.addCleanup(lambda: setattr(verify, "_get", self._orig_get))

    def test_a_real_solicitation_page_is_recognised_either_way(self):
        self.served["u"] = "Invitation to Bid -- 2026 Sidewalk Program"
        self.assertIsNotNone(verify._bid_page_at("u"))
        self.assertIsNotNone(verify._bid_page_at("u", strict=True))

    def test_a_business_improvement_district_page_is_never_a_bid_page(self):
        """cityofselma.com/.../downtown_selma_bid.php passed the loose test:
        a BID page is dense with the word and is not a solicitation."""
        self.served["u"] = ("Downtown Selma BID -- the Business Improvement "
                            "District supports local merchants.")
        self.assertIsNone(verify._bid_page_at("u"))
        self.assertIsNone(verify._bid_page_at("u", strict=True))

    def test_an_incidental_mention_passes_loose_but_not_strict(self):
        self.served["u"] = "Council forbidden to discuss the bid informally."
        self.assertIsNotNone(verify._bid_page_at("u"))
        self.assertIsNone(verify._bid_page_at("u", strict=True))

    def test_a_page_that_never_arrived_is_not_a_bid_page(self):
        self.assertIsNone(verify._bid_page_at("missing"))

    def test_relevance_is_reported_but_does_not_gate_the_find(self):
        """~8% of live bid pages carry concrete work at any moment; the page
        belongs in the directory either way."""
        self.served["u"] = "Invitation to Bid -- roof replacement"
        hit = verify._bid_page_at("u", strict=True)
        self.assertEqual(hit["relevant"], "no")
        self.assertEqual(hit["bid_url"], "u")


if __name__ == "__main__":
    unittest.main()
