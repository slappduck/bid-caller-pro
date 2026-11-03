"""Which source brings visits, trials and customers -- for paid ads.

Counted as tallies per day per source, never tied to a person; who paid is
matched only to the customer's own answer to "How did you hear about us?"."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls
import kv_backend


class SourceTallyTests(unittest.TestCase):
    def setUp(self):
        self.store, self.db = {}, {}
        self.client = ls.app.test_client()
        self._p = [
            patch.object(kv_backend, "get", side_effect=lambda k, d=None: self.store.get(k, d)),
            patch.object(kv_backend, "set", side_effect=lambda k, v: self.store.__setitem__(k, v)),
            patch.object(ls, "_db", side_effect=lambda: self.db),
            patch.object(ls, "_save_db", side_effect=lambda d: None),
            patch.object(ls, "_verify_supabase_token", side_effect=lambda t: "pat@acme.example" if t else ""),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def trial(self, **body):
        return self.client.post("/trial", json={"device_id": "d1", "supabase_token": "tok", **body}).get_json()

    def test_visits_are_counted_by_source(self):
        for s in ("google-ad", "google-ad", "search", None):
            self.client.post("/visit", json={"source": s} if s else {})
        v = ls._source_summary()["visits"]
        self.assertEqual(v, {"google-ad": 2, "search": 1, "direct": 1})

    def test_a_made_up_label_is_not_counted(self):
        self.client.post("/visit", json={"source": "<script>alert(1)</script>"})
        self.assertEqual(ls._source_summary()["visits"], {})

    def test_a_new_trial_is_counted_by_the_link_that_brought_it(self):
        d = self.trial(source="google-ad")
        self.assertTrue(d["active"])
        self.assertEqual(ls._source_summary()["trials_by_link"], {"google-ad": 1})
        self.trial(source="google-ad")          # asking again doesn't count twice
        self.assertEqual(ls._source_summary()["trials_by_link"], {"google-ad": 1})

    def test_how_they_heard_is_kept_once_and_matched_to_payment(self):
        self.trial(source="direct")
        self.trial(heard="facebook")
        self.trial(heard="google-ad")            # the first answer stands
        rec = self.db["trials"]["email:pat@acme.example"]
        self.assertEqual(rec["heard"], "facebook")
        self.assertEqual(ls._source_summary()["trials_said"], {"facebook": 1})

    def test_an_answer_not_on_the_list_is_ignored(self):
        self.trial(heard="my-cousin-vinny")
        self.assertNotIn("heard", self.db["trials"]["email:pat@acme.example"])

    def test_nothing_identifies_anyone(self):
        self.trial(source="google-ad", heard="google-ad")
        self.client.post("/visit", json={"source": "google-ad"})
        self.assertNotIn("pat", repr(self.store.get(ls._SOURCE_KEY)))

    def test_the_weekly_note_has_a_sources_section(self):
        self.assertIn("Sources", [h for h, _d in ls._digest_sections()])

    def test_payment_is_counted_by_what_the_customer_said(self):
        import inspect
        self.assertIn('_tally_source("p"', inspect.getsource(ls.stripe_webhook) if hasattr(ls, "stripe_webhook")
                      else open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                             "license_server", "referrals.py")).read())


if __name__ == "__main__":
    unittest.main()
