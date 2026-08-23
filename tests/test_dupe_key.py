"""One job must not arrive as two cards.

The same solicitation routinely reaches a scan twice -- once off the agency's
own bid page and once via search or an aggregator -- written slightly
differently each time. The de-duplication key compared the deadline as raw
text, so "9/3/2026" and "09/03/2026" were two different bids, and so were
"09/03/2026" and "09/03/2026 02:00 PM EDT". The contractor got two cards for
one job, and because the client derives a bid's id from city+title+scope,
starring one of them did nothing to the other.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


def same(a, b):
    return ls._bid_dupe_key(a) == ls._bid_dupe_key(b)


class DupeKeyTests(unittest.TestCase):
    def test_the_same_date_written_two_ways(self):
        self.assertTrue(same({"title": "Olive Road Sidewalk", "deadline": "9/3/2026"},
                             {"title": "Olive Road Sidewalk", "deadline": "09/03/2026"}))

    def test_one_copy_carrying_a_time_of_day(self):
        self.assertTrue(same(
            {"title": "Tap River Sidewalk", "deadline": "09/03/2026"},
            {"title": "Tap River Sidewalk", "deadline": "09/03/2026 02:00 PM EDT"}))

    def test_case_and_spacing(self):
        self.assertTrue(same({"title": "OLIVE ROAD SIDEWALK", "deadline": "9/3/2026"},
                             {"title": "Olive  Road Sidewalk", "deadline": "9/3/2026"}))

    def test_trailing_punctuation(self):
        self.assertTrue(same({"title": "Olive Road Sidewalk.", "deadline": "9/3/2026"},
                             {"title": "Olive Road Sidewalk", "deadline": "9/3/2026"}))

    def test_different_titles_stay_apart(self):
        self.assertFalse(same({"title": "Olive Road Sidewalk", "deadline": "9/3/2026"},
                              {"title": "Maple Road Sidewalk", "deadline": "9/3/2026"}))

    def test_different_dates_stay_apart(self):
        self.assertFalse(same({"title": "Olive Road Sidewalk", "deadline": "9/3/2026"},
                              {"title": "Olive Road Sidewalk", "deadline": "10/3/2026"}))

    def test_two_undated_copies_are_one_job(self):
        self.assertTrue(same({"title": "Olive Road Sidewalk", "deadline": ""},
                             {"title": "Olive Road Sidewalk", "deadline": ""}))

    def test_unparseable_dates_are_compared_as_written(self):
        # Free text we cannot read must still separate two different bids
        # rather than collapsing everything undateable into one.
        self.assertFalse(same({"title": "X Project", "deadline": "see packet A"},
                              {"title": "X Project", "deadline": "see packet B"}))
        self.assertTrue(same({"title": "X Project", "deadline": "See Packet A"},
                             {"title": "X Project", "deadline": "see packet a"}))

    def test_missing_and_malformed_input_is_safe(self):
        self.assertEqual(ls._bid_dupe_key(None), ("", ""))
        self.assertEqual(ls._bid_dupe_key({}), ("", ""))


class PlacementDropsTheDuplicateTests(unittest.TestCase):
    CENTER = {"city": "Springfield", "state": "MO",
              "lat": 37.2090, "lon": -93.2923}

    def test_the_second_copy_is_not_placed(self):
        grouped, stats = {}, {}
        for due in ("9/3/2026", "09/03/2026 02:00 PM CDT"):
            ls._place_bid(grouped,
                          {"title": "Olive Road Sidewalk Project",
                           "status": "Open", "deadline": due,
                           "url": "https://www.springfieldmo.gov/Bids.aspx"},
                          self.CENTER, 125, {"geo_cache": {}},
                          default_city="Springfield", default_state="MO",
                          stats=stats)
        self.assertEqual(sum(len(v) for v in grouped.values()), 1)
        self.assertEqual(stats.get("duplicate"), 1)


if __name__ == "__main__":
    unittest.main()
