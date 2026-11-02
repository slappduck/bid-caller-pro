"""Open bids near a ZIP, shown on the landing page before anyone signs up.

Built from the bids scans already found -- nothing is scanned for a visitor
-- and holding only what the agency published."""
import datetime
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls
import kv_backend

AURORA = {"city": "Aurora", "state": "MO", "lat": 36.97, "lon": -93.72}


def day(n):
    return (datetime.date.today() + datetime.timedelta(days=n)).isoformat()


class BidsNearTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self.client = ls.app.test_client()
        self._p = [
            patch.object(kv_backend, "get", side_effect=lambda k, d=None: self.store.get(k, d)),
            patch.object(kv_backend, "set", side_effect=lambda k, v: self.store.__setitem__(k, v)),
            patch.object(ls, "_resolve_center", return_value=dict(AURORA)),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def scan(self, grouped, coords):
        return ls._remember_open_bids(grouped, coords)

    def ask(self, **body):
        return self.client.post("/bids-near", json={"location": "65605", "radius": 50, **body})

    def test_bids_a_scan_found_show_up_near_a_zip_soonest_first(self):
        self.scan({"Monett": [{"title": "Elm St Sidewalk", "deadline": day(20), "status": "Open"},
                              {"title": "Oak Ave Curb", "deadline": day(5), "status": "Accepting bids"}],
                   "Springfield": [{"title": "ADA Ramps Phase 2", "deadline": "", "status": ""}]},
                  {"Monett": {"lat": 36.93, "lon": -93.93}, "Springfield": {"lat": 37.21, "lon": -93.29}})
        d = self.ask().get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["count"], 3)
        self.assertEqual([b["title"] for b in d["bids"]], ["Oak Ave Curb", "Elm St Sidewalk", "ADA Ramps Phase 2"])
        self.assertEqual(d["bids"][0], {"title": "Oak Ave Curb", "town": "Monett", "due": day(5), "miles": 12})

    def test_far_away_closed_and_past_due_bids_are_left_out(self):
        self.scan({"Monett": [{"title": "Closed one", "deadline": day(9), "status": "Closed"},
                              {"title": "Past due", "deadline": day(-1), "status": "Open"}],
                   "Kansas City": [{"title": "KC Sidewalks", "deadline": day(9)}]},
                  {"Monett": {"lat": 36.93, "lon": -93.93}, "Kansas City": {"lat": 39.10, "lon": -94.58}})
        self.assertEqual(self.ask().get_json()["count"], 0)

    def test_only_what_the_agency_published_is_kept(self):
        self.scan({"Monett": [{"title": "Elm St Sidewalk", "deadline": day(9), "url": "https://e.gov/1",
                               "contact": "Dana", "phone": "555", "email": "d@e.gov"}]},
                  {"Monett": {"lat": 36.93, "lon": -93.93}})
        row = list(self.store[ls._OPEN_BIDS_KEY].values())[0]
        self.assertEqual(set(row), {"title", "town", "lat", "lon", "due", "seen"})

    def test_an_undated_bid_drops_off_after_three_weeks(self):
        self.scan({"Monett": [{"title": "Undated", "deadline": ""}]}, {"Monett": {"lat": 36.93, "lon": -93.93}})
        rows = self.store[ls._OPEN_BIDS_KEY]
        for r in rows.values():
            r["seen"] = day(-22)
        self.assertEqual(self.ask().get_json()["count"], 0)

    def test_the_same_bid_seen_twice_is_one_row(self):
        g = {"Monett": [{"title": "Elm St Sidewalk", "deadline": day(9)}]}
        c = {"Monett": {"lat": 36.93, "lon": -93.93}}
        self.scan(g, c)
        self.scan(g, c)
        self.assertEqual(len(self.store[ls._OPEN_BIDS_KEY]), 1)

    def test_a_fresh_scan_keeps_what_it_found(self):
        import inspect
        self.assertIn("_remember_open_bids(grouped, city_coords)", inspect.getsource(ls._perform_scan))

    def test_unknown_place_and_rate_limit(self):
        with patch.object(ls, "_resolve_center", return_value=None):
            self.assertEqual(self.ask().status_code, 404)
        with patch.object(ls, "_ip_rate_ok", return_value=False):
            self.assertEqual(self.ask().status_code, 429)
        self.assertEqual(self.client.post("/bids-near", json={}).status_code, 400)

    def test_a_bad_scan_never_breaks_anything(self):
        self.assertEqual(self.scan({"X": [{"title": "No place"}]}, {}), 0)
        self.assertEqual(self.scan(None, None), 0)


if __name__ == "__main__":
    unittest.main()
