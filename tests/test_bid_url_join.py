"""Bid links must resolve against the page they were found on.

parse_civicplus_html was handed the LISTING url ("https://x.gov/Bids.aspx")
and treated it as a directory, appending the posting's relative href to it:

    https://x.gov/Bids.aspx  +  bids.aspx?bidID=415
    -> https://x.gov/Bids.aspx/bids.aspx?bidID=415      404

Every posting link on every CivicPlus site was malformed. Sampled live, 4 of 4
returned 404 and 0 of 22 detail pages could be loaded at all.

That one join explains three separate symptoms: bid cards linking to pages
that don't exist, bids arriving with no contact, and half of all bids having
no deadline -- because _enrich_from_detail_pages, which reads contact,
deadline and scope off the posting, could never fetch one. After the fix the
same sample loads 22 of 22, recovering a deadline on 95% and a phone on 81%.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_sources as bs

LISTING = "https://x.gov/Bids.aspx"


def _page(href):
    return (f'<html><body><a href="{href}">2026 Sidewalk Program</a>'
            f'<span>Status:</span><span>Closes:</span>'
            f'<span>Open</span><span>12/01/2026</span></body></html>')


def _url(href, base=LISTING):
    rows = bs.parse_civicplus_html(_page(href), base_url=base)
    return rows[0]["url"] if rows else None


class CivicPlusLinkTests(unittest.TestCase):
    def test_a_relative_href_resolves_beside_the_listing_not_under_it(self):
        """The exact shape that shipped: it produced /Bids.aspx/bids.aspx?..."""
        self.assertEqual(_url("bids.aspx?bidID=415"),
                         "https://x.gov/bids.aspx?bidID=415")

    def test_a_root_relative_href_resolves_from_the_site_root(self):
        self.assertEqual(_url("/Bids.aspx?bidID=415"),
                         "https://x.gov/Bids.aspx?bidID=415")

    def test_an_absolute_href_is_left_alone(self):
        self.assertEqual(_url("https://other.gov/Bids.aspx?bidID=9"),
                         "https://other.gov/Bids.aspx?bidID=9")

    def test_a_listing_in_a_subdirectory_resolves_within_it(self):
        self.assertEqual(_url("bids.aspx?bidID=1",
                              base="https://x.gov/dept/Bids.aspx"),
                         "https://x.gov/dept/bids.aspx?bidID=1")

    def test_no_link_is_ever_built_under_the_listing_page(self):
        """The signature of the bug: the listing filename appearing twice."""
        for href in ("bids.aspx?bidID=1", "/Bids.aspx?bidID=2",
                     "./bids.aspx?bidID=3"):
            u = _url(href) or ""
            self.assertNotIn("/Bids.aspx/", u, f"{href} -> {u}")

    def test_a_scheme_relative_href_keeps_https(self):
        self.assertEqual(_url("//x.gov/bids.aspx?bidID=5"),
                         "https://x.gov/bids.aspx?bidID=5")


class HomepageLinkTests(unittest.TestCase):
    """extract_bid_link_candidates had the same string-concatenation join."""

    def _cands(self, href, base="https://x.gov"):
        html = f'<html><body><a href="{href}">Bid Opportunities</a></body></html>'
        return bs.extract_bid_link_candidates(html, base)

    def test_a_relative_link_off_a_homepage_resolves(self):
        self.assertEqual(self._cands("bids"), ["https://x.gov/bids"])

    def test_a_root_relative_link_resolves(self):
        self.assertEqual(self._cands("/purchasing/bids"),
                         ["https://x.gov/purchasing/bids"])

    def test_an_absolute_link_is_left_alone(self):
        self.assertEqual(self._cands("https://portal.gov/bids"),
                         ["https://portal.gov/bids"])

    def test_it_resolves_against_a_deeper_page_too(self):
        self.assertEqual(self._cands("bids", base="https://x.gov/gov/index.html"),
                         ["https://x.gov/gov/bids"])


if __name__ == "__main__":
    unittest.main()


class TitleToPostingLinkTests(unittest.TestCase):
    """On a non-CivicPlus portal the extraction model is shown text only --
    _fetch_text strips every tag -- so it can never return a real href and
    every bid ended up pointed at the listing page. Matching the model's
    title back to the page's own anchors recovers the posting link, which is
    also what makes those bids enrichable for contact and deadline.

    Matching is strict on purpose: a wrong link is worse than the listing.
    """

    PAGE = ('<html><body>'
            '<a href="/nav/bids">Bids</a>'
            '<a href="/postings/17">2026 Sidewalk and ADA Ramp Replacement</a>'
            '<a href="mailto:clerk@x.gov">2026 Sidewalk and ADA Ramp Replacement</a>'
            '<a href="/postings/18">Roof Replacement at the Annex Building</a>'
            '</body></html>')
    BASE = "https://x.gov/purchasing/bids"

    def _link(self, title):
        return bs.link_for_title(self.PAGE, self.BASE, title)

    def test_a_title_finds_its_own_posting(self):
        self.assertEqual(self._link("2026 Sidewalk and ADA Ramp Replacement"),
                         "https://x.gov/postings/17")

    def test_a_mailto_is_never_returned_as_a_posting(self):
        self.assertNotIn("mailto", self._link(
            "2026 Sidewalk and ADA Ramp Replacement"))

    def test_a_title_with_no_matching_anchor_returns_nothing(self):
        """So the caller falls back to the listing page rather than guessing."""
        self.assertEqual(self._link("Water Main Replacement Phase Four"), "")

    def test_a_short_title_is_refused_rather_than_matched_loosely(self):
        """'Bids' would otherwise match the navigation link."""
        self.assertEqual(self._link("Bids"), "")

    def test_the_tightest_match_wins(self):
        page = ('<html><body>'
                '<a href="/all">Index of every bid including Sidewalk Program 2026</a>'
                '<a href="/p/9">Sidewalk Program 2026</a></body></html>')
        self.assertEqual(bs.link_for_title(page, self.BASE, "Sidewalk Program 2026"),
                         "https://x.gov/p/9")

    def test_it_resolves_relative_hrefs_against_the_listing(self):
        page = '<html><body><a href="detail.aspx?id=4">Sidewalk Program 2026</a></body></html>'
        self.assertEqual(bs.link_for_title(page, self.BASE, "Sidewalk Program 2026"),
                         "https://x.gov/purchasing/detail.aspx?id=4")

    def test_missing_input_is_handled(self):
        self.assertEqual(bs.link_for_title("", self.BASE, "Sidewalk Program 2026"), "")
        self.assertEqual(bs.link_for_title(self.PAGE, "", "Sidewalk Program 2026"), "")
        self.assertEqual(bs.link_for_title(self.PAGE, self.BASE, None), "")
