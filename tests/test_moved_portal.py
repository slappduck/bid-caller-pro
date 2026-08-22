"""A city that moved its bids elsewhere must not be read as an empty page.

The commonest reason a CivicPlus Bids.aspx page holds no bids is that the
city migrated its solicitations onto a hosted procurement platform and left
the old page in place as a signpost: "View Open Solicitations", pointing at
BeaconBid, OpenGov or BidNet Direct.

Sampled live: Chicopee MA and Halifax MA moved to beaconbid.com -- a platform
nothing in the codebase knew existed -- and Farmville VA to OpenGov. The old
page parses to zero rows and is not empty, so every scan counted it as a
parser miss and spent an AI call reading a page with no bids on it. 25 of
~180 portal reads in the benchmark ended that way.
"""
import os
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_portals as bp
import bid_sources as bs
import license_server as ls

SIGNPOST = ("https://chicopeema.gov/Bids.aspx")
MOVED_TO = "https://www.beaconbid.com/solicitations/city-of-chicopee/open"
PAGE = (f'<html><head><title>Bid Opportunities | Chicopee, MA</title></head>'
        f'<body><a href="{MOVED_TO}">View Open Solicitations</a>'
        + "Bids are now posted on our new portal. " * 20 + '</body></html>')


class LinkDiscoveryTests(unittest.TestCase):
    def test_a_city_scoped_hosted_link_is_found(self):
        self.assertEqual(bs.hosted_portal_link(PAGE), MOVED_TO)

    def test_opengov_is_found(self):
        html = '<a href="https://procurement.opengov.com/portal/farmvilleva">Bids</a>'
        self.assertEqual(bs.hosted_portal_link(html),
                         "https://procurement.opengov.com/portal/farmvilleva")

    def test_a_search_url_is_not_a_portal(self):
        """A query string is how every one of these platforms says "search"."""
        html = '<a href="https://www.bidnetdirect.com/search?keywords=curb">Bids</a>'
        self.assertEqual(bs.hosted_portal_link(html), "")

    def test_a_platform_homepage_is_not_a_portal(self):
        html = '<a href="https://www.bidnetdirect.com/">BidNet</a>'
        self.assertEqual(bs.hosted_portal_link(html), "")

    def test_an_ordinary_link_on_the_city_site_is_not_one(self):
        html = '<a href="https://cityofwoodland.org/1710/Bid-Opportunities">Bids</a>'
        self.assertEqual(bs.hosted_portal_link(html), "")


class DirectoryTests(unittest.TestCase):
    def test_a_city_scoped_hosted_url_can_be_learned(self):
        d = {}
        bp.learn_portal(d, "Farmville", "VA",
                        "https://procurement.opengov.com/portal/farmvilleva",
                        allow_hosted=True)
        self.assertTrue(d, "the city's own page on a hosted platform belongs "
                           "in the directory")

    def test_it_is_still_refused_without_the_flag(self):
        """Only a caller that found the link on the city's own site knows
        whose page it is."""
        d = {}
        bp.learn_portal(d, "Farmville", "VA",
                        "https://procurement.opengov.com/portal/farmvilleva")
        self.assertEqual(d, {})

    def test_a_search_url_is_refused_even_with_the_flag(self):
        d = {}
        bp.learn_portal(d, "X", "VA",
                        "https://www.bidnetdirect.com/search?q=curb",
                        allow_hosted=True)
        self.assertEqual(d, {})


class ScanFollowsItTests(unittest.TestCase):
    def test_the_scan_learns_and_reads_the_new_address(self):
        fetched, learned = [], []

        def _fetch(u, timeout=None):
            fetched.append(u)
            if u == SIGNPOST:
                return PAGE, "ok"
            return "<html><body>" + "Sidewalk bids. " * 40 + "</body></html>", "ok"

        pdb, stats = {}, {}
        with patch.object(ls, "_fetch_page", _fetch), \
             patch.object(ls.bid_portals, "get_portals",
                          lambda *_a, **_k: [{"url": SIGNPOST,
                                              "platform": "civicplus"}]), \
             patch.object(ls.bid_portals, "record_result", lambda *_a, **_k: None), \
             patch.object(ls.bid_portals, "learn_portal",
                          lambda _d, _c, _s, u, **_k: learned.append(u)), \
             patch.object(ls, "_ai_extract", lambda *_a, **_k: []):
            ls._run_known_portals("Chicopee", "MA", "Chicopee, MA", {},
                                  {"city": "Chicopee", "state": "MA",
                                   "lat": 42.15, "lon": -72.61}, 25, {}, {},
                                  threading.Lock(), pdb, default_city="Chicopee",
                                  town_coords=(42.15, -72.61), stats=stats)

        self.assertEqual(learned, [MOVED_TO], "the new address must be kept")
        self.assertIn(MOVED_TO, fetched, "and read in this same scan")
        self.assertEqual(stats.get("portal_moved_to_hosted"), 1)
        self.assertIsNone(stats.get("civicplus_parse_miss"),
                          "a moved portal is not a parser gap")


if __name__ == "__main__":
    unittest.main()
