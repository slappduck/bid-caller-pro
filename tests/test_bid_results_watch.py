"""Bid results, read when they're posted.

After a bid opens, the agency posts a tabulation. The watch finds it on the
posting, the reader pulls out every bidder's total and the low bidder's unit
prices, and those unit prices -- no names -- become city and county going
rates any customer in the state sees (/city-prices).
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls  # noqa: E402
from license_server import kv_backend  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_bid_documents import _UploadSetup  # noqa: E402

RAW = {"is_results": True, "opened": "October 14, 2026 2:00 PM",
       "bidders": [{"name": "Ozark Flatwork LLC", "total": "$162,410.00"},
                   {"name": "Test Concrete LLC", "total": "158,900"},
                   {"name": "Late Bidder Inc", "total": ""}, "junk"],
       "awarded_to": "Test Concrete LLC",
       "items": [{"item_no": "1", "description": "4 in. Concrete Sidewalk", "quantity": "1,450", "unit": "SY", "unit_price": "$71.50"},
                 {"item_no": "2", "description": "Remove existing sidewalk", "quantity": "1,450", "unit": "SY", "unit_price": "12"},
                 {"item_no": "3", "description": "ADA Curb Ramp w/ detectable warning", "quantity": "12", "unit": "EA", "unit_price": "1,600"},
                 {"item_no": "4", "description": "Asphalt patching", "quantity": "40", "unit": "TON", "unit_price": "150"},
                 {"item_no": "5", "description": "Mobilization", "quantity": "1", "unit": "LS", "unit_price": ""}]}


class CleanResultsTests(unittest.TestCase):
    def test_bidders_come_back_low_first_with_numbers(self):
        got = ls._clean_results_result(RAW)
        self.assertTrue(got["is_results"])
        self.assertEqual([b["name"] for b in got["bidders"]],
                         ["Test Concrete LLC", "Ozark Flatwork LLC", "Late Bidder Inc"])
        self.assertEqual(got["bidders"][0]["total"], 158900.0)
        self.assertIsNone(got["bidders"][2]["total"])

    def test_only_priced_items_are_kept(self):
        items = ls._clean_results_result(RAW)["items"]
        self.assertEqual([i["item_no"] for i in items], ["1", "2", "3", "4"])
        self.assertEqual(items[0]["unit_price"], 71.5)

    def test_a_document_that_isnt_results_says_so(self):
        self.assertFalse(ls._clean_results_result({"is_results": False})["is_results"])
        self.assertFalse(ls._clean_results_result("nonsense")["is_results"])
        self.assertFalse(ls._clean_results_result({"bidders": []})["is_results"])


class WatchFindsResultsTests(unittest.TestCase):
    def test_the_tabulation_is_handed_back_but_not_the_bid_form_or_addenda(self):
        html = (b'<a href="/files/Bid-Form.pdf">Bid Form</a><a href="/files/Addendum-1.pdf">Addendum No. 1</a>'
                b'<a href="/files/2026-14-tab.pdf">Bid Tabulation</a><a href="/files/award.pdf">Notice of Award</a>')
        fp = ls._watch_fingerprint("https://93.184.216.34/bids/12", html, "text/html")
        self.assertEqual([d["name"] for d in fp["result_docs"]], ["Bid Tabulation", "Notice of Award"])

    def test_a_pdf_posting_has_none(self):
        self.assertEqual(ls._watch_fingerprint("https://x/y.pdf", b"%PDF-1.4", "application/pdf")["result_docs"], [])


class ReadResultsTests(_UploadSetup, unittest.TestCase):
    def ai(self, text, images=None, addendum=False, kind=None):
        self.kinds.append(kind)
        return ls._clean_results_result(RAW) if kind == "results" else ls._clean_doc_result({"line_items": []})

    def setUp(self):
        super().setUp()
        self.kinds = []
        self._ai = patch.object(ls, "_ai_read_bid_document", side_effect=self.ai)
        self._ai.start()

    def tearDown(self):
        self._ai.stop()
        super().tearDown()

    def test_a_results_read_returns_the_bidders_and_keeps_the_concrete_prices(self):
        d = self.upload("/bid-documents/read", b"%PDF-1.4 tab", kind="results", state="mo", town="Springfield").get_json()
        self.assertTrue(d["ok"] and d["is_results"])
        self.assertEqual(self.kinds, ["results"])
        rows = self.store[ls._CITY_PRICES_KEY + "MO"]
        self.assertEqual(sorted(r["cat"] for r in rows), ["ramp", "removal", "sidewalk"])
        self.assertFalse(any("Concrete LLC" in str(r) for r in rows), "no bidder names are kept")
        side = next(r for r in rows if r["cat"] == "sidewalk")
        self.assertEqual((side["unit"], side["qty"], side["price"], side["town"]), ("sq yd", 1450.0, 71.5, "Springfield"))

    def test_reading_the_same_tabulation_twice_adds_nothing(self):
        self.upload("/bid-documents/read", b"%PDF-1.4 tab", kind="results", state="MO")
        d = self.upload("/bid-documents/read", b"%PDF-1.4 tab", kind="results", state="MO").get_json()
        self.assertTrue(d["cached"])
        self.assertEqual(len(self.store[ls._CITY_PRICES_KEY + "MO"]), 3)

    def test_city_prices_come_back_by_item_and_unit(self):
        self.upload("/bid-documents/read", b"%PDF-1.4 tab", kind="results", state="MO")
        d = self.client.get("/city-prices?state=mo").get_json()
        self.assertEqual(d["prices"]["sidewalk"]["sq yd"][0][:2], [1450.0, 71.5])
        self.assertEqual(d["prices"]["ramp"]["each"][0][1], 1600.0)
        self.assertEqual(self.client.get("/city-prices?state=Missouri").status_code, 400)

    def test_old_rows_drop_out(self):
        self.store[ls._CITY_PRICES_KEY + "KS"] = [
            {"k": "a", "cat": "sidewalk", "unit": "sq yd", "qty": 100, "price": 80, "at": "2015-01-01"},
            {"k": "b", "cat": "sidewalk", "unit": "sq yd", "qty": 100, "price": 70, "at": ls.time.strftime("%Y-%m-%d")}]
        d = self.client.get("/city-prices?state=KS").get_json()
        self.assertEqual([r[1] for r in d["prices"]["sidewalk"]["sq yd"]], [70])


class CategoryTests(unittest.TestCase):
    def test_items_land_in_the_right_place(self):
        cases = {"4\" PCC Sidewalk": "sidewalk", "Concrete curb and gutter, type B": "curb_gutter",
                 "Saw cut and remove concrete": "removal", "Detectable warning surface": "domes",
                 "Concrete driveway approach": "driveway", "Asphalt overlay": "", "Concrete curb": "curb",
                 "ADA concrete ramp": "ramp"}
        for desc, cat in cases.items():
            self.assertEqual(ls._city_cat(desc), cat, desc)


if __name__ == "__main__":
    unittest.main()
