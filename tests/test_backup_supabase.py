"""Supabase's Free tier backs up nothing on its own -- no daily snapshot, no
point-in-time recovery. This is the only copy of customer data (accounts,
terms acceptances, saved bids/searches, reviews, feeds) that exists off
Supabase's own infrastructure, so the interesting behaviour is the same as
backup_local_data.py's: verification and partial-failure handling matter more
than the copying itself.

Every HTTP call is stubbed -- a `get(url, headers)` function is injected
everywhere the real code would call urllib, so nothing here reaches a real
Supabase project or the network.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import backup_supabase as B  # noqa: E402


def _pages(rows, page_size=B.PAGE_SIZE):
    """A fake `get` that paginates `rows` like PostgREST/Auth admin would."""
    def get(url, headers):
        if "/auth/v1/admin/users" in url:
            page = int(url.split("page=")[1].split("&")[0])
            start = (page - 1) * page_size
            chunk = rows[start:start + page_size]
            body = json.dumps({"users": chunk}).encode()
            return 200, body, {}
        # PostgREST: offset comes from the Range header "start-end".
        rng = headers["Range"]
        start = int(rng.split("-")[0])
        chunk = rows[start:start + page_size]
        return 200, json.dumps(chunk).encode(), {}
    return get


def _always(status, body=b"[]"):
    return lambda url, headers: (status, body, {})


class FetchTableTests(unittest.TestCase):
    def test_a_single_page_comes_back_whole(self):
        rows = [{"id": i} for i in range(5)]
        self.assertEqual(B.fetch_table("saved_bids", _pages(rows)), rows)

    def test_pagination_reassembles_every_row_in_order(self):
        rows = [{"id": i} for i in range(2500)]
        result = B.fetch_table("saved_bids", _pages(rows, page_size=1000))
        self.assertEqual(result, rows)

    def test_an_empty_table_is_an_empty_list_not_an_error(self):
        self.assertEqual(B.fetch_table("reviews", _pages([])), [])

    def test_a_bad_status_raises_rather_than_returning_partial_data(self):
        with self.assertRaises(IOError):
            B.fetch_table("saved_bids", _always(500))


class FetchUsersTests(unittest.TestCase):
    def test_paginates_the_account_list(self):
        users = [{"id": str(i), "email": f"u{i}@x.com"} for i in range(1500)]
        result = B.fetch_users(_pages(users, page_size=1000))
        self.assertEqual(result, users)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp()
        self.rows = {name: [{"id": 1, "val": name}] for name in B.TABLES}
        self.rows["auth_users"] = [{"id": "u1", "email": "a@b.com"}]

    def _get(self, url, headers):
        if "/auth/v1/admin/users" in url:
            page = int(url.split("page=")[1].split("&")[0])
            return (200, json.dumps({"users": self.rows["auth_users"] if page == 1 else []}).encode(), {})
        for name in B.TABLES:
            if f"/rest/v1/{name}?" in url:
                return 200, json.dumps(self.rows[name]).encode(), {}
        return 404, b"[]", {}

    def test_every_table_and_the_account_list_are_saved(self):
        saved, failed = B.backup(self.dest, today="2026-09-15", get=self._get)
        self.assertEqual(failed, [])
        names = {os.path.basename(p) for p, _ in saved}
        self.assertEqual(names, {f"2026-09-15__{n}.json" for n in B.TABLES + ["auth_users"]})

    def test_the_saved_content_matches_what_was_fetched(self):
        B.backup(self.dest, today="2026-09-15", get=self._get)
        with open(os.path.join(self.dest, "2026-09-15__reviews.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), self.rows["reviews"])

    def test_one_failing_table_does_not_abort_the_rest(self):
        def flaky(url, headers):
            if "/rest/v1/reviews" in url:
                return 500, b"", {}
            return self._get(url, headers)
        saved, failed = B.backup(self.dest, today="2026-09-15", get=flaky)
        self.assertEqual([n for n, _ in failed], ["reviews"])
        saved_names = {os.path.basename(p) for p, _ in saved}
        self.assertNotIn("2026-09-15__reviews.json", saved_names)
        self.assertIn("2026-09-15__company_profiles.json", saved_names)

    def test_a_copy_that_does_not_verify_is_not_left_behind(self):
        """A backup nobody checked is a backup nobody has -- same rule as
        backup_local_data.py. Corrupts the verification read-back (not the
        write itself) by monkeypatching json.load for exactly one call."""
        import json as jsonmod
        original_load = jsonmod.load
        state = {"n": 0}

        def flaky_load(f):
            state["n"] += 1
            if state["n"] == 2:  # the verification read-back for the 1st file
                return []
            return original_load(f)

        jsonmod.load = flaky_load
        try:
            saved, failed = B.backup(self.dest, today="2026-09-15", get=self._get)
        finally:
            jsonmod.load = original_load
        failed_names = [n for n, _ in failed]
        self.assertEqual(len(failed_names), 1)
        self.assertFalse(
            os.path.exists(os.path.join(self.dest, f"2026-09-15__{failed_names[0]}.json")))


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp()

    def touch(self, name):
        open(os.path.join(self.dest, name), "w").close()

    def test_old_copies_go(self):
        import datetime
        self.touch("2026-01-01__reviews.json")
        removed = B.prune(self.dest, 30, today=datetime.date(2026, 9, 15))
        self.assertEqual(len(removed), 1)

    def test_recent_copies_stay(self):
        import datetime
        self.touch("2026-09-14__reviews.json")
        B.prune(self.dest, 30, today=datetime.date(2026, 9, 15))
        self.assertEqual(len(os.listdir(self.dest)), 1)

    def test_unstamped_files_are_never_touched(self):
        self.touch("notes.txt")
        import datetime
        B.prune(self.dest, 0, today=datetime.date(2026, 9, 15))
        self.assertEqual(os.listdir(self.dest), ["notes.txt"])


class MainGuardTests(unittest.TestCase):
    def setUp(self):
        self._url = B.SUPABASE_URL
        self._key = B.SUPABASE_SERVICE_ROLE_KEY

    def tearDown(self):
        B.SUPABASE_URL = self._url
        B.SUPABASE_SERVICE_ROLE_KEY = self._key

    def run_main(self, argv):
        old_argv = sys.argv
        sys.argv = ["backup_supabase.py"] + argv
        try:
            return B.main()
        finally:
            sys.argv = old_argv

    def test_missing_credentials_stop_before_any_network_call(self):
        B.SUPABASE_URL = ""
        B.SUPABASE_SERVICE_ROLE_KEY = ""
        dest = tempfile.mkdtemp()
        rc = self.run_main([dest])
        self.assertEqual(rc, 2)
        self.assertEqual(os.listdir(dest), [])

    def test_list_on_an_existing_but_empty_destination_reports_none(self):
        B.SUPABASE_URL = "https://example.supabase.co"
        B.SUPABASE_SERVICE_ROLE_KEY = "x"
        dest = tempfile.mkdtemp()
        rc = self.run_main([dest, "--list"])
        self.assertEqual(rc, 0)

    def test_list_on_a_nonexistent_destination_is_an_error(self):
        B.SUPABASE_URL = "https://example.supabase.co"
        B.SUPABASE_SERVICE_ROLE_KEY = "x"
        rc = self.run_main([os.path.join(tempfile.mkdtemp(), "missing"), "--list"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
