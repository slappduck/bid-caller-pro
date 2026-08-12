"""The recall benchmark: does the real reader pipeline find real bids?

data/recall_fixtures/springfield_civicplus.html is real CivicPlus markup
(captured live from springfieldmo.gov/Bids.aspx) containing the two bids
SEARCH_PLAN.md documented as ground truth -- ADA Improvement Project and
Mt. Vernon & Miller Sidewalks -- reconstructed in that exact template
alongside real current unrelated postings (ice machine rental, a skate shop
concessionaire). Both ground-truth bids had already closed by the time this
was written, which is exactly why this is a fixture instead of a live check:
a recall test tied to bids staying open goes stale within weeks for reasons
that have nothing to do with whether the scanner works. This locks in that
the parser and relevance filter keep recognizing them regardless.

See tools/recall_check.py for the standalone CLI version of this same check,
runnable against any live source on demand.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_sources  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "recall_fixtures", "springfield_civicplus.html")


class SpringfieldRecallTests(unittest.TestCase):
    def setUp(self):
        with open(FIXTURE, encoding="utf-8") as f:
            self.html = f.read()
        self.rows = bid_sources.parse_civicplus_html(
            self.html, base_url="https://www.springfieldmo.gov")
        self.relevant_titles = {
            r["title"] for r in self.rows
            if bid_sources.looks_relevant(r["title"], r.get("scope"))
        }

    def test_the_reader_finds_every_posting_on_the_page(self):
        # Recall starts at the parser: a posting the parser never sees can't
        # survive any stage after it.
        self.assertEqual(len(self.rows), 4)

    def test_the_ada_improvement_project_is_found_and_kept(self):
        self.assertTrue(any("ADA IMPROVEMENT PROJECT" in t for t in self.relevant_titles))

    def test_mt_vernon_and_miller_sidewalks_is_found_and_kept(self):
        self.assertTrue(any("MT. VERNON & MILLER SIDEWALKS" in t for t in self.relevant_titles))

    def test_unrelated_postings_are_correctly_filtered_out(self):
        # Precision matters as much as recall (Phase 3) -- these two are real
        # current Springfield postings with nothing to do with this trade.
        unrelated = {"RENTAL OF ICE MACHINES WITH STORAGE BINS AND MAINTENANCE",
                     "SKATE AND PRO SHOP CONCESSIONAIRE"}
        self.assertEqual(self.relevant_titles & unrelated, set())

    def test_recall_is_100_percent_against_the_known_bids(self):
        expected = {"ADA IMPROVEMENT PROJECT", "MT. VERNON & MILLER SIDEWALKS"}
        found = {e for e in expected
                 if any(e in t for t in self.relevant_titles)}
        self.assertEqual(found, expected,
                         f"recall {len(found)}/{len(expected)} — missing: {expected - found}")


if __name__ == "__main__":
    unittest.main()
