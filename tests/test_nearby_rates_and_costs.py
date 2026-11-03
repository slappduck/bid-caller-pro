"""Starting prices for every state, and local costs for the profit estimate.

1. States with no rates of their own get rates/nearby/<st>.json, built from
   what each item won for in the nearest states with bid results, and
   always labeled as those states' prices.
2. rates/costs.json carries cement masons' median pay by state and the
   ready-mix price trend, both from BLS, fetched by the monthly refresh.
"""
import inspect
import json
import os
import re
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_cost_data as bc  # noqa: E402
import build_nearby_rates as bn  # noqa: E402
import refresh_state_data  # noqa: E402

RATES = os.path.join(ROOT, "curbcall_netlify_v4", "rates")
NEARBY = os.path.join(RATES, "nearby")
RESULTS = os.path.join(ROOT, "curbcall_netlify_v4", "results")


def _load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class NearbyRatesTests(unittest.TestCase):
    def setUp(self):
        self.own = {n[:2].upper() for n in os.listdir(RATES) if re.fullmatch(r"[a-z]{2}\.json", n)}
        self.results = {n[:2].upper() for n in os.listdir(RESULTS) if re.fullmatch(r"[a-z]{2}\.json", n)}
        self.files = {n[:2].upper(): _load(os.path.join(NEARBY, n))
                      for n in os.listdir(NEARBY) if re.fullmatch(r"[a-z]{2}\.json", n)}

    def test_the_committed_files_are_what_the_builder_makes(self):
        """A rebuild with nothing new changes nothing, and nobody hand-edited them."""
        built = bn.build()
        self.assertEqual(set(built), set(self.files))
        for st, d in built.items():
            self.assertEqual(json.loads(json.dumps(d)), self.files[st], st)

    def test_a_state_with_its_own_rates_never_gets_borrowed_ones(self):
        self.assertFalse(self.own & set(self.files))

    def test_most_of_the_country_is_covered_but_not_alaska_or_hawaii(self):
        self.assertGreaterEqual(len(self.own) + len(self.files), 45)
        self.assertNotIn("AK", self.files)
        self.assertNotIn("HI", self.files)
        for st in ("OH", "GA", "CA", "PA", "NY", "MI", "AZ"):
            self.assertIn(st, self.files)

    def test_prices_come_only_from_states_with_bid_results_and_say_which(self):
        for st, d in self.files.items():
            self.assertEqual(d["basis"], "nearby")
            self.assertTrue(set(d["from"]) <= self.results, st)
            label = d["districts"]["STATEWIDE"]
            for s in d["from"][:1]:
                self.assertIn(bn.NAMES[s], label + " ".join(i.get("where", "") for i in d["items"].values()))
            self.assertTrue(d["through"] and d["through"] <= bn.newest_letting())

    def test_every_price_is_internally_consistent(self):
        for st, d in self.files.items():
            self.assertTrue(d["items"], st)
            for cat, code in d["cats"].items():
                self.assertEqual(d["items"][code]["unit"], bn.ITEMS[cat][1])
                (y, row), = d["wins"][code]["STATEWIDE"].items()
                mid, low, high, n, p25, p75 = row
                self.assertTrue(low <= p25 <= mid <= p75 <= high, (st, cat, row))
                self.assertGreaterEqual(n, bn.MIN_JOBS)
                self.assertEqual(d["prices"][code]["STATEWIDE"][y][0], mid)

    def test_ohio_sidewalk_is_a_sane_price_per_square_yard(self):
        d = self.files["OH"]
        mid = d["wins"]["N-sidewalk"]["STATEWIDE"][str(d["years"][0])][0]
        self.assertTrue(40 < mid < 200, mid)

    def test_each_source_state_counts_the_same(self):
        """Texas's hundreds of jobs don't outvote a neighbor with a few dozen."""
        pairs = [(10.0, 1 / 100)] * 100 + [(50.0, 1 / 5)] * 5
        self.assertEqual(bn.weighted_quantile(pairs, 0.75), 50.0)
        self.assertEqual(bn.weighted_quantile(pairs, 0.25), 10.0)

    def test_square_feet_are_converted_to_square_yards(self):
        self.assertEqual(bn.convert(10.0, "sq ft", "sq yd"), 90.0)
        self.assertIsNone(bn.convert(10.0, "each", "sq yd"))


class CostDataTests(unittest.TestCase):
    def test_wage_series_ids_are_the_bls_format(self):
        self.assertEqual(bc.wage_series("MO"), "OEUS290000000000047205108")
        self.assertTrue(all(len(bc.wage_series(st)) == 25 for st in bc.FIPS))

    def test_latest_annual_skips_suppressed_values(self):
        rows = [{"year": "2025", "period": "A01", "value": "-"},
                {"year": "2024", "period": "A01", "value": "27.53"},
                {"year": "2023", "period": "A01", "value": "26.10"}]
        self.assertEqual(bc.latest_annual(rows), (27.53, 2024))

    def test_ready_mix_change_over_twelve_months(self):
        rows = [{"year": "2026", "period": "M09", "value": "104.0"},
                {"year": "2026", "period": "M08", "value": "103.0"},
                {"year": "2025", "period": "M09", "value": "100.0"}]
        self.assertEqual(bc.yearly_change(rows), {"month": "2026-09", "change_12m": 4.0})

    def fake(self, series, start, end, key):
        out = {}
        for s in series:
            if s == bc.READY_MIX_PPI:
                out[s] = [{"year": "2026", "period": "M09", "value": "103.1"},
                          {"year": "2025", "period": "M09", "value": "100.0"}]
            else:
                out[s] = [{"year": "2024", "period": "A01", "value": "25.00"}]
        return out

    def test_build_has_every_state_and_the_trend(self):
        d = bc.build(fetcher=self.fake)
        self.assertEqual(len(d["labor"]["by_state"]), len(bc.FIPS))
        self.assertEqual(d["labor"]["by_state"]["MO"], {"median": 25.0, "year": 2024})
        self.assertEqual(d["ready_mix"]["change_12m"], 3.1)

    def test_a_failed_fetch_keeps_last_months_file(self):
        with patch.object(bc, "fetch", side_effect=RuntimeError("daily threshold reached")), \
             patch.object(bc, "OUT", os.path.join(ROOT, "nonexistent-dir", "costs.json")):
            self.assertEqual(bc.main(), 1)

    def test_too_few_states_is_a_failure_not_a_thin_file(self):
        def thin(series, start, end, key):
            return {series[0]: [{"year": "2024", "period": "A01", "value": "25"}]}
        with self.assertRaises(RuntimeError):
            bc.build(fetcher=thin)

    def test_the_monthly_refresh_runs_both(self):
        src = inspect.getsource(refresh_state_data.main)
        self.assertIn("build_nearby_rates.main()", src)
        self.assertIn("build_cost_data.main()", src)


if __name__ == "__main__":
    unittest.main()
