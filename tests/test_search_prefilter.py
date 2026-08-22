"""Place a search result before paying to read it.

A live 75-mile Aurora, MO scan extracted 38 bids and threw 21 away as
out_of_radius -- more than half. Each of those cost a page fetch and an AI
extraction before _place_bid worked out where it was.

Search engines cause this reliably: "Aurora MO sidewalk bid" returns
auroragov.org, which is Aurora, COLORADO, 700 miles away. The directory
already knows which town owns that domain, so the distance is answerable
before anything is spent.

The filter only acts on domains the directory can place. An unknown domain
is still fetched -- absence from a 14,400-entry index proves nothing, and
dropping a real local bid costs far more than one wasted extraction.
"""
import os
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls
import bid_portals

CENTER = {"city": "Aurora", "state": "MO", "lat": 36.9709, "lon": -93.7180}


class DomainIndexTests(unittest.TestCase):
    def test_a_known_domain_resolves_to_its_town(self):
        self.assertEqual(bid_portals.town_for_url("https://emporiaks.gov/Bids.aspx"),
                         ("Emporia", "KS"))

    def test_the_same_town_name_in_another_state_is_not_confused(self):
        """The exact trap: auroragov.org is Colorado, not Missouri."""
        got = bid_portals.town_for_url("https://auroragov.org/bids")
        self.assertIsNotNone(got)
        self.assertEqual(got[1], "CO")

    def test_www_is_ignored(self):
        self.assertEqual(bid_portals.town_for_url("https://www.emporiaks.gov/x"),
                         ("Emporia", "KS"))

    def test_an_unknown_domain_returns_nothing(self):
        self.assertIsNone(bid_portals.town_for_url("https://example.com/bids"))

    def test_junk_input_does_not_raise(self):
        for bad in ("", None, "not a url", "http://"):
            self.assertIsNone(bid_portals.town_for_url(bad))


class PreFilterTests(unittest.TestCase):
    def _run(self, url):
        seen, stats = set(), {}
        with patch.object(ls, "_web_search",
                          return_value=([{"url": url, "content": ""}], False)), \
             patch.object(ls, "_bidnet_direct_urls", return_value=[]), \
             patch.object(ls, "_fetch_text", return_value="") as fetch, \
             patch.object(ls, "_ai_extract", return_value=[]) as ai:
            ls._run_local_queries(["q"], "Aurora, MO", 5, {}, CENTER, 75, {},
                                  {}, seen, threading.Lock(), {},
                                  default_city="", state="MO", stats=stats)
        return stats, fetch, ai

    def test_a_far_away_known_town_is_skipped_before_any_cost(self):
        stats, fetch, ai = self._run("https://auroragov.org/bids")
        self.assertEqual(stats.get("search_hit_out_of_area"), 1)
        ai.assert_not_called()
        fetch.assert_not_called()

    def test_an_unknown_domain_is_still_fetched(self):
        """Absence from the index proves nothing; dropping a real local bid
        costs far more than one wasted extraction."""
        stats, _, _ = self._run("https://some-town-we-never-crawled.org/bids")
        self.assertIsNone(stats.get("search_hit_out_of_area"))

    def test_a_nearby_known_town_is_not_skipped(self):
        stats, _, _ = self._run("https://aurora-cityhall.org/Bids.aspx")
        self.assertIsNone(stats.get("search_hit_out_of_area"))


if __name__ == "__main__":
    unittest.main()
