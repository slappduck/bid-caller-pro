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


class ConsentSurvivesDeletionTests(unittest.TestCase):
    """The one record that must NOT be deleted, and what goes from it.

    terms_acceptances cascaded from auth.users, so deleting an account
    destroyed the acceptance row. The disclaimer of warranties, the liability
    cap and the choice of Missouri law bind only somebody who accepted them,
    and a dispute is likeliest with somebody who has already left -- so the
    evidence was being thrown away at precisely the moment it started to
    matter.

    Keeping it is a trade, not a free win: /account/delete is supposed to
    forget people. So the row is minimised rather than kept whole. The proof
    stays -- which account, which versions, what time -- and the address is
    replaced with a salted one-way hash. A later claim can still be checked by
    hashing the address the claimant provides; a stolen copy of the table does
    not yield a roster of former customers.
    """

    def setUp(self):
        self.db = {"revoked": [], "trials": {}, "issued": {}, "emails": {}}
        self.patched = []
        self._orig_db, ls._db = ls._db, lambda: self.db
        self._orig_save, ls._save_db = ls._save_db, lambda d: None
        self._orig_user = ls._supabase_user
        self._orig_del = ls._supabase_delete_user
        self._orig_forget = ls._forget_terms_email
        self._orig_key = ls.SUPABASE_SERVICE_ROLE_KEY
        ls.SUPABASE_SERVICE_ROLE_KEY = "svc-key"
        ls._supabase_user = lambda tok: {"id": "u-1", "email": "josh@example.com"}
        ls._supabase_delete_user = lambda uid: True

        def forget(uid, email):
            self.patched.append((uid, email))
            return True
        ls._forget_terms_email = forget
        self.app = ls.app.test_client()

    def tearDown(self):
        ls._db = self._orig_db
        ls._save_db = self._orig_save
        ls._supabase_user = self._orig_user
        ls._supabase_delete_user = self._orig_del
        ls._forget_terms_email = self._orig_forget
        ls.SUPABASE_SERVICE_ROLE_KEY = self._orig_key

    def _delete(self):
        return self.app.post("/account/delete",
                             json={"supabase_token": "t", "device_id": "d1"})

    def test_the_address_is_minimised_on_the_way_out(self):
        r = self._delete()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.patched, [("u-1", "josh@example.com")])

    def test_it_happens_before_the_auth_user_goes(self):
        """Afterwards the row may already be unreachable."""
        order = []
        ls._forget_terms_email = lambda uid, e: order.append("minimise") or True
        ls._supabase_delete_user = lambda uid: order.append("delete") or True
        self._delete()
        self.assertEqual(order, ["minimise", "delete"])

    def test_a_failure_to_minimise_never_blocks_the_deletion(self):
        """They asked to be deleted. That has to happen regardless."""
        ls._forget_terms_email = lambda uid, e: False
        r = self._delete()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])


class ForgottenEmailHashTests(unittest.TestCase):
    def setUp(self):
        self._orig_key = ls.SUPABASE_SERVICE_ROLE_KEY
        ls.SUPABASE_SERVICE_ROLE_KEY = "svc-key-value-for-salting-purposes"

    def tearDown(self):
        ls.SUPABASE_SERVICE_ROLE_KEY = self._orig_key

    def test_the_address_does_not_appear_in_the_result(self):
        out = ls._terms_email_hash("josh@example.com")
        self.assertNotIn("josh", out)
        self.assertNotIn("example.com", out)

    def test_the_same_address_always_gives_the_same_value(self):
        """Otherwise a later claim could never be checked against it."""
        self.assertEqual(ls._terms_email_hash("josh@example.com"),
                         ls._terms_email_hash("josh@example.com"))

    def test_different_addresses_differ(self):
        self.assertNotEqual(ls._terms_email_hash("a@example.com"),
                            ls._terms_email_hash("b@example.com"))

    def test_it_is_salted_so_the_table_alone_cannot_be_reversed(self):
        """Unsalted, an attacker hashes a candidate list and learns who was
        a customer -- most of what deleting the address was meant to stop."""
        first = ls._terms_email_hash("josh@example.com")
        ls.SUPABASE_SERVICE_ROLE_KEY = "a-completely-different-service-key"
        self.assertNotEqual(first, ls._terms_email_hash("josh@example.com"))

    def test_it_is_marked_as_a_hash_rather_than_looking_like_an_address(self):
        self.assertTrue(ls._terms_email_hash("a@b.com").startswith("sha256:"))
