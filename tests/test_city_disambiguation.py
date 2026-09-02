"""A city name that means two places must resolve to the one the user meant.

Asked for Frankfort, IL, zippopotam returns three places: Frankfort itself
near Joliet, and Frankfort Heights and West Frankfort 250 miles south. The
old code averaged all three and landed in a field near Effingham, so
/coverage answered 8 agencies where the honest answer was 113 -- and a
contractor reading that concludes the product does not cover them.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import license_server as ls  # noqa: E402


# Real responses, trimmed to the fields the code reads.
FRANKFORT_IL = {"places": [
    {"place name": "Frankfort", "latitude": "41.5094", "longitude": "-87.8253"},
    {"place name": "Frankfort Heights", "latitude": "37.9937", "longitude": "-88.9420"},
    {"place name": "West Frankfort", "latitude": "37.8981", "longitude": "-88.9312"},
]}

# Sixteen ZIPs, one city, ten miles across. Averaging these is correct and
# must keep working -- the fix is not "stop averaging".
SPRINGFIELD_MO = {"places": [
    {"place name": "Springfield", "latitude": "37.2580", "longitude": "-93.3440"},
    {"place name": "Springfield", "latitude": "37.2120", "longitude": "-93.2990"},
    {"place name": "Springfield", "latitude": "37.2590", "longitude": "-93.2910"},
    {"place name": "Springfield", "latitude": "37.1650", "longitude": "-93.2520"},
]}

# A genuine same-name duplicate inside one state, where no exact-name filter
# can help: the larger cluster is the one to keep.
TWIN = {"places": [
    {"place name": "Twin", "latitude": "40.0000", "longitude": "-88.0000"},
    {"place name": "Twin", "latitude": "40.0200", "longitude": "-88.0200"},
    {"place name": "Twin", "latitude": "40.0100", "longitude": "-88.0100"},
    {"place name": "Twin", "latitude": "37.0000", "longitude": "-90.0000"},
]}


def _miles(a, b):
    return ls._miles_between(a[0], a[1], b[0], b[1])


class CityDisambiguationTests(unittest.TestCase):

    def _lookup(self, payload, city, state):
        with mock.patch.object(ls, "_get_json", return_value=payload):
            return ls._zippopotam_city(city, state)

    def test_frankfort_resolves_to_the_real_frankfort(self):
        got = self._lookup(FRANKFORT_IL, "Frankfort", "IL")
        self.assertIsNotNone(got)
        # Within a couple of miles of Frankfort, not the 150-mile-away mean.
        self.assertLess(_miles(got, (41.5094, -87.8253)), 2.0,
                        f"resolved to {got}, expected Frankfort near Joliet")

    def test_frankfort_is_not_the_average_of_three_places(self):
        got = self._lookup(FRANKFORT_IL, "Frankfort", "IL")
        bad = (39.1337, -88.5662)      # what the old averaging produced
        self.assertGreater(_miles(got, bad), 100.0)

    def test_a_city_with_many_zips_still_averages_them(self):
        got = self._lookup(SPRINGFIELD_MO, "Springfield", "MO")
        self.assertIsNotNone(got)
        for p in SPRINGFIELD_MO["places"]:
            self.assertLess(_miles(got, (float(p["latitude"]), float(p["longitude"]))),
                            15.0)

    def test_true_duplicate_keeps_the_larger_cluster(self):
        got = self._lookup(TWIN, "Twin", "IL")
        self.assertLess(_miles(got, (40.01, -88.01)), 5.0)

    def test_near_miss_names_do_not_count_as_exact(self):
        self.assertNotEqual(ls._zip_name_key("West Frankfort"),
                            ls._zip_name_key("Frankfort"))
        # Punctuation and case must not split one place in two, though.
        self.assertEqual(ls._zip_name_key("St. Louis"), ls._zip_name_key("st louis"))

    def test_no_places_returns_none(self):
        self.assertIsNone(self._lookup({"places": []}, "Nowhere", "IL"))
        self.assertIsNone(self._lookup(None, "Nowhere", "IL"))


if __name__ == "__main__":
    unittest.main()
