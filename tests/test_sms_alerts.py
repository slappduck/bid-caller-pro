"""Text-message bid alerts: opt-in proven by a code, at most one a day,
STOP honoured from the reply itself, and gone with the account."""
import base64
import hashlib
import hmac
import inspect
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls  # noqa: E402


class SmsTests(unittest.TestCase):
    def setUp(self):
        self.kv, self.sent = {}, []
        self.p = [
            patch.object(ls.kv_backend, "get", side_effect=lambda k, d=None: self.kv.get(k, d)),
            patch.object(ls.kv_backend, "set", side_effect=lambda k, v: self.kv.__setitem__(k, v)),
            patch.object(ls, "TWILIO_ACCOUNT_SID", "AC1"), patch.object(ls, "TWILIO_AUTH_TOKEN", "tok"),
            patch.object(ls, "TWILIO_FROM", "+18885550100"),
            patch.object(ls, "_send_sms", side_effect=lambda to, text: self.sent.append((to, text)) or True),
            patch.object(ls, "_supabase_user", return_value={"id": "u1", "email": "a@x.com"}),
        ]
        for p in self.p:
            p.start()
        self.c = ls.app.test_client()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def post(self, path, **body):
        return self.c.post(path, json={"supabase_token": "t", **body})

    def code(self):
        return next(t for _to, t in self.sent if "code is" in t).split("code is ")[1][:6]

    def test_us_numbers_only(self):
        self.assertEqual(ls._sms_phone("(417) 555-0123"), "+14175550123")
        self.assertEqual(ls._sms_phone("1 417 555 0123"), "+14175550123")
        self.assertEqual(ls._sms_phone("555-0123"), "")
        self.assertEqual(ls._sms_phone("+44 20 7946 0958"), "")

    def test_nothing_is_texted_without_ticking_consent(self):
        self.assertEqual(self.post("/sms/start", phone="4175550123").get_json()["reason"], "no_consent")
        self.assertEqual(self.sent, [])

    def test_a_code_confirms_the_number_and_the_consent_is_recorded(self):
        d = self.post("/sms/start", phone="(417) 555-0123", consent=True).get_json()
        self.assertTrue(d["ok"])
        self.assertIn("STOP", self.sent[0][1])
        self.assertEqual(self.kv[ls._SMS_KEY]["u1"]["status"], "pending")
        self.assertNotIn(self.code(), str(self.kv), "the code is kept only as a hash")
        self.assertEqual(self.post("/sms/confirm", code="000000" if self.code() != "000000" else "111111")
                         .get_json()["reason"], "wrong_code")
        self.assertTrue(self.post("/sms/confirm", code=self.code()).get_json()["ok"])
        row = self.kv[ls._SMS_KEY]["u1"]
        self.assertEqual((row["status"], row["phone"]), ("on", "+14175550123"))
        self.assertEqual(row["consent_text"], ls.SMS_CONSENT_TEXT)
        self.assertTrue(row["consent_at"])

    def confirmed(self):
        self.post("/sms/start", phone="4175550123", consent=True)
        self.post("/sms/confirm", code=self.code())
        self.sent.clear()

    def test_at_most_one_alert_text_a_day(self):
        self.confirmed()
        self.assertTrue(ls._text_user("u1", "3 new bids"))
        self.assertFalse(ls._text_user("u1", "2 more"))
        self.assertEqual(len(self.sent), 1)

    def test_turning_off_in_the_app_stops_them(self):
        self.confirmed()
        self.post("/sms/stop")
        self.assertFalse(ls._text_user("u1", "x"))

    def sign(self, params):
        url = "https://localhost/sms/inbound"
        payload = url + "".join(k + params[k] for k in sorted(params))
        return base64.b64encode(hmac.new(b"tok", payload.encode(), hashlib.sha1).digest()).decode()

    def test_replying_stop_turns_texts_off_and_start_back_on(self):
        self.confirmed()
        for word, want in (("Stop", "off"), ("START", "on")):
            params = {"From": "+14175550123", "Body": word}
            r = self.c.post("/sms/inbound", data=params, headers={"X-Twilio-Signature": self.sign(params)},
                            base_url="https://localhost")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(self.kv[ls._SMS_KEY]["u1"]["status"], want)

    def test_help_is_answered_and_an_unsigned_post_is_refused(self):
        params = {"From": "+14175550123", "Body": "help"}
        r = self.c.post("/sms/inbound", data=params, headers={"X-Twilio-Signature": self.sign(params)},
                        base_url="https://localhost")
        self.assertIn("support@curbcallpro.com", r.get_data(as_text=True))
        self.assertEqual(self.c.post("/sms/inbound", data=params, base_url="https://localhost").status_code, 403)

    def test_not_set_up_means_the_app_hides_it(self):
        with patch.object(ls, "TWILIO_FROM", ""):
            self.assertFalse(self.post("/sms/status").get_json()["configured"])

    def test_deleting_the_account_deletes_the_number(self):
        self.confirmed()
        ls._forget_sms("u1")
        self.assertNotIn("u1", self.kv[ls._SMS_KEY])
        self.assertIn('_forget_sms(user["id"])', inspect.getsource(ls.account_delete))

    def test_the_daily_alerts_text_too(self):
        self.assertIn("_text_user(user_id", inspect.getsource(ls._run_saved_search_alerts))


if __name__ == "__main__":
    unittest.main()
