"""Reading a posting is rationed, so it must not be spent on dead bids.

Only ten postings per portal get read for contact, scope, deadline and value.
That budget used to be spent in whatever order the listing page happened to
be in, and municipal bid pages very often lead with the finished projects: a
125-mile scan from New Salem, MA kept 93 bids of which 83 were already
closed. So the ten reads went to last spring's work and the bids somebody
could still bid on arrived with no phone number and no scope.

Nothing is dropped here -- closed bids are still kept and still shown with
their Closed badge. They just go to the back of the queue.
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


def _row(title, deadline="", status=""):
    return {"title": title, "url": "https://x.gov/bid/" + title.replace(" ", "-"),
            "deadline": deadline, "status": status}


LAST_YEAR = "03/14/%d" % (datetime.date.today().year - 1)
NEXT_YEAR = "03/14/%d" % (datetime.date.today().year + 1)


class ClosedOnArrivalTests(unittest.TestCase):
    def test_past_deadline_is_closed(self):
        self.assertTrue(ls._closed_on_arrival(_row("Sidewalk", LAST_YEAR)))

    def test_future_deadline_is_open(self):
        self.assertFalse(ls._closed_on_arrival(_row("Sidewalk", NEXT_YEAR)))

    def test_stated_closed_status_is_closed(self):
        self.assertTrue(
            ls._closed_on_arrival(_row("Sidewalk", NEXT_YEAR, "Closed")))

    def test_awarded_is_closed(self):
        self.assertTrue(
            ls._closed_on_arrival(_row("Sidewalk", NEXT_YEAR, "Awarded")))

    def test_undated_is_not_closed(self):
        # The whole point of reading the posting is to find the date. An
        # undated row is the last thing we would want to skip.
        self.assertFalse(ls._closed_on_arrival(_row("Sidewalk", "")))

    def test_unusual_open_wording_is_not_closed(self):
        # _is_open_bid deliberately treats anything not saying closed as open.
        self.assertFalse(
            ls._closed_on_arrival(_row("Sidewalk", NEXT_YEAR, "Accepting Bids")))


class EnrichmentOrderTests(unittest.TestCase):
    def test_undated_first_then_open_then_closed(self):
        rows = [_row("closed one", LAST_YEAR),
                _row("open one", NEXT_YEAR),
                _row("undated one", "")]
        got = [r["title"] for r in ls._enrichment_order(rows)]
        self.assertEqual(got, ["undated one", "open one", "closed one"])

    def test_listing_order_kept_within_a_group(self):
        rows = [_row("open a", NEXT_YEAR), _row("open b", NEXT_YEAR),
                _row("open c", NEXT_YEAR)]
        got = [r["title"] for r in ls._enrichment_order(rows)]
        self.assertEqual(got, ["open a", "open b", "open c"])

    def test_the_new_salem_shape(self):
        # A page that opens with ten finished projects and has two live ones
        # at the bottom. The budget is ten; both live bids must be inside it.
        rows = [_row("done %d" % i, LAST_YEAR) for i in range(10)]
        rows += [_row("live sidewalk", NEXT_YEAR), _row("live curb", NEXT_YEAR)]
        budget = ls._enrichment_order(rows)[:10]
        titles = [r["title"] for r in budget]
        self.assertIn("live sidewalk", titles)
        self.assertIn("live curb", titles)

    def test_nothing_is_dropped(self):
        rows = [_row("a", LAST_YEAR), _row("b", NEXT_YEAR), _row("c", "")]
        self.assertEqual(len(ls._enrichment_order(rows)), 3)

    def test_empty_and_none_are_safe(self):
        self.assertEqual(ls._enrichment_order([]), [])
        self.assertEqual(ls._enrichment_order(None), [])


class BudgetAccountingTests(unittest.TestCase):
    def test_closed_overflow_counts_as_spared(self):
        rows = ls._enrichment_order(
            [_row("open", NEXT_YEAR)] + [_row("done %d" % i, LAST_YEAR)
                                         for i in range(4)])
        stats = {}
        ls._note_enrich_budget(rows, 2, stats)
        self.assertEqual(stats.get("enrich_budget_spared"), 3)
        self.assertNotIn("enrich_budget_short", stats)

    def test_open_overflow_counts_as_short(self):
        rows = ls._enrichment_order([_row("open %d" % i, NEXT_YEAR)
                                     for i in range(5)])
        stats = {}
        ls._note_enrich_budget(rows, 2, stats)
        self.assertEqual(stats.get("enrich_budget_short"), 3)
        self.assertNotIn("enrich_budget_spared", stats)

    def test_nothing_recorded_when_everything_fits(self):
        stats = {}
        ls._note_enrich_budget([_row("open", NEXT_YEAR)], 10, stats)
        self.assertEqual(stats, {})

    def test_no_stats_object_is_safe(self):
        ls._note_enrich_budget([_row("open", NEXT_YEAR)], 0, None)


if __name__ == "__main__":
    unittest.main()
