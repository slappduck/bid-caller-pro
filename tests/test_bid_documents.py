"""Reading a bid's own documents: safe to fetch, honest about what it read.

The URL comes from the app, so this is the one fetch on the server that is
pointed by the client. It must never reach the server's own network, and
what the AI returns is validated rather than trusted.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls
import kv_backend


class PublicUrlTests(unittest.TestCase):
    def test_private_and_local_addresses_are_refused(self):
        for url in ("http://127.0.0.1/x", "http://10.0.0.5/bid.pdf", "http://192.168.1.1/",
                    "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://localhost/"):
            self.assertFalse(ls._public_http_url(url), url)

    def test_other_schemes_and_ports_are_refused(self):
        for url in ("file:///etc/passwd", "ftp://93.184.216.34/x", "http://93.184.216.34:8080/x",
                    "gopher://93.184.216.34/", "", "not a url"):
            self.assertFalse(ls._public_http_url(url), url)

    def test_a_public_address_is_allowed(self):
        self.assertTrue(ls._public_http_url("https://93.184.216.34/bid-form.pdf"))

    def test_a_redirect_to_a_private_address_is_refused(self):
        handler = ls._CheckedRedirect()
        with self.assertRaises(ls.urllib.error.URLError):
            handler.redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/")


class RobotsCheckTests(unittest.TestCase):
    """robots.txt is fetched through the checked opener, not the shared one."""

    def _opener(self, body=None, exc=None):
        class R:
            def __init__(s, b): s.b = b
            def read(s, n): return s.b
            def __enter__(s): return s
            def __exit__(s, *a): return False
        class O:
            def __init__(s): s.urls = []
            def open(s, req, timeout=None):
                s.urls.append(req.full_url)
                if exc: raise exc
                return R(body)
        return O()

    def test_a_disallow_is_honoured(self):
        o = self._opener(b"User-agent: *\nDisallow: /bids/")
        self.assertFalse(ls._robots_allows_checked("https://93.184.216.34/bids/form.pdf", o))
        self.assertEqual(o.urls, ["https://93.184.216.34/robots.txt"])

    def test_an_unreadable_robots_txt_is_not_a_refusal(self):
        o = self._opener(exc=ls.urllib.error.URLError("redirect to a non-public address"))
        self.assertTrue(ls._robots_allows_checked("https://93.184.216.34/form.pdf", o))

    def test_the_document_fetch_uses_the_checked_robots_rule(self):
        with patch.object(ls, "_public_http_url", return_value=True), \
             patch.object(ls, "_robots_allows_checked", return_value=False) as checked, \
             patch.object(ls, "_robots_allows", side_effect=AssertionError("shared robots check used")):
            self.assertEqual(ls._fetch_document("https://93.184.216.34/x.pdf")[2], "robots_disallow")
        checked.assert_called_once()


class CleanResultTests(unittest.TestCase):
    def test_quantities_are_numbers_and_bad_rows_are_dropped(self):
        got = ls._clean_doc_result({"line_items": [
            {"item_no": "1", "description": "4 in. concrete sidewalk", "quantity": "1,250", "unit": "SY"},
            {"item_no": "2", "description": "", "quantity": "5", "unit": "EA"},
            "not a dict",
            {"item_no": "3", "description": "Mobilization", "quantity": "", "unit": "LS"},
            {"item_no": "4", "description": "Curb", "quantity": "-3", "unit": "<script>"},
        ], "required_forms": ["Non-collusion affidavit", "  "], "submission": 42})
        items = got["line_items"]
        self.assertEqual([i["description"] for i in items], ["4 in. concrete sidewalk", "Mobilization", "Curb"])
        self.assertEqual(items[0]["quantity"], 1250.0)
        self.assertIsNone(items[1]["quantity"])
        self.assertIsNone(items[2]["quantity"], "a negative quantity is not a quantity")
        self.assertEqual(items[2]["unit"], "", "an odd unit is dropped, not passed to the app")
        self.assertEqual(got["required_forms"], ["Non-collusion affidavit"])
        self.assertEqual(got["submission"], "", "a non-string field is blanked")

    def test_a_non_object_is_rejected(self):
        self.assertIsNone(ls._clean_doc_result(["a list"]))


class DocumentTextTests(unittest.TestCase):
    def test_html_is_read_as_text(self):
        text, pages, truncated = ls._document_text(
            b"<html><body><p>Item 1 Concrete sidewalk 100 SY</p></body></html>", "text/html")
        self.assertIn("Concrete sidewalk 100 SY", text)
        self.assertFalse(truncated)

    def test_a_broken_pdf_reads_as_nothing_rather_than_raising(self):
        self.assertEqual(ls._document_text(b"%PDF-1.4 garbage", "application/pdf")[0], "")


class EndpointTests(unittest.TestCase):
    URL = "https://93.184.216.34/bid-form.pdf"

    def setUp(self):
        self.client = ls.app.test_client()
        self.store = {}
        self.fetches = []
        self._p = [
            patch.object(ls, "_license_is_active", return_value=True),
            patch.object(ls, "OPENAI_API_KEY", "k"),
            patch.object(kv_backend, "get", side_effect=lambda k, d=None: self.store.get(k, d)),
            patch.object(kv_backend, "set", side_effect=lambda k, v: self.store.__setitem__(k, v)),
            patch.object(ls, "_fetch_document",
                         side_effect=lambda u: (self.fetches.append(u), (b"%PDF-", "application/pdf", "ok"))[1]),
            patch.object(ls, "_document_text", return_value=("Item 1 sidewalk 100 SY", 3, False)),
            patch.object(ls, "_ai_read_bid_document", return_value={
                "line_items": [{"item_no": "1", "description": "Sidewalk", "quantity": 100.0, "unit": "SY"}],
                "submission": "Sealed envelope to City Clerk", "bid_security": "5% bid bond",
                "prebid": "", "required_forms": []}),
            patch.object(ls, "_ip_rate_ok", return_value=True),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def post(self, **over):
        body = {"key": "k", "device_id": "d", "url": self.URL}
        body.update(over)
        return self.client.post("/bid-documents/read", json=body)

    def test_a_read_returns_the_schedule_and_requirements(self):
        d = self.post().get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["line_items"][0]["quantity"], 100.0)
        self.assertEqual(d["bid_security"], "5% bid bond")
        self.assertEqual(d["pages_read"], 3)

    def test_an_unlicensed_caller_is_refused_before_any_fetch(self):
        with patch.object(ls, "_license_is_active", return_value=False):
            self.assertEqual(self.post().status_code, 403)
        self.assertEqual(self.fetches, [])

    def test_a_private_url_is_refused_before_any_fetch(self):
        self.assertEqual(self.post(url="http://169.254.169.254/").status_code, 400)
        self.assertEqual(self.fetches, [])

    def test_the_second_read_of_a_document_comes_from_cache(self):
        self.post()
        d = self.post().get_json()
        self.assertTrue(d["cached"])
        self.assertEqual(len(self.fetches), 1)

    def test_the_daily_limit_applies(self):
        with patch.object(ls, "_ip_rate_ok", return_value=False):
            self.assertEqual(self.post().status_code, 429)
        self.assertEqual(self.fetches, [])

    def test_a_scanned_pdf_says_so(self):
        with patch.object(ls, "_document_text", return_value=("", 2, False)):
            d = self.post().get_json()
        self.assertEqual(d["reason"], "no_text")

    def test_a_failed_fetch_is_reported(self):
        with patch.object(ls, "_fetch_document", return_value=(b"", "", "http_404")):
            d = self.post().get_json()
        self.assertEqual((d["ok"], d["detail"]), (False, "http_404"))


def fillable_pdf(fields):
    """A one-page PDF with an AcroForm text field per name, built by hand."""
    objs = ["<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [%s] >> >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Annots [%s] >>"]
    refs = []
    for i, name in enumerate(fields):
        n = 4 + i
        refs.append(f"{n} 0 R")
        objs.append(f"<< /Type /Annot /Subtype /Widget /FT /Tx /T ({name}) /TU ({name}) "
                    f"/Rect [50 {700 - 30 * i} 300 {720 - 30 * i}] /P 3 0 R /V () >>")
    objs[0] = objs[0] % " ".join(refs)
    objs[2] = objs[2] % " ".join(refs)
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


class FillValuesTests(unittest.TestCase):
    def test_only_supplied_values_exist(self):
        vals = ls._fill_values({"company": {"name": "Test Concrete LLC", "phone": ""},
                                "bid": {"total": 120500.25, "addenda": "#1"},
                                "lines": [{"item_no": "2", "description": "Sidewalk", "quantity": 1250,
                                           "unit": "SY", "unit_price": 62.5, "amount": 78125}]})
        self.assertEqual(vals["company.name"][1], "Test Concrete LLC")
        self.assertNotIn("company.phone", vals)
        self.assertEqual(vals["total"][1], "120,500.25")
        self.assertEqual(vals["line.1.unit_price"][1], "62.50")
        self.assertEqual(vals["line.1.quantity"][1], "1,250")

    def test_dollars_in_words(self):
        self.assertEqual(ls._dollars_in_words(120500.25),
                         "One hundred twenty thousand five hundred dollars and 25/100")
        self.assertEqual(ls._dollars_in_words(1001), "One thousand one dollars and 00/100")


class ResolveFillTests(unittest.TestCase):
    FIELDS = [("Bidder Name", ""), ("Total Bid", ""), ("Authorized Signature", ""),
              ("Text7", "Notary public commission expires")]
    VALS = {"company.name": ("Company name", "Test Concrete LLC"), "total": ("Total bid", "1,000.00")}

    def test_the_matcher_cannot_invent_fields_or_values(self):
        plan = ls._resolve_fill(self.FIELDS, self.VALS, {
            "Bidder Name": "company.name", "Total Bid": "total",
            "Made Up Field": "company.name", "Total Bid ": "a value nobody supplied"})
        self.assertEqual(plan, {"Bidder Name": "company.name", "Total Bid": "total"})

    def test_signature_and_notary_fields_are_never_filled(self):
        plan = ls._resolve_fill(self.FIELDS, self.VALS, {
            "Authorized Signature": "company.name", "Text7": "company.name"})
        self.assertEqual(plan, {})


class FillEndpointTests(unittest.TestCase):
    URL = "https://93.184.216.34/bid-form.pdf"
    PDF = fillable_pdf(["Bidder Name", "Total Bid", "Authorized Signature"])

    def setUp(self):
        self.client = ls.app.test_client()
        self.asked = []
        self._p = [
            patch.object(ls, "_license_is_active", return_value=True),
            patch.object(ls, "OPENAI_API_KEY", "k"),
            patch.object(ls, "_ip_rate_ok", return_value=True),
            patch.object(ls, "_fetch_document", return_value=(self.PDF, "application/pdf", "ok")),
            patch.object(ls, "_ai_match_fields", side_effect=lambda f, v: (self.asked.append((f, v)), {
                "Bidder Name": "company.name", "Total Bid": "total",
                "Authorized Signature": "company.contact"})[1]),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def post(self, **over):
        body = {"key": "k", "device_id": "d", "url": self.URL,
                "company": {"name": "Test Concrete LLC", "contact": "Pat"},
                "bid": {"total": 1000}}
        body.update(over)
        return self.client.post("/bid-documents/fill", json=body)

    def test_the_form_comes_back_filled_and_listed(self):
        d = self.post().get_json()
        self.assertTrue(d["ok"], d)
        self.assertEqual({f["field"]: f["value"] for f in d["filled"]},
                         {"Bidder Name": "Test Concrete LLC", "Total Bid": "1,000.00"})
        from pypdf import PdfReader
        got = PdfReader(ls.io.BytesIO(ls.base64.b64decode(d["pdf_b64"]))).get_fields()
        self.assertEqual(got["Bidder Name"].get("/V"), "Test Concrete LLC")
        self.assertFalse(got["Authorized Signature"].get("/V"))   # left for the contractor

    def test_the_matcher_sees_field_names_and_value_names_not_a_free_hand(self):
        self.post()
        fields, vals = self.asked[0]
        self.assertIn(("Bidder Name", "Bidder Name"), fields)
        self.assertEqual(vals["company.name"], ("Company name", "Test Concrete LLC"))

    def test_a_form_with_no_fields_says_so(self):
        with patch.object(ls, "_fetch_document", return_value=(fillable_pdf([]), "application/pdf", "ok")):
            d = self.post().get_json()
        self.assertEqual((d["ok"], d["reason"]), (False, "not_fillable"))

    def test_an_unlicensed_caller_is_refused(self):
        with patch.object(ls, "_license_is_active", return_value=False):
            self.assertEqual(self.post().status_code, 403)

    def test_a_private_url_is_refused(self):
        self.assertEqual(self.post(url="http://10.0.0.1/f.pdf").status_code, 400)


if __name__ == "__main__":
    unittest.main()


class DocumentPickerTests(unittest.TestCase):
    """Which links on a posting count as the bid's documents."""

    import bid_sources as B

    def test_documents_keep_their_names(self):
        html = ('<a href="/DocumentCenter/View/88/Bid-Form">Bid Form &amp; Schedule</a>'
                '<a href="/DocumentCenter/View/89/Plans">Plans</a>')
        got = self.B.detail_documents(html, "https://city.gov/bids.aspx?bidID=1")
        self.assertEqual(got, [{"name": "Bid Form & Schedule", "url": "https://city.gov/DocumentCenter/View/88/Bid-Form"},
                               {"name": "Plans", "url": "https://city.gov/DocumentCenter/View/89/Plans"}])

    def test_a_site_wide_civic_form_is_not_a_bid_document(self):
        # Rogers County, OK links this from every page, bids included.
        html = ('<a href="/DocumentCenter/View/1256/Open-Records-Request-Form">Open Records Request</a>'
                '<a href="/DocumentCenter/View/12/Agenda-2026-10-01">Council agenda</a>'
                '<a href="/files/specs.pdf">Specifications</a>')
        got = self.B.detail_documents(html, "https://county.gov/bids.aspx?bidID=137")
        self.assertEqual([d["name"] for d in got], ["Specifications"])

    def test_a_link_with_no_useful_text_is_named_from_its_file(self):
        got = self.B.detail_documents('<a href="/docs/2026_Sidewalk_Bid_Tabulation_Sheet.pdf">click here</a>', "https://t.gov/")
        self.assertEqual(got[0]["name"], "2026 Sidewalk Bid Tabulation Sheet")

    def test_bid_packet_items_that_sound_civic_are_kept(self):
        html = ('<a href="/d/location-map.pdf">Project location map</a>'
                '<a href="/d/prevailing-wage.pdf">Prevailing wage ordinance</a>')
        self.assertEqual(len(self.B.detail_documents(html, "https://t.gov/")), 2)
