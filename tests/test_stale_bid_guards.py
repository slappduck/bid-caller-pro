"""Tests for the two guards that keep expired solicitations out of the feed.

Both came from bids a customer was actually shown. The second one, a City of
Independence job on bidscopeai.com, is the sharper case: the page said
"Status: Closed (past due)" and "Due Date: Jul 24, 2026" -- 26 days in the
past -- and the card displayed "In 8 days".

That is the whole failure in one line. _apply_deadline_status already closes
anything whose deadline has passed, but "In 8 days" contains no date and no
year, so there was nothing to check it against. A countdown is not a
deadline: it was rendered by somebody's page at an unknown moment and cannot
be compared to today.
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


class CleanDeadlineTests(unittest.TestCase):
    def test_the_countdown_that_shipped_is_dropped(self):
        self.assertEqual(ls._clean_deadline("In 8 days"), "")

    def test_countdown_variants_are_dropped(self):
        for raw in ("in 3 days", "In 1 week", "tomorrow", "next week",
                    "12 days left", "closing soon", "TBD", "ongoing"):
            self.assertEqual(ls._clean_deadline(raw), "",
                             f"{raw!r} should not survive as a deadline")

    def test_a_real_date_is_kept_exactly(self):
        self.assertEqual(ls._clean_deadline("July 24, 2026"), "July 24, 2026")
        self.assertEqual(ls._clean_deadline("12/01/2026"), "12/01/2026")

    def test_a_date_wrapped_in_words_is_kept(self):
        raw = "Bids due December 1, 2026 at 2:00 p.m."
        self.assertEqual(ls._clean_deadline(raw), raw)

    def test_a_bare_year_is_kept_because_it_is_still_checkable(self):
        """_apply_deadline_status falls back to a 4-digit year, so FY2027
        carries real information even though it is not a date."""
        self.assertEqual(ls._clean_deadline("FY2027"), "FY2027")

    def test_empty_stays_empty(self):
        self.assertEqual(ls._clean_deadline(""), "")
        self.assertEqual(ls._clean_deadline(None), "")

    def test_whitespace_is_normalised_not_dropped(self):
        self.assertEqual(ls._clean_deadline("  July 24,   2026 "),
                         "July 24, 2026")


class PageDeclaresClosedTests(unittest.TestCase):
    DETAIL_PAGE = ("City of Independence. Status: Closed (past due). "
                   "Solicitation Identifier: 44850. Due Date: Jul 24, 2026.")

    def test_a_single_solicitation_page_saying_closed_is_believed(self):
        self.assertTrue(ls._page_declares_closed(self.DETAIL_PAGE, 1))

    def test_a_listing_page_is_never_closed_wholesale(self):
        """One row reading "Closed" says nothing about the other rows, and
        closing them all would throw away real work -- a worse failure than
        showing one stale bid."""
        self.assertFalse(ls._page_declares_closed(self.DETAIL_PAGE, 4))

    def test_a_page_with_no_such_declaration_is_left_alone(self):
        self.assertFalse(ls._page_declares_closed(
            "Invitation to Bid -- 2026 Sidewalk Program. Bids due Sept 15.", 1))

    def test_no_bids_means_nothing_to_close(self):
        self.assertFalse(ls._page_declares_closed(self.DETAIL_PAGE, 0))


class ExtractionAppliesBothGuardsTests(unittest.TestCase):
    def setUp(self):
        self._key = ls.OPENAI_API_KEY
        ls.OPENAI_API_KEY = "test-key"
        self.addCleanup(lambda: setattr(ls, "OPENAI_API_KEY", self._key))

    def _fake_openai(self, payload):
        import json as _json

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return _json.dumps({
                    "choices": [{"message": {"content": _json.dumps(payload)}}]
                }).encode()

        orig = ls.urllib.request.urlopen
        ls.urllib.request.urlopen = lambda *a, **k: _Resp()
        self.addCleanup(lambda: setattr(ls.urllib.request, "urlopen", orig))

    def test_the_independence_bid_comes_back_closed_with_no_fake_deadline(self):
        self._fake_openai([{"title": "2026 Concrete Program",
                            "status": "Open", "deadline": "In 8 days"}])
        bids = ls._ai_extract("Independence, KY",
                              PageDeclaresClosedTests.DETAIL_PAGE)
        self.assertEqual(bids[0]["status"], "Closed")
        self.assertEqual(bids[0]["deadline"], "")
        self.assertFalse(ls._is_open_bid(bids[0]))

    def test_a_live_bid_keeps_its_real_deadline_and_stays_open(self):
        page = ("Invitation to Bid -- 2026 Sidewalk and ADA Ramp Program. "
                "Sealed bids will be received until 2:00 p.m. December 1, 2026.")
        self._fake_openai([{"title": "2026 Sidewalk Program", "status": "Open",
                            "deadline": "December 1, 2026"}])
        bids = ls._ai_extract("Aurora, MO", page)
        self.assertEqual(bids[0]["deadline"], "December 1, 2026")
        self.assertTrue(ls._is_open_bid(bids[0]))

    def test_a_countdown_is_stripped_even_on_a_page_that_looks_open(self):
        page = "Invitation to Bid -- sidewalk work. Bids due soon."
        self._fake_openai([{"title": "Sidewalk", "status": "Open",
                            "deadline": "in 3 days"}])
        bids = ls._ai_extract("Aurora, MO", page)
        self.assertEqual(bids[0]["deadline"], "")

    def test_a_listing_page_with_one_closed_row_keeps_its_open_bids(self):
        page = ("Current Bids. 2026 Street Improvements, bids due Sept 30, "
                "2026. 2025 Sidewalk Program: status: closed.")
        self._fake_openai([
            {"title": "2026 Street Improvements", "status": "Open",
             "deadline": "September 30, 2026"},
            {"title": "2025 Sidewalk Program", "status": "Closed",
             "deadline": ""},
        ])
        bids = ls._ai_extract("Aurora, MO", page)
        self.assertTrue(ls._is_open_bid(bids[0]))
        self.assertFalse(ls._is_open_bid(bids[1]))


if __name__ == "__main__":
    unittest.main()
