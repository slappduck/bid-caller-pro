"""MoDOT unit bid prices: read exactly what MoDOT printed, or nothing.

The app shows these numbers to contractors pricing real work, so a misread
column is worse than a missing row. The lines below are copied from the
2021-2025 Unit Bid Price Books, including the two formatting quirks found
there: 2024 prints dollar signs, 2022 writes its statewide header as
"Statewide".
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import build_unit_prices as U  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BOOK_2025 = """MoDOT  2025 UNIT BID PRICES NW DISTRICT
PAY ITEM DESCRIPTION UNIT AVERAGE
6081010 CONCRETE CURB RAMP S.Y. 154.92 29 221.69 550.00 135.00 4492.60
6086004 CONCRETE SIDEWALK, 4 IN. S.Y. 1653.10 20 79.72 1269.13 70.00 33061.90
6093015 ASPHALT CURB (4 INCH) L.F. 120.00 3 15.00 20.00 10.00 360.00
6096041 PLACING TYPE 1 ROCK DITCH LINER C.Y. 10.00 8 315.26 569.42 155.00 80.00
MoDOT  2025 UNIT BID PRICES SW DISTRICT
6081010 CONCRETE CURB RAMP S.Y. 519.90 50 148.45 750.00 63.50 25995.00
"""
BOOK_2024 = """UNIT BID PRICES SW DISTRICT
6086004 CONCRETE SIDEWALK, 4 IN. S.Y. 1600.000 54 $65.48 $877.11 $43.35 86400.000
6091052 CURB AND GUTTER TYPE B L.F. 2,100.500 34 $44.40 $120.00 $31.52 71417.000
"""
BOOK_2022 = """UNIT BID PRICES Statewide
6081012 TRUNCATED DOMES S.F. 300.00 171 32.36 250.00 11.75 51300.00
"""


class ParseTests(unittest.TestCase):
    def test_columns_land_in_the_right_fields(self):
        r = U.parse_text(BOOK_2025, 2025)[0]
        self.assertEqual((r["district"], r["item"], r["unit"]), ("NW", "6081010", "S.Y."))
        self.assertEqual((r["avg_qty"], r["bids"], r["avg"], r["high"], r["low"], r["total_qty"]),
                         (154.92, 29, 221.69, 550.0, 135.0, 4492.6))

    def test_each_row_takes_the_district_above_it(self):
        rows = U.parse_text(BOOK_2025, 2025)
        self.assertEqual([r["district"] for r in rows], ["NW", "NW", "NW", "NW", "SW"])

    def test_dollar_signs_and_thousands_separators(self):
        rows = U.parse_text(BOOK_2024, 2024)
        self.assertEqual(rows[0]["avg"], 65.48)
        self.assertEqual(rows[1]["avg_qty"], 2100.5)

    def test_a_mixed_case_statewide_header(self):
        self.assertEqual(U.parse_text(BOOK_2022, 2022)[0]["district"], "STATEWIDE")

    def test_rows_before_any_header_are_not_guessed_into_a_district(self):
        self.assertEqual(U.parse_text("6081010 CONCRETE CURB RAMP S.Y. 1 2 3 4 1 5", 2025), [])

    def test_an_unreadable_line_is_skipped_not_half_read(self):
        text = "UNIT BID PRICES SW DISTRICT\n6131014 FULL DEPTH PAVEMENT REPAIR SAWL.F. 14220.36 25"
        self.assertEqual(U.parse_text(text, 2025), [])


class SelectionTests(unittest.TestCase):
    def test_only_concrete_flatwork_is_kept(self):
        kept = [r["item"] for r in U.parse_text(BOOK_2025, 2025) if U.is_flatwork(r)]
        self.assertEqual(kept, ["6081010", "6086004", "6081010"])  # no asphalt, no rock liner


class CheckTests(unittest.TestCase):
    def rows(self):
        return [r for r in U.parse_text(BOOK_2025, 2025) if U.is_flatwork(r)]

    def test_clean_data_passes(self):
        self.assertEqual(U.check(self.rows(), [2025]), [])

    def test_an_average_outside_its_own_range_blocks_the_write(self):
        rows = self.rows()
        rows[0]["avg"] = 999.0
        self.assertTrue(U.check(rows, [2025]))

    def test_a_year_that_read_nothing_blocks_the_write(self):
        self.assertTrue(U.check(self.rows(), [2025, 2024]))

    def test_a_row_listed_twice_blocks_the_write(self):
        rows = self.rows()
        self.assertTrue(U.check(rows + [rows[0]], [2025]))


class BuildTests(unittest.TestCase):
    def test_the_app_format(self):
        rows = [r for r in U.parse_text(BOOK_2025, 2025) if U.is_flatwork(r)]
        d = U.build(rows, [2025])
        self.assertEqual(d["items"]["6081010"], {"name": "Concrete curb ramp (ADA)", "unit": "sq yd"})
        self.assertEqual(d["prices"]["6081010"]["SW"]["2025"], [148.45, 63.5, 750.0, 50, 519.9])
        self.assertEqual(d["fields"], ["avg", "low", "high", "bids", "avg_qty"])


class CommittedDataTests(unittest.TestCase):
    """The file the app actually loads."""

    def setUp(self):
        with open(os.path.join(ROOT, "curbcall_netlify_v4", "rates", "mo.json"), encoding="utf-8") as f:
            self.d = json.load(f)

    def test_every_price_is_internally_consistent(self):
        for item, by_district in self.d["prices"].items():
            self.assertIn(item, self.d["items"])
            for district, by_year in by_district.items():
                self.assertIn(district, self.d["districts"])
                for year, (avg, low, high, bids, qty) in by_year.items():
                    self.assertIn(int(year), self.d["years"])
                    self.assertTrue(low <= avg <= high, (item, district, year))
                    self.assertGreaterEqual(bids, 1)

    def test_the_headline_items_are_present_for_every_district(self):
        for item in ("6081010", "6086004", "6091052"):
            for district in ("SW", "STATEWIDE"):
                self.assertIn(str(max(self.d["years"])), self.d["prices"][item][district])

    def test_a_known_value_from_the_2025_book(self):
        # MoDOT 2025 book, SW district, page for section 608.
        self.assertEqual(self.d["prices"]["6081010"]["SW"]["2025"], [148.45, 63.5, 750.0, 50, 519.9])


if __name__ == "__main__":
    unittest.main()
