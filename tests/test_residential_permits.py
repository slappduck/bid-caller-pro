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

    def test_case_and_whitespace_insensitive(self):
        self.assertTrue(rp.has_source("  austin ", "tx"))

    def test_unknown_city_not_covered(self):
        self.assertFalse(rp.has_source("Springfield", "MO"))


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

    def test_uncovered_city_reports_covered_false_not_a_bare_empty_list(self):
        with patch("license_server._resolve_center",
                   return_value={"lat": 37.2153, "lon": -93.2982, "city": "Springfield", "state": "MO"}), \
             patch("license_server.residential_permits.has_source", return_value=False), \
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
             patch("license_server.residential_permits.has_source", return_value=True), \
             patch("license_server.residential_permits.fetch_leads", return_value=[near, far]):
            resp = self.client.post("/residential-leads",
                                    json={"location": "Austin, TX", "radius": 25})
        d = resp.get_json()
        self.assertTrue(d["ok"])
        ids = [l["permit_id"] for l in d["leads"]]
        self.assertIn(near["permit_id"], ids)
        self.assertNotIn("far-one", ids)


if __name__ == "__main__":
    unittest.main()
