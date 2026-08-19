"""Tests for _resolve_bid_url — which link a bid card actually opens.

Contractors reported bid links landing on "page doesn't exist". Two causes,
both pinned here:

  * the extraction prompt says 'Use "" for any missing field', so the model
    returns "url": "". setdefault() saw an existing key and left the empty
    string, so the card rendered no link at all;
  * _fetch_text strips every HTML tag before the text reaches the model, so
    it never sees an href. Asked for a "url" regardless, it reconstructs a
    plausible-looking one from the domain plus a guessed path -- which 404s.

So an absolute URL from the model is trusted only if it appears verbatim in
the text the model was shown. Everything else falls back to the page the bid
was actually found on, which is known-reachable.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls

PAGE = "https://cityofaurora.mo.gov/Bids.aspx"


class EmptyAndMissingTests(unittest.TestCase):
    def test_empty_string_falls_back_to_the_page(self):
        """The original bug: setdefault never fired on "" so the card had no
        link at all."""
        self.assertEqual(ls._resolve_bid_url("", PAGE), PAGE)

    def test_none_falls_back_to_the_page(self):
        self.assertEqual(ls._resolve_bid_url(None, PAGE), PAGE)

    def test_whitespace_only_falls_back_to_the_page(self):
        self.assertEqual(ls._resolve_bid_url("   ", PAGE), PAGE)


class FabricatedUrlTests(unittest.TestCase):
    def test_an_invented_absolute_url_is_rejected(self):
        """The 404 case. The model never saw an href, so a URL that is not in
        the source text was reconstructed, not read."""
        invented = "https://cityofaurora.mo.gov/bids/2026-14-sidewalk-program"
        self.assertEqual(
            ls._resolve_bid_url(invented, PAGE, source_text="Bid 2026-14 Sidewalk Program"),
            PAGE)

    def test_same_domain_does_not_make_it_trustworthy(self):
        """A guessed path on the RIGHT domain is the most common 404 of all --
        matching hosts must not be treated as verification."""
        invented = "https://cityofaurora.mo.gov/some/guessed/path"
        self.assertEqual(ls._resolve_bid_url(invented, PAGE, source_text="no urls here"),
                         PAGE)

    def test_a_url_printed_on_the_page_is_kept(self):
        real = "https://cityofaurora.mo.gov/DocumentCenter/View/812"
        text = f"Full notice and plans: {real} — bids due Sept 15."
        self.assertEqual(ls._resolve_bid_url(real, PAGE, source_text=text), real)

    def test_a_cross_domain_url_printed_on_the_page_is_kept(self):
        """Plan rooms are legitimately on another host; the test is whether
        the page published it, not whether the domain matches."""
        real = "https://www.bidnetdirect.com/mo/solicitation/9912"
        text = f"Documents are available at {real}"
        self.assertEqual(ls._resolve_bid_url(real, PAGE, source_text=text), real)

    def test_no_source_text_means_no_verification_possible(self):
        self.assertEqual(
            ls._resolve_bid_url("https://cityofaurora.mo.gov/x", PAGE, source_text=""),
            PAGE)


class RelativeUrlTests(unittest.TestCase):
    def test_root_relative_path_is_resolved_against_the_page(self):
        self.assertEqual(
            ls._resolve_bid_url("/Bids.aspx?bidID=42", PAGE),
            "https://cityofaurora.mo.gov/Bids.aspx?bidID=42")

    def test_relative_resolution_does_not_need_source_text(self):
        """A root-relative path is a real path read off the page, not an
        invented absolute URL, so it does not need the same proof."""
        out = ls._resolve_bid_url("/DocumentCenter/View/9", PAGE, source_text="")
        self.assertEqual(out, "https://cityofaurora.mo.gov/DocumentCenter/View/9")


class UnsafeSchemeTests(unittest.TestCase):
    def test_javascript_scheme_never_survives(self):
        self.assertEqual(ls._resolve_bid_url("javascript:alert(1)", PAGE), PAGE)

    def test_mailto_is_not_a_bid_link(self):
        self.assertEqual(ls._resolve_bid_url("mailto:clerk@aurora.gov", PAGE), PAGE)

    def test_a_bare_word_is_not_a_link(self):
        self.assertEqual(ls._resolve_bid_url("see attached", PAGE), PAGE)

    def test_case_is_not_a_bypass(self):
        self.assertEqual(ls._resolve_bid_url("JaVaScRiPt:alert(1)", PAGE), PAGE)


class NoPageUrlTests(unittest.TestCase):
    def test_everything_degrades_to_empty_rather_than_crashing(self):
        self.assertEqual(ls._resolve_bid_url("", ""), "")
        self.assertEqual(ls._resolve_bid_url(None, None), "")
        self.assertEqual(ls._resolve_bid_url("/x", ""), "")


if __name__ == "__main__":
    unittest.main()
