"""Tests for deadline checking to the minute, not just the day.

A Sedgwick bid due "08/19/2026 01:00 AM EDT" was shown as OPEN at 6:19 AM on
the 19th. _apply_deadline_status compared dates only, so a bid due at 1 AM
counted as live for the rest of that day.

The hazard in fixing it is the opposite error. Bid pages state local time,
the server runs UTC, and closing a bid that is still live somewhere is far
worse than showing one that just shut. So when a deadline gives a time but
no zone, it is treated as Hawaii -- the latest zone a US bid could be in --
and only called closed once it has closed everywhere.
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls

UTC = datetime.timezone.utc


class ParseMomentTests(unittest.TestCase):
    def test_the_case_that_shipped(self):
        got = ls._parse_deadline_moment("08/19/2026 01:00 AM EDT")
        self.assertEqual(got, datetime.datetime(2026, 8, 19, 5, 0, tzinfo=UTC))

    def test_afternoon_central_time(self):
        got = ls._parse_deadline_moment("Bids due December 1, 2026 at 2:00 PM CST")
        self.assertEqual(got, datetime.datetime(2026, 12, 1, 20, 0, tzinfo=UTC))

    def test_noon_and_midnight_are_not_shifted_wrongly(self):
        self.assertEqual(ls._parse_deadline_moment("12/01/2026 12:00 PM UTC"),
                         datetime.datetime(2026, 12, 1, 12, 0, tzinfo=UTC))
        self.assertEqual(ls._parse_deadline_moment("12/01/2026 12:00 AM UTC"),
                         datetime.datetime(2026, 12, 1, 0, 0, tzinfo=UTC))

    def test_a_missing_zone_is_assumed_to_be_the_latest_one(self):
        """Hawaii, so a bid is only ever called closed once it has closed
        everywhere in the US."""
        got = ls._parse_deadline_moment("12/01/2026 2:00 PM")
        self.assertEqual(got, datetime.datetime(2026, 12, 2, 0, 0, tzinfo=UTC))

    def test_a_date_with_no_time_has_no_moment(self):
        self.assertIsNone(ls._parse_deadline_moment("December 1, 2026"))

    def test_unparseable_text_has_no_moment(self):
        self.assertIsNone(ls._parse_deadline_moment("FY2027"))
        self.assertIsNone(ls._parse_deadline_moment(""))

    def test_a_nonsense_clock_is_rejected_rather_than_guessed(self):
        self.assertIsNone(ls._parse_deadline_moment("12/01/2026 25:00 PM"))


class ApplyDeadlineStatusTests(unittest.TestCase):
    def _status(self, deadline):
        bid = {"status": "Open", "deadline": deadline}
        ls._apply_deadline_status(bid)
        return bid["status"]

    def _fmt(self, dt):
        return dt.strftime("%m/%d/%Y %I:%M %p UTC")

    def test_a_deadline_that_passed_hours_ago_today_is_closed(self):
        past = datetime.datetime.now(UTC) - datetime.timedelta(hours=5)
        self.assertEqual(self._status(self._fmt(past)), "Closed")

    def test_a_deadline_later_today_stays_open(self):
        soon = datetime.datetime.now(UTC) + datetime.timedelta(hours=5)
        self.assertEqual(self._status(self._fmt(soon)), "Open")

    def test_a_date_only_deadline_today_stays_open_all_day(self):
        """Nobody stated an hour, so there is nothing to say it has passed."""
        today = datetime.date.today().strftime("%m/%d/%Y")
        self.assertEqual(self._status(today), "Open")

    def test_a_date_only_deadline_yesterday_is_closed(self):
        y = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%m/%d/%Y")
        self.assertEqual(self._status(y), "Closed")

    def test_a_future_date_stays_open(self):
        f = (datetime.date.today() + datetime.timedelta(days=30)).strftime("%m/%d/%Y")
        self.assertEqual(self._status(f), "Open")

    def test_a_zoneless_time_just_passed_in_eastern_stays_open(self):
        """It is still that morning in Hawaii, so the bid may still be live.
        Staying open is the safe error."""
        eastern_ago = datetime.datetime.now(UTC) - datetime.timedelta(hours=3)
        stamp = eastern_ago.strftime("%m/%d/%Y %I:%M %p")
        self.assertEqual(self._status(stamp), "Open")

    def test_a_stale_year_is_still_closed(self):
        self.assertEqual(self._status("FY2019"), "Closed")


if __name__ == "__main__":
    unittest.main()
