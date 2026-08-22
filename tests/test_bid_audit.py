"""Tests for the nightly feed-accuracy audit.

Every stale bid a customer saw was found by the customer, not by the system.
The CivicPlus status bug behind most of them sat across ~2,400 portals until
someone recognised a job they had already won. This audit exists so the next
one shows up as a number moving.

It must therefore fail loudly on exactly the failure it was built for -- a row
the parser would present as open that is already awarded or past its deadline
-- and must not cry wolf on an honestly open feed.
"""
import datetime
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


def _row(bid_id, title, status, closes):
    """One posting as CivicPlus renders it: labels, then values."""
    return (f'<a href="/Bids.aspx?bidID={bid_id}">{title}</a>'
            f'<a href="/Bids.aspx?bidID={bid_id}">Read on: {title}</a>'
            f'<span>Status:</span><span>Closes:</span>'
            f'<span>{status}</span><span>{closes}</span>')


FUTURE = (datetime.date.today() + datetime.timedelta(days=30)).strftime("%m/%d/%Y")
PAST = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%m/%d/%Y")

CLEAN = "<html>" + _row(1, "2026 Sidewalk Program", "Open", FUTURE) + "</html>"
AWARDED_AS_OPEN = "<html>" + _row(2, "Rec Center Lot", "Open", PAST) + "</html>"
PROPERLY_AWARDED = "<html>" + _row(3, "Rec Center Lot", "Awarded", PAST) + "</html>"

ENTRY = {"url": "https://x.gov/Bids.aspx", "base": "https://x.gov",
         "city": "X", "state": "MO"}


class AuditPortalTests(unittest.TestCase):
    def _audit(self, html):
        with patch.object(ls, "_fetch_raw_html", return_value=html):
            return ls._audit_portal(ENTRY)

    def test_an_honestly_open_bid_counts_as_open_and_not_stale(self):
        r = self._audit(CLEAN)
        self.assertEqual(r["rows"], 1)
        self.assertEqual(r["shown_open"], 1)
        self.assertEqual(r["open_but_expired"], 0)

    def test_an_expired_bid_still_marked_open_is_caught(self):
        """The exact shape of the bug that shipped."""
        r = self._audit(AWARDED_AS_OPEN)
        self.assertEqual(r["shown_open"], 1)
        self.assertEqual(r["open_but_expired"], 1)

    def test_a_correctly_awarded_bid_is_not_counted_as_shown(self):
        r = self._audit(PROPERLY_AWARDED)
        self.assertEqual(r["rows"], 1)
        self.assertEqual(r["shown_open"], 0)
        self.assertEqual(r["open_but_expired"], 0)

    def test_a_row_with_no_status_is_tracked_separately(self):
        html = ("<html>" + _row(4, "Mystery Job", "", FUTURE) + "</html>")
        r = self._audit(html)
        self.assertEqual(r["no_status"], 1)

    def test_an_unreachable_portal_is_counted_not_crashed(self):
        with patch.object(ls, "_fetch_raw_html", return_value=None):
            r = ls._audit_portal(ENTRY)
        self.assertEqual(r.get("unreachable"), 1)
        self.assertEqual(r["rows"], 0)

    def test_unparseable_html_costs_that_portal_and_nothing_else(self):
        r = self._audit("<html>not a bid listing at all</html>")
        self.assertEqual(r["rows"], 0)


class AuditRollupTests(unittest.TestCase):
    def setUp(self):
        self.stored = {}
        self._set = ls.kv_backend.set
        ls.kv_backend.set = lambda k, v: self.stored.__setitem__(k, v)
        self.addCleanup(lambda: setattr(ls.kv_backend, "set", self._set))
        self._alerts = []
        self._alert = ls._alert_admin
        ls._alert_admin = lambda s, d: self._alerts.append(s)
        self.addCleanup(lambda: setattr(ls, "_alert_admin", self._alert))
        self._seeds = bid_seeds = {("X", "MO"): [
            {"url": "https://x.gov/Bids.aspx", "platform": "civicplus"}]}
        self._orig_seeds = ls.bid_portals._national_seeds
        ls.bid_portals._national_seeds = lambda: bid_seeds
        self.addCleanup(
            lambda: setattr(ls.bid_portals, "_national_seeds", self._orig_seeds))

    def test_a_clean_feed_reports_zero_and_raises_nothing(self):
        with patch.object(ls, "_fetch_raw_html", return_value=CLEAN):
            r = ls._run_bid_audit(sample_size=1)
        self.assertEqual(r["stale_rate_pct"], 0.0)
        self.assertEqual(self._alerts, [])

    def test_a_stale_feed_is_measured_and_alerted(self):
        many = "<html>" + "".join(
            _row(i, f"Job {i}", "Open", PAST) for i in range(4)) + "</html>"
        with patch.object(ls, "_fetch_raw_html", return_value=many):
            r = ls._run_bid_audit(sample_size=1)
        self.assertEqual(r["open_but_expired"], 4)
        self.assertEqual(r["stale_rate_pct"], 100.0)
        self.assertTrue(self._alerts, "a fully stale feed must alert")

    def test_one_bad_row_does_not_wake_anyone(self):
        """A single expired listing is noise; the alert is for a regression."""
        mixed = "<html>" + _row(1, "Live", "Open", FUTURE) \
                + _row(2, "Dead", "Open", PAST) + "</html>"
        with patch.object(ls, "_fetch_raw_html", return_value=mixed):
            ls._run_bid_audit(sample_size=1)
        self.assertEqual(self._alerts, [])

    def test_the_result_is_stored_for_health_to_report(self):
        with patch.object(ls, "_fetch_raw_html", return_value=CLEAN):
            ls._run_bid_audit(sample_size=1)
        self.assertIn(ls.BID_AUDIT_KEY, self.stored)

    def test_no_portals_is_reported_rather_than_dividing_by_zero(self):
        ls.bid_portals._national_seeds = lambda: {}
        r = ls._run_bid_audit(sample_size=1)
        self.assertFalse(r["ok"])


class AuditRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()

    def test_it_refuses_without_the_cron_secret(self):
        with patch.object(ls, "CRON_SECRET", "s3cret"):
            r = self.client.post("/run-bid-audit", json={})
        self.assertEqual(r.status_code, 403)

    def test_it_refuses_when_no_secret_is_configured_at_all(self):
        """An unset secret must close the endpoint, not open it."""
        with patch.object(ls, "CRON_SECRET", ""):
            r = self.client.post("/run-bid-audit",
                                 json={}, headers={"X-Cron-Secret": ""})
        self.assertEqual(r.status_code, 403)

    def test_the_right_secret_runs_it(self):
        with patch.object(ls, "CRON_SECRET", "s3cret"), \
             patch.object(ls, "_run_bid_audit", return_value={"ok": True}):
            r = self.client.post("/run-bid-audit", json={},
                                 headers={"X-Cron-Secret": "s3cret"})
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
