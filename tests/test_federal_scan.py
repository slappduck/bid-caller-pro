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


class KeyedTransportTests(unittest.TestCase):
    """The keyed path is what runs once SAM_API_KEY is set, and it cannot be
    exercised against the live service without a key -- so it is exercised
    here against the documented payload shape instead. Without this the
    transport Josh will actually use is the one nothing has ever run."""

    # One row in api.data.gov's `opportunitiesData` shape, trimmed to the
    # fields _normalize_opp reads.
    OPP = {
        "noticeId": "1c77a3137ef64a73a44cb5fb084bc9de",
        "title": "Gateway Arch NP - Sidewalk Leveling",
        "solicitationNumber": "140P6226Q0005",
        "responseDeadLine": "2099-09-09T17:00:00-05:00",
        "active": "Yes",
        "naicsCode": "238110",
        "fullParentPathName": "INTERIOR.NATIONAL PARK SERVICE",
        "pointOfContact": [{"fullName": "Frank, Matthew",
                            "email": "Matthew_Frank@ios.doi.gov",
                            "phone": "5017629927"}],
        "placeOfPerformance": {"city": {"name": "Saint Louis"},
                               "state": {"code": "MO"}, "zip": "63102"},
        "uiLink": "https://sam.gov/opp/1c77a3137ef64a73a44cb5fb084bc9de/view",
    }

    def setUp(self):
        self.calls = []
        self._real_fetch = ls._sam_fetch
        self._real_key = ls.SAM_API_KEY

        def fake_fetch(state):
            self.calls.append(state)
            return [self.OPP, {"noticeId": "b" * 32, "title": "Janitorial "
                               "Services", "active": "Yes",
                               "naicsCode": "561720"}]
        ls._sam_fetch = fake_fetch
        ls.SAM_API_KEY = "test-key"

    def tearDown(self):
        ls._sam_fetch = self._real_fetch
        ls.SAM_API_KEY = self._real_key

    def test_it_asks_every_state_in_the_radius(self):
        ls._federal_keyed(["MO", "KS", "IA"], {})
        self.assertEqual(self.calls, ["MO", "KS", "IA"])

    def test_it_keeps_our_trade_and_drops_the_rest(self):
        bids = ls._federal_keyed(["MO"], {})
        self.assertEqual(len(bids), 1)
        self.assertEqual(bids[0]["title"], "Gateway Arch NP - Sidewalk Leveling")

    def test_the_bid_carries_location_and_contact(self):
        bid = ls._federal_keyed(["MO"], {})[0]
        self.assertEqual(bid["city"], "Saint Louis")
        self.assertEqual(bid["state"], "MO")
        self.assertEqual(bid["phone"], "5017629927")
        self.assertEqual(bid["email"], "Matthew_Frank@ios.doi.gov")

    def test_a_dead_state_does_not_sink_the_others(self):
        def half_broken(state):
            if state == "KS":
                raise RuntimeError("SAM is down")
            return [self.OPP]
        ls._sam_fetch = half_broken
        stats = {}
        bids = ls._federal_keyed(["MO", "KS", "IA"], stats)
        self.assertEqual(len(bids), 2)
        self.assertEqual(stats.get("federal_search_error"), 1)

    def test_the_runner_places_a_keyed_bid_end_to_end(self):
        center = {"city": "Saint Louis", "state": "MO",
                  "lat": 38.6270, "lon": -90.1994}
        grouped, coords, stats = {}, {}, {}
        placed = ls._run_federal_sources(center, 50, grouped, {}, coords, stats)
        self.assertEqual(placed, 1)
        bids = [b for v in grouped.values() for b in v]
        self.assertEqual(len(bids), 1)
        self.assertTrue(bids[0].get("phone"))
        self.assertTrue(bids[0]["url"].startswith("https://sam.gov/opp/"))

    def test_a_closed_notice_is_dropped_even_when_marked_active(self):
        """"Active" at SAM means the notice is live, not that its response
        date is ahead."""
        past = dict(self.OPP, responseDeadLine="2020-01-01T17:00:00-05:00")
        ls._sam_fetch = lambda state: [past]
        # Springfield at 25 miles stays inside Missouri, so the stub is asked
        # exactly once and the count is unambiguous.
        center = {"city": "Springfield", "state": "MO",
                  "lat": 37.2090, "lon": -93.2923}
        stats = {}
        placed = ls._run_federal_sources(center, 25, {}, {}, {}, stats)
        self.assertEqual(placed, 0)
        self.assertEqual(stats.get("federal_already_closed"), 1)
