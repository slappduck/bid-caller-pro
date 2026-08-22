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


class ParkedAndHijackedDomainTests(unittest.TestCase):
    """A municipal domain that lapsed is worse than a dead link.

    Two entries in the live directory -- forestparkga.org and lewistonmn.org
    -- were re-registered after the city let them go and now serve offshore
    gambling pages. Both were being recorded as healthy portals on every
    scan, which means the app was handing customers a link to a casino.
    """

    def test_a_broker_parking_page_is_missing(self):
        for html in ('<html><title>HugeDomains.com</title></html>',
                     '<html><title>x</title>Buy this domain</html>',
                     '<html><title>y</title>This domain is for sale</html>'):
            self.assertTrue(bs.page_is_missing(html), html[:50])

    def test_a_hijacked_domain_is_missing(self):
        for html in ('<html><title>ALEXISTOGEL Kehadiran Situs Togel 4D '
                     '&amp; Bandar Toto Macau</title></html>',
                     '<html><title>CARICUAN99: Situs Game Online</title>'
                     'bandar togel terpercaya</html>'):
            self.assertTrue(bs.page_is_missing(html), html[:50])

    def test_other_ways_of_writing_404(self):
        self.assertTrue(bs.page_is_missing(
            "<html><title>Status Code 404 - The City of Thibodaux, "
            "Louisiana</title></html>"))


class WrongModuleTests(unittest.TestCase):
    """A /Bids.aspx URL serving the site's homepage.

    Sampling 500 CivicPlus entries: 16 answered with something that is not a
    bid page at all -- "Home - Lake County, Ohio", "News & Events | City of
    Arlington, TX", "Sitka Police Department", "Ethics Review Board - City of
    New Orleans". Each cleared the 200-character length check, was recorded
    as healthy, and cost an AI call every scan to read bids it never had.

    The discriminator is the page's own title, because our parser failing to
    read a body proves nothing. Validated against all 500: zero false
    positives on the 437 pages that yield rows, are a healthy empty portal,
    or have moved.
    """

    def test_a_homepage_served_for_the_bid_url_is_wrong(self):
        for title in ("Home - Lake County, Ohio",
                      "News &amp; Events | City of Arlington, TX",
                      "Sitka Police Department | City and Borough of Sitka",
                      "Ethics Review Board - Home - City of New Orleans",
                      "Home | Muscle Shoals"):
            html = f"<html><head><title>{title}</title></head><body>x</body></html>"
            self.assertTrue(bs.page_is_wrong_module(html), title)

    def test_a_real_bid_page_is_never_wrong(self):
        """These three survived the sweep of 500 and must keep surviving:
        our parser cannot read them, but they are genuinely bid pages."""
        for title in ("Purchasing | Taos County, NM",
                      "Procurement | Teton County, WY",
                      "Portal - Open Opportunities - City of Norwich, CT",
                      "Oconee County Current Solicitations | Vendor Registry",
                      "City of Owosso | Owosso Michigan &gt; Bids",
                      "Bids &amp; RFPs | Farmville, VA"):
            html = f"<html><head><title>{title}</title></head><body>x</body></html>"
            self.assertFalse(bs.page_is_wrong_module(html), title)

    def test_the_civicplus_marker_overrides_a_bare_title(self):
        """A real CivicPlus bids page whose title says nothing useful."""
        html = ('<html><head><title>City of X</title></head><body>'
                '<a href="/Bids.aspx">Bid Postings</a></body></html>')
        self.assertFalse(bs.page_is_wrong_module(html))

    def test_no_title_is_not_evidence(self):
        self.assertFalse(bs.page_is_wrong_module("<html><body>x</body></html>"))


class WrongModuleFailsOutTests(unittest.TestCase):
    def test_the_scan_records_it_as_a_failure(self):
        calls, stats = [], {}
        html = ("<html><head><title>Home - Lake County, Ohio</title></head>"
                "<body>" + "County news and services. " * 40 + "</body></html>")
        url = "https://lakecountyohio.gov/Bids.aspx"
        with patch.object(ls, "_fetch_page", lambda u, timeout=None: (html, "ok")), \
             patch.object(ls.bid_portals, "get_portals",
                          lambda *_a, **_k: [{"url": url, "platform": "civicplus"}]), \
             patch.object(ls.bid_portals, "record_result",
                          lambda _p, _c, _s, _u, ok: calls.append(ok)), \
             patch.object(ls, "_ai_extract",
                          lambda *_a, **_k: self.fail("must not reach the AI")):
            ls._run_known_portals("Painesville", "OH", "Painesville, OH", {},
                                  {"city": "Painesville", "state": "OH",
                                   "lat": 41.72, "lon": -81.24}, 25, {}, {},
                                  threading.Lock(), {},
                                  default_city="Painesville",
                                  town_coords=(41.72, -81.24), stats=stats)
        self.assertEqual(calls, [False])
        self.assertEqual(stats.get("portal_wrong_module"), 1)


if __name__ == "__main__":
    unittest.main()
