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


class ArkansasTests(unittest.TestCase):
    # ARDOT Weighted Average Unit Prices, 12 months to June 24, 2026, as
    # pypdf extracts it.
    TEXT = """ITEM ITEM DESCRIPTION UNIT QUANTITY HIGH LOW
FROM 6/25/2025 TO 6/24/2026
202 R&D OF CONCRETE WALKS SQYD 6.00 824.69 824.69 824.69 *
202 R&D OF WALKS SQYD 4,379.00 249.67 8.00 20.86
633 CONCRETE WALKS SQYD 30,775.00 225.00 76.00 92.90
633 CONCRETE WALKS (TY. SPECIAL) SQYD 2,090.00 1,035.00 83.00 923.97
634 CC CURB & GUTTER-A (1'6") LF  84,431.00 100.00 22.50 31.25
634 CONCRETE CURB (TYPE B) LF  500.00 43.00 43.00 43.00 *
641 WHEELCHAIR RAMPS (TYPE 3) SQYD 1,343.00 1,313.48 128.00 346.83
641 WHEELCHAIR RAMPS(TYPE 3) SQYD 10.00 1,680.42 812.61 1,159.73
641 WHEELCHAIR RAMPS (TYPE SPECIAL) SQYD 70.00 410.00 410.00 410.00 *
"""

    def test_period_and_plain_rows(self):
        period, got = B.parse_ar(self.TEXT)
        self.assertEqual(period, "Jun 25, 2025 – Jun 24, 2026")
        self.assertEqual(got["633 CONCRETE WALKS"], (92.90, 76.0, 225.0, None, 30775.0))
        self.assertEqual(got["634 CC CURB & GUTTER-A (1'6\")"][:3], (31.25, 22.5, 100.0))

    def test_one_job_only_means_one_contract(self):
        self.assertEqual(B.parse_ar(self.TEXT)[1]["634 CONCRETE CURB (TYPE B)"][3], 1)

    def test_names_for_one_item_are_pooled_by_quantity(self):
        got = B.parse_ar(self.TEXT)[1]
        avg, low, high, n, qty = got["641 WHEELCHAIR RAMPS"]
        self.assertEqual(qty, 1353.0)   # TYPE SPECIAL isn't a numbered ramp
        self.assertAlmostEqual(avg, (1343 * 346.83 + 10 * 1159.73) / 1353)
        self.assertEqual((low, high, n), (128.0, 1680.42, None))
        self.assertEqual(got["202 R&D OF WALKS"][4], 4385.0)

    def test_variants_are_left_out(self):
        self.assertNotIn("633 CONCRETE WALKS (TY. SPECIAL)", B.parse_ar(self.TEXT)[1])


class NebraskaTests(unittest.TestCase):
    # NDOT AUP summary, January-December 2025, one page as pypdf extracts
    # it: code rows first, then description rows in another order.
    PAGE = """06/18/2026
10:19 AMEnglish Average Unit Price for Lettings
January 1, 2025 to December 31, 2025
3014.11 267 LF $124.00
3016.21 8,942 SY $69.32
3016.33 8,065 SY $92.10
3016.39 3,413 SF $39.49
CONCRETE CLASS 47B-3000 SIDEWALK 5" $14,506.00
CONCRETE CLASS 47B-3000 SIDEWALKS $619,890.74
DETECTABLE WARNING PANEL $134,778.37
COMBINATION CONCRETE CLASS 47B-3500 CURB AND GUTTER $33,108.00
REMOVE AND REPLACE SIDEWALK $742,816.50
"""

    def test_codes_find_their_description_by_total(self):
        period, got = B.parse_ne([self.PAGE])
        self.assertEqual(period, "January 1, 2025 – December 31, 2025")
        self.assertEqual(got["3016.21"], (69.32, 8942.0))
        self.assertEqual(got["3016.39"][0], 39.49)
        self.assertEqual(got["3014.11"][0], 124.0)

    def test_a_total_that_names_another_item_is_not_taken(self):
        # 8,065 x $92.10 lands within rounding of another item's total; its
        # name isn't this item's, so the price isn't read as this item's.
        self.assertNotIn("3016.33", B.parse_ne([self.PAGE])[1])


class DiscoveryTests(unittest.TestCase):
    def test_a_web_page_where_a_pdf_should_be_is_not_posted_yet(self):
        # MDT answers a year it hasn't posted with its home page and a 200.
        import tempfile
        from unittest.mock import patch
        cache = tempfile.mkdtemp()

        def fake(url, cache_dir, name):
            path = os.path.join(cache_dir, name)
            with open(path, "wb") as f:
                f.write(b"<!DOCTYPE html><html>" if "2026" in name else b"%PDF-1.7 ...")
            return path
        with patch.object(B, "_download", side_effect=fake):
            self.assertIsNone(B._try_download("https://x/2026.pdf", cache, "mt_avg_2026.pdf"))
            self.assertFalse(os.path.exists(os.path.join(cache, "mt_avg_2026.pdf")))
            self.assertTrue(B._try_download("https://x/2025.pdf", cache, "mt_avg_2025.pdf"))

    def test_recent_years_end_this_year(self):
        this = datetime.date.today().year
        self.assertEqual(B._recent_years(2000), list(range(this - 4, this + 1)))
        self.assertEqual(B._recent_years(this), [this])


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
                    self.assertIn(d["items"][item]["unit"], ("sq yd", "sq ft", "ft", "each", "cu yd"))
                    for district, by_y in by_d.items():
                        self.assertIn(district, d["districts"], (st, item, district))
                        for year, row in by_y.items():
                            self.assertIn(int(year), d["years"])
                            avg, low, high, n = row[:4]
                            self.assertGreater(avg, 0)
                            if n is not None:   # TN doesn't publish a count
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
        # must not show a low-high range nobody printed. ARDOT prints the
        # high and low winning contract price, and the app labels it so.
        for st, d in self.data.items():
            if d["basis"] == "awarded" and st not in ("AR",):
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
