"""Tests for retiring dateless bids.

_apply_deadline_status can only close a bid whose deadline has passed. A bid
with no stated deadline has nothing to compare against, so it stays open
forever -- and the nightly feed audit measured that at half of everything a
scan shows.

Recording when a dateless bid was first seen gives the only clock available.
The window is deliberately generous: retiring a job that is genuinely still
open costs a contractor real work, while showing a dead one costs a wasted
phone call. Wrong in the cheap direction.
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


def _days_ago(n):
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


class AgeOutTests(unittest.TestCase):
    def setUp(self):
        self.db = {}
        self.stats = {}

    def _bid(self, **kw):
        b = {"title": "Sidewalk Program", "status": "Open", "deadline": "",
             "url": "https://x.gov/b/1"}
        b.update(kw)
        return b

    def test_a_dateless_bid_is_recorded_on_first_sight_and_still_shown(self):
        b = self._bid()
        ls._age_out_undated(b, "Aurora", self.db, self.stats)
        self.assertTrue(ls._is_open_bid(b), "must not hide it the first time")
        self.assertEqual(len(self.db["undated_first_seen"]), 1)

    def test_it_is_retired_once_the_window_passes(self):
        b = self._bid()
        sig = ls._bid_sig("Aurora", b)
        self.db["undated_first_seen"] = {sig: _days_ago(ls.UNDATED_MAX_DAYS + 1)}
        ls._age_out_undated(b, "Aurora", self.db, self.stats)
        self.assertFalse(ls._is_open_bid(b))
        self.assertEqual(self.stats["aged_out_undated"], 1)

    def test_it_is_still_shown_inside_the_window(self):
        b = self._bid()
        sig = ls._bid_sig("Aurora", b)
        self.db["undated_first_seen"] = {sig: _days_ago(ls.UNDATED_MAX_DAYS - 1)}
        ls._age_out_undated(b, "Aurora", self.db, self.stats)
        self.assertTrue(ls._is_open_bid(b))

    def test_a_bid_with_a_real_deadline_is_left_entirely_alone(self):
        """Those already age out properly; this must not second-guess them."""
        future = (datetime.date.today() + datetime.timedelta(days=400)).strftime("%m/%d/%Y")
        b = self._bid(deadline=future)
        ls._age_out_undated(b, "Aurora", self.db, self.stats)
        self.assertTrue(ls._is_open_bid(b))
        self.assertEqual(self.db.get("undated_first_seen", {}), {})

    def test_an_already_closed_bid_is_not_tracked(self):
        b = self._bid(status="Closed")
        ls._age_out_undated(b, "Aurora", self.db, self.stats)
        self.assertEqual(self.db.get("undated_first_seen", {}), {})

    def test_the_same_bid_keeps_one_clock_across_scans(self):
        """_bid_sig must be stable for a dateless bid, or the clock resets
        every scan and nothing is ever retired."""
        b1, b2 = self._bid(), self._bid()
        ls._age_out_undated(b1, "Aurora", self.db, self.stats)
        ls._age_out_undated(b2, "Aurora", self.db, self.stats)
        self.assertEqual(len(self.db["undated_first_seen"]), 1)

    def test_two_different_bids_get_their_own_clocks(self):
        ls._age_out_undated(self._bid(), "Aurora", self.db, self.stats)
        ls._age_out_undated(self._bid(title="Curb Repair", url="https://x.gov/b/2"),
                            "Aurora", self.db, self.stats)
        self.assertEqual(len(self.db["undated_first_seen"]), 2)

    def test_the_same_title_in_two_towns_is_two_bids(self):
        ls._age_out_undated(self._bid(), "Aurora", self.db, self.stats)
        ls._age_out_undated(self._bid(), "Nixa", self.db, self.stats)
        self.assertEqual(len(self.db["undated_first_seen"]), 2)

    def test_a_corrupt_stored_date_restarts_the_clock_rather_than_crashing(self):
        b = self._bid()
        sig = ls._bid_sig("Aurora", b)
        self.db["undated_first_seen"] = {sig: "not-a-date"}
        ls._age_out_undated(b, "Aurora", self.db, self.stats)
        self.assertTrue(ls._is_open_bid(b))
        self.assertNotEqual(self.db["undated_first_seen"][sig], "not-a-date")

    def test_the_store_does_not_grow_without_limit(self):
        """Left unbounded this eventually becomes the whole cache."""
        store = {f"sig{i}": _days_ago(500 - i) for i in range(ls._UNDATED_STORE_MAX + 50)}
        self.db["undated_first_seen"] = dict(store)
        ls._age_out_undated(self._bid(), "Aurora", self.db, self.stats)
        self.assertLessEqual(len(self.db["undated_first_seen"]),
                             ls._UNDATED_STORE_MAX + 1)

    def test_eviction_keeps_the_newest_entries(self):
        self.db["undated_first_seen"] = {
            f"sig{i}": _days_ago(500 - i) for i in range(ls._UNDATED_STORE_MAX + 10)}
        ls._age_out_undated(self._bid(), "Aurora", self.db, self.stats)
        left = self.db["undated_first_seen"]
        self.assertNotIn("sig0", left, "oldest should be evicted first")
        self.assertIn(f"sig{ls._UNDATED_STORE_MAX + 9}", left)

    def test_the_window_is_generous_enough_for_a_real_solicitation(self):
        """Bids typically run two to four weeks; retiring one early costs a
        contractor actual work."""
        self.assertGreaterEqual(ls.UNDATED_MAX_DAYS, 45)


if __name__ == "__main__":
    unittest.main()


class PublishedDateTests(unittest.TestCase):
    """88% of postings state when they went up. That is a real age, and it
    works on the first sighting -- the first-seen clock can only start one."""

    def setUp(self):
        self.db, self.stats = {}, {}

    def _bid(self, published):
        return {"title": "Sidewalk Program", "status": "Open", "deadline": "",
                "url": "https://x.gov/b/1", "published": published}

    def test_an_old_posting_is_retired_on_first_sight(self):
        old = (datetime.date.today()
               - datetime.timedelta(days=ls.UNDATED_MAX_DAYS + 5)).strftime("%m/%d/%Y")
        b = self._bid(old)
        ls._age_out_undated(b, "Aurora", self.db, self.stats)
        self.assertFalse(ls._is_open_bid(b))
        self.assertEqual(self.stats["aged_out_undated"], 1)

    def test_a_recent_posting_is_shown(self):
        recent = (datetime.date.today()
                  - datetime.timedelta(days=3)).strftime("%m/%d/%Y")
        b = self._bid(recent)
        ls._age_out_undated(b, "Aurora", self.db, self.stats)
        self.assertTrue(ls._is_open_bid(b))

    def test_a_stated_date_means_no_first_seen_clock_is_started(self):
        """No need to track what the page already told us."""
        recent = (datetime.date.today()
                  - datetime.timedelta(days=3)).strftime("%m/%d/%Y")
        ls._age_out_undated(self._bid(recent), "Aurora", self.db, self.stats)
        self.assertEqual(self.db.get("undated_first_seen", {}), {})

    def test_an_unparseable_published_date_falls_back_to_the_clock(self):
        b = self._bid("sometime last spring")
        ls._age_out_undated(b, "Aurora", self.db, self.stats)
        self.assertTrue(ls._is_open_bid(b))
        self.assertEqual(len(self.db["undated_first_seen"]), 1)
