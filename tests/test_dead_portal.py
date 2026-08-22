"""A wrong portal URL has to be able to fail its way out of the directory.

bid_portals.MAX_FAIL retires an entry after five consecutive no-content
results, which only works if a bad read is recorded as a failure. Municipal
sites almost always serve their not-found page with HTTP 200, and a lapsed
domain serves a sales page the same way, so _fetch_page reported "ok", the
body cleared the 200-character length check, and the entry was recorded as a
SUCCESS on every scan -- for good.

Sampled across 400 CivicPlus entries in the live directory, 21 were pages
like this: "404 | City of Drayton", "Page not found - City of Sheffield Lake
Ohio", "CityOfPawnee.com is for sale | HugeDomains", and one lapsed city
domain now serving an online-casino page. Roughly one entry in twenty, held
in the directory permanently.
"""
import os
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_sources as bs
import license_server as ls

MISSING = (
    "<html><head><title>404 | City of Drayton</title></head>"
    "<body>" + "The page you requested could not be found. " * 20 + "</body></html>",
    "<html><head><title>Page not found - City of Sheffield Lake Ohio</title>"
    "</head><body>" + "Sorry. " * 100 + "</body></html>",
    "<html><head><title>CityOfPawnee.com is for sale | HugeDomains</title>"
    "</head><body>" + "Buy this domain. " * 40 + "</body></html>",
    "<html><body><h1>404</h1>" + "Not here. " * 40 + "</body></html>",
)
# Real bid pages our CivicPlus parser happens not to read. These must NOT be
# failed: they fall through to the AI path, which handles them.
LIVE = (
    "<html><head><title>Bid Opportunities | Chicopee, MA</title></head>"
    "<body>" + "Sidewalk reconstruction bids are listed below. " * 20 + "</body></html>",
    "<html><head><title>Purchasing | Taos County, NM</title></head>"
    "<body>" + "Current solicitations for paving work. " * 20 + "</body></html>",
    # A street address containing 404 is not a status code.
    "<html><head><title>Holton KS - Official Website</title></head>"
    "<body>City Hall, 404 Oak Street. " + "Bids for curb work. " * 20 + "</body></html>",
)


class DetectionTests(unittest.TestCase):
    def test_a_not_found_page_served_with_200_is_recognised(self):
        for html in MISSING:
            self.assertTrue(bs.page_is_missing(html), html[:70])

    def test_a_real_bid_page_is_not(self):
        for html in LIVE:
            self.assertFalse(bs.page_is_missing(html), html[:70])

    def test_an_empty_portal_is_not_a_missing_one(self):
        """The right page with nothing posted stays in the directory."""
        empty = ('<html><head><title>Bids | City of X</title></head><body>'
                 '<a href="/Bids.aspx">Bid Postings</a>'
                 'There are no bid postings at this time.</body></html>')
        self.assertTrue(bs.civicplus_page_is_empty(empty))
        self.assertFalse(bs.page_is_missing(empty))


class ItIsRecordedAsAFailureTests(unittest.TestCase):
    def _scan(self, html):
        url = "https://draytonnd.com/Bids.aspx"
        calls = []
        stats = {}
        with patch.object(ls, "_fetch_page", lambda u, timeout=None: (html, "ok")), \
             patch.object(ls.bid_portals, "get_portals",
                          lambda *_a, **_k: [{"url": url, "platform": "civicplus"}]), \
             patch.object(ls.bid_portals, "record_result",
                          lambda _pdb, _c, _s, u, ok: calls.append(ok)):
            ls._run_known_portals("Drayton", "ND", "Drayton, ND", {},
                                  {"city": "Drayton", "state": "ND",
                                   "lat": 48.57, "lon": -97.18}, 25, {}, {},
                                  threading.Lock(), {}, default_city="Drayton",
                                  town_coords=(48.57, -97.18), stats=stats)
        return calls, stats

    def test_a_missing_page_is_recorded_as_a_failure(self):
        """Without this the entry is immortal: MAX_FAIL never counts."""
        calls, stats = self._scan(MISSING[0])
        self.assertEqual(calls, [False])
        self.assertEqual(stats.get("portal_page_missing"), 1)

    def test_a_live_page_is_still_recorded_as_a_success(self):
        calls, stats = self._scan(LIVE[0])
        self.assertEqual(calls, [True])
        self.assertIsNone(stats.get("portal_page_missing"))


if __name__ == "__main__":
    unittest.main()
