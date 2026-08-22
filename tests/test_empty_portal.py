"""An empty bid page is not a parser failure.

Most municipal bid pages have nothing posted most of the time -- 21 of 30
sampled CivicPlus portals were empty. Every one of those used to fall through
to a full AI extraction of the page, to discover the same nothing.

That cost real money, but the expensive part was latency: those calls run
inside the scan's known-town time budget, so pointless extractions on empty
pages are paid for by other towns never being read at all.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_sources as bs

EMPTY = ("<html><head><title>Bid Postings | Anytown, MO</title></head>"
         "<body><h1>Bid Postings</h1><p>Sign up to receive a text message "
         "when new bids are added.</p></body></html>")
WITH_BIDS = ('<html><body><a href="/Bids.aspx?bidID=7">Sidewalk Program</a>'
             '<span>Status:</span><span>Closes:</span>'
             '<span>Open</span><span>12/01/2026</span></body></html>')


class EmptyPageTests(unittest.TestCase):
    def test_a_real_but_empty_bid_page_is_recognised(self):
        self.assertTrue(bs.civicplus_page_is_empty(EMPTY))

    def test_a_page_with_bids_is_not_empty(self):
        self.assertFalse(bs.civicplus_page_is_empty(WITH_BIDS))

    def test_an_error_page_is_not_treated_as_empty(self):
        """A 404, a login wall or a redirect also has no bid links -- and
        those DO deserve the AI fallback, so emptiness needs a positive
        marker rather than just the absence of links."""
        for junk in ("<html><body>404 Not Found</body></html>",
                     "<html><body>Please sign in to continue</body></html>",
                     "<html><body>Access denied</body></html>"):
            self.assertFalse(bs.civicplus_page_is_empty(junk), junk[:30])

    def test_nothing_at_all_is_not_empty_it_is_a_failure(self):
        self.assertFalse(bs.civicplus_page_is_empty(""))
        self.assertFalse(bs.civicplus_page_is_empty(None))

    def test_the_marker_match_is_case_insensitive(self):
        self.assertTrue(bs.civicplus_page_is_empty(
            "<html><body>BID POSTINGS</body></html>"))

    def test_a_page_whose_only_marker_is_the_url_still_counts(self):
        """19 of 21 empty pages said 'Bid Postings'; 20 of 21 mentioned
        Bids.aspx. Either alone is enough."""
        self.assertTrue(bs.civicplus_page_is_empty(
            '<html><body><a href="/Bids.aspx">All bids</a></body></html>'))


if __name__ == "__main__":
    unittest.main()
