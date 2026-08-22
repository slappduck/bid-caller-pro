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
AWARDED_AS_OPEN = "<html>" + _row(2, "Rec Center Parking Lot Concrete", "Open", PAST) + "</html>"
PROPERLY_AWARDED = "<html>" + _row(3, "Rec Center Parking Lot Concrete", "Awarded", PAST) + "</html>"

ENTRY = {"url": "https://x.gov/Bids.aspx", "base": "https://x.gov",
         "city": "X", "state": "MO"}


class AuditPortalTests(unittest.TestCase):
    def setUp(self):
        # Link checking is real network I/O; stubbed so the suite stays
        # offline and fast. Its own behaviour is covered separately below.
        p = patch.object(ls, "_url_is_alive", return_value=True)
        p.start()
        self.addCleanup(p.stop)

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
        html = ("<html>" + _row(4, "Curb and Gutter Job", "", FUTURE) + "</html>")
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
        p = patch.object(ls, "_url_is_alive", return_value=True)
        p.start()
        self.addCleanup(p.stop)
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
            _row(i, f"Sidewalk Repair Job {i}", "Open", PAST) for i in range(4)) + "</html>"
        with patch.object(ls, "_fetch_raw_html", return_value=many):
            r = ls._run_bid_audit(sample_size=1)
        self.assertEqual(r["open_but_expired"], 4)
        self.assertEqual(r["stale_rate_pct"], 100.0)
        self.assertTrue(self._alerts, "a fully stale feed must alert")

    def test_one_bad_row_does_not_wake_anyone(self):
        """A single expired listing is noise; the alert is for a regression."""
        mixed = "<html>" + _row(1, "Live Sidewalk Job", "Open", FUTURE) \
                + _row(2, "Dead Sidewalk Job", "Open", PAST) + "</html>"
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


class LinkLivenessTests(unittest.TestCase):
    """Calling a live link dead would be worse than the bug being measured,
    so anything short of a definite 404 is reported as unknown."""

    def _with(self, side_effect):
        return patch("license_server.urllib.request.urlopen",
                     side_effect=side_effect)

    def test_a_200_is_alive(self):
        class R:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
        with self._with(lambda *a, **k: R()):
            self.assertTrue(ls._url_is_alive("https://x.gov/b"))

    def test_a_404_is_dead(self):
        err = ls.urllib.error.HTTPError("u", 404, "gone", None, None)
        with self._with(err):
            self.assertFalse(ls._url_is_alive("https://x.gov/b"))

    def test_a_server_error_is_unknown_not_dead(self):
        err = ls.urllib.error.HTTPError("u", 503, "busy", None, None)
        with self._with(err):
            self.assertIsNone(ls._url_is_alive("https://x.gov/b"))

    def test_a_head_rejection_is_retried_as_a_get(self):
        """Plenty of government stacks answer 405 to HEAD and 200 to GET."""
        calls = []
        class R:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
        def fake(req, *a, **k):
            calls.append(req.get_method())
            if req.get_method() == "HEAD":
                raise ls.urllib.error.HTTPError("u", 405, "no", None, None)
            return R()
        with self._with(fake):
            self.assertTrue(ls._url_is_alive("https://x.gov/b"))
        self.assertEqual(calls, ["HEAD", "GET"])

    def test_a_timeout_is_unknown(self):
        with self._with(TimeoutError("slow")):
            self.assertIsNone(ls._url_is_alive("https://x.gov/b"))


class ExtendedMetricTests(unittest.TestCase):
    """Staleness was only one way the feed can be wrong."""

    def setUp(self):
        p = patch.object(ls, "_url_is_alive", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def _audit(self, html, **kw):
        with patch.object(ls, "_fetch_raw_html", return_value=html):
            return ls._audit_portal(ENTRY, **kw)

    def test_a_shown_row_with_no_date_is_counted(self):
        """Nothing can ever age these out, so they sit in the feed forever."""
        html = "<html>" + _row(1, "Sidewalk Program", "Open", "") + "</html>"
        r = self._audit(html)
        self.assertEqual(r["shown_open"], 1)
        self.assertEqual(r["shown_no_deadline"], 1)

    def test_a_dated_row_is_not_counted_as_undated(self):
        r = self._audit(CLEAN)
        self.assertEqual(r["shown_no_deadline"], 0)

    def test_off_niche_rows_are_never_counted_as_shown(self):
        """A listing page carries every trade the agency buys; the scan drops
        those before a customer sees them, so the audit must too."""
        html = ("<html>" + _row(1, "Janitorial Services Contract", "Open",
                                FUTURE) + "</html>")
        r = self._audit(html)
        self.assertEqual(r["rows"], 1)
        self.assertEqual(r["niche_rows"], 0)
        self.assertEqual(r["shown_open"], 0)

    def test_concrete_work_counts_as_both_niche_and_shown(self):
        r = self._audit(CLEAN)
        self.assertEqual(r["niche_rows"], 1)
        self.assertEqual(r["shown_open"], 1)

    def test_an_off_niche_row_is_not_link_checked(self):
        html = ("<html>" + _row(1, "Janitorial Services Contract", "Open",
                                FUTURE) + "</html>")
        self.assertEqual(self._audit(html)["links_checked"], 0)

    def test_a_dead_link_is_counted(self):
        with patch.object(ls, "_url_is_alive", return_value=False), \
             patch.object(ls, "_fetch_raw_html", return_value=CLEAN):
            r = ls._audit_portal(ENTRY)
        self.assertEqual(r["links_checked"], 1)
        self.assertEqual(r["links_dead"], 1)

    def test_an_unknown_link_is_not_counted_either_way(self):
        with patch.object(ls, "_url_is_alive", return_value=None), \
             patch.object(ls, "_fetch_raw_html", return_value=CLEAN):
            r = ls._audit_portal(ENTRY)
        self.assertEqual(r["links_checked"], 0)
        self.assertEqual(r["links_dead"], 0)

    def test_link_checks_are_capped_per_portal(self):
        """Checking every link would turn the audit into a crawl."""
        many = "<html>" + "".join(
            _row(i, f"Sidewalk Job {i}", "Open", FUTURE) for i in range(9)) + "</html>"
        r = self._audit(many, link_checks=2)
        self.assertEqual(r["shown_open"], 9)
        self.assertEqual(r["links_checked"], 2)

    def test_a_closed_rows_link_is_still_checked(self):
        """Liveness tests how the URL was built, not whether the bid is
        current -- a closed posting's page still exists, and sampling only
        open rows gave two links a night."""
        html = "<html>" + _row(1, "Old Sidewalk Job", "Closed", PAST) + "</html>"
        r = self._audit(html)
        self.assertEqual(r["shown_open"], 0)
        self.assertEqual(r["links_checked"], 1)

    def test_an_off_niche_rows_link_is_not_checked(self):
        """Still bounded to work we would ever surface."""
        html = ("<html>" + _row(1, "Janitorial Services", "Open", FUTURE) + "</html>")
        self.assertEqual(self._audit(html)["links_checked"], 0)


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


class AuditCallsProductionsCodePathTests(unittest.TestCase):
    """The audit reported 0 dead links while every CivicPlus posting link was
    a 404, because it passed the site origin as base_url where the scanner
    passes the listing url. A monitor that does not call the code the way
    production calls it will confirm whatever you already believe."""

    def test_the_listing_url_is_passed_as_the_parse_base(self):
        seen = {}

        def spy(html, base_url=""):
            seen["base"] = base_url
            return []

        entry = {"url": "https://x.gov/Bids.aspx?showAllBids=on",
                 "base": "https://x.gov", "city": "X", "state": "MO"}
        with patch.object(ls.bid_sources, "parse_civicplus_html", spy), \
             patch.object(ls, "_fetch_raw_html", return_value="<html></html>"):
            ls._audit_portal(entry)
        self.assertEqual(seen["base"], entry["url"],
                         "the audit must parse the way the scanner does")

    def test_a_malformed_link_would_now_be_seen_as_dead(self):
        """End to end: the bug's own signature, caught."""
        html = ('<html><body><a href="bids.aspx?bidID=1">Sidewalk Program</a>'
                '<a href="bids.aspx?bidID=1">Read on: Sidewalk Program</a>'
                '<span>Status:</span><span>Closes:</span>'
                f'<span>Open</span><span>{FUTURE}</span></body></html>')
        entry = {"url": "https://x.gov/Bids.aspx", "base": "https://x.gov",
                 "city": "X", "state": "MO"}
        checked = []
        with patch.object(ls, "_fetch_raw_html", return_value=html), \
             patch.object(ls, "_url_is_alive",
                          side_effect=lambda u, **k: checked.append(u) or False):
            r = ls._audit_portal(entry)
        self.assertEqual(r["links_dead"], 1)
        self.assertNotIn("/Bids.aspx/", checked[0],
                         "a link built under the listing page is the bug")
