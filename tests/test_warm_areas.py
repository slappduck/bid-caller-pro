"""Scanning outreach towns a few at a time, so the landing page has real
open bids where the outreach links point."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls
import kv_backend


class WarmAreasTests(unittest.TestCase):
    def setUp(self):
        self.store, self.scanned = {}, []
        self.client = ls.app.test_client()
        self._p = [
            patch.object(kv_backend, "get", side_effect=lambda k, d=None: self.store.get(k, d)),
            patch.object(kv_backend, "set", side_effect=lambda k, v: self.store.__setitem__(k, v)),
            patch.object(ls, "CRON_SECRET", "s3cret"),
            patch.object(ls, "_perform_scan", side_effect=lambda a, r: (self.scanned.append((a, r)), {"total_bids": 3})[1]),
            patch.object(ls, "_warm_area_list", return_value=["Joplin, MO", "Monett, MO", "Neosho, MO"]),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def run_job(self, **body):
        return self.client.post("/run-warm-areas", json=body, headers={"X-Cron-Secret": "s3cret"})

    def test_one_town_a_call_rotating_through_the_list(self):
        for _ in range(4):
            self.assertEqual(self.run_job(n=1).status_code, 200)
        self.assertEqual([a for a, _r in self.scanned], ["Joplin, MO", "Monett, MO", "Neosho, MO", "Joplin, MO"])
        self.assertTrue(all(r == ls.WARM_RADIUS_MI for _a, r in self.scanned))

    def test_it_reports_a_heartbeat(self):
        self.run_job(n=1)
        self.assertIn("warm-areas", self.store[ls.CRON_HEARTBEAT_KEY])
        self.assertIn("warm-areas", ls.CRON_EXPECTED)

    def test_the_secret_is_required(self):
        self.assertEqual(self.client.post("/run-warm-areas", json={}).status_code, 403)
        self.assertEqual(self.scanned, [])

    def test_zero_turns_it_off(self):
        with patch.object(ls, "WARM_AREAS_PER_RUN", 0):
            d = self.run_job(n=1).get_json()
        self.assertEqual((d["scanned"], self.scanned), (0, []))

    def test_a_failed_scan_moves_on(self):
        with patch.object(ls, "_perform_scan", side_effect=RuntimeError("down")):
            d = self.run_job(n=1).get_json()
        self.assertEqual(d["failed"], ["Joplin, MO"])
        self.run_job(n=1)
        self.assertEqual(self.scanned[0][0], "Monett, MO")


class AreaListTests(unittest.TestCase):
    def test_the_outreach_towns_and_extra_areas_without_repeats(self):
        with patch.dict(os.environ, {"WARM_EXTRA_AREAS": "Springfield, MO; joplin, mo"}):
            areas = ls._warm_area_list()
        self.assertEqual(areas[0], "Springfield, MO")
        self.assertIn("Kansas City, KS", areas)
        self.assertEqual(len([a for a in areas if a.lower() == "joplin, mo"]), 1)


if __name__ == "__main__":
    unittest.main()
