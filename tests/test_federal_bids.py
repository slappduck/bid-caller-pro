"""SAM.gov federal opportunities.

Fixtures are trimmed copies of real payloads captured from the live service
on 2026-08-26, so the shapes here are the shapes that actually arrive rather
than shapes invented to make a parser pass.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import federal_bids as fb


# The real Gateway Arch notice, cut down to the fields this module reads.
DETAIL = {
    "data2": {
        "type": "k",
        "naics": [{"code": ["238110"], "type": "primary"}],
        "title": "Gateway Arch NP - Sidewalk Leveling",
        "solicitation": {
            "setAside": "SBA",
            "deadlines": {"response": "2026-09-09T17:00:00-05:00",
                          "responseTz": "America/Chicago"},
        },
        "pointOfContact": [{"type": "primary",
                            "email": "Matthew_Frank@ios.doi.gov",
                            "phone": "5017629927",
                            "fullName": "Frank, Matthew"}],
        "placeOfPerformance": {
            "zip": "63102",
            "city": {"name": "Saint Louis"},
            "state": {"code": "MO", "name": "Missouri"},
            "streetAddress": "11 N 4th St",
        },
        "solicitationNumber": "140P6226Q0005",
    }
}

SEARCH_PUBLIC = {
    "_embedded": {"results": [{
        "_id": "1c77a3137ef64a73a44cb5fb084bc9de",
        "title": "Gateway Arch NP - Sidewalk Leveling",
        "solicitationNumber": "140P6226Q0005",
        "responseDate": "2026-09-09T22:00:00+00:00",
        "responseDateActual": "2026-09-09T17:00:00-05:00",
        "isActive": True,
        "isCanceled": False,
    }]},
    "page": {"totalElements": 24},
}

SEARCH_OFFICIAL = {
    "opportunitiesData": [{
        "noticeId": "1c77a3137ef64a73a44cb5fb084bc9de",
        "title": "Gateway Arch NP - Sidewalk Leveling",
        "solicitationNumber": "140P6226Q0005",
        "responseDeadLine": "2026-09-09T17:00:00-05:00",
        "active": "Yes",
    }]
}


class TradeSelectionTests(unittest.TestCase):
    def test_237990_is_excluded(self):
        """Measured, not assumed: across six states it carried 17 active
        notices and every one that passed a title filter was wrong -- a boat
        ramp, two spill gates, a drainage district."""
        self.assertNotIn("237990", fb.CONCRETE_NAICS)

    def test_the_poured_concrete_code_is_included(self):
        self.assertIn("238110", fb.CONCRETE_NAICS)
        self.assertIn("237310", fb.CONCRETE_NAICS)

    def test_only_the_road_repair_psc_is_used(self):
        """Y1PZ and Z2AZ were measured and rejected -- 15 notices between
        them, none of them concrete work."""
        self.assertEqual(fb.CONCRETE_PSC, ("Z2PZ",))


class SearchUrlTests(unittest.TestCase):
    def test_place_of_performance_not_office_state(self):
        """A contract let from Washington for work at a Missouri fort is a
        Missouri job. `pop_state` filters on where the work is; `state` does
        not, and returned zero."""
        self.assertIn("pop_state=MO", fb.search_url("237310", state="MO"))

    def test_a_key_switches_to_the_documented_api(self):
        url = fb.search_url("237310", state="MO", api_key="abc123")
        self.assertTrue(url.startswith(fb.OFFICIAL_BASE))
        self.assertIn("api_key=abc123", url)
        self.assertIn("ncode=237310", url)

    def test_no_key_uses_the_public_endpoint(self):
        url = fb.search_url("237310", state="MO")
        self.assertTrue(url.startswith(fb.PUBLIC_SEARCH))
        self.assertNotIn("api_key", url)

    def test_psc_and_naics_are_separate_parameters(self):
        self.assertIn("psc=Z2PZ", fb.search_url(psc="Z2PZ"))
        self.assertNotIn("naics=", fb.search_url(psc="Z2PZ"))


class ParseSearchTests(unittest.TestCase):
    def test_public_shape(self):
        rows = fb.parse_search(SEARCH_PUBLIC)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "1c77a3137ef64a73a44cb5fb084bc9de")
        self.assertEqual(rows[0]["solicitation"], "140P6226Q0005")
        self.assertTrue(rows[0]["active"])

    def test_official_shape(self):
        rows = fb.parse_search(SEARCH_OFFICIAL)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "1c77a3137ef64a73a44cb5fb084bc9de")
        self.assertTrue(rows[0]["active"])

    def test_a_cancelled_notice_is_not_active(self):
        payload = {"_embedded": {"results": [
            {"_id": "x", "title": "t", "isActive": True, "isCanceled": True}]}}
        self.assertFalse(fb.parse_search(payload)[0]["active"])

    def test_junk_does_not_raise(self):
        for junk in (None, "", "not json", 42, {}, {"_embedded": {}}):
            self.assertEqual(fb.parse_search(junk), [])


class ParseDetailTests(unittest.TestCase):
    def setUp(self):
        self.d = fb.parse_detail(DETAIL)

    def test_place_of_performance(self):
        self.assertEqual(self.d["city"], "Saint Louis")
        self.assertEqual(self.d["state"], "MO")
        self.assertEqual(self.d["zip"], "63102")
        self.assertEqual(self.d["street"], "11 N 4th St")

    def test_contact_details(self):
        self.assertEqual(self.d["email"], "Matthew_Frank@ios.doi.gov")
        self.assertEqual(self.d["phone"], "5017629927")

    def test_naics_is_unwrapped_from_its_nesting(self):
        """The code arrives as naics[0]["code"][0] -- a list inside a dict
        inside a list."""
        self.assertEqual(self.d["naics"], "238110")

    def test_set_aside_is_kept(self):
        self.assertEqual(self.d["set_aside"], "SBA")

    def test_a_contact_with_no_way_to_reach_them_is_not_a_contact(self):
        payload = {"data2": dict(DETAIL["data2"],
                                 pointOfContact=[{"fullName": "A Name"}])}
        self.assertEqual(fb.parse_detail(payload)["email"], "")
        self.assertEqual(fb.parse_detail(payload)["contact"], "")

    def test_junk_does_not_raise(self):
        """Whatever comes back, it must not throw and must not claim a
        location -- to_bid drops anything with no city or state, so an empty
        record is inert rather than dangerous."""
        for junk in (None, "", "not json", 42, {}, {"data2": None}):
            got = fb.parse_detail(junk)
            self.assertIsInstance(got, dict)
            self.assertFalse(got.get("city"))
            self.assertFalse(got.get("state"))


class ToBidTests(unittest.TestCase):
    def setUp(self):
        self.row = fb.parse_search(SEARCH_PUBLIC)[0]
        self.bid = fb.to_bid(self.row, fb.parse_detail(DETAIL))

    def test_links_to_the_human_page_not_the_api(self):
        self.assertEqual(
            self.bid["url"],
            "https://sam.gov/opp/1c77a3137ef64a73a44cb5fb084bc9de/view")

    def test_name_is_turned_around_for_a_human(self):
        """"Frank, Matthew" is a filing convention, not how you greet
        somebody on the phone."""
        self.assertEqual(self.bid["contact"], "Matthew Frank")

    def test_the_set_aside_is_surfaced(self):
        """It is the difference between a job a small contractor can win and
        one they cannot."""
        self.assertIn("SBA set-aside", self.bid["scope"])

    def test_source_marks_it_federal(self):
        self.assertEqual(self.bid["source"], "SAM.gov")

    def test_deadline_prefers_the_detail_record(self):
        self.assertTrue(self.bid["deadline"].startswith("2026-09-09"))

    def test_an_unplaceable_notice_is_dropped(self):
        """A federal job with no place of performance cannot be put on a map
        or measured against a radius, so it is not a bid we can show."""
        self.assertIsNone(fb.to_bid(self.row, {"city": "", "state": ""}))
        self.assertIsNone(fb.to_bid(self.row, {}))

    def test_a_row_with_no_id_is_dropped(self):
        self.assertIsNone(fb.to_bid({"title": "x"}, fb.parse_detail(DETAIL)))


if __name__ == "__main__":
    unittest.main()
