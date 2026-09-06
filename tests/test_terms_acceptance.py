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
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
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


class DuplicateWriteTests(unittest.TestCase):
    """One signup wrote two rows, and the fix is in three places.

    supabase-js fires onAuthStateChange more than once for a single sign-in.
    flushTermsAcceptance() is async -- it reads the pending flag, then awaits a
    token and a round trip, and only clears the flag once the server confirms
    -- so both calls saw the flag set and both posted.

    The browser now guards against re-entering while a flush is in flight, the
    table has a unique index, and the server treats the resulting rejection as
    success. The last part matters most: without it the browser would never
    clear its flag and would retry a permanently rejected write at every single
    sign-in.
    """

    def setUp(self):
        self._orig_open = ls.urllib.request.urlopen
        self._orig_url = ls.SUPABASE_URL
        self._orig_key = ls.SUPABASE_SERVICE_ROLE_KEY
        ls.SUPABASE_URL = "https://project.supabase.co"
        ls.SUPABASE_SERVICE_ROLE_KEY = "svc-key"

    def tearDown(self):
        ls.urllib.request.urlopen = self._orig_open
        ls.SUPABASE_URL = self._orig_url
        ls.SUPABASE_SERVICE_ROLE_KEY = self._orig_key

    def _raise(self, code, body):
        def fake_open(req, timeout=None):
            raise ls.urllib.error.HTTPError(
                "u", code, "conflict", {},
                io.BytesIO(json.dumps(body).encode("utf-8")))
        ls.urllib.request.urlopen = fake_open

    def test_a_row_that_already_exists_counts_as_recorded(self):
        self._raise(409, {"code": "23505", "message": "duplicate key"})
        self.assertTrue(
            ls._record_terms_acceptance({"id": "u-1", "email": "a@b.com"},
                                        "signup_form"))

    def test_a_foreign_key_conflict_is_still_a_failure(self):
        """409 alone is not proof; a missing user must not look like consent."""
        self._raise(409, {"code": "23503", "message": "violates foreign key"})
        self.assertFalse(
            ls._record_terms_acceptance({"id": "ghost", "email": "a@b.com"},
                                        "signup_form"))

    def test_an_unreadable_conflict_body_is_not_assumed_to_be_a_duplicate(self):
        def fake_open(req, timeout=None):
            raise ls.urllib.error.HTTPError("u", 409, "conflict", {},
                                            io.BytesIO(b"<html>nope"))
        ls.urllib.request.urlopen = fake_open
        self.assertFalse(
            ls._record_terms_acceptance({"id": "u-1", "email": "a@b.com"},
                                        "signup_form"))

    def test_other_http_errors_are_failures(self):
        self._raise(500, {"message": "boom"})
        self.assertFalse(
            ls._record_terms_acceptance({"id": "u-1", "email": "a@b.com"},
                                        "signup_form"))


class BrowserFlushGuardTests(unittest.TestCase):
    """The browser half, run rather than grepped for.

    A test that only checks the guard variable appears in the file would pass
    on a guard that is set and never read.
    """

    def test_two_overlapping_flushes_post_once(self):
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node not available")
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               os.pardir, "curbcall_netlify_v4", "app.html"),
                  encoding="utf-8") as f:
            html = f.read()
        i = html.index("let termsFlushInFlight=false;")
        j = html.index("\n}", html.index("finally{termsFlushInFlight=false;}")) + 2
        fn = html[i:j]
        harness = """
const TERMS_METHOD_KEY="pending_terms_method";
const SERVER="https://server.test";
const _d={pending_terms_method:"signup_form"};
const localStorage={getItem:k=>k in _d?_d[k]:null,
  setItem:(k,v)=>{_d[k]=String(v);}, removeItem:k=>{delete _d[k];}};
let posts=0;
const getSupabaseToken=async()=>"tok";
const fetch=async()=>{ posts++;
  await new Promise(r=>setTimeout(r,20));   // the await that opened the race
  return {ok:true}; };
%s
Promise.all([flushTermsAcceptance(), flushTermsAcceptance()])
  .then(()=>console.log(JSON.stringify(
    {posts, flagLeft:_d.pending_terms_method ?? null})));
""" % fn
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "flush.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(harness)
            out = subprocess.run([node, path], capture_output=True,
                                 text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-1500:])
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(got["posts"], 1,
                         "one signup posted %s consent rows" % got["posts"])
        self.assertIsNone(got["flagLeft"], "pending flag was not cleared")


class SchemaGuardsTheTableTests(unittest.TestCase):
    """The index is the part that holds when the client is wrong again."""

    def setUp(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               os.pardir, "supabase_sync_schema.sql"),
                  encoding="utf-8") as f:
            self.sql = "\n".join(l for l in f.read().splitlines()
                                 if not l.strip().startswith("--"))

    def test_one_acceptance_per_account_per_version(self):
        self.assertRegex(
            self.sql,
            r"create unique index[^;]*terms_acceptances\s*\("
            r"\s*user_id\s*,\s*terms_version\s*,\s*privacy_version\s*\)")

    def test_existing_duplicates_are_cleared_before_the_index(self):
        """Creating the index on a table that already has duplicates fails."""
        self.assertLess(self.sql.index("delete from terms_acceptances"),
                        self.sql.index("terms_acceptances_once"))

    def test_the_earliest_row_is_the_one_kept(self):
        """The moment they agreed, not the moment a retry happened."""
        self.assertIn("a.id > b.id", self.sql)


if __name__ == "__main__":
    unittest.main()


class DiagCountsTests(unittest.TestCase):
    """Counts on /diag, so a failed recording is visible without running SQL.

    Two things this must not become: a public leak, and a way to read the
    rows. The rows carry email addresses and the question being asked is only
    "did it record?", so the answer is numbers behind the diag token -- the
    same reasoning that moved go_clicks off /health after it nearly published
    which named contractors opened a cold email.
    """

    def setUp(self):
        self._orig_open = ls.urllib.request.urlopen
        self._orig_url = ls.SUPABASE_URL
        self._orig_key = ls.SUPABASE_SERVICE_ROLE_KEY
        ls.SUPABASE_URL = "https://project.supabase.co"
        ls.SUPABASE_SERVICE_ROLE_KEY = "svc-key"
        self.asked = []

        outer = self

        class Resp:
            def __init__(self, total):
                self.headers = {"Content-Range": "0-0/%d" % total}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(req, timeout=None):
            outer.asked.append(req.full_url)
            filtered = "terms_version=eq." in req.full_url
            return Resp(2 if filtered else 5)
        ls.urllib.request.urlopen = fake_open

    def tearDown(self):
        ls.urllib.request.urlopen = self._orig_open
        ls.SUPABASE_URL = self._orig_url
        ls.SUPABASE_SERVICE_ROLE_KEY = self._orig_key

    def test_it_reports_a_total_and_a_current_count(self):
        got = ls._terms_acceptance_stats()
        self.assertEqual(got["total"], 5)
        self.assertEqual(got["current"], 2)

    def test_it_says_which_versions_it_is_stamping(self):
        """A gap between total and current is only readable alongside these."""
        got = ls._terms_acceptance_stats()
        self.assertEqual(got["stamping"],
                         {"terms": ls.TERMS_VERSION,
                          "privacy": ls.PRIVACY_VERSION})

    def test_it_asks_for_a_count_not_the_rows(self):
        ls._terms_acceptance_stats()
        self.assertTrue(self.asked)
        for url in self.asked:
            self.assertIn("limit=1", url)
            self.assertIn("select=id", url)
            self.assertNotIn("email", url)

    def test_an_unconfigured_project_says_so_rather_than_erroring(self):
        ls.SUPABASE_SERVICE_ROLE_KEY = ""
        self.assertEqual(ls._terms_acceptance_stats(), {"configured": False})

    def test_a_failure_reports_the_type_and_not_the_detail(self):
        """The URL carries the project ref and messages echo request detail."""
        def boom(req, timeout=None):
            raise OSError("connect to https://project.supabase.co failed")
        ls.urllib.request.urlopen = boom
        got = ls._terms_acceptance_stats()
        self.assertEqual(got, {"configured": True, "error": "OSError"})

    def test_it_never_returns_a_row(self):
        blob = json.dumps(ls._terms_acceptance_stats())
        self.assertNotIn("@", blob)
        self.assertNotIn("user_id", blob)


class DiagCountsAreNotPublicTests(unittest.TestCase):
    """/health is unauthenticated. This belongs behind the token."""

    @staticmethod
    def _body(name):
        """One function, stopping at the next top-level def.

        Slicing to the next @app.route ran straight past health() into the
        helper below it and reported a leak that was not there.
        """
        src = ls_source()
        start = src.index("def %s(" % name)
        end = src.find("\ndef ", start + 1)
        return src[start:end if end != -1 else len(src)]

    def test_health_does_not_carry_acceptance_counts(self):
        self.assertNotIn("_terms_acceptance_stats", self._body("health"))

    def test_diag_does(self):
        self.assertIn("_terms_acceptance_stats", self._body("diag"))


def ls_source():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           os.pardir, "license_server.py"), encoding="utf-8") as f:
        return re.sub(r"^\s*#.*$", "", f.read(), flags=re.M)
