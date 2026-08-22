"""Tests for reading status and closing date off a CivicPlus listing.

A customer's scan surfaced a job he had already won. The cause was in this
parser and it affected roughly 2,400 portals -- CivicPlus is the single most
common platform in the directory, and one of the URLs read for every one of
them is `showAllBids=on`, which deliberately includes closed, awarded and
cancelled postings.

CivicPlus lays a row out as LABELS then VALUES:

    Status: Closes: Closed 3/11/2025 4:00 PM

so the status word does not follow "Status:" directly. The old pattern
required that it did, matched nothing, and produced status "" -- which
_place_bid defaults to Open. Every awarded job on every CivicPlus site was
therefore presented as live work.

Two further faults in the same rows: CivicPlus emits a second "Read on:"
link per posting, which produced a duplicate row titled after the link text;
and the Status/Closes values sit AFTER that second link, past the posting's
summary, so a fixed 600-character window never reached them.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_sources as bs


def _posting(bid_id, title, summary, status, closes):
    """One posting as CivicPlus actually renders it."""
    return (
        f'<a href="/Bids.aspx?bidID={bid_id}">{title}</a>'
        f'<div class="summary">{summary}</div>'
        f'<a href="/Bids.aspx?bidID={bid_id}">Read&nbsp;on: {title}</a>'
        f'<span>Status:</span><span>Closes:</span>'
        f'<span>{status}</span><span>{closes} 4:00 PM</span>'
    )


LONG = ("The City of Emporia is seeking qualified firms. " * 12)

PAGE = "<html><body>" + "".join([
    _posting(1, "2026 Sidewalk Replacement Program", LONG, "Open", "9/1/2026"),
    _posting(2, "Rec Center Parking Lot", LONG, "Awarded", "3/11/2025"),
    _posting(3, "Curb and Gutter Repair", LONG, "Closed", "7/30/2024"),
]) + "</body></html>"


class CivicPlusListingTests(unittest.TestCase):
    def setUp(self):
        self.rows = bs.parse_civicplus_html(PAGE, "https://emporiaks.gov")
        self.by_title = {r["title"]: r for r in self.rows}

    def test_one_row_per_posting_not_two(self):
        """The "Read on:" link is the same bid, not another one."""
        self.assertEqual(len(self.rows), 3)

    def test_no_row_is_titled_after_the_read_on_link(self):
        self.assertFalse([t for t in self.by_title if t.lower().startswith("read on")])

    def test_an_awarded_posting_is_read_as_awarded(self):
        """The bug that shipped: this came back "" and displayed as Open."""
        self.assertEqual(self.by_title["Rec Center Parking Lot"]["status"], "Awarded")

    def test_a_closed_posting_is_read_as_closed(self):
        self.assertEqual(self.by_title["Curb and Gutter Repair"]["status"], "Closed")

    def test_an_open_posting_is_still_open(self):
        self.assertEqual(
            self.by_title["2026 Sidewalk Replacement Program"]["status"], "Open")

    def test_the_closing_date_survives_a_long_summary(self):
        """A fixed 600-char window fell short of the values on any posting
        with a real description."""
        self.assertEqual(
            self.by_title["2026 Sidewalk Replacement Program"]["deadline"],
            "9/1/2026")

    def test_a_postings_values_do_not_leak_from_the_next_one(self):
        self.assertEqual(self.by_title["Curb and Gutter Repair"]["deadline"],
                         "7/30/2024")


class StatusPatternTests(unittest.TestCase):
    def test_the_label_then_value_layout_is_read(self):
        self.assertEqual(bs._status_near("Status: Closes: Awarded 3/11/2025"),
                         "Awarded")

    def test_the_plain_layout_still_works(self):
        self.assertEqual(bs._status_near("Status: Closed"), "Closed")

    def test_no_status_stays_empty_rather_than_guessing(self):
        self.assertEqual(bs._status_near("Closes: 3/11/2025"), "")


class RelevanceTests(unittest.TestCase):
    """NICHE_TERMS is a cheap gate before an AI call. On the known-portal
    path there is no AI call afterwards, so a weak contextual match is the
    only thing between a listing and the customer's feed."""

    def test_the_listing_that_shipped_is_rejected(self):
        self.assertFalse(bs.looks_relevant(
            "Specialized Legal Services for a Potential Large-Scale "
            "Digital Infrastructure Project"))

    def test_real_work_with_an_unrelated_word_in_it_survives(self):
        """Scope lists routinely mix trades; a real trade word wins."""
        self.assertTrue(bs.looks_relevant(
            "2026 Sidewalk Replacement and Landscaping"))

    def test_a_weak_term_alone_still_passes_when_nothing_contradicts_it(self):
        self.assertTrue(bs.looks_relevant("Street Improvement Project Phase 2"))

    def test_ada_only_counts_as_a_word(self):
        """As a bare substring it matches Nevada, Canada, Adams, Palisades."""
        self.assertTrue(bs.looks_relevant("Citywide ADA Ramp Upgrades"))
        self.assertFalse(bs.looks_relevant("Nevada Street Lighting Audit"))
        self.assertFalse(bs.looks_relevant("Canada Road Sign Replacement Legal"))

    def test_plainly_unrelated_trades_are_still_rejected(self):
        for t in ("Janitorial Services for Public Works Buildings",
                  "Legal Services RFQ", "Mowing and tree trimming contract"):
            self.assertFalse(bs.looks_relevant(t), t)

    def test_empty_input_is_not_relevant(self):
        self.assertFalse(bs.looks_relevant(""))
        self.assertFalse(bs.looks_relevant(None))


if __name__ == "__main__":
    unittest.main()
