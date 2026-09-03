"""Deciding whether a coverage answer really describes the prospect's patch.

The number in a cold email is the only claim the recipient can check in
thirty seconds, so a wrong one is worse than no email. Frankfort is the case
this guard exists for: /coverage answered 8 agencies for Frankfort, IL
because three Illinois places share the name and the geocoder averaged them
into a field 150 miles away.

The first version tested whether the prospect's own town appeared among the
three nearest agencies. That proxy failed in both directions and cost real
prospects: Milwaukee's three closest entries are Whitefish Bay, South
Milwaukee and New Berlin -- suburbs within fifteen miles -- and Skippack, PA
has no bid page of its own though Lansdale is eight miles away. Both had
resolved perfectly and both were held.

The question was always "how far is the nearest work", which is a number.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.outreach_draft import _looks_like_the_right_town, NEAR_ENOUGH_MI


class DistanceDecidesTests(unittest.TestCase):
    def test_a_nearby_agency_passes_even_in_another_town(self):
        """Skippack, PA -- no bid page of its own, Lansdale eight miles off."""
        data = {"agencies": 479, "nearest_mi": 8.2,
                "nearest": ["Lansdale, PA", "Conshohocken, PA", "Souderton, PA"]}
        self.assertTrue(_looks_like_the_right_town(data, "Skippack"))

    def test_suburbs_of_the_prospects_own_city_pass(self):
        """Milwaukee -- its three closest entries are all suburbs."""
        data = {"agencies": 223, "nearest_mi": 5.4,
                "nearest": ["Whitefish Bay, WI", "South Milwaukee, WI",
                            "New Berlin, WI"]}
        self.assertTrue(_looks_like_the_right_town(data, "Milwaukee"))

    def test_the_frankfort_case_is_still_caught(self):
        data = {"agencies": 8, "nearest_mi": 151.0,
                "nearest": ["Quincy, IL", "Macomb, IL"]}
        self.assertFalse(_looks_like_the_right_town(data, "Frankfort"))

    def test_the_boundary_is_inclusive(self):
        self.assertTrue(_looks_like_the_right_town(
            {"nearest_mi": NEAR_ENOUGH_MI, "nearest": ["Anywhere, XX"]}, "Town"))
        self.assertFalse(_looks_like_the_right_town(
            {"nearest_mi": NEAR_ENOUGH_MI + 0.1, "nearest": ["Anywhere, XX"]}, "Town"))

    def test_zero_miles_passes(self):
        """The prospect's own town has a bid page."""
        self.assertTrue(_looks_like_the_right_town(
            {"nearest_mi": 0.0, "nearest": ["Waukesha, WI"]}, "Waukesha"))


class FallsBackWhenTheServerIsOlderTests(unittest.TestCase):
    """nearest_mi arrives with a deploy. Until then, the old name test."""

    def test_name_match_still_works_without_a_distance(self):
        self.assertTrue(_looks_like_the_right_town(
            {"nearest": ["Waukesha, WI", "Pewaukee, WI"]}, "Waukesha"))

    def test_no_distance_and_no_name_match_is_held(self):
        self.assertFalse(_looks_like_the_right_town(
            {"nearest": ["Quincy, IL", "Macomb, IL"]}, "Frankfort"))

    def test_an_empty_answer_is_held(self):
        self.assertFalse(_looks_like_the_right_town({}, "Anywhere"))
        self.assertFalse(_looks_like_the_right_town({"nearest": []}, "Anywhere"))

    def test_a_non_numeric_distance_does_not_crash(self):
        self.assertFalse(_looks_like_the_right_town(
            {"nearest_mi": "close", "nearest": ["Quincy, IL"]}, "Frankfort"))


if __name__ == "__main__":
    unittest.main()
