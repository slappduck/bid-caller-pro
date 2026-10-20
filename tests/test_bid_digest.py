"""The free weekly bid email: a public form that makes this server send mail.

What's checked is everything that keeps it from being a way to email
strangers: the form only ever sends one confirmation, nothing weekly goes out
until the signed link is clicked, a suppressed address is never mailed, and
every weekly email carries the postal address and a working unsubscribe. Then
that the weekly run scans each area once, sends each person one email a week
however many times it's called, and drops bids that have already closed.
"""
import ast
import datetime
import glob
import os
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls
import kv_backend

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "curbcall_netlify_v4")
GEO = {"65802": {"lat": 37.2, "lon": -93.3, "city": "Springfield", "state": "MO"},
       "65201": {"lat": 38.95, "lon": -92.33, "city": "Columbia", "state": "MO"}}
SOON = (datetime.date.today() + datetime.timedelta(days=9)).strftime("%m/%d/%Y")
LATER = (datetime.date.today() + datetime.timedelta(days=20)).strftime("%m/%d/%Y")
PAST = (datetime.date.today() - datetime.timedelta(days=3)).strftime("%m/%d/%Y")


class DigestCase(unittest.TestCase):
    def setUp(self):
        self.store, self.sent, self.scans = {}, [], []
        self.client = ls.app.test_client()
        self.scan_result = {"bids": {"Springfield": [
            {"title": "Sidewalk replacement", "deadline": LATER, "url": "https://x.gov/a"},
            {"title": "ADA ramps", "deadline": SOON, "url": "https://x.gov/b"},
            {"title": "Old curb job", "deadline": PAST, "url": "https://x.gov/c"},
            {"title": "Awarded walk", "status": "Awarded", "url": "https://x.gov/d"}]}}
        self._p = [
            patch.object(kv_backend, "get", side_effect=lambda k, d=None: self.store.get(k, d)),
            patch.object(kv_backend, "set", side_effect=lambda k, v, **kw: self.store.__setitem__(k, v)),
            patch.object(ls, "_send_email", side_effect=self._send),
            patch.object(ls, "_geo_from_zip", side_effect=lambda z: GEO.get(z)),
            patch.object(ls, "_perform_scan", side_effect=self._scan),
            patch.object(ls, "RESEND_API_KEY", "re_test"),
            patch.object(ls, "MAILING_ADDRESS", "PO Box 1, Aurora, MO 65605"),
            patch.object(ls, "CRON_SECRET", "cron"),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def _send(self, to, subject, text, reply_to=None, headers=None):
        self.sent.append({"to": to, "subject": subject, "text": text, "headers": headers or {}})
        return True

    def _scan(self, location, radius, **kw):
        self.scans.append(location)
        return self.scan_result

    def subscribe(self, email="pat@example.com", zip_code="65802", **extra):
        return self.client.post("/digest/subscribe", json={"email": email, "zip": zip_code, **extra})

    def confirm_link(self):
        m = re.search(r"https?://\S+/digest/confirm\?\S+", self.sent[-1]["text"])
        self.assertIsNotNone(m, "no confirmation link in the email")
        return m.group(0).split(".com", 1)[1]

    def run_digest(self, **body):
        return self.client.post("/run-bid-digest", json=body, headers={"X-Cron-Secret": "cron"})


class SignupTests(DigestCase):
    def test_signing_up_sends_only_a_confirmation_and_subscribes_nobody(self):
        self.assertEqual(self.subscribe().status_code, 200)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Confirm", self.sent[0]["subject"])
        self.assertIn("Springfield, MO", self.sent[0]["text"])
        self.assertFalse(ls._digest_subs()["pat@example.com"].get("confirmed"))

    def test_the_confirmation_link_subscribes_them(self):
        self.subscribe()
        r = self.client.get(self.confirm_link())
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"You're in", r.data)
        rec = ls._digest_subs()["pat@example.com"]
        self.assertTrue(rec["confirmed"])
        self.assertEqual((rec["zip"], rec["place"]), ("65802", "Springfield, MO"))

    def test_a_link_edited_to_another_zip_or_address_is_refused(self):
        self.subscribe()
        link = self.confirm_link()
        for bad in (link.replace("65802", "65201"), link.replace("pat%40", "sam%40")):
            self.assertEqual(self.client.get(bad).status_code, 400)
        self.assertNotIn("sam@example.com", ls._digest_subs())

    def test_a_bot_filling_the_hidden_field_gets_the_normal_answer_and_nothing_sends(self):
        self.assertEqual(self.subscribe(website="http://spam").status_code, 200)
        self.assertEqual(self.sent, [])
        self.assertEqual(ls._digest_subs(), {})

    def test_bad_input_is_refused(self):
        for email, z in (("not-an-email", "65802"), ("pat@example.com", "6580"),
                         ("pat@example.com", "abcde"), ("", "65802")):
            self.assertEqual(self.subscribe(email, z).status_code, 400, (email, z))
        self.assertEqual(self.sent, [])

    def test_an_unknown_zip_is_refused(self):
        self.assertEqual(self.subscribe(zip_code="00000").status_code, 400)
        self.assertEqual(self.sent, [])

    def test_a_suppressed_address_is_never_mailed_and_the_answer_does_not_say_so(self):
        ls._suppress("pat@example.com")
        r = self.subscribe()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.sent, [])

    def test_repeated_signups_send_one_confirmation(self):
        for _ in range(3):
            self.subscribe()
        self.assertEqual(len(self.sent), 1)

    def test_nothing_sends_without_a_postal_address(self):
        with patch.object(ls, "MAILING_ADDRESS", ""):
            self.assertEqual(self.subscribe().status_code, 503)
        self.assertEqual(self.sent, [])

    def test_the_per_ip_cap_holds(self):
        with patch.object(ls, "DIGEST_SIGNUPS_PER_IP_PER_DAY", 2):
            codes = [self.subscribe(f"p{i}@example.com").status_code for i in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_a_plain_form_post_gets_a_page_not_json(self):
        r = self.client.post("/digest/subscribe", data={"email": "pat@example.com", "zip": "65802"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["Content-Type"])
        self.assertIn(b"Check your email", r.data)

    def test_confirming_a_suppressed_address_does_not_subscribe_it(self):
        self.subscribe()
        link = self.confirm_link()
        ls._suppress("pat@example.com")
        self.assertEqual(self.client.get(link).status_code, 409)
        self.assertFalse(ls._digest_subs()["pat@example.com"].get("confirmed"))


class WeeklyRunTests(DigestCase):
    def add(self, email, zip_code="65802"):
        self.subscribe(email, zip_code)
        self.client.get(self.confirm_link())
        self.sent.clear()

    def test_it_needs_the_cron_secret(self):
        self.assertEqual(self.client.post("/run-bid-digest", json={}).status_code, 403)

    def test_only_confirmed_subscribers_get_open_bids_soonest_first(self):
        self.add("pat@example.com")
        self.subscribe("unconfirmed@example.com")
        self.sent.clear()
        d = self.run_digest().get_json()
        self.assertEqual((d["sent"], d["remaining"]), (1, 0))
        self.assertEqual([m["to"] for m in self.sent], ["pat@example.com"])
        text = self.sent[0]["text"]
        self.assertLess(text.index("ADA ramps"), text.index("Sidewalk replacement"))
        self.assertNotIn("Old curb job", text)
        self.assertNotIn("Awarded walk", text)
        self.assertIn("2 open concrete bids near Springfield, MO", self.sent[0]["subject"])

    def test_every_email_has_the_address_and_a_working_unsubscribe(self):
        self.add("pat@example.com")
        self.run_digest()
        msg = self.sent[0]
        self.assertIn("PO Box 1, Aurora, MO 65605", msg["text"])
        unsub = re.search(r"Unsubscribe: (\S+)", msg["text"]).group(1)
        self.assertIn(unsub, msg["headers"]["List-Unsubscribe"])
        self.assertEqual(msg["headers"]["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        # The mail client's one-click button POSTs to the same URL.
        path = unsub.split(".com", 1)[1]
        self.assertEqual(self.client.post(path, data="List-Unsubscribe=One-Click").status_code, 200)
        self.assertIn("pat@example.com", ls._suppression())

    def test_an_unsubscribed_address_is_dropped_and_not_mailed(self):
        self.add("pat@example.com")
        self.add("sam@example.com")
        ls._suppress("sam@example.com")
        self.run_digest()
        self.assertEqual([m["to"] for m in self.sent], ["pat@example.com"])
        self.assertNotIn("sam@example.com", ls._digest_subs())

    def test_each_area_is_scanned_once(self):
        self.add("pat@example.com")
        self.add("sam@example.com")
        self.add("lee@example.com", "65201")
        self.run_digest()
        self.assertEqual(sorted(self.scans), ["65201", "65802"])
        self.assertEqual(len(self.sent), 3)

    def test_batches_finish_the_week_without_sending_anyone_twice(self):
        self.add("pat@example.com")
        self.add("lee@example.com", "65201")
        with patch.object(ls, "_cron_beat") as beat:
            first = self.run_digest(max_areas=1).get_json()
            self.assertEqual(first["remaining"], 1)
            beat.assert_not_called()
            second = self.run_digest(max_areas=1).get_json()
            self.assertEqual(second["remaining"], 0)
            beat.assert_called_once_with("bid-digest")
            self.run_digest()
        self.assertEqual(sorted(m["to"] for m in self.sent), ["lee@example.com", "pat@example.com"])

    def test_no_open_bids_means_no_email(self):
        self.add("pat@example.com")
        self.scan_result = {"bids": {}}
        d = self.run_digest().get_json()
        self.assertEqual((d["sent"], d["no_bids"]), (0, 1))
        self.assertEqual(self.sent, [])

    def test_it_refuses_to_run_without_a_postal_address(self):
        self.add("pat@example.com")
        with patch.object(ls, "MAILING_ADDRESS", ""):
            self.assertEqual(self.run_digest().status_code, 500)
        self.assertEqual(self.sent, [])


class SignupFormTests(unittest.TestCase):
    def forms(self, path):
        with open(path, encoding="utf-8") as f:
            html = f.read()
        return re.findall(r'<form class="su" method="post" action="([^"]+)">(.*?)</form>', html, re.S)

    def test_the_price_pages_and_the_landing_page_have_the_form(self):
        pages = [os.path.join(SITE, "index.html")] + glob.glob(os.path.join(SITE, "concrete-cost", "*.html"))
        for page in pages:
            got = self.forms(page)
            self.assertEqual(len(got), 1, page)
            action, inner = got[0]
            self.assertEqual(action, "https://bid-caller-pro.onrender.com/digest/subscribe")
            for field in ('name="email"', 'name="zip"', 'name="website"'):
                self.assertIn(field, inner, page)

    def test_the_privacy_policy_covers_it(self):
        with open(os.path.join(SITE, "privacy.html"), encoding="utf-8") as f:
            self.assertIn("weekly bid email", f.read())


class OneNamespaceTests(unittest.TestCase):
    def test_no_two_package_files_define_the_same_function(self):
        """Every file in license_server/ runs in one shared namespace, so a
        second def of the same name silently replaces the first. digest.py's
        email builder was first written as _digest_text, which would have
        replaced the weekly admin digest's function of that name."""
        owners = {}
        dupes = []
        for path in sorted(glob.glob(os.path.join(ROOT, "license_server", "*.py"))):
            if path.endswith("__init__.py"):
                continue
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    if node.name in owners:
                        dupes.append(f"{node.name}: {owners[node.name]} and {os.path.basename(path)}")
                    owners[node.name] = os.path.basename(path)
        self.assertEqual(dupes, [])


if __name__ == "__main__":
    unittest.main()
