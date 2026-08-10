"""Tests for the geo/placement half of the scan pipeline.

These cover three bugs that quietly cost the scan real bids rather than
throwing anything:

  * every bid was geocoded against the search centre's state, so a wide
    radius crossing a state line dropped all the out-of-state bids it found;
  * a transient geocoder outage was cached as a permanent failure, silently
    blacklisting that city for every future scan;
  * deadlines written as prose ("Due by 12/01/2026 at 2:00 PM") were
    unparseable, costing the bid its urgency ranking and letting expired
    listings keep showing as open.
"""
import datetime
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


# Aurora MO sits close enough to the state line that a 100mi radius reaches
# well into Arkansas — the exact situation the old code got wrong.
CENTER = {"lat": 36.9709, "lon": -93.7180, "city": "Aurora", "state": "MO"}
REAL_PLACES = {
    ("Aurora", "MO"): (36.9709, -93.7180),
    ("Bentonville", "AR"): (36.3729, -94.2088),
    ("Springfield", "MO"): (37.2090, -93.2923),
    ("Chicago", "IL"): (41.8781, -87.6298),
}


def _fake_geo(city, state):
    """Stand-in for zippopotam: only real (city, state) pairs resolve."""
    hit = REAL_PLACES.get((city, state))
    if not hit:
        return None
    return {"lat": hit[0], "lon": hit[1], "city": city, "state": state}


class SplitCityStateTests(unittest.TestCase):
    def test_abbreviation(self):
        self.assertEqual(ls._split_city_state("Bentonville, AR"), ("Bentonville", "AR"))

    def test_full_state_name(self):
        self.assertEqual(ls._split_city_state("Bentonville, Arkansas"), ("Bentonville", "AR"))

    def test_bare_city(self):
        self.assertEqual(ls._split_city_state("Bentonville"), ("Bentonville", ""))

    def test_trailing_noise_is_not_a_state(self):
        self.assertEqual(ls._split_city_state("Bentonville, somewhere"), ("Bentonville", ""))

    def test_empty(self):
        self.assertEqual(ls._split_city_state(""), ("", ""))


class PlaceBidStateTests(unittest.TestCase):
    def setUp(self):
        self.grouped, self.coords, self.db = {}, {}, {}

    def _place(self, bid, **kw):
        with patch.object(ls, "_geo_from_city", side_effect=_fake_geo):
            ls._place_bid(self.grouped, bid, CENTER, 100, self.db,
                          city_coords=self.coords, **kw)

    def test_out_of_state_bid_is_kept_when_the_ai_states_the_state(self):
        self._place({"title": "Bentonville Sidewalk & ADA Ramps",
                     "city": "Bentonville, AR", "status": "Open"})
        self.assertEqual(list(self.grouped), ["Bentonville, AR"])

    def test_out_of_state_bid_is_kept_via_the_anchor_towns_state(self):
        # The AI didn't restate the location; the anchor town it came from did.
        self._place({"title": "Sidewalk program", "status": "Open"},
                    default_city="Bentonville", default_state="AR")
        self.assertEqual(list(self.grouped), ["Bentonville, AR"])

    def test_in_state_bid_keeps_a_bare_city_label(self):
        self._place({"title": "Sidewalk program", "city": "Springfield", "status": "Open"})
        self.assertEqual(list(self.grouped), ["Springfield"])

    def test_out_of_state_label_carries_the_state(self):
        self._place({"title": "x", "city": "Bentonville, AR", "status": "Open"})
        self.assertIn("Bentonville, AR", self.coords)

    def test_bid_outside_the_radius_is_still_dropped(self):
        self._place({"title": "x", "city": "Chicago, IL", "status": "Open"})
        self.assertEqual(self.grouped, {})

    def test_unresolvable_city_is_dropped(self):
        self._place({"title": "x", "city": "Nowheresville, ZZ", "status": "Open"})
        self.assertEqual(self.grouped, {})

    def test_bid_with_no_location_at_all_is_dropped(self):
        self._place({"title": "x", "status": "Open"})
        self.assertEqual(self.grouped, {})

    def test_stated_state_wins_over_the_centre(self):
        # "Springfield" exists in both MO and IL. Saying IL must not silently
        # resolve to the Missouri one just because that's the centre's state.
        self._place({"title": "x", "city": "Springfield, IL", "status": "Open"})
        self.assertEqual(self.grouped, {})  # the IL one is >100mi away


class CityCoordsCacheTests(unittest.TestCase):
    def test_success_is_cached_and_not_refetched(self):
        db, calls = {}, []

        def counting(city, state):
            calls.append((city, state))
            return _fake_geo(city, state)

        with patch.object(ls, "_geo_from_city", side_effect=counting):
            first = ls._city_coords("Springfield", "MO", db)
            second = ls._city_coords("Springfield", "MO", db)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)

    def test_a_fresh_miss_is_not_immediately_refetched(self):
        db, calls = {}, []

        def always_fail(city, state):
            calls.append((city, state))
            return None

        with patch.object(ls, "_geo_from_city", side_effect=always_fail):
            ls._city_coords("Springfield", "MO", db)
            ls._city_coords("Springfield", "MO", db)
        self.assertEqual(len(calls), 1)

    def test_an_expired_miss_is_retried(self):
        stale = (datetime.datetime.now()
                 - datetime.timedelta(hours=ls.GEO_MISS_RETRY_HOURS + 1)).isoformat()
        db = {"geo_cache": {"springfield|MO": {"missed_at": stale}}}
        with patch.object(ls, "_geo_from_city", side_effect=_fake_geo):
            self.assertIsNotNone(ls._city_coords("Springfield", "MO", db))

    def test_a_legacy_none_entry_heals_instead_of_blacklisting_forever(self):
        # Caches written by the previous version stored a bare None and
        # returned it forever, permanently dropping every bid in that city.
        db = {"geo_cache": {"springfield|MO": None}}
        with patch.object(ls, "_geo_from_city", side_effect=_fake_geo):
            self.assertIsNotNone(ls._city_coords("Springfield", "MO", db))


class DeadlineParsingTests(unittest.TestCase):
    def test_parses_dates_embedded_in_prose(self):
        for text in ("2026-12-01",
                     "12/01/2026",
                     "December 1, 2026",
                     "Due by 12/01/2026 at 2:00 PM",
                     "Bids due December 1, 2026 at 2:00 p.m.",
                     "Thursday, December 1, 2026",
                     "12/01/2026 2:00 PM CST",
                     "Submit no later than 3:00 PM on 12/1/2026",
                     "Sept. 1, 2026"):
            with self.subTest(text=text):
                self.assertIsNotNone(ls._parse_deadline(text))

    def test_a_bare_year_is_still_not_a_date(self):
        # _apply_deadline_status handles these separately; parsing one as a
        # real date would break its stale-listing check.
        for text in ("FY2024", "Due 2025", "2025 cycle", "Closed as of 2024"):
            with self.subTest(text=text):
                self.assertIsNone(ls._parse_deadline(text))

    def test_prose_deadline_scores_the_same_as_the_iso_one(self):
        due = datetime.date.today() + datetime.timedelta(days=3)
        iso = {"title": "Sidewalk repair", "status": "Open", "deadline": due.isoformat()}
        prose = {"title": "Sidewalk repair", "status": "Open",
                 "deadline": f"Due by {due.strftime('%m/%d/%Y')} at 2:00 PM"}
        self.assertEqual(ls._score_bid(iso), ls._score_bid(prose))

    def test_expired_prose_deadline_marks_the_bid_closed(self):
        past = datetime.date.today() - datetime.timedelta(days=45)
        bid = {"status": "Open", "deadline": f"Due by {past.strftime('%m/%d/%Y')} at 2:00 PM"}
        ls._apply_deadline_status(bid)
        self.assertEqual(bid["status"], "Closed")


if __name__ == "__main__":
    unittest.main()
