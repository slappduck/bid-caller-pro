"""A consent record is only worth what it is hard to fake.

The signup checkbox blocked a button in the browser and did nothing else --
no request, no row. The Terms carry the disclaimer of warranties, the
liability cap and the choice of Missouri law, and each of those binds only
somebody who accepted them, so with no record there was nothing to show.

Three properties make the record evidence rather than decoration, and each
one is a way this could have been built wrong:

  * The server decides WHO accepted. The endpoint takes a Supabase token and
    resolves it; it never takes a user id from the caller. A record anyone
    can write on anyone's behalf proves nothing.

  * The VERSION is stored, not just the fact. "They agreed to the Terms" is
    weak the moment the Terms change.

  * A failed write is reported as a failure. The browser only forgets its
    pending flag on a 200, so a Supabase outage means the record is written
    at the next sign-in instead of being lost silently -- silence would be
    worse than having no feature at all, because it looks like evidence
    exists.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


class TermsAcceptEndpointTests(unittest.TestCase):
    def setUp(self):
        self.posted = []
        self._orig_user = ls._supabase_user
        self._orig_record = ls._record_terms_acceptance
        self._orig_key = ls.SUPABASE_SERVICE_ROLE_KEY
        ls.SUPABASE_SERVICE_ROLE_KEY = "svc-key"
        ls._supabase_user = lambda tok: (
            {"id": "u-1", "email": "josh@example.com"} if tok == "good" else None)

        def record(user, method):
            self.posted.append((user, method))
            return True
        ls._record_terms_acceptance = record
        self.app = ls.app.test_client()

    def tearDown(self):
        ls._supabase_user = self._orig_user
        ls._record_terms_acceptance = self._orig_record
        ls.SUPABASE_SERVICE_ROLE_KEY = self._orig_key

    def test_a_valid_session_is_recorded(self):
        r = self.app.post("/terms/accept",
                          json={"supabase_token": "good", "method": "signup_form"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(len(self.posted), 1)
        user, method = self.posted[0]
        self.assertEqual(user["id"], "u-1")
        self.assertEqual(method, "signup_form")

    def test_the_response_names_the_versions_that_were_stored(self):
        r = self.app.post("/terms/accept", json={"supabase_token": "good"})
        body = r.get_json()
        self.assertEqual(body["terms_version"], ls.TERMS_VERSION)
        self.assertEqual(body["privacy_version"], ls.PRIVACY_VERSION)

    def test_a_bad_token_records_nothing(self):
        r = self.app.post("/terms/accept",
                          json={"supabase_token": "forged", "method": "google"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.posted, [])

    def test_a_caller_supplied_user_id_is_ignored(self):
        """Nobody gets to sign on somebody else's behalf."""
        r = self.app.post("/terms/accept",
                          json={"supabase_token": "good", "user_id": "victim",
                                "email": "victim@example.com"})
        self.assertEqual(r.status_code, 200)
        user, _ = self.posted[0]
        self.assertEqual(user["id"], "u-1")
        self.assertEqual(user["email"], "josh@example.com")

    def test_a_write_that_did_not_land_is_not_reported_as_success(self):
        ls._record_terms_acceptance = lambda user, method: False
        r = self.app.post("/terms/accept", json={"supabase_token": "good"})
        self.assertEqual(r.status_code, 502)
        self.assertFalse(r.get_json()["ok"])

    def test_no_service_key_is_a_visible_failure_not_a_silent_ok(self):
        ls.SUPABASE_SERVICE_ROLE_KEY = ""
        r = self.app.post("/terms/accept", json={"supabase_token": "good"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.posted, [])


class RecordedRowTests(unittest.TestCase):
    """What actually goes into terms_acceptances."""

    def setUp(self):
        self.sent = []
        self._orig_open = ls.urllib.request.urlopen
        self._orig_url = ls.SUPABASE_URL
        self._orig_key = ls.SUPABASE_SERVICE_ROLE_KEY
        ls.SUPABASE_URL = "https://project.supabase.co"
        ls.SUPABASE_SERVICE_ROLE_KEY = "svc-key"

        class Resp:
            status = 201

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        def fake_open(req, timeout=None):
            self.sent.append(req)
            return Resp()
        ls.urllib.request.urlopen = fake_open

    def tearDown(self):
        ls.urllib.request.urlopen = self._orig_open
        ls.SUPABASE_URL = self._orig_url
        ls.SUPABASE_SERVICE_ROLE_KEY = self._orig_key

    def _row_for(self, user, method):
        ok = ls._record_terms_acceptance(user, method)
        if not self.sent:
            return ok, None
        return ok, json.loads(self.sent[-1].data.decode("utf-8"))

    def test_the_row_carries_the_user_and_both_versions(self):
        ok, row = self._row_for({"id": "u-9", "email": "Josh@Example.com"},
                                "signup_form")
        self.assertTrue(ok)
        self.assertEqual(row["user_id"], "u-9")
        self.assertEqual(row["terms_version"], ls.TERMS_VERSION)
        self.assertEqual(row["privacy_version"], ls.PRIVACY_VERSION)

    def test_the_email_is_normalised_the_way_the_rest_of_the_app_stores_it(self):
        _, row = self._row_for({"id": "u-9", "email": "  Josh@Example.com "},
                               "google")
        self.assertEqual(row["email"], "josh@example.com")

    def test_an_unrecognised_method_is_dropped_rather_than_stored(self):
        """The column is a fixed vocabulary; free text from a caller is not it."""
        _, row = self._row_for({"id": "u-9", "email": "a@b.com"},
                               "<script>whatever</script>")
        self.assertEqual(row["method"], "")

    def test_every_method_the_app_can_send_is_accepted(self):
        import re
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               os.pardir, "curbcall_netlify_v4", "app.html"),
                  encoding="utf-8") as f:
            html = f.read()
        used = set(re.findall(r'captureTermsAcceptance\("([a-z_]+)"\)', html))
        self.assertTrue(used, "app.html no longer records any signup route")
        self.assertEqual(used - ls._ACCEPT_METHODS, set(),
                         "app.html sends a method the server silently drops")

    def test_a_user_with_no_id_is_not_written(self):
        ok, _ = self._row_for({"email": "a@b.com"}, "signup_form")
        self.assertFalse(ok)
        self.assertEqual(self.sent, [])

    def test_a_supabase_failure_is_reported_not_swallowed(self):
        def boom(req, timeout=None):
            raise OSError("supabase down")
        ls.urllib.request.urlopen = boom
        self.assertFalse(
            ls._record_terms_acceptance({"id": "u-9", "email": "a@b.com"},
                                        "signup_form"))


if __name__ == "__main__":
    unittest.main()
