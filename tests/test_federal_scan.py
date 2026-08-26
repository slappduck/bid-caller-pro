"""The federal path inside the scan.

Federal bids were already wired up before this and could never have produced
one: the endpoint 404s, the relevance test used keywords real federal titles
do not contain, and only the centre state was ever asked. These pin each of
those so they cannot come back.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import federal_bids
import license_server as ls


class EndpointTests(unittest.TestCase):
    def test_the_default_endpoint_is_the_live_one(self):
        """api.sam.gov/prod/... answers 404 on every path, so the old default
        could not have worked with any key. api.data.gov/sam/... is live."""
        self.assertIn("api.data.gov", ls.SAM_SEARCH_URL)
        self.assertNotIn("api.sam.gov/prod", ls.SAM_SEARCH_URL)


class RelevanceTests(unittest.TestCase):
    """NAICS states the trade; a title only hints at it."""

    def test_naics_alone_admits_a_job_no_keyword_would(self):
        for title in ("Whiteman AFB - FY27 Airfield Pavement",
                      "XTLF 28-1017 Renovate B174",
                      "Repair Building 174"):
            self.assertTrue(
                ls._is_construction({"title": title, "naicsCode": "238110"}),
                title)

    def test_a_wrong_naics_is_rejected_however_the_title_reads(self):
        """The code is an assertion. If it says janitorial, the job is
        janitorial, whatever words the title happens to contain."""
        self.assertFalse(ls._is_construction(
            {"title": "Concrete sidewalk replacement", "naicsCode": "561720"}))

    def test_keywords_still_decide_a_notice_with_no_code(self):
        self.assertTrue(ls._is_construction({"title": "Sidewalk Replacement"}))
        self.assertTrue(ls._is_construction({"title": "Asphalt Paving FY26"}))
        self.assertFalse(ls._is_construction({"title": "Janitorial Services"}))

    def test_the_keyword_list_covers_how_federal_titles_are_written(self):
        """Three real jobs found in one probe matched none of the original
        terms; "pavement" was absent from a concrete product's keyword list."""
        for term in ("pavement", "paving", "resurfac"):
            self.assertIn(term, ls.CONSTRUCTION_KEYWORDS)

    def test_naics_is_read_in_either_transports_spelling(self):
        nested = {"title": "x", "naics": [{"code": ["237310"]}]}
        self.assertEqual(ls._opp_naics(nested), "237310")
        self.assertEqual(ls._opp_naics({"naicsCode": "237310"}), "237310")
        self.assertEqual(ls._opp_naics({}), "")


class RadiusTests(unittest.TestCase):
    def test_every_state_the_radius_touches_is_asked(self):
        """The old block asked for centre["state"] alone -- the same bug
        _place_bid documents for cities. A 125-mile circle is usually several
        states wide, and seeing across the line is why that radius exists."""
        center = {"city": "Kansas City", "state": "MO",
                  "lat": 39.0997, "lon": -94.5786}
        states = ls._federal_states(center, 125)
        self.assertIn("MO", states)
        self.assertIn("KS", states)
        self.assertGreater(len(states), 1)

    def test_a_tight_radius_stays_in_one_state(self):
        center = {"city": "Springfield", "state": "MO",
                  "lat": 37.2090, "lon": -93.2923}
        self.assertEqual(ls._federal_states(center, 10), ["MO"])


class StructureTests(unittest.TestCase):
    """Structural, because these are the properties that keep the source
    cheap and honest, and they are easy to lose in a refactor."""

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "license_server.py"), encoding="utf-8") as fh:
            src = fh.read()
        start = src.index("def _federal_public(")
        self.public = src[start:src.index("\ndef ", start + 10)]
        start = src.index("def _run_federal_sources(")
        self.runner = src[start:src.index("\ndef ", start + 10)]

    def test_amendments_are_collapsed_before_any_detail_fetch(self):
        """A solicitation reappears with each amendment and each is the same
        job. De-duplicating after the fetch would spend the budget on
        duplicates -- Fort Leavenworth's asphalt job appeared three times."""
        dedupe = self.public.index("federal_amendment_collapsed")
        fetch = self.public.index("def _detail(")
        self.assertLess(dedupe, fetch)

    def test_closed_notices_are_dropped(self):
        """"Active" at SAM means the notice is live, not that its response
        date is ahead: 29 of 60 probe candidates were already past theirs."""
        self.assertIn("federal_already_closed", self.runner)
        self.assertIn("_is_open_bid(bid)", self.runner)

    def test_psc_hits_are_not_trusted_on_sight(self):
        """PSC is broader than the trade, so a PSC hit goes through the
        normal relevance filter; a NAICS hit does not need to."""
        self.assertIn("federal_psc_off_trade", self.public)
        self.assertIn("looks_relevant", self.public)

    def test_no_extraction_call_is_made(self):
        """The whole point of this source is that the trade, the location and
        the contact are all stated. Paying an AI to re-read them would be
        pure waste."""
        self.assertNotIn("_ai_extract", self.public)
        self.assertNotIn("_ai_extract", self.runner)

    def test_the_feature_is_not_dark_without_a_key(self):
        self.assertIn("_federal_public", self.runner)
        self.assertIn("SAM_API_KEY", self.runner)


if __name__ == "__main__":
    unittest.main()
