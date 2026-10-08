"""MoDOT bid tabulations: who bid, in what order, at what unit price.

The app tells a contractor "this is what the winning bid charged" and names
the companies they're bidding against, so a price landing in the wrong
bidder's column is worse than no price. The text below is how pypdf
extracts MoDOT's September 2026 tabulations, trimmed, with the layouts that
matter: a vendor ranking, columns named "Low Bid 2nd Low Bid ...", an item
line that wraps its unit onto the next line, the same item in two projects
of one contract, and a contract MoDOT ranked out of total order.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import build_bid_results as R  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TABS = """CERTIFIED
CONTRACT ID :260918-C04
LETTING DATE :09/18/26 VENDOR RANKING
Call Order:C04
Contract ID:260918-C04
Letting Date:Sep 18, 2026 12:00:00 AM
Contract Description:J4P3567 - ROUTE 291 - JACKSON COUNTY
Counties:JACKSON
District:Kansas City
Rank Vendor  ID Vendor Name Total Bid
 Percent Of Low Bid
1 0010001 Amino Bros. Co., Inc. $3,000,000.00 100.000
2 0010002 Realm Construction  Inc. $3,400,000.00 113.333
3 0010003 Radmacher Brothers Excavating Co., Inc. $3,500,000.00 116.667
CERTIFIED
CONTRACT ID :260918-C04
LETTING DATE :09/18/26 BID TABULATION
Amino Bros.
Co., Inc.
Low Bid 2nd Low Bid 3rd Low Bid
Project   J4P3567:    Pavement and ADA
Section   0001:   Roadway Items
0200 6081010 CONCRETE CURB RAMP 155.3  SQYD
Bid Price 281.00 178.75 127.00
Bid Amount $43,639.30 $27,759.88 $19,723.10
0230 6086004 CONCRETE SIDEWALK, 4 IN. 1,121.3  SQYD
Bid Price 88.30 63.30 73.10
Bid Amount $99,010.79 $70,978.29 $81,967.03
0240 4030206 ASPHALTIC CONCRETE MIXTURE PG 64-22 (SP190C MIX) 2,649.8  TONS
Bid Price 110.00 141.25 137.75
0250 6091052 CURB AND GUTTER TYPE B, LONG ENOUGH TO WRAP ITS UNIT 697
LF
Bid Price 40.00 45.00 50.00
Project   J4P3568:    Second project
Section   0001:   Roadway Items
0010 6086004 CONCRETE SIDEWALK, 4 IN. 100  SQYD
Bid Price 100.00 70.00 80.00
CERTIFIED
CONTRACT ID :260918-D07
LETTING DATE :09/18/26 VENDOR RANKING
Contract ID:260918-D07
Letting Date:Sep 18, 2026 12:00:00 AM
Contract Description:Sidewalks in Fulton
Counties:CALLAWAY
District:Central
1 0020001 Ti-Zack Concrete, LLC $500,000.00 100.000
2 0020002 Second Co. $520,000.00 104.000
CONTRACT ID :260918-D07
LETTING DATE :09/18/26 BID TABULATION
Low Bid 2nd Low Bid
0010 6086004 CONCRETE SIDEWALK, 4 IN. 2,000  SQYD
Bid Price 60.00
"""


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.got = {c["id"]: c for c in R.parse_tabs(TABS)}

    def test_contract_details_and_bidders_in_rank_order(self):
        c = self.got["260918-C04"]
        self.assertEqual((c["date"], c["district"], c["counties"]), ("2026-09-18", "KC", "JACKSON"))
        self.assertEqual([b[0] for b in c["bidders"]],
                         ["Amino Bros. Co., Inc.", "Realm Construction Inc.",
                          "Radmacher Brothers Excavating Co., Inc."])

    def test_each_price_lands_in_its_bidders_column(self):
        self.assertEqual(self.got["260918-C04"]["items"]["6081010"], [155.3, [281.0, 178.75, 127.0]])

    def test_only_concrete_flatwork_is_kept(self):
        self.assertNotIn("4030206", self.got["260918-C04"]["items"])

    def test_a_wrapped_item_line_is_still_read(self):
        self.assertEqual(self.got["260918-C04"]["items"]["6091052"], [697.0, [40.0, 45.0, 50.0]])

    def test_an_item_in_two_projects_is_combined_by_quantity(self):
        qty, prices = self.got["260918-C04"]["items"]["6086004"]
        self.assertEqual(qty, 1221.3)
        self.assertEqual(prices[0], round((1121.3 * 88.3 + 100 * 100) / 1221.3, 2))

    def test_a_row_short_of_its_bidders_is_dropped_not_guessed(self):
        # D07 has two bidders but its sidewalk row printed one price.
        self.assertNotIn("260918-D07", self.got)

    def test_ranks_not_totals_order_the_columns(self):
        text = TABS.replace("$3,400,000.00 113.333", "$3,600,000.00 120.000")
        c = {c["id"]: c for c in R.parse_tabs(text)}["260918-C04"]
        self.assertEqual(c["bidders"][1][0], "Realm Construction Inc.")
        self.assertEqual(c["items"]["6081010"][1][1], 178.75)


class WinsTests(unittest.TestCase):
    def test_rank_one_prices_by_district_and_statewide(self):
        w = R.wins(R.parse_tabs(TABS))
        self.assertEqual(w["6081010"]["KC"]["2026"], [281.0, 281.0, 281.0, 1])
        self.assertIn("STATEWIDE", w["6081010"])


class CommittedResultsTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "curbcall_netlify_v4", "results", "mo.json"), encoding="utf-8") as f:
            self.d = json.load(f)
        with open(os.path.join(ROOT, "curbcall_netlify_v4", "rates", "index.json"), encoding="utf-8") as f:
            self.index = json.load(f)["states"]

    def test_every_contract_lines_up(self):
        self.assertGreater(len(self.d["contracts"]), 50)
        for c in self.d["contracts"]:
            n = len(c["bidders"])
            self.assertGreaterEqual(n, 1)
            self.assertIn(c["district"], ("NW", "NE", "KC", "CD", "SL", "SW", "SE", ""))
            for code, (qty, prices) in c["items"].items():
                self.assertTrue(code.startswith(("608", "609")), code)
                self.assertGreater(qty, 0)
                self.assertEqual(len(prices), n, (c["id"], code))

    def test_wins_are_rank_one_prices(self):
        for code, by_d in self.d["wins"].items():
            for by_y in by_d.values():
                for avg, low, high, n in by_y.values():
                    self.assertTrue(low <= avg <= high)
                    self.assertGreaterEqual(n, 1)

    def test_the_index_says_missouri_has_results(self):
        self.assertTrue(self.index["MO"].get("results"))


if __name__ == "__main__":
    unittest.main()
