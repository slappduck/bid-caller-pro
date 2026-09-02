"""A bid must not be lent a town it has nothing to do with.

Reported from a live board: a card headed "Charlestown - 16 mi" for
"Concrete Sidewalks and ADA Ramps Project", whose posting URL was
cms3.revize.com/revize/fairfield/Purchasing/2025/... Charlestown, IN has its
own portal at cityofcharlestown.com, so that bid came off a different city's
website entirely and was given Charlestown's name AND coordinates -- which
made the 16 miles fiction for work in a Fairfield hundreds of miles away.

The borrowing itself is deliberate and usually right: a posting on a town's
own bid page is that town's even when it does not restate the city. What was
missing is the case where the URL plainly says otherwise.

Checked BEFORE the coordinate lookup, not in the unresolvable-place fallback:
that fallback only runs when the borrowed name fails to geocode, and a
borrowed name that geocodes perfectly is exactly the dangerous case.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_portals
import license_server as ls

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DIRECTORY = {
    "charlestown|IN": [{"url": "https://cityofcharlestown.com/Bids.aspx"}],
    "fairfield|OH": [{"url": "https://fairfieldoh.gov/Bids.aspx"}],
    "springfield|MO": [{"url": "https://www.springfieldmo.gov/Bids.aspx"}],
    "aurora|MO": [{"url": "https://www.aurora-cityhall.org/Bids.aspx"}],
    "joplin|MO": [{"url": "https://joplinmo.org/Bids.aspx"}],
}


class UrlNamesOtherPlaceTests(unittest.TestCase):
    def test_the_reported_case(self):
        self.assertTrue(bid_portals.url_names_other_place(
            "https://cms3.revize.com/revize/fairfield/Purchasing/2025/x.pdf",
            "Charlestown", DIRECTORY))

    def test_a_towns_own_portal_is_fine(self):
        self.assertFalse(bid_portals.url_names_other_place(
            "https://cityofcharlestown.com/Bids.aspx", "Charlestown", DIRECTORY))

    def test_state_suffixed_domain_is_fine(self):
        self.assertFalse(bid_portals.url_names_other_place(
            "https://www.springfieldmo.gov/Bids.aspx", "Springfield", DIRECTORY))

    def test_a_different_towns_domain_is_caught(self):
        self.assertTrue(bid_portals.url_names_other_place(
            "https://www.springfieldmo.gov/Bids.aspx", "Aurora", DIRECTORY))

    def test_a_url_naming_nowhere_gets_the_benefit_of_the_doubt(self):
        # Most URLs name no place at all. Absence of evidence is not evidence.
        self.assertFalse(bid_portals.url_names_other_place(
            "https://x.gov/DocumentCenter/View/1234/bid.pdf", "Aurora",
            DIRECTORY))

    def test_hyphenated_town_domain(self):
        self.assertFalse(bid_portals.url_names_other_place(
            "https://www.aurora-cityhall.org/Bids.aspx", "Aurora", DIRECTORY))

    def test_a_town_we_do_not_know_is_not_evidence(self):
        # Only fires on names the directory actually knows, so an unfamiliar
        # word in a path cannot silently drop real work.
        self.assertFalse(bid_portals.url_names_other_place(
            "https://cms3.revize.com/revize/nowheresville/Bids", "Aurora",
            DIRECTORY))

    def test_empty_and_missing_inputs_are_safe(self):
        self.assertFalse(bid_portals.url_names_other_place("", "Aurora", DIRECTORY))
        self.assertFalse(bid_portals.url_names_other_place(None, "Aurora", None))
        self.assertFalse(bid_portals.url_names_other_place(
            "https://x.gov/a", "", DIRECTORY))


class PlacementTests(unittest.TestCase):
    CENTER = {"city": "New Albany", "state": "IN",
              "lat": 38.2856, "lon": -85.8241}

    def _place(self, url, stated_city=None):
        grouped, stats = {}, {}
        bid = {"title": "Concrete Sidewalks and ADA Ramps Project",
               "status": "Open", "deadline": "12/01/2026", "url": url}
        if stated_city:
            bid["city"] = stated_city
        ls._place_bid(grouped, bid, self.CENTER, 125, {"geo_cache": {}},
                      default_city="Charlestown", default_state="IN",
                      fallback_coords=(38.4526, -85.6699), stats=stats,
                      pdb=DIRECTORY)
        return sum(len(v) for v in grouped.values()), stats

    def test_another_towns_posting_is_not_placed_here(self):
        placed, stats = self._place(
            "https://cms3.revize.com/revize/fairfield/Purchasing/2025/x.pdf")
        self.assertEqual(placed, 0)
        self.assertEqual(stats.get("url_names_another_town"), 1)

    def test_the_towns_own_posting_still_borrows_the_name(self):
        placed, _ = self._place("https://cityofcharlestown.com/Bids.aspx")
        self.assertEqual(placed, 1)

    def test_a_url_naming_nowhere_still_borrows(self):
        placed, _ = self._place("https://someagency.gov/DocumentCenter/View/9/x.pdf")
        self.assertEqual(placed, 1)

    def test_a_bid_that_states_a_distant_city_is_believed_then_dropped(self):
        # Not dropped by this rule -- geocoded properly and then judged on
        # distance like anything else. Fairfield CA is a continent away, so
        # distance is what removes it, not the borrowed-town rule.
        placed, stats = self._place(
            "https://cms3.revize.com/revize/fairfield/Purchasing/x.pdf",
            stated_city="Fairfield, CA")
        self.assertEqual(placed, 0)
        self.assertNotIn("url_names_another_town", stats)

    def test_a_bid_that_states_a_nearby_city_is_believed_and_kept(self):
        # The other half of the same rule, and the half that used to fail
        # silently: Fairfield OH is 102 miles from New Albany, inside the 125
        # radius, so it belongs in the results. It was dropped for a while
        # because geocoding averaged Fairfield with North Fairfield, a
        # different town 100 miles up the state, and put the pin 154 miles
        # out. Asserting the keep as well as the drop is what would have
        # caught that.
        placed, stats = self._place(
            "https://cms3.revize.com/revize/fairfield/Purchasing/x.pdf",
            stated_city="Fairfield, OH")
        self.assertEqual(placed, 1)
        self.assertNotIn("url_names_another_town", stats)

    def test_the_live_directory_loads_and_is_usable(self):
        path = os.path.join(HERE, "kv_bidcaller_portal_directory.json")
        if not os.path.exists(path):
            self.skipTest("directory snapshot not present")
        with open(path) as f:
            real = json.load(f)
        self.assertTrue(bid_portals.url_names_other_place(
            "https://cms3.revize.com/revize/fairfield/Purchasing/2025/x.pdf",
            "Charlestown", real))


if __name__ == "__main__":
    unittest.main()
