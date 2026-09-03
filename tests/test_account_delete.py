"""Deleting an account has to actually remove the person.

The endpoint's promise is in its own docstring: the trial record goes too,
and the trade is accepted deliberately -- a deleted account frees a fresh
trial on that email, because keeping a row about someone who asked to be
forgotten is worse than losing seven days of trial protection.

That promise was not kept for a plus-tagged address. The trial is FILED under
the plus-stripped identity (josh+test@gmail.com -> josh@gmail.com, so one
inbox cannot farm unlimited trials) but deletion popped the key by the raw
address. The strings match for an ordinary email and differ for a tagged one,
so a tagged account deleted itself and left its trial row in place -- with the
full tagged address still stored inside the row.

These tests pin both halves: the row goes, and it goes for tagged addresses
too.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


class DeleteRemovesTheTrialRowTests(unittest.TestCase):
    def setUp(self):
        self.db = {"revoked": [], "trials": {}, "issued": {}, "emails": {}}
        self._orig_db, ls._db = ls._db, lambda: self.db
        self._orig_save, ls._save_db = ls._save_db, lambda d: None
        self._orig_user = ls._supabase_user
        self._orig_del = ls._supabase_delete_user
        self._orig_key = ls.SUPABASE_SERVICE_ROLE_KEY
        ls._supabase_delete_user = lambda uid: True
        ls.SUPABASE_SERVICE_ROLE_KEY = "svc-key"
        self.app = ls.app.test_client()

    def tearDown(self):
        ls._db = self._orig_db
        ls._save_db = self._orig_save
        ls._supabase_user = self._orig_user
        ls._supabase_delete_user = self._orig_del
        ls.SUPABASE_SERVICE_ROLE_KEY = self._orig_key

    def _delete_as(self, email):
        ls._supabase_user = lambda tok: {"id": "u-1", "email": email}
        return self.app.post("/account/delete",
                             json={"supabase_token": "t", "device_id": "dev1"})

    def _start_trial_for(self, email):
        """File a trial exactly the way _license_is_active does."""
        key = f"email:{ls._trial_identity(email)}"
        self.db["trials"][key] = {"started": "2026-09-01T00:00:00", "email": email}
        return key

    def test_plain_address_trial_row_is_removed(self):
        self._start_trial_for("josh@gmail.com")
        r = self._delete_as("josh@gmail.com")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.db["trials"], {})

    def test_plus_tagged_address_trial_row_is_removed(self):
        """The regression. Filed under josh@, deletion popped josh+test@."""
        self._start_trial_for("josh+test@gmail.com")
        r = self._delete_as("josh+test@gmail.com")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.db["trials"], {})

    def test_the_deleted_address_is_not_left_inside_a_surviving_row(self):
        """The row stores the full address, so a surviving row keeps it."""
        self._start_trial_for("josh+walkthrough@gmail.com")
        self._delete_as("josh+walkthrough@gmail.com")
        blob = repr(self.db)
        self.assertNotIn("josh+walkthrough@gmail.com", blob)
        self.assertNotIn("josh@gmail.com", blob)

    def test_a_row_filed_under_the_raw_address_is_also_cleared(self):
        """Rows written before the fix are keyed the old way."""
        self.db["trials"]["email:josh+old@gmail.com"] = {
            "started": "2026-09-01T00:00:00", "email": "josh+old@gmail.com"}
        self._delete_as("josh+old@gmail.com")
        self.assertEqual(self.db["trials"], {})

    def test_another_persons_trial_is_untouched(self):
        self._start_trial_for("josh@gmail.com")
        keep = self._start_trial_for("jordan@gmail.com")
        self._delete_as("josh@gmail.com")
        self.assertIn(keep, self.db["trials"])
        self.assertEqual(len(self.db["trials"]), 1)

    def test_the_device_trial_goes_too(self):
        self.db["trials"]["dev1"] = {"started": "2026-09-01T00:00:00"}
        self._start_trial_for("josh@gmail.com")
        self._delete_as("josh@gmail.com")
        self.assertEqual(self.db["trials"], {})

    def test_a_signed_out_caller_is_refused(self):
        ls._supabase_user = lambda tok: None
        r = self.app.post("/account/delete", json={"supabase_token": ""})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["reason"], "not_signed_in")


if __name__ == "__main__":
    unittest.main()
