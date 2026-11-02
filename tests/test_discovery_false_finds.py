"""Discovery must not record a page that only looks like a bid page.

Re-crawling Missouri on 2026-11-02 recorded a "404 | City of Liberal" page
(the server answers 200 to everything), a JPEG at Paris's address whose
bytes happened to contain "bid", and Holden's one news post about an August
trash-hauler RFP. Each would have been re-read by every scan forever.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import discover_bid_portals as d  # noqa: E402

LISTING = ("<html><head><title>Bids &amp; RFPs | City of Dexter</title></head><body>"
           + "<h1>Bids and RFPs</h1><p>Invitation to bid: 2026 sidewalk program. "
           "Sealed bids due November 20. Request for proposal: engineering.</p>" * 6
           + "</body></html>")
SOFT_404 = ("<html><head><title>404 | City of Liberal</title></head><body>"
            "<nav>Bids Purchasing RFP Solicitations Vendor registration</nav>"
            + "<p>Welcome to the City of Liberal. Pay your utility bill online.</p>" * 8
            + "</body></html>")
JPEG = "\xff\xd8\xff\xe0\x00\x10JFIF" + "".join(chr(c % 31) for c in range(4000)) + " bid rfp "


class FalseFindTests(unittest.TestCase):
    def test_a_real_listing_passes(self):
        self.assertTrue(d._looks_like_a_bid_page(LISTING)[0])

    def test_a_soft_404_is_rejected(self):
        self.assertFalse(d._looks_like_a_bid_page(SOFT_404)[0])

    def test_a_file_read_as_text_is_rejected(self):
        self.assertFalse(d._looks_like_a_bid_page(JPEG)[0])

    def test_a_single_news_post_is_not_a_listing(self):
        for url in ("https://holdenmo.gov/news-detail?item_id=35064",
                    "https://chillicothecity.org/CivicAlerts.aspx?AID=55",
                    "https://x.gov/news/sidewalk-bid-awarded",
                    "https://x.gov/Government/News/National-Procurement-Month"):
            self.assertFalse(d.is_listing_url(url), url)
        self.assertTrue(d.is_listing_url("https://x.gov/Bids.aspx"))
        self.assertTrue(d.is_listing_url("https://kirksville.gov/p/bid-postings"))
        self.assertTrue(d.is_listing_url("https://allendalenj.gov/news/bids/"))
        self.assertTrue(d.is_listing_url("https://bayvilleny.gov/news/bid-notices"))


class ReverifyTests(unittest.TestCase):
    def verdict(self, html, url="https://x.gov/bids"):
        with patch.object(d, "_fetch", return_value=html):
            return d._reverify_verdict({"bid_url": url})

    def test_a_page_that_fails_to_load_is_kept(self):
        """Unreachable from here proves nothing about the page."""
        self.assertEqual(self.verdict(""), "keep")

    def test_false_pages_are_dropped_with_a_reason(self):
        self.assertEqual(self.verdict(SOFT_404), "error_page")
        self.assertEqual(self.verdict(JPEG), "binary")
        self.assertEqual(self.verdict(LISTING, "https://x.gov/news-detail?item_id=1"),
                         "single_document")

    def test_a_real_listing_is_kept(self):
        self.assertEqual(self.verdict(LISTING), "keep")


if __name__ == "__main__":
    unittest.main()
