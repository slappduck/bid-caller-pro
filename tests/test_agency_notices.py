"""Agency-posted notices — the "lister" side.

This exists because of what crawling could not reach: re-probing 269 missed
Missouri domains against 24 URL patterns found exactly one bid page. The
rest are towns of a few hundred people and rural water districts with no bid
page anywhere, and three with no working domain at all. Those agencies are
far too small for Bonfire or OpenGov to sell to, so a free form is the only
route by which their work becomes visible.

Deliberately a listing and nothing more: no bid submission, no attachments,
no sealed-bid handling.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls
import kv_backend

TOKEN = "a-real-admin-token"
AURORA = {"lat": 36.9709, "lon": -93.7183, "city": "Aurora", "state": "MO"}


class AgencyNoticeTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()
        self.store = {}
        self._p = [
            patch.object(ls, "ADMIN_TOKEN", TOKEN),
            patch.object(ls, "_alert_admin"),
            patch.object(kv_backend, "get",
                         side_effect=lambda k, d=None: self.store.get(k, d)),
            patch.object(kv_backend, "set",
                         side_effect=lambda k, v: self.store.__setitem__(k, v)),
            patch.object(ls, "_geo_from_city", side_effect=lambda c, s:
                         {"lat": 36.9709, "lon": -93.7183, "city": c, "state": s}
                         if c.lower() == "aurora" else None),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def _submit(self, **over):
        body = {"title": "2026 Sidewalk Program", "city": "Aurora", "state": "MO"}
        body.update(over)
        return self.client.post("/agency/submit", json=body)

    def _approve(self, bid_id):
        return self.client.post("/agency/review",
                                json={"admin_token": TOKEN, "approve": bid_id})

    def test_anyone_can_post_without_an_account(self):
        # A rural clerk is not going to create a login, so this is public.
        r = self._submit()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_title_city_and_state_are_required(self):
        for bad in ({"title": ""}, {"city": ""}, {"state": ""},
                    {"state": "ZZ"}, {"state": "Not A State"}):
            with self.subTest(bad=bad):
                self.assertEqual(self._submit(**bad).status_code, 400)

    def test_a_full_state_name_is_accepted_not_truncated(self):
        """Truncating to two characters turned "Missouri" into MI --
        Michigan. A valid code, the wrong state, and the notice would then
        surface for contractors six hundred miles away."""
        bid_id = self._submit(state="Missouri").get_json()["id"]
        self.assertEqual(ls._agency_bids()[bid_id]["state"], "MO")
        bid_id = self._submit(state="  missouri ").get_json()["id"]
        self.assertEqual(ls._agency_bids()[bid_id]["state"], "MO")
        bid_id = self._submit(state="mo").get_json()["id"]
        self.assertEqual(ls._agency_bids()[bid_id]["state"], "MO")

    def test_a_new_notice_is_not_visible_to_anyone_yet(self):
        bid_id = self._submit().get_json()["id"]
        self.assertFalse(ls._agency_bids()[bid_id]["approved"])
        grouped = {}
        self.assertEqual(
            ls._add_agency_bids(grouped, AURORA, 50, {}, {}, {}), 0)
        self.assertEqual(grouped, {})

    def test_reviewing_requires_the_admin_token(self):
        self.assertEqual(self.client.post("/agency/review", json={}).status_code, 403)

    def test_an_approved_notice_reaches_scans_covering_that_area(self):
        bid_id = self._submit(scope="4,000 LF sidewalk").get_json()["id"]
        self._approve(bid_id)
        grouped = {}
        added = ls._add_agency_bids(grouped, AURORA, 50, {}, {}, {})
        self.assertEqual(added, 1)
        titles = [b["title"] for v in grouped.values() for b in v]
        self.assertIn("2026 Sidewalk Program", titles)

    def test_an_approved_notice_does_not_reach_scans_elsewhere(self):
        self._approve(self._submit().get_json()["id"])
        boston = {"lat": 42.3601, "lon": -71.0589, "city": "Boston", "state": "MA"}
        grouped = {}
        self.assertEqual(ls._add_agency_bids(grouped, boston, 50, {}, {}, {}), 0)

    def test_a_notice_whose_town_cannot_be_placed_is_not_approved(self):
        """An unplaceable notice can't be radius-filtered, so it would show
        up for the wrong contractors. Better to refuse the approval."""
        bid_id = self._submit(city="Nowheresville").get_json()["id"]
        r = self._approve(bid_id)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "ungeocodable_city")
        self.assertFalse(ls._agency_bids()[bid_id]["approved"])

    def test_a_notice_can_be_deleted(self):
        bid_id = self._submit().get_json()["id"]
        self.client.post("/agency/review",
                         json={"admin_token": TOKEN, "delete": bid_id})
        self.assertNotIn(bid_id, ls._agency_bids())

    def test_one_source_cannot_flood_the_queue(self):
        with patch.object(ls, "AGENCY_MAX_PER_IP_PER_DAY", 3):
            codes = [self._submit().status_code for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])

    def test_oversized_input_is_truncated_not_rejected(self):
        # A clerk pasting a whole spec sheet shouldn't get an error.
        bid_id = self._submit(scope="x" * 9000).get_json()["id"]
        self.assertLessEqual(len(ls._agency_bids()[bid_id]["scope"]), 2000)


if __name__ == "__main__":
    unittest.main()
