"""Phone alerts with the app closed: Web Push done right, and only where wanted.

The encryption is checked against RFC 8291's own test vector, the VAPID
token is verified with the public key the app is given, and the watch job
is run end to end with the network and database stubbed out.
"""
import datetime
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls
import kv_backend


class EncryptionTests(unittest.TestCase):
    def test_rfc8291_test_vector(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        as_key = ec.derive_private_key(
            int.from_bytes(ls._unb64u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
        body = ls._encrypt_push(
            b"When I grow up, I want to be a watermelon",
            "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
            "BTBZMqHH6r4Tts7J_aSIgg", _as_key=as_key, _salt=ls._unb64u("DGv6ra1nlYgDCS1FRnbzlw"))
        self.assertEqual(ls._b64u(body),
                         "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6Tlz"
                         "AC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


class VapidTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self._p = [patch.object(kv_backend, "get", side_effect=lambda k, d=None: self.store.get(k, d)),
                   patch.object(kv_backend, "set", side_effect=lambda k, v: self.store.__setitem__(k, v)),
                   patch.object(ls, "VAPID_PRIVATE_KEY", "")]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_a_key_is_made_once_and_kept(self):
        a = ls._public_point(ls._vapid_private())
        b = ls._public_point(ls._vapid_private())
        self.assertEqual(a, b)
        self.assertEqual(len(a), 65)

    def test_the_token_verifies_with_the_published_key(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        header = ls._vapid_header("https://fcm.googleapis.com/fcm/send/abc")
        jwt = header.split("t=")[1].split(",")[0]
        key = header.split("k=")[1]
        head, claims, sig = jwt.split(".")
        self.assertEqual(json.loads(ls._unb64u(claims))["aud"], "https://fcm.googleapis.com")
        pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ls._unb64u(key))
        raw = ls._unb64u(sig)
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        pub.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))   # raises if wrong

    def test_the_public_key_endpoint(self):
        d = ls.app.test_client().get("/push/vapid-public-key").get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(len(ls._unb64u(d["key"])), 65)


class EndpointGuardTests(unittest.TestCase):
    def test_only_push_services_over_https(self):
        for ok in ("https://fcm.googleapis.com/fcm/send/x", "https://web.push.apple.com/abc",
                   "https://updates.push.services.mozilla.com/wpush/v2/x"):
            self.assertTrue(ls._push_endpoint_ok(ok), ok)
        for bad in ("http://fcm.googleapis.com/x", "https://169.254.169.254/latest", "https://evil.example/x",
                    "https://fcm.googleapis.com.evil.example/x", "https://fcm.googleapis.com:8443/x", ""):
            self.assertFalse(ls._push_endpoint_ok(bad), bad)

    def test_a_gone_subscription_is_reported_gone(self):
        sub = {"endpoint": "https://fcm.googleapis.com/fcm/send/x",
               "p256dh": "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
               "auth": "BTBZMqHH6r4Tts7J_aSIgg"}
        err = ls.urllib.error.HTTPError(sub["endpoint"], 410, "Gone", {}, None)
        with patch.object(ls.urllib.request, "urlopen", side_effect=err), \
             patch.object(ls, "_vapid_header", return_value="vapid t=x, k=y"):
            self.assertEqual(ls._send_push(sub, {"title": "t"}), "gone")

    def test_an_endpoint_elsewhere_is_never_contacted(self):
        with patch.object(ls.urllib.request, "urlopen", side_effect=AssertionError("contacted")):
            self.assertEqual(ls._send_push({"endpoint": "https://10.0.0.1/x", "p256dh": "a", "auth": "b"}, {}), "gone")


class BidWatchJobTests(unittest.TestCase):
    def setUp(self):
        self.store, self.sent, self.page = {}, [], b"<html><a href=\"/f.pdf\">Bid Form</a></html>"
        tomorrow = (ls._today_central() + datetime.timedelta(days=1)).isoformat()
        self.rows = [
            {"user_id": "u1", "bid_id": "b1", "title": "Elm St Sidewalk", "deadline": tomorrow,
             "url": "https://93.184.216.34/elm", "pipeline": ""},
            {"user_id": "u1", "bid_id": "b2", "title": "Done Job", "deadline": tomorrow,
             "url": "https://93.184.216.34/done", "pipeline": "won"},
        ]

        def admin(path, method="GET", data=None):
            if path.startswith("/rest/v1/saved_bids"):
                return self.rows
            if path.startswith("/rest/v1/push_subscriptions"):
                return [] if method == "DELETE" else [{"user_id": "u1", "endpoint": "https://fcm.googleapis.com/x",
                                                       "p256dh": "k", "auth": "a"}]
            return None
        self._p = [
            patch.object(ls, "SUPABASE_URL", "https://sb.example"),
            patch.object(ls, "SUPABASE_SERVICE_ROLE_KEY", "service"),
            patch.object(ls, "_supabase_admin_request", side_effect=admin),
            patch.object(kv_backend, "get", side_effect=lambda k, d=None: self.store.get(k, d)),
            patch.object(kv_backend, "set", side_effect=lambda k, v: self.store.__setitem__(k, v)),
            patch.object(ls, "_public_http_url", return_value=True),
            patch.object(ls, "_fetch_document", side_effect=lambda u: (self.page, "text/html", "ok")),
            patch.object(ls, "_send_push", side_effect=lambda sub, msg: (self.sent.append(msg["title"]), "ok")[1]),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_due_tomorrow_is_sent_once_and_a_new_addendum_is_sent(self):
        out = ls._run_bid_watch()
        self.assertTrue(out["ok"])
        self.assertEqual(self.sent, ["Due tomorrow: Elm St Sidewalk"])   # baseline only, no change yet
        self.sent.clear()
        self.page = (b"<html><a href=\"/f.pdf\">Bid Form</a><a href=\"/a2.pdf\">Addendum 2</a>"
                     b"<p>Addendum No. 2</p></html>")
        ls._run_bid_watch()
        self.assertEqual(self.sent, ["Elm St Sidewalk: Addendum 2 posted"])   # reminder not repeated

    def test_closed_out_bids_are_left_alone(self):
        ls._run_bid_watch()
        self.assertFalse(any("Done Job" in t for t in self.sent))

    def test_the_endpoint_needs_the_cron_secret(self):
        with patch.object(ls, "CRON_SECRET", "s"):
            self.assertEqual(ls.app.test_client().post("/run-bid-watch", json={}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
