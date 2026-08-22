"""Distance has to reach the customer, now that the radius is 125 miles.

At the old 25-mile default every bid on the board was near, so the card
never showed a distance and nothing sorted by one. At 125 a board spans
twenty towns and "8 miles away" versus "120 miles away" is the first thing a
contractor needs to know -- and the server was computing that number for the
radius check and throwing it away.

Ordering matters just as much. The server ranks bids PER CITY, so across a
twenty-town board the city order was whatever the scan happened to return.
That put a job 120 miles out above one 8 miles away on the default sort.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ServerAttachesDistanceTests(unittest.TestCase):
    def setUp(self):
        self.center = {"city": "Springfield", "state": "MO",
                       "lat": 37.2090, "lon": -93.2923}
        # Branson is about 40 miles south of Springfield.
        self.coords = {("springfield", "MO"): (37.2090, -93.2923),
                       ("branson", "MO"): (36.6437, -93.2185)}

    def _place(self, city):
        grouped = {}
        ls._place_bid(grouped, {"title": "Curb and Gutter Replacement",
                                "status": "Open", "deadline": "12/01/2026",
                                "url": "https://x.gov/1", "city": city},
                      self.center, 125, {}, city_coords=self.coords,
                      default_state="MO")
        return [b for v in grouped.values() for b in v][0]

    def test_a_bid_carries_its_distance(self):
        """A bid in the scan's own town rounds to zero or one mile."""
        self.assertLessEqual(self._place("Springfield")["miles"], 2)

    def test_a_distant_bid_carries_the_real_number(self):
        miles = self._place("Branson")["miles"]
        self.assertTrue(35 <= miles <= 45, miles)


class ScoringTests(unittest.TestCase):
    def _bid(self, **kw):
        b = {"title": "Sidewalk Replacement", "status": "Open",
             "deadline": "", "miles": 0}
        b.update(kw)
        return b

    def test_a_nearer_bid_outranks_an_identical_far_one(self):
        near = self._bid(miles=5)
        far = self._bid(miles=120)
        self.assertGreater(ls._score_bid(near), ls._score_bid(far))

    def test_urgency_still_beats_proximity(self):
        """A job closing in three days forty miles out beats one closing in a
        month next door -- the near one will still be there tomorrow."""
        soon_far = self._bid(miles=40, deadline=_in_days(3))
        later_near = self._bid(miles=2, deadline=_in_days(29))
        self.assertGreater(ls._score_bid(soon_far), ls._score_bid(later_near))

    def test_a_bid_with_no_distance_is_not_penalised_into_oblivion(self):
        """Agency notices and federal bids may arrive without coordinates."""
        self.assertEqual(ls._score_bid(self._bid(miles=None)),
                         ls._score_bid({"title": "Sidewalk Replacement",
                                        "status": "Open", "deadline": ""}))


def _in_days(n):
    import datetime
    return (datetime.date.today() + datetime.timedelta(days=n)).strftime("%m/%d/%Y")


class ClientTests(unittest.TestCase):
    """Structural: the card and the sort live in app.html."""

    def setUp(self):
        with open(os.path.join(HERE, "curbcall_netlify_v4", "app.html"),
                  encoding="utf-8") as fh:
            self.app = fh.read()

    def test_the_card_shows_the_distance(self):
        card = self.app[self.app.index("function bidCard("):]
        card = card[:card.index("\n}")]
        self.assertIn("b.miles", card,
                      "the bid card should show how far away the job is")

    def test_there_is_a_nearest_sort(self):
        self.assertIn('<option value="near">', self.app)

    def test_best_match_no_longer_leaves_city_order_alone(self):
        """It used to fall through with no sort at all, which at 125 miles
        means the scan's arbitrary city order."""
        self.assertIn("fitScore", self.app)

    def test_a_bid_without_a_distance_sorts_last_not_first(self):
        """milesOf must not return 0 for a missing field."""
        fn = re.search(r"function milesOf\(b\)\{(.*?)\}", self.app, re.S).group(1)
        self.assertIn("MAX_SAFE_INTEGER", fn)

    def test_the_client_and_server_weight_distance_the_same(self):
        fn = re.search(r"function fitScore\(b\)\{(.*?)\n\}", self.app, re.S).group(1)
        self.assertIn("Math.min(m,125)/10", fn.replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
