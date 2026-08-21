"""Tests for bid_portals.snap_city_name.

The extraction model reads city names off page text and occasionally drops a
character. A real Cooper County scan filed a Missouri bid under "Ashlan".
That town does not exist, so it never geocodes -- which means radius search
cannot see the bid at all, and it never groups with the rest of Ashland's
work.

The correction is deliberately narrow, because the opposite error is worse:
silently relocating a bid to a town hundreds of miles away and presenting it
as local beats failing to place it only in the sense that it is louder.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_portals as bp


class WithinOneEditTests(unittest.TestCase):
    def test_a_dropped_character(self):
        self.assertTrue(bp._within_one_edit("ashlan", "ashland"))

    def test_an_inserted_character(self):
        self.assertTrue(bp._within_one_edit("ashlannd", "ashland"))

    def test_a_single_substitution(self):
        self.assertTrue(bp._within_one_edit("ashlend", "ashland"))

    def test_identical_strings_are_not_one_edit_apart(self):
        self.assertFalse(bp._within_one_edit("ashland", "ashland"))

    def test_two_edits_is_too_far(self):
        self.assertFalse(bp._within_one_edit("ashlnd", "ashlander"))
        self.assertFalse(bp._within_one_edit("aaa", "bbb"))

    def test_very_different_lengths_are_rejected_cheaply(self):
        self.assertFalse(bp._within_one_edit("x", "ashland"))


class SnapCityNameTests(unittest.TestCase):
    def setUp(self):
        self._orig = bp._towns_by_state_cache
        bp._towns_by_state_cache = {
            "MO": {"ashland": "Ashland", "springfield": "Springfield",
                   "aurora": "Aurora"},
            "KS": {"olathe": "Olathe", "olathr": "Olathr"},
        }
        self.addCleanup(lambda: setattr(bp, "_towns_by_state_cache", self._orig))

    def test_the_case_that_shipped(self):
        self.assertEqual(bp.snap_city_name("Ashlan", "MO"), "Ashland")

    def test_a_known_town_is_never_second_guessed(self):
        self.assertEqual(bp.snap_city_name("Aurora", "MO"), "Aurora")

    def test_an_unknown_town_far_from_everything_is_left_alone(self):
        self.assertEqual(bp.snap_city_name("Nowhereville", "MO"),
                         "Nowhereville")

    def test_an_ambiguous_match_is_left_alone(self):
        """Two known towns a single edit away means we cannot know which was
        meant, and guessing would relocate the bid."""
        self.assertEqual(bp.snap_city_name("Olathi", "KS"), "Olathi")

    def test_it_does_not_reach_into_another_state(self):
        self.assertEqual(bp.snap_city_name("Ashlan", "KS"), "Ashlan")

    def test_case_is_ignored_when_matching_but_canonical_case_is_returned(self):
        self.assertEqual(bp.snap_city_name("ASHLAN", "MO"), "Ashland")

    def test_missing_input_is_returned_unchanged(self):
        self.assertEqual(bp.snap_city_name("", "MO"), "")
        self.assertEqual(bp.snap_city_name("Ashlan", ""), "Ashlan")
        self.assertIsNone(bp.snap_city_name(None, "MO"))

    def test_an_unknown_state_is_left_alone(self):
        self.assertEqual(bp.snap_city_name("Ashlan", "ZZ"), "Ashlan")


class RealDirectoryTests(unittest.TestCase):
    """Against the shipped directory, not a fixture."""

    def test_ashland_is_actually_in_the_directory(self):
        self.assertIn("ashland", bp.towns_by_state().get("MO", {}))

    def test_the_real_directory_corrects_the_real_typo(self):
        self.assertEqual(bp.snap_city_name("Ashlan", "MO"), "Ashland")


if __name__ == "__main__":
    unittest.main()
