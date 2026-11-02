"""Town bid notices re-posted on a state local-programs page.

Arkansas DOT posts each town's own notice for federal-aid local work
(sidewalks, trails) on one page. Two of the towns on the captured copy,
Tyronza and Lake Village, have no bid page of their own that discovery could
find, so this page is the only place their sidewalk jobs show up online.
The fixture is the real page body captured 2026-11-02.
"""
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bid_sources  # noqa: E402
import license_server as ls  # noqa: E402

FIXTURE = os.path.join(ROOT, "data", "recall_fixtures", "ardot_local_projects.html")
URL = ls.LOCAL_NOTICE_SOURCES["AR"]


def page():
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


def rows():
    return bid_sources.parse_local_agency_notices(page(), "AR", URL)


class ParseTests(unittest.TestCase):
    def by_place(self):
        return {r.get("town") or r.get("county"): r for r in rows()}

    def test_the_sidewalk_jobs_are_read(self):
        got = self.by_place()
        self.assertEqual(got["Lake Village"]["title"],
                         "Lake Village Rodney Angel Sports Complex Sidewalks")
        self.assertEqual(got["Lake Village"]["deadline"], "November 17, 2026")
        self.assertEqual(got["Tyronza"]["title"],
                         "Tyronza Hwy. 118 Sidewalk Phase 1 (Main St)")
        self.assertEqual(got["Tyronza"]["deadline"], "November 10, 2026")
        self.assertEqual(got["Alma"]["title"], "Alma Southside Sidewalk Improvements")

    def test_abbreviations_are_spelled_out_so_the_trade_filter_sees_them(self):
        """"Ped. Impvts." is a sidewalk job the filter could not recognise."""
        self.assertEqual(self.by_place()["Jonesboro"]["title"],
                         "Jonesboro Marion Berry Parkway Pedestrian Improvements")

    def test_a_county_letting_is_kept_as_a_county(self):
        r = self.by_place()["Craighead"]
        self.assertNotIn("town", r)
        self.assertEqual(r["deadline"], "October 27, 2026")   # "October 27 th , 2026"
        self.assertEqual(r["title"],
                         "Crowley’s Ridge Gravel Trail Respite Hub (Craighead County)")

    def test_other_trades_are_left_out(self):
        self.assertNotIn("Lowell", self.by_place())          # traffic signals

    def test_an_addendum_joins_the_notice_above_it(self):
        lr = self.by_place()["Little Rock"]
        self.assertEqual([d["name"] for d in lr["documents"]],
                         ["Bid notice", "Addendum 1", "Addendum 2"])
        self.assertFalse(any(r["agency"].lower().startswith("addend") for r in rows()))

    def test_each_row_links_to_its_own_notice(self):
        for r in rows():
            self.assertTrue(r["url"].startswith("https://media.ark.org/ardot/"), r["url"])
            self.assertEqual(r["source"], "state_local")


class PlacementTests(unittest.TestCase):
    CENTER = {"lat": 35.3859, "lon": -94.3985, "state": "AR"}   # Fort Smith

    def place(self, row, radius=50):
        grouped, coords = {}, {}
        with patch("license_server._city_coords", return_value=[35.4776, -94.2219]):
            kept = ls._place_local_notice(grouped, row, self.CENTER, radius, coords, {})
        return kept, grouped, coords

    def alma(self, **kw):
        r = next(x for x in rows() if x.get("town") == "Alma")
        r.update(kw)
        return r

    def test_placed_at_the_town(self):
        with patch("license_server._is_open_bid", return_value=True):
            kept, grouped, coords = self.place(self.alma())
        self.assertIsNotNone(kept)
        self.assertIn("Alma", grouped)
        self.assertAlmostEqual(kept["miles"], 12, delta=1)
        self.assertIn("Alma", coords)

    def test_out_of_radius_is_dropped(self):
        with patch("license_server._is_open_bid", return_value=True):
            kept, grouped, _ = self.place(self.alma(), radius=5)
        self.assertIsNone(kept)
        self.assertEqual(grouped, {})

    def test_a_closed_notice_is_dropped(self):
        kept, grouped, _ = self.place(self.alma(deadline="October 1, 2020"))
        self.assertIsNone(kept)

    def test_another_states_town_is_labelled_with_its_state(self):
        center = {"lat": 37.209, "lon": -93.2923, "state": "MO"}
        with patch("license_server._city_coords", return_value=[36.3353, -93.4524]), \
             patch("license_server._is_open_bid", return_value=True):
            grouped = {}
            r = next(x for x in rows() if x.get("town") == "Green Forest")
            ls._place_local_notice(grouped, r, center, 100, {}, {})
        self.assertIn("Green Forest, AR", grouped)


class ScanWiringTests(unittest.TestCase):
    def test_a_scan_touching_arkansas_reads_the_page(self):
        fetched = []

        def fake_fetch(url, timeout=None):
            fetched.append(url)
            return page(), "ok"

        center = {"lat": 35.3859, "lon": -94.3985, "state": "AR"}
        grouped, stats = {}, {}
        with patch("license_server._state_sources", return_value={}), \
             patch("license_server._fetch_page", side_effect=fake_fetch), \
             patch("license_server._city_coords", return_value=[35.4776, -94.2219]), \
             patch("license_server._is_open_bid", return_value=True), \
             patch("license_server.counties.states_within", return_value=["AR"]):
            ls._run_state_sources(center, 50, grouped, {}, stats)
        self.assertEqual(fetched, [URL])
        self.assertEqual(stats.get("state_local_rows_read"), len(rows()))
        self.assertIn("Alma Southside Sidewalk Improvements",
                      [b["title"] for v in grouped.values() for b in v])

    def test_a_scan_elsewhere_does_not_fetch_it(self):
        with patch("license_server._state_sources", return_value={}), \
             patch("license_server._fetch_page") as f, \
             patch("license_server.counties.states_within", return_value=["MO"]):
            ls._run_state_sources({"lat": 38.6, "lon": -90.2, "state": "MO"}, 25, {}, {}, {})
        f.assert_not_called()


if __name__ == "__main__":
    unittest.main()
