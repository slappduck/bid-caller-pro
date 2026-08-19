"""Tests for _trial_identity, the free-trial anti-farming normalization.

The trial is cardless -- no card required -- and every scan spends real
OpenAI/search budget, so it needs to survive the cheapest abuse trick: Gmail,
Outlook and most other providers deliver josh+1@gmail.com and josh+2@gmail.com
to the same real inbox, so without normalizing away the +tag, one person could
claim unlimited free trials from a single mailbox.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


class TrialIdentityTests(unittest.TestCase):
    def test_plus_tags_collapse_to_the_same_identity(self):
        self.assertEqual(ls._trial_identity("josh+1@gmail.com"),
                         ls._trial_identity("josh+2@gmail.com"))
        self.assertEqual(ls._trial_identity("josh@gmail.com"),
                         ls._trial_identity("josh+trial@gmail.com"))

    def test_genuinely_different_mailboxes_stay_different(self):
        self.assertNotEqual(ls._trial_identity("josh@gmail.com"),
                            ls._trial_identity("jordan@gmail.com"))
        self.assertNotEqual(ls._trial_identity("josh@gmail.com"),
                            ls._trial_identity("josh@outlook.com"))

    def test_case_is_normalized(self):
        self.assertEqual(ls._trial_identity("Josh+Test@Gmail.com"),
                         ls._trial_identity("josh@gmail.com"))

    def test_no_at_sign_is_returned_unchanged_rather_than_crashing(self):
        self.assertEqual(ls._trial_identity("not-an-email"), "not-an-email")

    def test_empty_input_does_not_crash(self):
        self.assertEqual(ls._trial_identity(""), "")
        self.assertEqual(ls._trial_identity(None), "")

    def test_only_the_first_plus_tag_is_stripped(self):
        """A local part with a literal second + is unusual but must not
        produce a malformed identity."""
        self.assertEqual(ls._trial_identity("josh+a+b@gmail.com"),
                         "josh@gmail.com")


class TrialFarmingIsBlockedTests(unittest.TestCase):
    """End-to-end: two +tagged emails on the same real inbox must not each
    get their own trial window through _license_is_active."""

    def setUp(self):
        self.db = {"revoked": [], "trials": {}, "issued": {}, "emails": {}}
        self._patchers_db = ls._db
        ls._db = lambda: self.db
        self._orig_save = ls._save_db
        ls._save_db = lambda d: None
        self._orig_verify = ls._verify_supabase_token
        self.emails = iter(["josh+1@gmail.com", "josh+2@gmail.com"])
        ls._verify_supabase_token = lambda token: next(self.emails)

    def tearDown(self):
        ls._db = self._patchers_db
        ls._save_db = self._orig_save
        ls._verify_supabase_token = self._orig_verify

    def test_second_plus_tagged_signup_reuses_the_first_trial_window(self):
        self.assertTrue(ls._license_is_active("", "dev1", supabase_token="t1"))
        self.assertTrue(ls._license_is_active("", "dev2", supabase_token="t2"))
        # One trial record, not two -- the second signup found the first
        # rather than starting a fresh clock.
        self.assertEqual(len(self.db["trials"]), 1)


if __name__ == "__main__":
    unittest.main()
