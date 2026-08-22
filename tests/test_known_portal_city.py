"""A bid on a town's own bid page belongs to that town.

_place_bid drops any bid it cannot locate (no_location) -- correct for a
search result, which could be about anywhere, and wrong for a known portal,
which IS that town's own page. A solicitation that doesn't restate its city
in the body is completely normal there.

This was fixed once, on the CivicPlus branch, and missed on the branch that
handles every other platform -- 1,260 `agency` portals in the directory. A
live Emporia scan lost 8 bids to it.
"""
import os
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls

SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "license_server.py"), encoding="utf-8").read()


class BothBranchesAgreeTests(unittest.TestCase):
    """Structural, because the failure mode is two branches drifting apart."""

    def test_every_known_portal_placement_falls_back_to_the_town(self):
        start = SRC.index("def _run_known_portals(")
        end = SRC.index("\ndef ", start + 10)
        body = SRC[start:end]
        calls = re.findall(r"default_city=([^,\n]+)", body)
        placements = [c.strip() for c in calls if "or city" in c or "default_city" == c.strip()]
        self.assertTrue(placements, "expected _place_bid calls to inspect")
        for c in placements:
            self.assertIn("or city", c,
                          "a known portal is the town's own page; placing a "
                          "bid from it without the town fallback drops it")


class PlacementTests(unittest.TestCase):
    """Behavioural: a dateless, city-less bid from a known portal is kept."""

    def setUp(self):
        self.center = {"city": "Emporia", "state": "KS",
                       "lat": 38.4039, "lon": -96.1817}

    def _place(self, default_city):
        grouped, stats = {}, {}
        bid = {"title": "2026 Sidewalk Replacement", "status": "Open",
               "deadline": "", "url": "https://emporiaks.gov/b/1", "city": ""}
        ls._place_bid(grouped, bid, self.center, 25, {},
                      default_city=default_city,
                      city_coords={("emporia", "KS"): (38.4039, -96.1817)},
                      default_state="KS",
                      fallback_coords=(38.4039, -96.1817), stats=stats)
        return grouped, stats

    def test_without_a_town_the_bid_is_dropped(self):
        """The old behaviour, kept as a test so the reason is recorded."""
        grouped, stats = self._place("")
        self.assertEqual(grouped, {})
        self.assertEqual(stats.get("no_location"), 1)

    def test_with_the_town_the_bid_is_kept(self):
        grouped, stats = self._place("Emporia")
        self.assertTrue(grouped, "a bid on Emporia's own page is an Emporia bid")
        self.assertIsNone(stats.get("no_location"))


if __name__ == "__main__":
    unittest.main()
