"""Tests for which bids count as open.

Written after a live scan reported funnel `kept=7` and a total of `0`: seven
real, in-radius solicitations were placed into the result and every one of them
was then ruled not-open and hidden. The old rule demanded the status string be
exactly "open", so anything an agency or the extraction model phrased
differently — "Accepting Bids", "Active", "Open - Bids Due 12/1" — silently
vanished between being found and being shown.

This is the single most expensive class of bug in the product: it costs a bid
that was already found and paid for, and it looks from the outside exactly like
"there is no work in your area".
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


class OpenStatusTests(unittest.TestCase):
    def test_the_plain_cases(self):
        self.assertTrue(ls._is_open_bid({"status": "Open"}))
        self.assertFalse(ls._is_open_bid({"status": "Closed"}))

    def test_the_ways_a_live_bid_is_really_written(self):
        for status in ("open", " Open ", "OPEN", "Accepting Bids", "Active",
                       "Advertised", "Open - Bids Due 12/1/2026", "Currently Open",
                       "Bidding", "Open for Bids", "Issued", "Posted"):
            with self.subTest(status=status):
                self.assertTrue(ls._is_open_bid({"status": status}), status)

    def test_the_ways_a_dead_bid_is_really_written(self):
        for status in ("Closed", "closed", "CLOSED", "Bid Closed", "Awarded",
                       "Award to Acme Concrete", "Cancelled", "Canceled",
                       "Expired", "Withdrawn", "Archived", "No Longer Accepting",
                       "Not Accepting Bids", "Complete"):
            with self.subTest(status=status):
                self.assertFalse(ls._is_open_bid({"status": status}), status)

    def test_an_unstated_status_is_not_evidence_of_a_closed_bid(self):
        # Structured listings often carry no status at all. Treating that as
        # closed would throw away the most reliable source in the pipeline.
        for bid in ({}, {"status": ""}, {"status": None}, {"status": "   "}):
            with self.subTest(bid=bid):
                self.assertTrue(ls._is_open_bid(bid))

    def test_malformed_input_does_not_raise(self):
        for bid in (None, {"status": 12345}):
            with self.subTest(bid=bid):
                ls._is_open_bid(bid)

    def test_a_closed_bid_still_ranks_last(self):
        closed = {"title": "Sidewalk repair", "status": "Awarded"}
        open_ = {"title": "Sidewalk repair", "status": "Accepting Bids"}
        self.assertLess(ls._score_bid(closed), ls._score_bid(open_))


class KeptAndTotalAgreeTests(unittest.TestCase):
    """The funnel's `kept` and the reported total must not be able to disagree
    without the funnel saying why."""

    CENTER = {"lat": 37.209, "lon": -93.292, "city": "Springfield", "state": "MO"}

    def _place(self, bids):
        grouped, stats, cdb = {}, {}, {}
        for b in bids:
            ls._place_bid(grouped, dict(b), self.CENTER, 25, cdb,
                          default_city="Springfield", default_state="MO",
                          fallback_coords=(37.209, -93.292), stats=stats)
        total = sum(1 for v in grouped.values() for b in v if ls._is_open_bid(b))
        return grouped, stats, total

    def test_bids_phrased_as_accepting_are_counted(self):
        _, stats, total = self._place([
            {"title": "2026 Sidewalk Program", "status": "Accepting Bids"},
            {"title": "ADA Ramp Replacement", "status": "Active"},
        ])
        self.assertEqual(stats.get("kept"), 2, stats)
        self.assertEqual(total, 2, stats)

    def test_a_kept_but_closed_bid_is_called_out_in_the_funnel(self):
        _, stats, total = self._place([
            {"title": "Old Curb Job", "status": "Awarded"},
            {"title": "New Sidewalk Job", "status": "Open"},
        ])
        self.assertEqual(stats.get("kept"), 2)
        self.assertEqual(stats.get("kept_but_closed"), 1, stats)
        self.assertEqual(total, 1)

    def test_an_expired_deadline_shows_up_as_kept_but_closed(self):
        # Not a silent zero: the funnel says the work was found and had lapsed.
        _, stats, total = self._place([
            {"title": "Sidewalk Program", "status": "Open", "deadline": "3/1/2019"},
        ])
        self.assertEqual(stats.get("kept"), 1)
        self.assertEqual(stats.get("kept_but_closed"), 1, stats)
        self.assertEqual(total, 0)


class LastScanDiagnosticsTests(unittest.TestCase):
    """/health has to show what a scan found, not only how many."""

    GROUPED = {
        "Springfield": [
            {"title": "2026 Sidewalk & ADA Ramp Program", "status": "Open",
             "deadline": "12/1/2026", "email": "buyer@springfieldmo.gov"},
            {"title": "Curb and Gutter - Grant Ave", "status": "Awarded",
             "deadline": "3/3/2026", "phone": "417-555-0100"},
        ],
        "Nixa": [{"title": "Concrete Flatwork Contract", "deadline": ""}],
    }

    def test_statuses_are_broken_down(self):
        self.assertEqual(ls._status_breakdown(self.GROUPED),
                         {"Open": 1, "Awarded": 1, "(none)": 1})

    def test_the_sample_says_whether_each_bid_counted_as_open(self):
        sample = ls._scan_sample(self.GROUPED)
        by_title = {s["title"]: s for s in sample}
        self.assertTrue(by_title["2026 Sidewalk & ADA Ramp Program"]["open"])
        self.assertFalse(by_title["Curb and Gutter - Grant Ave"]["open"])
        self.assertTrue(by_title["Concrete Flatwork Contract"]["open"])

    def test_the_sample_carries_no_contact_details(self):
        import json
        raw = json.dumps(ls._scan_sample(self.GROUPED))
        self.assertNotIn("buyer@springfieldmo.gov", raw)
        self.assertNotIn("417-555-0100", raw)

    def test_the_sample_is_bounded(self):
        big = {"X": [{"title": f"Job {i}", "status": "Open"} for i in range(50)]}
        self.assertLessEqual(len(ls._scan_sample(big)), 8)

    def test_empty_input_is_handled(self):
        self.assertEqual(ls._status_breakdown({}), {})
        self.assertEqual(ls._scan_sample(None), [])


if __name__ == "__main__":
    unittest.main()
