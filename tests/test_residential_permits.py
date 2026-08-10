"""Offline tests for residential_permits.py and the /residential-leads
endpoint in license_server.py. Network calls are mocked -- a separate live
smoke test against the real Austin API was run manually during development
(see session notes); this suite must stay runnable with no network."""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import residential_permits as rp
import license_server as ls

_SAMPLE_ROW = {
    "permit_number": "2026-037320 DS",
    "permit_type_desc": "Driveway / Sidewalks",
    "permit_location": "2018 FORD ST",
    "description": "Construct new driveway approach of 20.4 FT.",
    "issue_date": "2026-07-31T00:00:00.000",
    "status_current": "Active",
    "contractor_company_name": "Canedo Builders",
    "contractor_full_name": "Guido Macouzet",
    "contractor_trade": "General Contractor",
    "contractor_phone": "5123735885",
    "original_zip": "78704",
    "latitude": "30.25463834",
    "longitude": "-97.77418547",
    "link": {"url": "https://abc.austintexas.gov/web/permit/x"},
}

_SAMPLE_ROW_NO_COORDS = {**_SAMPLE_ROW, "permit_number": "2026-084793 DS"}
del _SAMPLE_ROW_NO_COORDS["latitude"]
del _SAMPLE_ROW_NO_COORDS["longitude"]


def _mock_response(payload):
    import json
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
    return cm


class HasSourceTests(unittest.TestCase):
    def test_known_city_covered(self):
        self.assertTrue(rp.has_source("Austin", "TX"))

    def test_second_known_city_covered(self):
        self.assertTrue(rp.has_source("Cambridge", "MA"))

    def test_case_and_whitespace_insensitive(self):
        self.assertTrue(rp.has_source("  austin ", "tx"))

    def test_unknown_city_not_covered(self):
        self.assertFalse(rp.has_source("Springfield", "MO"))


class CambridgeParserTests(unittest.TestCase):
    _SAMPLE = {
        "id": "1206441",
        "full_address": "87-101 BLANCHARD RD, Unit CCC, Cambridge, MA",
        "latitude": "42.394072",
        "longitude": "-71.158005",
        "status": "Complete",
        "applicant_submit_date": "2026-04-14T00:00:00.000",
        "driveway_width": "23",
        "permit_type": "Curb Cut",
        "applicant_name": "Patrick Conte",
    }

    def test_parses_correctly(self):
        lead = rp._cambridge_parser(self._SAMPLE)
        self.assertEqual(lead["address"], "87-101 BLANCHARD RD, Unit CCC, Cambridge, MA")
        self.assertEqual(lead["permit_type"], "Curb Cut")
        self.assertIn("23 ft wide", lead["description"])
        self.assertEqual(lead["contractor_name"], "Patrick Conte")
        self.assertAlmostEqual(lead["lat"], 42.394072)

    def test_uses_a_longer_lookback_than_austin(self):
        # Cambridge is much lower-volume -- the whole reason it needs its
        # own configured "days" instead of sharing Austin's 45-day default.
        self.assertGreater(rp.SOURCES[("cambridge", "MA")]["days"],
                           rp.SOURCES[("austin", "TX")]["days"])


class ClassifyLeadTests(unittest.TestCase):
    def test_general_contractor_is_builder_lead(self):
        self.assertEqual(rp._classify_lead("General Contractor", "Highland Homes"), "builder")

    def test_no_contractor_at_all_is_open_lead(self):
        self.assertEqual(rp._classify_lead("", ""), "open")
        self.assertEqual(rp._classify_lead(None, None), "open")

    def test_named_individual_with_no_trade_is_open_lead(self):
        # Cambridge-style: an applicant name but no trade field at all reads
        # as an individual/owner permit, not a company.
        self.assertEqual(rp._classify_lead("", "Patrick Conte"), "open")

    def test_concrete_trade_already_listed_is_taken(self):
        self.assertEqual(rp._classify_lead("Concrete Contractor", "ABC Concrete"), "taken")
        self.assertEqual(rp._classify_lead("Paving", "XYZ Paving Co"), "taken")

    def test_other_trade_is_unknown(self):
        self.assertEqual(rp._classify_lead("Electrical", "Some Electrician"), "unknown")


class FetchLeadsTests(unittest.TestCase):
    def test_uncovered_city_returns_empty_without_network_call(self):
        with patch("residential_permits.urllib.request.urlopen") as mock_open:
            result = rp.fetch_leads("Nowhere", "ZZ")
            mock_open.assert_not_called()
        self.assertEqual(result, [])

    def test_parses_austin_row_correctly(self):
        with patch("residential_permits.urllib.request.urlopen",
                   return_value=_mock_response([_SAMPLE_ROW])):
            leads = rp.fetch_leads("Austin", "TX")
        self.assertEqual(len(leads), 1)
        lead = leads[0]
        self.assertEqual(lead["address"], "2018 FORD ST")
        self.assertEqual(lead["permit_type"], "Driveway / Sidewalks")
        self.assertEqual(lead["contractor_name"], "Canedo Builders")
        self.assertEqual(lead["contractor_phone"], "5123735885")
        self.assertEqual(lead["zip"], "78704")
        self.assertAlmostEqual(lead["lat"], 30.25463834)
        self.assertAlmostEqual(lead["lon"], -97.77418547)

    def test_missing_coordinates_parsed_as_none_not_an_error(self):
        with patch("residential_permits.urllib.request.urlopen",
                   return_value=_mock_response([_SAMPLE_ROW_NO_COORDS])):
            leads = rp.fetch_leads("Austin", "TX")
        self.assertIsNone(leads[0]["lat"])
        self.assertIsNone(leads[0]["lon"])
        self.assertEqual(leads[0]["zip"], "78704")  # still usable as a fallback

    def test_leads_are_labeled_and_sorted_open_first(self):
        builder_row = {**_SAMPLE_ROW, "permit_number": "builder-1"}
        open_row = {**_SAMPLE_ROW, "permit_number": "open-1",
                    "contractor_trade": "", "contractor_company_name": "", "contractor_full_name": ""}
        # Response order deliberately puts the builder lead first -- the
        # sort should still bring the open lead to the front.
        with patch("residential_permits.urllib.request.urlopen",
                   return_value=_mock_response([builder_row, open_row])):
            leads = rp.fetch_leads("Austin", "TX")
        self.assertEqual([l["permit_id"] for l in leads], ["open-1", "builder-1"])
        self.assertEqual(leads[0]["lead_type"], "open")
        self.assertEqual(leads[1]["lead_type"], "builder")
        self.assertIn("lead_type_label", leads[0])

    def test_network_failure_returns_empty_list_not_an_exception(self):
        with patch("residential_permits.urllib.request.urlopen", side_effect=OSError("boom")):
            result = rp.fetch_leads("Austin", "TX")
        self.assertEqual(result, [])


class ResidentialLeadsEndpointTests(unittest.TestCase):
    """Each test patches _cache/_save_cache to a fresh in-memory dict --
    the endpoint's own day-based cache would otherwise leak between test
    runs (and into the real local scan_cache.json) and mask what's actually
    being tested."""

    def setUp(self):
        self.client = ls.app.test_client()
        self._orig_license_check = ls._license_is_active
        ls._license_is_active = lambda *a, **k: True
        self._fresh_cache = {"scan_cache": {}, "geo_cache": {}}
        self._cache_patcher = patch("license_server._cache", side_effect=lambda: self._fresh_cache)
        self._save_patcher = patch("license_server._save_cache")
        self._cache_patcher.start()
        self._save_patcher.start()

    def tearDown(self):
        ls._license_is_active = self._orig_license_check
        self._cache_patcher.stop()
        self._save_patcher.stop()

    _AUSTIN_CENTER = {"lat": 30.2672, "lon": -97.7431, "city": "Austin", "state": "TX"}

    # A town ~20mi north of Austin: not itself a configured source, but well
    # inside a 25mi search of Austin's permit data.
    _ROUND_ROCK_CENTER = {"lat": 30.5083, "lon": -97.6789, "city": "Round Rock", "state": "TX"}

    def test_area_with_no_source_anywhere_near_reports_covered_false(self):
        with patch("license_server._resolve_center",
                   return_value={"lat": 37.2153, "lon": -93.2982, "city": "Springfield", "state": "MO"}), \
             patch("license_server.residential_permits.fetch_leads") as mock_fetch:
            resp = self.client.post("/residential-leads",
                                    json={"location": "Springfield, MO", "radius": 25})
            mock_fetch.assert_not_called()
        d = resp.get_json()
        self.assertTrue(d["ok"])
        self.assertFalse(d["covered"])
        self.assertEqual(d["leads"], [])

    def test_radius_filters_out_far_away_leads(self):
        near = {**rp._austin_parser(_SAMPLE_ROW)}  # real Austin, TX coords
        far = {**near, "permit_id": "far-one", "lat": 40.7128, "lon": -74.0060}  # NYC
        with patch("license_server._resolve_center", return_value=self._AUSTIN_CENTER), \
             patch("license_server.residential_permits.fetch_leads", return_value=[near, far]):
            resp = self.client.post("/residential-leads",
                                    json={"location": "Austin, TX", "radius": 25})
        d = resp.get_json()
        self.assertTrue(d["ok"])
        ids = [l["permit_id"] for l in d["leads"]]
        self.assertIn(near["permit_id"], ids)
        self.assertNotIn("far-one", ids)

    def test_a_nearby_town_is_covered_by_its_neighbours_source(self):
        """Searching from a suburb used to report "not set up for your area
        yet" even though the covered city's permits were inside the radius."""
        near = {**rp._austin_parser(_SAMPLE_ROW)}
        with patch("license_server._resolve_center", return_value=self._ROUND_ROCK_CENTER), \
             patch("license_server.residential_permits.fetch_leads",
                   return_value=[near]) as mock_fetch:
            resp = self.client.post("/residential-leads",
                                    json={"location": "Round Rock, TX", "radius": 25})
            mock_fetch.assert_called_once_with("austin", "TX")
        d = resp.get_json()
        self.assertTrue(d["covered"])
        self.assertEqual([l["permit_id"] for l in d["leads"]], [near["permit_id"]])

    def test_source_selection_needs_no_geocoder(self):
        # Source coordinates ship with the registry, so an unreachable
        # geocoder must not make coverage collapse to nothing.
        with patch("license_server._resolve_center", return_value=self._AUSTIN_CENTER), \
             patch("license_server._geo_from_city", side_effect=AssertionError("geocoded!")), \
             patch("license_server.residential_permits.fetch_leads", return_value=[]):
            resp = self.client.post("/residential-leads",
                                    json={"location": "Austin, TX", "radius": 25})
        self.assertTrue(resp.get_json()["covered"])

    def test_a_zip_geocode_blip_is_not_cached_forever(self):
        """The ZIP cache used to store None on failure and reuse it forever.
        Here the failure keeps the lead, so one blip meant that ZIP's leads
        stopped being distance-checked at all — out-of-radius leads shown
        permanently."""
        cdb = {"zip_geo_cache": {"78701": None}}  # poisoned by the old code
        lead = {"zip": "78701", "lat": None, "lon": None}
        far_center = {"lat": 40.7128, "lon": -74.0060, "city": "New York", "state": "NY"}
        with patch("license_server._geo_from_zip",
                   return_value={"lat": 30.2672, "lon": -97.7431}):
            inside = ls._lead_within_radius(lead, far_center, 25, cdb)
        self.assertFalse(inside, "a legacy None should be retried, not trusted")
        self.assertEqual(cdb["zip_geo_cache"]["78701"], [30.2672, -97.7431])

    def test_a_zip_that_really_cannot_be_resolved_keeps_the_lead(self):
        cdb = {}
        lead = {"zip": "00000", "lat": None, "lon": None}
        far_center = {"lat": 40.7128, "lon": -74.0060, "city": "New York", "state": "NY"}
        with patch("license_server._geo_from_zip", return_value=None):
            self.assertTrue(ls._lead_within_radius(lead, far_center, 25, cdb))

    def test_a_tiny_radius_far_from_any_source_is_not_covered(self):
        far_from_austin = {"lat": 32.7767, "lon": -96.7970, "city": "Dallas", "state": "TX"}
        with patch("license_server._resolve_center", return_value=far_from_austin), \
             patch("license_server.residential_permits.fetch_leads") as mock_fetch:
            resp = self.client.post("/residential-leads",
                                    json={"location": "Dallas, TX", "radius": 25})
            mock_fetch.assert_not_called()
        self.assertFalse(resp.get_json()["covered"])


if __name__ == "__main__":
    unittest.main()
