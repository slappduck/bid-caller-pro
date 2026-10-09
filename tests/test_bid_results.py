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
        self.assertEqual(w["6081010"]["KC"]["2026"], [281.0, 281.0, 281.0, 1, 281.0, 281.0])
        self.assertIn("STATEWIDE", w["6081010"])

    def test_the_middle_half_of_winning_prices(self):
        self.assertEqual(R.quartiles([10.0, 20.0, 30.0, 40.0, 50.0]), (20.0, 40.0))


class OregonContractTests(unittest.TestCase):
    def row(self, cid, code, desc, qty, price, rank, unit="SQFT"):
        import datetime
        return (None, datetime.datetime(2025, 2, 27), 2025, "Q1", cid, "1", "MAY ST", code, desc,
                unit, qty, price, qty * price, rank)

    def test_bidders_totals_and_flatwork_prices_by_rank(self):
        rows = [self.row("15589", "0759-0128000J", "CONCRETE WALKS", 100, 12.0, 1),
                self.row("15589", "0759-0128000J", "CONCRETE WALKS", 100, 15.0, 2),
                self.row("15589", "0222-0102000J", "TEMPORARY SIGNS", 10, 30.0, 1),
                self.row("15589", "0222-0102000J", "TEMPORARY SIGNS", 10, 20.0, 2)]
        c = R.parse_or_contracts(rows)[0]
        self.assertEqual(c["bidders"], [[None, 1500.0], [None, 1700.0]])
        self.assertEqual(c["items"], {"0759-0128000J": [100.0, [12.0, 15.0]]})
        self.assertEqual(c["district"], "1")

    def test_a_contract_missing_a_rank_is_dropped(self):
        rows = [self.row("1", "0759-0128000J", "CONCRETE WALKS", 100, 12.0, 1),
                self.row("1", "0759-0128000J", "CONCRETE WALKS", 100, 15.0, 3)]
        self.assertEqual(R.parse_or_contracts(rows), [])


class NorthCarolinaTests(unittest.TestCase):
    """NCDOT's spreadsheet: three bidders a row, more on later page numbers."""

    def row(self, page, item, desc, qty, unit, bidders):
        v = ["L250617", float(page), "06/17/2025", "2:00 PM", 1.0, "C204798", "", "", "", "GUILFORD", 0, "E",
             "RESURFACING", "", item, "848", 59.0, 1.0, "ROADWAY ITEMS", desc, "", qty, unit, ""]
        for name, price in bidders:
            v += [name, "TOWN, NC", str(price), str(price * qty), ""]
        while len(v) < 39:
            v += ["", "", "", "", ""]
        return v[:39] + [46000.0]

    def test_bidders_across_pages_keep_their_rank(self):
        rows = [self.row(1, "2591000000-E", '4" CONCRETE SIDEWALK', 100, "SY", [("A CO", 50), ("B CO", 55), ("C CO", 60)]),
                self.row(2, "2591000000-E", '4" CONCRETE SIDEWALK', 100, "SY", [("D CO", 70)]),
                self.row(1, "0000100000-N", "MOBILIZATION", 1, "LS", [("A CO", 1000), ("B CO", 2000), ("C CO", 2500)]),
                self.row(2, "0000100000-N", "MOBILIZATION", 1, "LS", [("D CO", 3000)])]
        c = R.parse_nc_tabs(rows)[0]
        self.assertEqual([b[0] for b in c["bidders"]], ["A CO", "B CO", "C CO", "D CO"])
        self.assertEqual([b[1] for b in c["bidders"]], [6000.0, 7500.0, 8500.0, 10000.0])
        self.assertEqual(c["items"]["2591000000-E"], [100.0, [50.0, 55.0, 60.0, 70.0]])
        self.assertEqual(c["date"], "2025-06-17")

    def test_a_row_missing_a_bidders_price_drops_the_item(self):
        rows = [self.row(1, "2591000000-E", '4" CONCRETE SIDEWALK', 100, "SY", [("A CO", 50), ("B CO", 55)]),
                self.row(1, "0000100000-N", "MOBILIZATION", 1, "LS", [("A CO", 1000), ("B CO", 2000), ("C CO", 2500)])]
        self.assertEqual(R.parse_nc_tabs(rows), [])


class KentuckyTests(unittest.TestCase):
    TEXT = """Call: 101
GRADE & DRAIN US 60
Number of Bidders 3
0570 SIDEWALK-4 IN CONCRETE 7,760.000 SQYD A A 73.00 57.00 70.21
0580 DGA BASE 100.000 TON A A 39.00 45.35 50.00
0590 DETECTABLE WARNINGS 120.000 SQFT A A 40.00 45.00
1 00563 LOUISVILLE PAVING COMPANY INC 20,325,703.42
2 00568 MAC CONSTRUCTION & EXCAVATING INC 20,988,000.00
3 02233 CLEARY CONSTRUCTION INC 23,525,000.00
Contid: 26-1519
County: JEFFERSON COUNTY District: 05 Date Let: 9/24/26 Contid: 26-1519 SYP: 05-00481.00
0600 SIDEWALK-4 IN CONCRETE 240.000 SQYD A A 80.00 60.00 75.00
"""

    def test_a_contract_with_its_ranked_bidders_and_flatwork(self):
        c = R.parse_ky(self.TEXT)[0]
        self.assertEqual((c["id"], c["date"], c["counties"], c["district"]), ("26-1519", "2026-09-24", "Jefferson", "5"))
        self.assertEqual(c["bidders"][0], ["LOUISVILLE PAVING COMPANY INC", 20325703.42])
        qty, prices = c["items"]["SIDEWALK-4 IN CONCRETE"]
        self.assertEqual(qty, 8000.0)   # both rows, either side of a page break
        self.assertEqual(prices[0], round((7760 * 73 + 240 * 80) / 8000, 2))

    def test_a_row_short_of_its_bidders_is_dropped(self):
        self.assertNotIn("DETECTABLE WARNINGS", R.parse_ky(self.TEXT)[0]["items"])


class KansasTests(unittest.TestCase):
    # KDOT monthly bid-tab CSV: one row per item per bidder.
    HEAD = "PROPOSAL_NM,PROJECT_NM,DESCR,VENDORNAME,REFITEM_NM,UNIT,QTY,BIDPRICE,EXTENDEDAMOUNT\n"
    ROWS = [
        ("226091234", "K-10 SIDEWALK", "JOHNSON", "AMINO BROTHERS CO INC", "025026", "SQYD", "500", "60.00", "30000.00"),
        ("226091234", "K-10 SIDEWALK", "JOHNSON", "AMINO BROTHERS CO INC", "061597", "LNFT", "200", "40.00", "8000.00"),
        ("226091234", "K-10 SIDEWALK", "JOHNSON", "MILLER  PAVING", "025026", "SQYD", "500", "70.00", "35000.00"),
        ("226091234", "K-10 SIDEWALK", "JOHNSON", "MILLER  PAVING", "061597", "LNFT", "200", "45.00", "9000.00"),
        ("226091234", "K-10 SIDEWALK", "JOHNSON", "SLOW CO", "025026", "SQYD", "500", "65.00", "32500.00"),
        ("226091234", "K-10 SIDEWALK", "JOHNSON", "SLOW CO", "999999", "LS", "1", "20000.00", "20000.00"),
    ]

    def csv(self):
        return self.HEAD + "".join(",".join(r) + "\n" for r in self.ROWS)

    def test_bidders_rank_by_their_summed_totals(self):
        c = R.parse_ks_csv(self.csv(), "2026-09")[0]
        self.assertEqual((c["id"], c["date"], c["counties"]), ("226091234", "2026-09-01", "Johnson"))
        self.assertEqual([b[0] for b in c["bidders"]], ["AMINO BROTHERS CO INC", "MILLER PAVING", "SLOW CO"])
        self.assertEqual(c["items"]["025026"], [500.0, [60.0, 70.0, 65.0]])

    def test_an_item_not_every_bidder_priced_is_dropped(self):
        self.assertNotIn("061597", R.parse_ks_csv(self.csv(), "2026-09")[0]["items"])


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
                for avg, low, high, n, p25, p75 in by_y.values():
                    self.assertTrue(low <= avg <= high)
                    self.assertTrue(low <= p25 <= p75 <= high)
                    self.assertGreaterEqual(n, 1)

    def test_the_index_says_missouri_and_oregon_have_results(self):
        self.assertTrue(self.index["MO"].get("results"))
        self.assertTrue(self.index["OR"].get("results"))

    def test_oregon_contracts_line_up(self):
        with open(os.path.join(ROOT, "curbcall_netlify_v4", "results", "or.json"), encoding="utf-8") as f:
            d = json.load(f)
        self.assertFalse(d["named"])
        self.assertGreater(len(d["contracts"]), 50)
        for c in d["contracts"]:
            totals = [b[1] for b in c["bidders"]]
            self.assertTrue(all(t > 0 for t in totals))
            for code, (qty, prices) in c["items"].items():
                self.assertEqual(len(prices), len(totals))


if __name__ == "__main__":
    unittest.main()
