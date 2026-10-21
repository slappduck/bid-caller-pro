"""Documents emailed to the app: only for the address's owner, only PDFs
and document links, kept 30 days, and nothing at all without the secret."""
import os
import sys
import time
import unittest
from email.message import EmailMessage
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls
import kv_backend

PDF = b"%PDF-1.4 the bid form"


def message(pdfs=(("Bid Form.pdf", PDF),), body="Your documents for Elm St Sidewalk, Bid B-17.", html=None):
    m = EmailMessage()
    m["Subject"] = "Plan holder documents: Elm St Sidewalk"
    m["From"] = "Plan Room <noreply@planroom.example>"
    m["To"] = "docs+abcdef1234@curbcallpro.com"
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    for name, data in pdfs:
        m.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return m.as_bytes()


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.store, self.files = {}, {}
        self.client = ls.app.test_client()

        def storage(path, method="GET", data=None, ctype=None):
            if method == "POST" and path.startswith("object/"):
                self.files[path[len("object/inbox/"):]] = data
                return 200, b"{}"
            if method == "GET" and path.startswith("object/inbox/"):
                got = self.files.get(path[len("object/inbox/"):])
                return (200, got) if got else (404, b"")
            if method == "DELETE":
                for p in ls.json.loads(data)["prefixes"]:
                    self.files.pop(p, None)
                return 200, b"[]"
            return 404, b""
        self._p = [
            patch.object(ls, "INBOUND_EMAIL_SECRET", "s3cret"),
            patch.object(ls, "SUPABASE_URL", "https://sb.example"),
            patch.object(ls, "SUPABASE_SERVICE_ROLE_KEY", "service"),
            patch.object(ls, "_license_is_active", return_value=True),
            patch.object(ls, "_supabase_user", return_value={"id": "u1"}),
            patch.object(ls, "_storage_request", side_effect=storage),
            patch.object(kv_backend, "get", side_effect=lambda k, d=None: self.store.get(k, d)),
            patch.object(kv_backend, "set", side_effect=lambda k, v: self.store.__setitem__(k, v)),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def app_post(self, path, **body):
        return self.client.post(path, json={"key": "k", "device_id": "d", "supabase_token": "t", **body})

    def address(self):
        return self.app_post("/inbox/address").get_json()["address"]

    def deliver(self, raw, to=None, secret="s3cret"):
        return self.client.post("/inbound/email", data=raw, content_type="message/rfc822",
                                headers={"X-Inbound-Secret": secret, "X-Envelope-To": to or self.address()})

    def test_each_user_gets_one_private_address(self):
        a = self.address()
        self.assertRegex(a, r"^docs\+[0-9a-f]{10}@curbcallpro\.com$")
        self.assertEqual(self.address(), a)

    def test_an_emailed_pdf_shows_up_and_downloads(self):
        d = self.deliver(message()).get_json()
        self.assertEqual(d["kept"], 1)
        items = self.app_post("/inbox/list").get_json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["subject"], "Plan holder documents: Elm St Sidewalk")
        self.assertEqual(items[0]["files"], [{"n": 0, "name": "Bid Form.pdf", "size": len(PDF)}])
        self.assertIn("Bid B-17", items[0]["snippet"])
        r = self.app_post("/inbox/file", id=items[0]["id"], n=0)
        self.assertEqual((r.status_code, r.data, r.mimetype), (200, PDF, "application/pdf"))

    def test_download_links_in_the_email_are_listed_but_not_footer_links(self):
        html = ('<p>Download: <a href="https://planroom.example/download/123">here</a></p>'
                '<p><a href="https://planroom.example/unsubscribe?x=1">Unsubscribe</a> '
                '<a href="https://facebook.com/planroom">Facebook</a></p>')
        d = self.deliver(message(pdfs=(), html=html,
                                 body="Get the plans at https://planroom.example/download/123 . "
                                      "https://planroom.example/unsubscribe?x=1")).get_json()
        self.assertEqual(d["links"], 1)
        item = self.app_post("/inbox/list").get_json()["items"][0]
        self.assertEqual(item["links"], ["https://planroom.example/download/123"])

    def test_an_email_with_nothing_useful_is_not_kept(self):
        self.address()
        d = self.deliver(message(pdfs=(("notes.pdf", b"not a pdf at all"),), body="Thanks for registering!")).get_json()
        self.assertEqual(d["kept"], 0)
        self.assertEqual(self.app_post("/inbox/list").get_json()["items"], [])

    def test_mail_for_an_unknown_address_is_bounced(self):
        self.address()
        r = self.deliver(message(), to="docs+0000000000@curbcallpro.com")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.files, {})

    def test_the_secret_is_required(self):
        a = self.address()
        self.assertEqual(self.deliver(message(), to=a, secret="wrong").status_code, 403)
        with patch.object(ls, "INBOUND_EMAIL_SECRET", ""):
            self.assertEqual(self.deliver(message(), to=a, secret="").status_code, 503)
        self.assertEqual(self.files, {})

    def test_one_user_cannot_read_another_users_documents(self):
        self.deliver(message())
        item = self.app_post("/inbox/list").get_json()["items"][0]
        with patch.object(ls, "_supabase_user", return_value={"id": "u2"}):
            self.assertEqual(self.app_post("/inbox/list").get_json()["items"], [])
            self.assertEqual(self.app_post("/inbox/file", id=item["id"], n=0).status_code, 404)

    def test_documents_go_after_thirty_days(self):
        self.deliver(message())
        rec = self.store[ls._INBOX_ITEMS_PREFIX + "u1"]
        rec["items"][0]["at"] = time.time() - 31 * 86400
        self.assertEqual(self.app_post("/inbox/list").get_json()["items"], [])
        self.assertEqual(self.files, {})

    def test_deleting_removes_the_files(self):
        self.deliver(message())
        item = self.app_post("/inbox/list").get_json()["items"][0]
        self.assertEqual(self.app_post("/inbox/delete", id=item["id"]).get_json()["deleted"], 1)
        self.assertEqual(self.files, {})

    def test_the_app_is_told_when_it_is_not_set_up(self):
        with patch.object(ls, "INBOUND_EMAIL_SECRET", ""):
            self.assertEqual(self.app_post("/inbox/address").get_json()["reason"], "not_configured")

    def test_unlicensed_or_signed_out_callers_are_refused(self):
        with patch.object(ls, "_license_is_active", return_value=False):
            self.assertEqual(self.app_post("/inbox/list").status_code, 403)
        with patch.object(ls, "_supabase_user", return_value=None):
            self.assertEqual(self.app_post("/inbox/list").status_code, 401)


if __name__ == "__main__":
    unittest.main()
