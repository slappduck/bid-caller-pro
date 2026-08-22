"""An aggregator page's buyer can be anywhere. Never lend it the search town.

_place_bid falls back to the town whose scan turned a bid up when the bid
does not name its own city. That is right for a town's OWN bid page -- a
posting on Rollingwood's site is Rollingwood's work whether or not it
restates the city -- and it is catastrophic for PlanetBids, BidNet,
DemandStar and the rest, which host every agency in the country behind one
domain.

Seen on a real scan: searching Rollingwood, CA surfaced a City of DUARTE
job, 358 miles away on the far side of the state. It was given Rollingwood's
name AND Rollingwood's coordinates, which carried it past the radius check
and onto the board under the wrong town's heading. A contractor reading that
card has no way to know the job is a six-hour drive away.

Dropping it is the right answer. A bid we cannot locate is not a local bid,
and showing it under a wrong town is worse than not showing it.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_portals as bp
import license_server as ls

ROLLINGWOOD = (37.9613, -122.3175)
DUARTE = (34.1395, -117.9773)
PLANETBIDS = "https://vendors.planetbids.com/portal/42035/bo/bo-detail/137702"
OWN_PORTAL = "https://rollingwood.ca.gov/Bids.aspx?bidID=3"


class DistanceSanityTests(unittest.TestCase):
    def test_the_two_towns_really_are_far_apart(self):
        miles = ls._miles_between(*ROLLINGWOOD, *DUARTE)
        self.assertGreater(miles, 300, "the premise of this test")

    def test_planetbids_is_recognised_as_an_aggregator(self):
        self.assertTrue(bp.is_aggregator_url(PLANETBIDS))


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.center = {"city": "Rollingwood", "state": "CA",
                       "lat": ROLLINGWOOD[0], "lon": ROLLINGWOOD[1]}
        self.coords = {("rollingwood", "CA"): ROLLINGWOOD,
                       ("duarte", "CA"): DUARTE}

    def _place(self, **bid):
        grouped, stats = {}, {}
        b = {"title": "ADA Ramp Construction", "status": "Open",
             "deadline": "", "city": ""}
        b.update(bid)
        ls._place_bid(grouped, b, self.center, 125, {},
                      default_city="Rollingwood", city_coords=self.coords,
                      default_state="CA", fallback_coords=ROLLINGWOOD,
                      stats=stats)
        return list(grouped.keys()), stats

    def test_an_aggregator_bid_with_no_city_is_dropped(self):
        """The exact bug: this used to come back as ['Rollingwood']."""
        cities, stats = self._place(url=PLANETBIDS)
        self.assertEqual(cities, [])
        self.assertEqual(stats.get("aggregator_no_location"), 1)

    def test_an_aggregator_bid_that_names_its_city_is_located_honestly(self):
        """Duarte is real and 358 miles away, so it fails the radius check --
        which is the correct outcome, not a silent relabel."""
        cities, stats = self._place(url=PLANETBIDS, city="Duarte, CA")
        self.assertEqual(cities, [])
        self.assertEqual(stats.get("out_of_radius"), 1)

    def test_an_aggregator_bid_genuinely_nearby_is_kept(self):
        self.assertEqual(
            self._place(url=PLANETBIDS, city="Rollingwood, CA")[0],
            ["Rollingwood"])

    def test_a_town_s_own_portal_still_lends_its_name(self):
        """The fallback this narrows must keep working where it belongs."""
        cities, stats = self._place(url=OWN_PORTAL)
        self.assertEqual(cities, ["Rollingwood"])
        self.assertIsNone(stats.get("aggregator_no_location"))

    def test_an_ungeocodable_authority_on_an_aggregator_is_not_anchored(self):
        """A name that resolves nowhere is anchored to the search town's
        coordinates when it was found on that town's own page -- road
        districts and drainage boards are real buyers no gazetteer knows. On
        an aggregator that anchor is unfounded: the page could belong to any
        of the Greene Counties.

        _city_coords is stubbed because the live geocoder answers this name
        with a plausible-looking point (it returns Menlo Park for "Greene
        County Road District"), which would mask the branch under test.
        """
        with patch.object(ls, "_city_coords", lambda *_a, **_k: None):
            cities, stats = self._place(url=PLANETBIDS,
                                        city="Greene County Road District")
        self.assertEqual(cities, [])
        self.assertEqual(stats.get("unresolvable_place"), 1)

    def test_the_same_authority_on_the_town_s_own_page_is_anchored(self):
        """The behaviour being narrowed, kept as a test so it is not lost."""
        with patch.object(ls, "_city_coords", lambda *_a, **_k: None):
            cities, stats = self._place(url=OWN_PORTAL,
                                        city="Greene County Road District")
        self.assertEqual(cities, ["Greene County Road District"])
        self.assertEqual(stats.get("placed_by_search_town"), 1)


if __name__ == "__main__":
    unittest.main()
