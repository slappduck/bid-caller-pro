"""Offline tests for the bid fit-scoring and DDG circuit-breaker logic in
license_server.py. No network access, no license/API keys needed — these
exercise pure functions only."""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


def _in(days):
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


class ScoreBidTests(unittest.TestCase):
    def test_closed_bid_scores_below_any_open_bid(self):
        closed = {"status": "Closed", "title": "Sidewalk repair"}
        open_far = {"status": "Open", "title": "General renovation", "deadline": _in(60)}
        self.assertLess(ls._score_bid(closed), ls._score_bid(open_far))

    def test_sooner_deadline_scores_higher_than_later(self):
        soon = {"status": "Open", "title": "x", "deadline": _in(3)}
        later = {"status": "Open", "title": "x", "deadline": _in(25)}
        self.assertGreater(ls._score_bid(soon), ls._score_bid(later))

    def test_past_deadline_is_penalized_even_if_marked_open(self):
        past = {"status": "Open", "title": "x", "deadline": _in(-5)}
        future = {"status": "Open", "title": "x", "deadline": _in(5)}
        self.assertLess(ls._score_bid(past), ls._score_bid(future))

    def test_niche_keyword_match_scores_higher_than_generic(self):
        niche = {"status": "Open", "title": "Sidewalk ADA curb concrete flatwork bid"}
        generic = {"status": "Open", "title": "General building renovation bid"}
        self.assertGreater(ls._score_bid(niche), ls._score_bid(generic))

    def test_contact_info_gives_a_bonus(self):
        with_contact = {"status": "Open", "title": "x", "phone": "555-1212"}
        without_contact = {"status": "Open", "title": "x"}
        self.assertGreater(ls._score_bid(with_contact), ls._score_bid(without_contact))


class DdgCircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        ls._ddg_fail_streak = 0

    def tearDown(self):
        ls._ddg_fail_streak = 0

    def test_not_degraded_initially(self):
        self.assertFalse(ls._ddg_is_degraded())

    def test_trips_after_threshold_consecutive_failures(self):
        for _ in range(ls.DDG_TRIP_THRESHOLD):
            ls._ddg_note_result(False)
        self.assertTrue(ls._ddg_is_degraded())

    def test_one_success_resets_the_streak(self):
        for _ in range(ls.DDG_TRIP_THRESHOLD):
            ls._ddg_note_result(False)
        ls._ddg_note_result(True)
        self.assertFalse(ls._ddg_is_degraded())


if __name__ == "__main__":
    unittest.main()
