"""Geocoding every known bid-portal town once, offline, so a wide-radius
scan can do arithmetic against known coordinates instead of a live geocode
call per candidate town per request.

The module's own docstring names the exact bug this guards against twice
over: reading only the national-crawl CSV left towns from bid_portals.
SEED_PORTALS (Joplin, Ozark) and from the non-.gov wikidata list invisible
to towns_within_radius, because they had a real bid page and no
coordinates -- "a 50mi scan from Aurora, MO silently skipped Joplin
entirely." And a case-sensitive dedupe let "Springfield" and "springfield"
both through as two rows for one town, so the scanner would read and
search it twice. _load_towns() is tested against both.

No network call in this file: geocoding itself (license_server.
_geo_from_city) is stubbed, and each test builds its own small source CSV.
"""
import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import geocode_bid_portals as G  # noqa: E402
import bid_portals  # noqa: E402
import license_server as ls  # noqa: E402


class _Stubbed(unittest.TestCase):
    def stub(self, obj, name, value):
        original = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(lambda: setattr(obj, name, original))

    def source_csv(self, rows):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "dir.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["city", "state", "status"])
            w.writeheader()
            w.writerows(rows)
        self.stub(G, "SOURCE_CSV", path)
        return d


class LoadTownsTests(_Stubbed):
    def setUp(self):
        self.stub(bid_portals, "SEED_PORTALS", set())
        self.stub(bid_portals, "_wikidata_seeds", lambda: [])

    def test_only_found_status_rows_are_kept(self):
        self.source_csv([
            {"city": "Springfield", "state": "MO", "status": "found"},
            {"city": "DeadEnd", "state": "MO", "status": "not_found"},
        ])
        self.assertEqual(G._load_towns(), [("Springfield", "MO")])

    def test_the_same_town_in_different_case_is_not_double_counted(self):
        """The exact bug named in the docstring: "Springfield" and
        "springfield" must be one row, not two."""
        self.source_csv([
            {"city": "Springfield", "state": "MO", "status": "found"},
            {"city": "springfield", "state": "MO", "status": "found"},
        ])
        self.assertEqual(len(G._load_towns()), 1)

    def test_seed_portals_absent_from_the_crawl_csv_are_still_added(self):
        """Joplin and Ozark: a hand-verified working portal the national
        crawl never recorded a "found" row for."""
        self.source_csv([{"city": "Springfield", "state": "MO",
                          "status": "found"}])
        self.stub(bid_portals, "SEED_PORTALS", {("joplin", "mo")})
        towns = G._load_towns()
        self.assertIn(("Joplin", "MO"), towns)

    def test_wikidata_only_towns_are_still_added(self):
        self.source_csv([])
        self.stub(bid_portals, "_wikidata_seeds", lambda: [("Ozark", "MO")])
        self.assertEqual(G._load_towns(), [("Ozark", "MO")])

    def test_a_seed_portal_already_in_the_crawl_csv_is_not_duplicated(self):
        self.source_csv([{"city": "Joplin", "state": "MO", "status": "found"}])
        self.stub(bid_portals, "SEED_PORTALS", {("joplin", "mo")})
        towns = G._load_towns()
        self.assertEqual(towns.count(("Joplin", "MO")), 1)

    def test_state_filter_narrows_every_source(self):
        self.source_csv([{"city": "Springfield", "state": "MO",
                          "status": "found"},
                         {"city": "Topeka", "state": "KS", "status": "found"}])
        self.assertEqual(G._load_towns(state_filter="ks"), [("Topeka", "KS")])

    def test_a_row_missing_city_or_state_is_skipped_not_crashed_on(self):
        self.source_csv([{"city": "", "state": "MO", "status": "found"},
                         {"city": "Aurora", "state": "", "status": "found"}])
        self.assertEqual(G._load_towns(), [])

    def test_limit_caps_the_town_list(self):
        self.source_csv([{"city": "A", "state": "MO", "status": "found"},
                         {"city": "B", "state": "MO", "status": "found"}])
        self.assertEqual(len(G._load_towns(limit=1)), 1)


class MainGeocodesUnresolvedTownsOnlyTests(_Stubbed):
    """A town that fails to geocode is skipped, not written with blank
    coordinates -- a bid page with no known location is invisible to
    towns_within_radius either way, so a bad row would only mislead."""

    def setUp(self):
        self.stub(bid_portals, "SEED_PORTALS", set())
        self.stub(bid_portals, "_wikidata_seeds", lambda: [])
        self.dir = self.source_csv([
            {"city": "Springfield", "state": "MO", "status": "found"},
            {"city": "Nowhereville", "state": "MO", "status": "found"},
        ])
        self.out = os.path.join(self.dir, "out.csv")
        self.stub(G, "OUT_CSV", self.out)
        geo = {("springfield", "mo"): {"lat": 37.2, "lon": -93.3}}
        self.stub(ls, "_geo_from_city",
                  lambda city, state: geo.get((city.lower(), state.lower())))
        self._argv = sys.argv
        self.addCleanup(lambda: setattr(sys, "argv", self._argv))

    def test_only_the_resolvable_town_is_written(self):
        sys.argv = ["geocode_bid_portals.py"]
        G.main()
        with open(self.out, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["city"], "Springfield")
        self.assertEqual(rows[0]["lat"], "37.2")

    def test_resume_skips_towns_already_in_the_output(self):
        with open(self.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=G.FIELDS)
            w.writeheader()
            w.writerow({"city": "Springfield", "state": "MO",
                       "lat": "37.2", "lon": "-93.3"})
        calls = []
        self.stub(ls, "_geo_from_city",
                  lambda city, state: calls.append(city) or None)
        sys.argv = ["geocode_bid_portals.py", "--resume"]
        G.main()
        self.assertNotIn("Springfield", calls)
        self.assertIn("Nowhereville", calls)


if __name__ == "__main__":
    unittest.main()
