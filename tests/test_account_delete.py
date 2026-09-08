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
import datetime
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
    """The one record that must NOT be deleted, and what happens to it.

    terms_acceptances cascaded from auth.users, so deleting an account
    destroyed the acceptance row -- the evidence was thrown away at exactly
    the moment a dispute became likely.

    An earlier fix replaced the address with a salted hash. That was wrong
    twice over. The salt was the service-role key, so rotating a credential
    (routine, and mandatory after a leak) would have made every retained
    record permanently unverifiable while still looking intact. And a hash is
    weaker evidence than a plain address in the only situation the record
    exists for: an address is a screenshot, a hash is a scheme somebody has to
    explain and reproduce.

    So the address stays, and the retention is bounded instead: the row is
    stamped with a deletion date and a scheduled job removes it once claims
    are time-barred. "Kept until claims expire" is both better evidence and a
    better answer to a privacy question than "kept forever".
    """

    def setUp(self):
        self.db = {"revoked": [], "trials": {}, "issued": {}, "emails": {}}
        self.stamped = []
        self._orig_db, ls._db = ls._db, lambda: self.db
        self._orig_save, ls._save_db = ls._save_db, lambda d: None
        self._orig_user = ls._supabase_user
        self._orig_del = ls._supabase_delete_user
        self._orig_mark = ls._mark_terms_account_deleted
        self._orig_key = ls.SUPABASE_SERVICE_ROLE_KEY
        ls.SUPABASE_SERVICE_ROLE_KEY = "svc-key"
        ls._supabase_user = lambda tok: {"id": "u-1", "email": "josh@example.com"}
        ls._supabase_delete_user = lambda uid: True
        ls._mark_terms_account_deleted = lambda uid: self.stamped.append(uid) or True
        self.app = ls.app.test_client()

    def tearDown(self):
        ls._db = self._orig_db
        ls._save_db = self._orig_save
        ls._supabase_user = self._orig_user
        ls._supabase_delete_user = self._orig_del
        ls._mark_terms_account_deleted = self._orig_mark
        ls.SUPABASE_SERVICE_ROLE_KEY = self._orig_key

    def _delete(self):
        return self.app.post("/account/delete",
                             json={"supabase_token": "t", "device_id": "d1"})

    def test_the_record_is_stamped_not_removed(self):
        r = self._delete()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.stamped, ["u-1"])

    def test_it_happens_before_the_auth_user_goes(self):
        """Afterwards the row may already be unreachable."""
        order = []
        ls._mark_terms_account_deleted = lambda uid: order.append("stamp") or True
        ls._supabase_delete_user = lambda uid: order.append("delete") or True
        self._delete()
        self.assertEqual(order, ["stamp", "delete"])

    def test_a_failure_to_stamp_never_blocks_the_deletion(self):
        """They asked to be deleted. That has to happen regardless."""
        ls._mark_terms_account_deleted = lambda uid: False
        r = self._delete()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])


class RetentionPurgeTests(unittest.TestCase):
    """What the scheduled job may and may not delete."""

    def setUp(self):
        self.calls = []
        self._orig_open = ls.urllib.request.urlopen
        self._orig_url = ls.SUPABASE_URL
        self._orig_key = ls.SUPABASE_SERVICE_ROLE_KEY
        self._orig_cron = ls.CRON_SECRET
        ls.SUPABASE_URL = "https://project.supabase.co"
        ls.SUPABASE_SERVICE_ROLE_KEY = "svc-key"
        ls.CRON_SECRET = "cron-secret"
        outer = self

        class Resp:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def read(self_):
                return b"[]"

        def fake_open(req, timeout=None):
            outer.calls.append((req.get_method(), req.full_url))
            return Resp()
        ls.urllib.request.urlopen = fake_open
        self.app = ls.app.test_client()

    def tearDown(self):
        ls.urllib.request.urlopen = self._orig_open
        ls.SUPABASE_URL = self._orig_url
        ls.SUPABASE_SERVICE_ROLE_KEY = self._orig_key
        ls.CRON_SECRET = self._orig_cron

    def test_it_never_touches_a_live_customers_record(self):
        """No deletion date means the account still exists, and a live
        customer is still bound by what they accepted."""
        ls._purge_expired_terms_acceptances()
        method, url = self.calls[0]
        self.assertEqual(method, "DELETE")
        self.assertIn("account_deleted_at=not.is.null", url)

    def test_it_only_removes_records_past_the_period(self):
        ls._purge_expired_terms_acceptances()
        _, url = self.calls[0]
        self.assertIn("account_deleted_at=lt.", url)

    def test_the_cutoff_is_the_configured_number_of_years_ago(self):
        got = ls._purge_expired_terms_acceptances()
        cutoff = datetime.datetime.fromisoformat(got["cutoff"])
        years = (datetime.datetime.now(datetime.timezone.utc) - cutoff).days / 365.0
        self.assertAlmostEqual(years, ls.TERMS_RETENTION_YEARS, delta=0.1)

    def test_the_endpoint_refuses_without_the_cron_secret(self):
        r = self.app.post("/terms/purge", json={})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.calls, [], "it purged anyway")

    def test_the_endpoint_refuses_a_wrong_secret(self):
        r = self.app.post("/terms/purge", json={}, headers={"X-Cron-Secret": "nope"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.calls, [])

    def test_the_endpoint_runs_with_the_right_secret(self):
        r = self.app.post("/terms/purge", json={},
                          headers={"X-Cron-Secret": "cron-secret"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(len(self.calls), 1)

    def test_an_unset_cron_secret_does_not_open_the_door(self):
        """An empty configured secret must not match an empty supplied one."""
        ls.CRON_SECRET = ""
        r = self.app.post("/terms/purge", json={}, headers={"X-Cron-Secret": ""})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.calls, [])
