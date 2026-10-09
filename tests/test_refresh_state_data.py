"""The monthly refresh keeps last month's data when this month's looks broken."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import refresh_state_data as R  # noqa: E402


def rates(years, items):
    return json.dumps({"years": years, "prices": {c: {"STATEWIDE": {"2025": [1]}} for c in items}}).encode()


def results(dates):
    return json.dumps({"contracts": [{"date": d} for d in dates]}).encode()


class GuardTests(unittest.TestCase):
    def test_a_normal_month_passes(self):
        self.assertIsNone(R.worse("rates/ar.json", rates([2025], "abcd"), rates([2025, 2026], "abcd")))
        self.assertIsNone(R.worse("results/il.json", results(["2026-06-12"] * 10),
                                  results(["2026-06-12"] * 8 + ["2026-09-18"])))

    def test_a_lost_year_or_most_items_gone_is_kept_back(self):
        self.assertIn("newest year", R.worse("rates/ar.json", rates([2025, 2026], "abcd"), rates([2025], "abcd")))
        self.assertIn("items", R.worse("rates/ar.json", rates([2026], "abcdefghij"), rates([2026], "ab")))

    def test_results_that_shrink_or_go_back_in_time_are_kept_back(self):
        self.assertIn("contracts", R.worse("results/ia.json", results(["2026-01-01"] * 100),
                                           results(["2026-01-01"] * 10)))
        self.assertIn("newest letting", R.worse("results/ia.json", results(["2026-09-01"]),
                                                results(["2026-08-01"])))

    def test_unreadable_is_kept_back_and_a_new_state_passes(self):
        self.assertEqual(R.worse("rates/ar.json", rates([2026], "a"), b"<html>"), "not readable")
        self.assertIsNone(R.worse("rates/zz.json", None, rates([2026], "a")))

    def test_every_state_with_data_is_refreshed(self):
        written = {f for _n, _c, files in R.jobs() for f in files}
        for folder in ("rates", "results"):
            for name in os.listdir(os.path.join(R.SITE, folder)):
                if len(name) == 7 and name.endswith(".json"):
                    self.assertIn(f"{folder}/{name}", written)


if __name__ == "__main__":
    unittest.main()
