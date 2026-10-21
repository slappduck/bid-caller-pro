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

    def test_the_meeting_place_comes_through_tidied_and_capped(self):
        got = ls._clean_doc_result({"prebid": "Pre-bid Oct 20 at City Hall",
                                    "prebid_place": "  City Hall,\n 200 E Main St ", "line_items": []})
        self.assertEqual(got["prebid_place"], "City Hall, 200 E Main St")
        self.assertLessEqual(len(ls._clean_doc_result({"prebid_place": "x" * 900})["prebid_place"]), 200)
        self.assertEqual(ls._clean_doc_result({})["prebid_place"], "")
        self.assertEqual(ls._clean_doc_result({"project_location": " Elm St  from 1st to 5th "})["project_location"],
                         "Elm St from 1st to 5th")

    def test_the_bid_contact_comes_through_checked(self):
        got = ls._clean_doc_result({"line_items": [], "contact": {
            "name": " Dana  Reyes ", "title": "Purchasing Agent", "organization": "City of Aurora",
            "phone": "(417) 678-5551 ext. 3", "email": "dreyes@aurora-mo.gov"}})
        self.assertEqual(got["contact"], {"name": "Dana Reyes", "title": "Purchasing Agent",
                                          "organization": "City of Aurora", "phone": "(417) 678-5551 ext. 3",
                                          "email": "dreyes@aurora-mo.gov"})
        bad = ls._clean_doc_result({"line_items": [], "contact": {"name": "", "phone": "ext 3", "email": "see website"}})
        self.assertEqual(bad["contact"], {})

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

    def test_a_document_read_by_an_older_reader_is_read_again(self):
        """A result cached before the reader returned the meeting place and
        job site would otherwise come back without them for 30 days."""
        self.post()
        for k, v in self.store.items():
            if k.startswith(ls._BID_DOC_CACHE_PREFIX):
                v.pop("v", None)
                v["result"].pop("project_location", None)
        d = self.post().get_json()
        self.assertFalse(d["cached"])
        self.assertEqual(len(self.fetches), 2)
        self.assertTrue(self.post().get_json()["cached"])

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


class _UploadSetup:
    def setUp(self):
        self.client = ls.app.test_client()
        self.store = {}
        self._p = [
            patch.object(ls, "_license_is_active", return_value=True),
            patch.object(ls, "OPENAI_API_KEY", "k"),
            patch.object(ls, "_ip_rate_ok", return_value=True),
            patch.object(kv_backend, "get", side_effect=lambda k, d=None: self.store.get(k, d)),
            patch.object(kv_backend, "set", side_effect=lambda k, v: self.store.__setitem__(k, v)),
            patch.object(ls, "_fetch_document", side_effect=AssertionError("an upload was fetched")),
            patch.object(ls, "_document_text", return_value=("Item 1 sidewalk 100 SY", 3, False)),
            patch.object(ls, "_ai_read_bid_document", return_value={
                "line_items": [{"item_no": "1", "description": "Sidewalk", "quantity": 100.0, "unit": "SY"}],
                "submission": "", "bid_security": "5% bid bond", "prebid": "", "required_forms": []}),
            patch.object(ls, "_ai_match_fields", return_value={"Bidder Name": "company.name"}),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def upload(self, path, raw, **over):
        body = {"key": "k", "device_id": "d"}
        body.update(over)
        return self.client.post(path, content_type="multipart/form-data", data={
            "json": ls.json.dumps(body), "file": (ls.io.BytesIO(raw), "plans.pdf")})

class UploadTests(_UploadSetup, unittest.TestCase):
    """A PDF the contractor got by registering as a plan holder, uploaded
    from the phone: read and filled like a linked one, never fetched."""

    def test_an_uploaded_pdf_is_read_without_fetching_anything(self):
        d = self.upload("/bid-documents/read", b"%PDF-1.4 plans").get_json()
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["bid_security"], "5% bid bond")

    def test_the_same_file_uploaded_again_comes_from_cache(self):
        self.upload("/bid-documents/read", b"%PDF-1.4 plans")
        with patch.object(ls, "_ai_read_bid_document", side_effect=AssertionError("read twice")):
            self.assertTrue(self.upload("/bid-documents/read", b"%PDF-1.4 plans").get_json()["cached"])

    def test_a_file_that_is_not_a_pdf_is_refused(self):
        r = self.upload("/bid-documents/read", b"<html>hi</html>")
        self.assertEqual((r.status_code, r.get_json()["reason"]), (400, "not_pdf"))

    def test_a_file_over_the_cap_is_refused(self):
        with patch.object(ls, "BID_DOC_MAX_BYTES", 10):
            r = self.upload("/bid-documents/read", b"%PDF-1.4 " + b"x" * 50)
        self.assertEqual((r.status_code, r.get_json()["reason"]), (413, "too_large"))

    def test_a_multipart_post_with_no_file_is_refused(self):
        r = self.client.post("/bid-documents/read", content_type="multipart/form-data",
                             data={"json": ls.json.dumps({"key": "k", "device_id": "d"})})
        self.assertEqual((r.status_code, r.get_json()["reason"]), (400, "no_file"))

    def test_an_unlicensed_upload_is_refused(self):
        with patch.object(ls, "_license_is_active", return_value=False):
            self.assertEqual(self.upload("/bid-documents/read", b"%PDF-1.4 plans").status_code, 403)

    def test_an_uploaded_form_is_filled(self):
        d = self.upload("/bid-documents/fill", fillable_pdf(["Bidder Name"]),
                        company={"name": "Test Concrete LLC"}, bid={"total": 1000}).get_json()
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["filled"][0]["value"], "Test Concrete LLC")


def text_pdf(lines, size=(612, 792)):
    """A one-page typed PDF (Helvetica): [(x, y, font size, text)]."""
    st = "".join(f"BT /F1 {sz} Tf {x} {y} Td ({t}) Tj ET\n" for x, y, sz, t in lines).encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Resources << /Font << /F1 5 0 R >> >> "
            b"/Contents 4 0 R >>" % size,
            b"<< /Length %d >>\nstream\n" % len(st) + st + b"endstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def scanned_pdf():
    """A PDF whose only page is a picture, the way a photocopied form comes."""
    from PIL import Image, ImageDraw
    img = Image.new("L", (850, 1100), 255)
    ImageDraw.Draw(img).text((80, 100), "BID FORM  Item 1  Sidewalk  100 SY", fill=0)
    buf = ls.io.BytesIO()
    img.save(buf, "PDF")
    return buf.getvalue()


FLAT_FORM = text_pdf([(72, 700, 12, "BID FORM"),
                      (72, 660, 11, "Name of Bidder: ______________________________"),
                      (72, 630, 11, "Address: _______________________  Phone: ______________"),
                      (72, 600, 11, "Total Base Bid $ ____________________"),
                      (72, 560, 11, "Signature: ______________________   Date: __________")])


def page_text(pdf, n=0):
    import pypdfium2 as pdfium
    return pdfium.PdfDocument(pdf)[n].get_textpage().get_text_range()


class MoreThanOneUploadTests(_UploadSetup, unittest.TestCase):
    """The plans, the bid form and an addendum, read together."""

    def test_several_files_are_read_together_with_their_names(self):
        seen = []
        with patch.object(ls, "_ai_read_bid_document",
                          side_effect=lambda t, im=None: (seen.append(t), ls._clean_doc_result({"line_items": []}))[1]):
            r = self.client.post("/bid-documents/read", content_type="multipart/form-data", data={
                "json": ls.json.dumps({"key": "k", "device_id": "d"}),
                "file": [(ls.io.BytesIO(b"%PDF-1.4 a"), "Bid form.pdf"), (ls.io.BytesIO(b"%PDF-1.4 b"), "Addendum 1.pdf")]})
        d = r.get_json()
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["files"], 2)
        self.assertIn("=== FILE: Bid form.pdf ===", seen[0])
        self.assertIn("=== FILE: Addendum 1.pdf ===", seen[0])

    def test_the_same_files_in_another_set_are_read_again(self):
        self.upload("/bid-documents/read", b"%PDF-1.4 plans")
        r = self.client.post("/bid-documents/read", content_type="multipart/form-data", data={
            "json": ls.json.dumps({"key": "k", "device_id": "d"}),
            "file": [(ls.io.BytesIO(b"%PDF-1.4 plans"), "a.pdf"), (ls.io.BytesIO(b"%PDF-1.4 more"), "b.pdf")]})
        self.assertFalse(r.get_json()["cached"])


class ScannedPageTests(unittest.TestCase):
    def test_only_picture_pages_are_drawn(self):
        self.assertEqual(ls._scan_page_images(FLAT_FORM), [])
        imgs = ls._scan_page_images(scanned_pdf())
        self.assertEqual(len(imgs), 1)
        self.assertEqual(imgs[0][:2], b"\xff\xd8")   # a JPEG

    def test_a_broken_pdf_draws_nothing_rather_than_raising(self):
        self.assertEqual(ls._scan_page_images(b"%PDF-1.4 not really"), [])

    def test_a_scanned_upload_is_read_from_its_pictures(self):
        client = ls.app.test_client()
        got = []
        with patch.object(ls, "_license_is_active", return_value=True), \
             patch.object(ls, "OPENAI_API_KEY", "k"), patch.object(ls, "_ip_rate_ok", return_value=True), \
             patch.object(kv_backend, "get", return_value=None), patch.object(kv_backend, "set"), \
             patch.object(ls, "_ai_read_bid_document", side_effect=lambda t, im=None: (
                 got.append((t, im)), ls._clean_doc_result({"line_items": [
                     {"item_no": "1", "description": "Sidewalk", "quantity": "100", "unit": "SY"}]}))[1]):
            d = client.post("/bid-documents/read", content_type="multipart/form-data", data={
                "json": ls.json.dumps({"key": "k", "device_id": "d"}),
                "file": (ls.io.BytesIO(scanned_pdf()), "scan.pdf")}).get_json()
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["scanned_pages"], 1)
        self.assertEqual(got[0][0].strip(), "")
        self.assertEqual(len(got[0][1]), 1)

    def test_the_model_is_shown_the_pages(self):
        sent = {}

        class Resp:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s): return ls.json.dumps({"choices": [{"message": {"content": '{"line_items": []}'}}]}).encode()

        def fake_open(req, timeout=None):
            sent.update(ls.json.loads(req.data))
            return Resp()
        with patch.object(ls, "OPENAI_API_KEY", "k"), patch.object(ls.urllib.request, "urlopen", fake_open):
            self.assertIsNotNone(ls._ai_read_bid_document("", [b"\xff\xd8jpeg"]))
        parts = sent["messages"][0]["content"]
        self.assertEqual(parts[1]["type"], "image_url")
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))


class FlatFormTests(unittest.TestCase):
    VALS = ls._fill_values({"company": {"name": "Test Concrete LLC", "address": "1 Main St", "contact": "Pat Lee"},
                            "bid": {"total": 75000, "date": "10/21/2026"}})

    def test_each_underlined_blank_is_found_with_its_label(self):
        blanks = ls._flat_blanks(FLAT_FORM)
        self.assertEqual([b["label"] for b in blanks],
                         ["Name of Bidder", "Address", "Phone", "Total Base Bid", "Signature", "Date"])
        self.assertEqual(blanks[1]["line"], "Address: [B2] Phone: [B3]")
        self.assertLess(blanks[1]["x1"], blanks[2]["x0"])

    def test_values_are_typed_onto_their_blanks_and_the_signature_is_left(self):
        matched = {"B1": "company.name", "B4": "total", "B5": "company.contact", "B6": "date"}
        with patch.object(ls, "_ai_match_fields", return_value=matched):
            out, status = ls._fill_flat(FLAT_FORM, self.VALS)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["flat"])
        self.assertEqual({f["field"]: f["value"] for f in out["filled"]},
                         {"B1": "Test Concrete LLC", "B4": "75,000.00", "B6": "10/21/2026"})
        text = page_text(ls.base64.b64decode(out["pdf_b64"]))
        self.assertIn("Test Concrete LLC", text)
        self.assertNotIn("Pat Lee", text)

    def test_the_matcher_cannot_place_a_value_on_a_blank_that_does_not_exist(self):
        with patch.object(ls, "_ai_match_fields", return_value={"B99": "company.name"}):
            out, _ = ls._fill_flat(FLAT_FORM, self.VALS)
        self.assertEqual(out["reason"], "nothing_matched")

    def test_a_scan_has_no_blanks_to_fill(self):
        out, _ = ls._fill_flat(scanned_pdf(), self.VALS)
        self.assertEqual((out["ok"], out["reason"]), (False, "not_fillable"))

    def test_the_fill_endpoint_falls_back_to_the_blanks(self):
        client = ls.app.test_client()
        with patch.object(ls, "_license_is_active", return_value=True), \
             patch.object(ls, "OPENAI_API_KEY", "k"), patch.object(ls, "_ip_rate_ok", return_value=True), \
             patch.object(ls, "_ai_match_fields", return_value={"B1": "company.name"}):
            d = client.post("/bid-documents/fill", content_type="multipart/form-data", data={
                "json": ls.json.dumps({"key": "k", "device_id": "d", "company": {"name": "Test Concrete LLC"}}),
                "file": (ls.io.BytesIO(FLAT_FORM), "form.pdf")}).get_json()
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["filled"][0]["label"], "Name of Bidder")

    def test_affidavit_values_are_split_from_the_city_line(self):
        vals = ls._fill_values({"company": {"city_state_zip": "Aurora, MO 65605"}, "bid": {"agency": "City of Aurora"}})
        self.assertEqual((vals["company.city"][1], vals["company.state"][1], vals["company.zip"][1]),
                         ("Aurora", "MO", "65605"))
        self.assertEqual(vals["bid.agency"][1], "City of Aurora")


class PacketTests(unittest.TestCase):
    BODY = {"key": "k", "device_id": "d",
            "bid": {"title": "Elm St Sidewalk", "agency": "City of Aurora", "number": "B-17",
                    "total": 75000, "submission": "Sealed envelope to the City Clerk", "addenda": "#1"},
            "company": {"name": "Test Concrete LLC"},
            "checklist": ["Sign the bid form", "Attach the 5% bid bond"],
            "lines": [{"item_no": "2", "description": "4 in. concrete sidewalk", "quantity": 1250,
                       "unit": "SY", "unit_price": 60, "amount": 75000}]}

    def setUp(self):
        self.client = ls.app.test_client()
        self._p = patch.object(ls, "_license_is_active", return_value=True)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def post(self, files):
        return self.client.post("/bid-documents/packet", content_type="multipart/form-data",
                                data={"json": ls.json.dumps(self.BODY), "file": files})

    def test_cover_summary_then_each_form_in_order(self):
        from pypdf import PdfReader
        form = ls._filled_pdf(PdfReader(ls.io.BytesIO(fillable_pdf(["Bidder Name"]))),
                              {"Bidder Name": "Test Concrete LLC"})
        d = self.post([(ls.io.BytesIO(form), "Bid form.pdf"), (ls.io.BytesIO(FLAT_FORM), "Affidavit.pdf")]).get_json()
        self.assertTrue(d["ok"], d)
        pdf = ls.base64.b64decode(d["pdf_b64"])
        self.assertEqual(d["pages"], 4)
        cover = page_text(pdf, 0)
        for want in ("Bid packet", "Elm St Sidewalk", "Sign the bid form", "Attach the 5% bid bond",
                     "Sealed envelope to the City Clerk", "3. Bid form.pdf", "4. Affidavit.pdf",
                     "Seventy-five thousand dollars"):
            self.assertIn(want, cover)
        self.assertIn("4 in. concrete sidewalk", page_text(pdf, 1))
        self.assertIn("75,000.00", page_text(pdf, 1))
        # The filled form's value is printed into the page, not a box that
        # could clash with another form's box of the same name.
        self.assertIn("Test Concrete LLC", page_text(pdf, 2))
        self.assertFalse(PdfReader(ls.io.BytesIO(pdf)).get_fields())
        self.assertIn("Name of Bidder", page_text(pdf, 3))

    def test_a_packet_with_no_forms_is_just_the_cover_and_summary(self):
        d = self.client.post("/bid-documents/packet", content_type="multipart/form-data",
                             data={"json": ls.json.dumps(self.BODY)}).get_json()
        self.assertEqual((d["ok"], d["pages"]), (True, 2))

    def test_only_pdfs_go_in(self):
        r = self.post([(ls.io.BytesIO(b"<html>"), "x.pdf")])
        self.assertEqual(r.get_json()["reason"], "not_pdf")

    def test_an_unlicensed_caller_is_refused(self):
        with patch.object(ls, "_license_is_active", return_value=False):
            self.assertEqual(self.post([]).status_code, 403)


class AddendumTests(_UploadSetup, unittest.TestCase):
    """An addendum read for what it changes: validated, and cached apart
    from the same file's ordinary reading."""
    RAW = {"addendum_number": "Addendum No. 2", "issued": "October 18, 2026",
           "bid_due": "November 6, 2026 at 2:00 PM", "prebid": "",
           "quantity_changes": [{"item_no": "2", "description": "4 in. concrete sidewalk",
                                 "old_quantity": "1,250", "new_quantity": "1,410", "unit": "SY"},
                                {"item_no": "9", "description": "No new number", "new_quantity": ""}, "junk"],
           "items_added": [{"item_no": "12", "description": "Detectable warning panels", "quantity": "8", "unit": "EA"}],
           "items_removed": [{"item_no": "7", "description": "Brick pavers"}],
           "answers": [{"question": "Is night work allowed?", "answer": "No."}, {"question": "Blank answer", "answer": ""}],
           "other_changes": ["Sheet C-3 replaced.", ""]}

    def test_the_changes_come_through_validated(self):
        got = ls._clean_addendum_result(self.RAW)
        self.assertEqual(got["addendum_number"], "2")
        self.assertEqual(got["bid_due"], "November 6, 2026 at 2:00 PM")
        self.assertEqual(got["quantity_changes"], [{"item_no": "2", "description": "4 in. concrete sidewalk",
                                                    "old_quantity": 1250.0, "new_quantity": 1410.0, "unit": "SY"}])
        self.assertEqual(got["items_added"][0]["quantity"], 8.0)
        self.assertEqual(got["items_removed"], [{"item_no": "7", "description": "Brick pavers"}])
        self.assertEqual(got["answers"], [{"question": "Is night work allowed?", "answer": "No."}])
        self.assertEqual(got["other_changes"], ["Sheet C-3 replaced."])
        self.assertIsNone(ls._clean_addendum_result("not an object"))

    def test_an_addendum_read_asks_for_changes_and_is_cached_apart(self):
        asked = []

        def ai(text, images=None, addendum=False):
            asked.append(addendum)
            return ls._clean_addendum_result(self.RAW) if addendum else ls._clean_doc_result({"line_items": []})
        with patch.object(ls, "_ai_read_bid_document", side_effect=ai):
            d = self.upload("/bid-documents/read", b"%PDF-1.4 addendum", kind="addendum").get_json()
            self.assertEqual(d["quantity_changes"][0]["new_quantity"], 1410.0)
            self.assertFalse(self.upload("/bid-documents/read", b"%PDF-1.4 addendum").get_json()["cached"])
            self.assertTrue(self.upload("/bid-documents/read", b"%PDF-1.4 addendum", kind="addendum").get_json()["cached"])
        self.assertEqual(asked, [True, False])



class AddendumPromptTests(unittest.TestCase):
    def test_the_model_is_given_the_addendum_instructions(self):
        sent = {}

        class Resp:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s): return ls.json.dumps({"choices": [{"message": {"content": '{"answers": []}'}}]}).encode()

        def fake_open(req, timeout=None):
            sent.update(ls.json.loads(req.data))
            return Resp()
        with patch.object(ls, "OPENAI_API_KEY", "k"), patch.object(ls.urllib.request, "urlopen", fake_open):
            got = ls._ai_read_bid_document("Addendum 1 text", addendum=True)
        self.assertIn("ADDENDUM", sent["messages"][0]["content"])
        self.assertEqual(got["quantity_changes"], [])

    def test_the_watch_hands_back_the_addendum_documents_with_their_links(self):
        html = (b'<a href="/files/Bid-Form.pdf">Bid Form</a><a href="/files/Addendum-1.pdf">Addendum No. 1</a>'
                b'<p>Addendum No. 1 issued.</p>')
        fp = ls._watch_fingerprint("https://93.184.216.34/bids/12", html, "text/html")
        self.assertEqual(fp["addenda"], [1])
        self.assertEqual(fp["addendum_docs"], [{"name": "Addendum No. 1", "url": "https://93.184.216.34/files/Addendum-1.pdf"}])


class BidWatchTests(unittest.TestCase):
    URL = "https://93.184.216.34/bids/elm-st"
    PAGE = (b"<html><body><h1>Elm St Sidewalk</h1><p>Addendum No. 1 issued 9/30. See Addendum #2.</p>"
            b'<a href="/docs/bid-form.pdf">Bid Form</a><a href="/docs/addendum-2.pdf">Addendum 2</a></body></html>')

    def setUp(self):
        self.client = ls.app.test_client()
        self.store, self.fetches = {}, []
        self._p = [
            patch.object(ls, "_license_is_active", return_value=True),
            patch.object(ls, "_ip_rate_ok", return_value=True),
            patch.object(kv_backend, "get", side_effect=lambda k, d=None: self.store.get(k, d)),
            patch.object(kv_backend, "set", side_effect=lambda k, v: self.store.__setitem__(k, v)),
            patch.object(ls, "_fetch_document",
                         side_effect=lambda u: (self.fetches.append(u), (self.PAGE, "text/html", "ok"))[1]),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def post(self, urls):
        return self.client.post("/bid-watch/check", json={"key": "k", "device_id": "d", "urls": urls})

    def test_documents_and_addendum_numbers_come_back(self):
        r = self.post([self.URL]).get_json()["results"][self.URL]
        self.assertTrue(r["ok"])
        self.assertEqual(r["addenda"], [1, 2])
        self.assertIn("Addendum 2", r["docs"])
        self.assertTrue(r["hash"])

    def test_a_posting_is_fetched_once_while_cached(self):
        self.post([self.URL])
        self.post([self.URL])
        self.assertEqual(len(self.fetches), 1)

    def test_private_addresses_are_never_fetched(self):
        r = self.post(["http://169.254.169.254/latest/"]).get_json()["results"]
        self.assertFalse(r["http://169.254.169.254/latest/"]["ok"])
        self.assertEqual(self.fetches, [])

    def test_a_batch_is_capped(self):
        urls = [f"https://93.184.216.34/b/{i}" for i in range(40)]
        self.assertEqual(len(self.post(urls).get_json()["results"]), ls.BID_WATCH_MAX_URLS)

    def test_an_unlicensed_caller_is_refused(self):
        with patch.object(ls, "_license_is_active", return_value=False):
            self.assertEqual(self.post([self.URL]).status_code, 403)


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

    def test_the_city_board_volunteer_form_is_not_a_bid_document(self):
        html = ('<a href="/DocumentCenter/View/88/Application-for-Boards">Application for Boards</a>'
                '<a href="/DocumentCenter/View/89/Boards-Commissions-Application">Boards &amp; Commissions</a>'
                '<a href="/DocumentCenter/View/90/Bid-Form">Bid Form</a>')
        got = self.B.detail_documents(html, "https://city.gov/bids.aspx?bidID=12")
        self.assertEqual([d["name"] for d in got], ["Bid Form"])

    def test_a_link_with_no_useful_text_is_named_from_its_file(self):
        got = self.B.detail_documents('<a href="/docs/2026_Sidewalk_Bid_Tabulation_Sheet.pdf">click here</a>', "https://t.gov/")
        self.assertEqual(got[0]["name"], "2026 Sidewalk Bid Tabulation Sheet")

    def test_bid_packet_items_that_sound_civic_are_kept(self):
        html = ('<a href="/d/location-map.pdf">Project location map</a>'
                '<a href="/d/prevailing-wage.pdf">Prevailing wage ordinance</a>')
        self.assertEqual(len(self.B.detail_documents(html, "https://t.gov/")), 2)


class PlanRoomTests(unittest.TestCase):
    """Bids whose documents the agency gives out only to registered plan
    holders. The files can't be fetched; the link to register can."""

    import bid_sources as B

    def test_a_civicplus_view_documents_link_is_the_plan_room(self):
        # Bensenville, IL: "View Document(s)" leads to "Please fill out this
        # form to view bid details. Plan Holders Form".
        html = ('<a href="/DocumentView.aspx?DID=1925">Board &amp; Committee Meeting Schedule</a>'
                '<a href="/Bids/PlanHolders/181?documentId=-1">View Document(s)</a>')
        self.assertEqual(self.B.detail_plan_room(html, "https://bensenville.gov/bids.aspx?bidID=181"),
                         "https://bensenville.gov/Bids/PlanHolders/181?documentId=-1")

    def test_a_plan_room_platform_counts_when_the_link_says_documents(self):
        html = '<a href="https://qcpi.questcdn.com/cdn/posting/?provider=123">Download the bid documents</a>'
        self.assertIn("questcdn.com", self.B.detail_plan_room(html, "https://town.gov/bids/7"))

    def test_a_platform_in_the_footer_for_vendor_sign_up_does_not(self):
        html = '<a href="https://vendor.bonfirehub.com/register">Vendor registration</a>'
        self.assertEqual(self.B.detail_plan_room(html, "https://town.gov/bids.aspx?bidID=1"), "")

    def test_a_posting_on_a_plan_room_platform_is_its_own_plan_room(self):
        url = "https://cityx.bonfirehub.com/opportunities/12345"
        self.assertEqual(self.B.detail_plan_room("", url), url)

    def test_ordinary_document_links_are_not_a_plan_room(self):
        html = '<a href="/DocumentCenter/View/88/Bid-Form">Bid Form</a>'
        self.assertEqual(self.B.detail_plan_room(html, "https://city.gov/bids.aspx?bidID=1"), "")
