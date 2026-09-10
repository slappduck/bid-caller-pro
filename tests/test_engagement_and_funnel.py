"""Two numbers the system could not produce, and both decide something.

ENGAGEMENT. A cancellation tells you about a decision already made. Somebody
stops opening a tool weeks before they cancel it, so how often paying
customers actually scan is the earliest signal there is -- and unlike a
cancellation it is still reversible while you can see it.

THE MARKETING FUNNEL. /click counts outreach links, which answers "did this
contractor open my email". It cannot answer the question that decides whether
the landing page is worth anything: how many people who arrive start a trial.
That needs a denominator, and nothing was counting one.

Both are built from tallies rather than journeys. Following individuals across
a site means identifying them, and neither question requires it: a scan
carries the same salted handle the rest of the lifecycle log uses, and a visit
carries nothing at all.
"""
import datetime
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


def ago(days):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=days)).isoformat()


class EngagementTests(unittest.TestCase):
    def db(self):
        return {"lifecycle": [
            {"at": ago(1), "event": "scanned", "id": "A"},
            {"at": ago(3), "event": "scanned", "id": "A"},
            {"at": ago(8), "event": "scanned", "id": "A"},
            {"at": ago(20), "event": "scanned", "id": "A"},
            {"at": ago(2), "event": "scanned", "id": "B"},
            {"at": ago(40), "event": "scanned", "id": "C"},
            {"at": ago(1), "event": "subscribed", "id": "A"},
        ]}

    def test_only_people_who_scanned_recently_count_as_active(self):
        got = ls._engagement(self.db())
        self.assertEqual(got["active_people"], 2)      # C is outside the window

    def test_scans_are_counted_per_week_not_per_window(self):
        """A rate is comparable between windows; a total is not."""
        got = ls._engagement(self.db())
        self.assertEqual(got["median_scans_per_week"], 0.62)   # 1.0 and 0.25
        self.assertEqual(got["busiest_scans_per_week"], 1.0)

    def test_a_customer_going_quiet_is_the_number_worth_acting_on(self):
        """Still reversible, unlike a cancellation."""
        self.assertEqual(ls._engagement(self.db())["silent_14d_or_more"], 1)

    def test_other_lifecycle_events_are_not_mistaken_for_use(self):
        """Subscribing is not using."""
        db = {"lifecycle": [{"at": ago(1), "event": "subscribed", "id": "A"}]}
        self.assertEqual(ls._engagement(db)["active_people"], 0)

    def test_nobody_active_yields_no_rate_rather_than_zero(self):
        got = ls._engagement({"lifecycle": []})
        self.assertIsNone(got["median_scans_per_week"])

    def test_a_corrupt_row_does_not_sink_the_report(self):
        db = self.db()
        db["lifecycle"].append({"at": "not-a-date", "event": "scanned", "id": "D"})
        self.assertEqual(ls._engagement(db)["active_people"], 2)

    def test_it_names_nobody(self):
        self.assertNotIn("@", json.dumps(ls._engagement(self.db())))


class ScanRecordsWhoScannedTests(unittest.TestCase):
    def test_the_scan_route_records_it(self):
        import re
        src = re.sub(r"^\s*#.*$", "",
                     open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       os.pardir, "license_server.py"),
                          encoding="utf-8").read(), flags=re.M)
        body = src[src.index("def scan():"):]
        body = body[:body.index("\ndef ")]
        self.assertIn('_bi_note(_db_, "scanned"', body)

    def test_the_address_comes_from_the_verified_token(self):
        import re
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "license_server.py"),
                   encoding="utf-8").read()
        body = src[src.index("def scan():"):]
        body = body[:body.index("\ndef ")]
        self.assertIn("_verify_supabase_token(supabase_token)", body)

    def test_a_metric_never_fails_a_scan(self):
        import re
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "license_server.py"),
                   encoding="utf-8").read()
        i = src.index('_bi_note(_db_, "scanned"')
        self.assertIn("except Exception", src[i - 400:i + 400])

    def test_the_place_scanned_is_not_recorded(self):
        """Where somebody works is theirs. The count is the question."""
        import re
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "license_server.py"),
                   encoding="utf-8").read()
        i = src.index('_bi_note(_db_, "scanned"')
        self.assertNotIn("location", src[i:i + 120])


class VisitCountingTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self._g, self._s = ls.kv_backend.get, ls.kv_backend.set
        ls.kv_backend.get = lambda k, d=None: self.store.get(k, d)
        ls.kv_backend.set = lambda k, v: self.store.__setitem__(k, v)
        self.app = ls.app.test_client()

    def tearDown(self):
        ls.kv_backend.get, ls.kv_backend.set = self._g, self._s

    def test_a_visit_is_counted(self):
        self.app.post("/visit", json={})
        day = datetime.datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(self.store[ls._VISIT_KEY][day], 1)

    def test_visits_accumulate(self):
        for _ in range(4):
            self.app.post("/visit", json={})
        day = datetime.datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(self.store[ls._VISIT_KEY][day], 4)

    def test_nothing_identifying_is_stored(self):
        self.app.post("/visit", json={})
        blob = json.dumps(self.store[ls._VISIT_KEY])
        for leak in ("@", "ip", "agent", "referer"):
            self.assertNotIn(leak, blob.lower())

    def test_the_store_cannot_grow_forever(self):
        """A counter with no bound is a slow outage."""
        self.store[ls._VISIT_KEY] = {
            (datetime.date(2020, 1, 1) + datetime.timedelta(days=i)).isoformat(): 1
            for i in range(ls._VISIT_DAYS + 50)}
        self.app.post("/visit", json={})
        self.assertLessEqual(len(self.store[ls._VISIT_KEY]), ls._VISIT_DAYS + 1)

    def test_a_dead_store_does_not_error_the_page(self):
        def boom(*a, **k):
            raise RuntimeError("kv down")
        ls.kv_backend.set = boom
        r = self.app.post("/visit", json={})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["recorded"])


class FunnelTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self._g = ls.kv_backend.get
        ls.kv_backend.get = lambda k, d=None: self.store.get(k, d)

    def tearDown(self):
        ls.kv_backend.get = self._g

    def test_the_count_is_not_named_in_a_way_that_trips_the_diag_guard(self):
        """/diag bans the word `trials` so the trials TABLE -- device records
        carrying addresses -- can never appear on a weaker credential. A count
        is not that, but the guard is blunt on purpose and worth more than the
        field name."""
        self.assertNotIn("trials", json.dumps(ls._funnel_summary({"lifecycle": []})))

    def test_the_ratio_the_landing_page_is_judged_on(self):
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        self.store[ls._VISIT_KEY] = {today: 50}
        db = {"lifecycle": [{"at": ago(1), "event": "trial_started", "id": "A"},
                            {"at": ago(2), "event": "trial_started", "id": "B"}]}
        got = ls._funnel_summary(db)
        self.assertEqual(got["site_visits"], 50)
        self.assertEqual(got["signups_started"], 2)
        self.assertEqual(got["visit_to_trial_pct"], 4.0)

    def test_no_visitors_yields_no_rate_rather_than_zero(self):
        """Zero would read as 'the page converts nobody' when it means
        'nobody came'."""
        self.assertIsNone(ls._funnel_summary({"lifecycle": []})["visit_to_trial_pct"])

    def test_days_outside_the_window_are_not_counted(self):
        old = (datetime.date.today() - datetime.timedelta(days=ls._VISIT_DAYS + 5))
        self.store[ls._VISIT_KEY] = {old.isoformat(): 999}
        self.assertEqual(ls._funnel_summary({"lifecycle": []})["site_visits"], 0)


class BothAreBehindTheDiagTokenTests(unittest.TestCase):
    def setUp(self):
        import re
        self.src = re.sub(r"^\s*#.*$", "",
                          open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            os.pardir, "license_server.py"),
                               encoding="utf-8").read(), flags=re.M)

    def _body(self, name):
        start = self.src.index("def %s(" % name)
        end = self.src.find("\ndef ", start + 1)
        return self.src[start:end if end != -1 else len(self.src)]

    def test_diag_reports_them(self):
        diag = self._body("diag")
        self.assertIn("_engagement()", diag)
        self.assertIn("_funnel_summary()", diag)

    def test_public_health_does_not(self):
        health = self._body("health_detail")
        self.assertNotIn("_engagement", health)
        self.assertNotIn("_funnel_summary", health)
