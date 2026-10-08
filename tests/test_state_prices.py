"""Going rates for every state: read what each DOT printed, or nothing.

Each state publishes in its own shape, so each adapter is tested on rows
copied from that state's real file, and every committed rates/<st>.json is
checked the way the app relies on it.
"""
import datetime
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import build_state_prices as B  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RATES = os.path.join(ROOT, "curbcall_netlify_v4", "rates")


class FloridaTests(unittest.TestCase):
    # FDOT "Item Average Unit Cost" export, Sep 2025 - Aug 2026 statewide.
    ROWS = [
        ("Report Run On: 09/28/2026", None, None, None, None, None, None, None),
        ("From 2025/09/01  to 2026/08/31 ", None, None, None, None, None, None, None),
        ("0522  1", 123, 67.93, 9977195.33, 146864, "SY", "N", 'CONCRETE SIDEWALK AND DRIVEWAYS, 4" THICK'),
        ("0522  1108", 1, 141, 190914, 1354, "SY", "N", 'COQUINA CONCRETE SIDEWALK AND DRIVEWAYS, 4" THICK'),
        ("0527  2", 157, 36.35, 3333336.74, 91707, "SF", "N", "DETECTABLE WARNINGS"),
    ]

    def test_period_and_listed_items_only(self):
        period, got = B.parse_fl(self.ROWS)
        self.assertEqual(period, (2026, "Sep 2025 – Aug 2026"))
        self.assertEqual(got["0522-1"], (123, 67.93, 146864.0, "sq yd"))
        self.assertEqual(got["0527-2"], (157, 36.35, 91707.0, "sq ft"))
        self.assertNotIn("0522-1108", got)   # coquina sidewalk is not plain sidewalk


class OregonTests(unittest.TestCase):
    def row(self, desc, price, rank, code="0759-0128000J", unit="SQFT"):
        return (None, datetime.datetime(2025, 2, 27), 2025, "Q1", "15589", "1",
                "MAY STREET ELEVATED SIDEWALK REPLACEMENT", code, desc, unit,
                4200, price, 4200 * price, rank)

    def test_rank_and_price_land_in_the_right_fields(self):
        got = B.parse_or([self.row("CONCRETE WALKS", 14.5, 2)])
        self.assertEqual(len(got), 1)
        b = got[0]
        self.assertEqual((b["code"], b["region"], b["unit"], b["price"], b["rank"], b["qty"]),
                         ("0759-0128000J", "1", "sq ft", 14.5, 2, 4200.0))

    def test_variants_under_the_same_code_are_left_out(self):
        self.assertEqual(B.parse_or([self.row("CONCRETE WALKS, MODIFIED", 9.18, 1)]), [])

    def test_header_and_formula_rows_are_skipped(self):
        self.assertEqual(B.parse_or([(None, "ITEM NO.", "=INDEX(...)") + (None,) * 11]), [])


class MinnesotaTests(unittest.TestCase):
    ROWS = [
        ("ITEM GROUP", "ITEM NUMBER", "ITEM DESCRIPTION", "UNITS", "QUANTITY", "TOTAL DOLLARS",
         "AVERAGE\nBID PRICE", "CONTRACT\nOCCURANCE"),
        (2521, "2521518/00040", '4" CONCRETE WALK', "S F", 231359, 1915169.06, 8.28, 21),
        (2521, "2521518/00042", '4" CONCRETE WALK SPECIAL', "S F", 14539, 181753.5, 12.5, 2),
        (2531, "2531503/02320", "CONCRETE CURB AND GUTTER DESIGN B624", "L F", 120759, 3385701.38, 28.04, 35),
    ]

    def test_units_with_spaces_and_listed_items_only(self):
        got = B.parse_mn(self.ROWS)
        self.assertEqual(got["2521518/00040"], (8.28, 21, 231359.0, "sq ft"))
        self.assertEqual(got["2531503/02320"][3], "ft")
        self.assertNotIn("2521518/00042", got)


class OklahomaTests(unittest.TestCase):
    # ODOT eng260722.pdf as pypdf extracts it: quarter and district run into
    # the last price, the district only on the first row of each run, and an
    # item total row with no quarter at all.
    TEXT = """610(A)5200 / 4" CONCRETE SIDEWALK / SY
 1  2,433.00 $ 158,145.00 $ 65.00 $ 72.002025Q101
 3  5,366.00 $ 431,038.25 $ 80.33 $ 84.672025Q2
 2  7,270.00 $ 579,289.50 $ 79.68 $ 91.132026Q2
 1  2,719.75 $ 190,382.50 $ 70.00 $ 70.372025Q202
 67  130,346.53 $ 10,451,587.18 $ 80.18 $ 81.67
201(B)1300 / SELECTIVE CLEARING / LSUM
 2  2.00 $ 14,300.00 $ 7,150.00 $ 8,833.332025Q401
"""

    def test_rows_take_the_district_printed_above_them(self):
        rows = B.parse_ok(self.TEXT)
        self.assertEqual([(r[2], r[3], r[4]) for r in rows],
                         [("01", 2025, 1), ("01", 2025, 3), ("01", 2026, 2), ("02", 2025, 1)])
        self.assertEqual(rows[0][5:], (2433.0, 158145.0))

    def test_unlisted_items_and_total_rows_are_not_read(self):
        self.assertTrue(all(r[0] == "610(A)5200" for r in B.parse_ok(self.TEXT)))
        self.assertEqual(len(B.parse_ok(self.TEXT)), 4)


class StateCheckTests(unittest.TestCase):
    def state(self):
        s = B.State("ZZ", "Test", "src", "https://example.org", "awarded",
                    items={"A": {"name": "Walk", "unit": "sq ft", "cat": "sidewalk"}},
                    cats={"sidewalk": "A"}, headline=["A"],
                    districts={"STATEWIDE": "All"})
        s.add("A", "STATEWIDE", 2025, 10.0, None, None, 3, 100)
        return s

    def test_clean_data_passes(self):
        self.assertEqual(self.state().problems(), [])

    def test_an_average_outside_its_range_blocks_the_write(self):
        s = self.state()
        s.add("A", "STATEWIDE", 2024, 10.0, 11.0, 12.0, 3, 100)
        self.assertTrue(s.problems())

    def test_a_category_pointing_at_nothing_blocks_the_write(self):
        s = self.state()
        s.cats["curb"] = "B"
        self.assertTrue(s.problems())

    def test_an_unknown_district_blocks_the_write(self):
        s = self.state()
        s.add("A", "NW", 2025, 10.0, None, None, 3, 100)
        self.assertTrue(s.problems())


class CommittedRatesTests(unittest.TestCase):
    """The files the app loads."""

    def setUp(self):
        with open(os.path.join(RATES, "index.json"), encoding="utf-8") as f:
            self.index = json.load(f)["states"]
        self.data = {}
        for st in self.index:
            with open(os.path.join(RATES, st.lower() + ".json"), encoding="utf-8") as f:
                self.data[st] = json.load(f)

    def test_the_index_lists_every_state_file(self):
        files = {n[:2].upper() for n in os.listdir(RATES) if len(n) == 7 and n.endswith(".json")}
        self.assertEqual(set(self.index), files)
        self.assertTrue({"MO", "FL", "OR", "MN", "OK"} <= files)

    def test_every_price_is_internally_consistent(self):
        for st, d in self.data.items():
            self.assertEqual(d["state"], st)
            self.assertIn(d["basis"], ("all_bids", "awarded"))
            for table in ("prices", "wins"):
                for item, by_d in d.get(table, {}).items():
                    self.assertIn(item, d["items"], (st, item))
                    self.assertIn(d["items"][item]["unit"], ("sq yd", "sq ft", "ft", "each"))
                    for district, by_y in by_d.items():
                        self.assertIn(district, d["districts"], (st, item, district))
                        for year, row in by_y.items():
                            self.assertIn(int(year), d["years"])
                            avg, low, high, n = row[:4]
                            self.assertGreater(avg, 0)
                            self.assertGreaterEqual(n, 1)
                            if low is not None:
                                self.assertTrue(low <= avg <= high, (st, item, district, year))

    def test_categories_and_headlines_have_statewide_prices(self):
        for st, d in self.data.items():
            self.assertIn("sidewalk", d["cats"], st)
            for code in list(d["cats"].values()) + d["headline"]:
                self.assertIn("STATEWIDE", d["prices"][code], (st, code))

    def test_awarded_states_never_claim_a_bid_range(self):
        # FL, MN and OK publish averages of winning prices only; the app
        # must not show a low-high range nobody printed.
        for st, d in self.data.items():
            if d["basis"] == "awarded":
                for by_d in d["prices"].values():
                    for by_y in by_d.values():
                        for row in by_y.values():
                            self.assertEqual(row[1:3], [None, None], st)

    def test_known_values(self):
        # FDOT statewide 12-month, Sep 2025 - Aug 2026: 4 in. sidewalk.
        self.assertEqual(self.data["FL"]["prices"]["0522-1"]["STATEWIDE"]["2026"][0], 67.93)
        # MnDOT 2025 average bid prices: 4 in. concrete walk.
        self.assertEqual(self.data["MN"]["prices"]["2521518/00040"]["STATEWIDE"]["2025"][0], 8.28)
        # MoDOT 2025 book, SW district, curb ramp.
        self.assertEqual(self.data["MO"]["prices"]["6081010"]["SW"]["2025"],
                         [148.45, 63.5, 750.0, 50, 519.9])


if __name__ == "__main__":
    unittest.main()
