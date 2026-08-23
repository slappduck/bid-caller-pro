"""/support is unauthenticated by necessity and mails our own inbox.

Somebody whose sign-in is broken still has to be able to say so, so the
endpoint cannot require a session. That makes it a public path to
SUPPORT_EMAIL and to our Resend quota: uncapped, one script can spend the
month's send allowance overnight and bury every real message under it,
including the license keys and bid alerts that go out through the same
provider.

None of this stops a determined attacker. It stops the failure that
actually happens to a small public form.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kv_backend
import license_server as ls


class SupportTests(unittest.TestCase):
    def setUp(self):
        self.client = ls.app.test_client()
        self.store = {}
        self.sent = []
        self._p = [
            patch.object(ls, "RESEND_API_KEY", "resend-key"),
            patch.object(kv_backend, "get",
                         side_effect=lambda k, d=None: self.store.get(k, d)),
            patch.object(kv_backend, "set",
                         side_effect=lambda k, v: self.store.__setitem__(k, v)),
            patch.object(ls, "_send_email",
                         side_effect=lambda to, subj, text, **kw:
                             (self.sent.append({"to": to, "subject": subj,
                                                "text": text, **kw}), True)[1]),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def _post(self, ip="203.0.113.9", **body):
        payload = {"email": "a@x.com", "message": "help please"}
        payload.update(body)
        return self.client.post("/support", json=payload,
                                headers={"X-Forwarded-For": ip})

    def test_an_ordinary_message_goes_through(self):
        self.assertTrue(self._post().get_json()["ok"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["reply_to"], "a@x.com")

    def test_one_address_cannot_flood_the_inbox(self):
        for _ in range(ls.SUPPORT_MAX_PER_IP_PER_DAY):
            self._post()
        r = self._post()
        self.assertEqual(r.status_code, 429)
        self.assertEqual(len(self.sent), ls.SUPPORT_MAX_PER_IP_PER_DAY)

    def test_the_cap_is_per_address_not_global(self):
        """A shared office IP being noisy must not mute everyone else."""
        for _ in range(ls.SUPPORT_MAX_PER_IP_PER_DAY):
            self._post(ip="203.0.113.9")
        self.assertTrue(self._post(ip="198.51.100.4").get_json()["ok"])

    def test_a_huge_payload_is_truncated_not_relayed(self):
        self._post(message="A" * 500000)
        self.assertLessEqual(len(self.sent[0]["text"]), ls.SUPPORT_MAX_CHARS + 200)

    def test_a_junk_contact_never_becomes_a_reply_to_header(self):
        """The value is caller-supplied and lands in a mail header."""
        for bad in ("not-an-email", "a@x.com\\nBcc: victim@y.com",
                    "<script>alert(1)</script>", "a@x.com, b@y.com"):
            self.sent.clear()
            self._post(email=bad, ip="198.51.100.%d" % (len(bad) % 200 + 1))
            self.assertIsNone(self.sent[0]["reply_to"], bad)

    def test_an_unverified_contact_is_still_passed_along_in_the_body(self):
        """Dropping it silently would lose the only way to answer them."""
        self._post(email="weird address")
        self.assertIn("weird address", self.sent[0]["text"])

    def test_an_empty_message_sends_nothing(self):
        self.assertFalse(self._post(message="   ").get_json()["ok"])
        self.assertEqual(self.sent, [])

    def test_it_always_mails_our_own_address_never_the_callers(self):
        """The one property that keeps this from being an open relay."""
        self._post(email="victim@elsewhere.com", message="hi")
        self.assertEqual(self.sent[0]["to"], ls.SUPPORT_EMAIL)


if __name__ == "__main__":
    unittest.main()
