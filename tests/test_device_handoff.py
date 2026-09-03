"""One browser, two accounts: the second must not inherit the first's data.

Signing out ends the Supabase session and clears nothing else. Every feed,
starred bid, note, pipeline entry, company profile and saved search stays in
localStorage under a key with no user in it. So creating a second account on
the same browser opened a Bids tab already full of the previous person's work.

The visible part was the smaller half. syncPullFeeds() finds no server row for
a new account and treats whatever the browser holds as the feed to seed it
with -- so the previous user's bids were UPLOADED into the new account and
became its permanent server-side copy. On a shared office computer that hands
one contractor's pipeline to another.

The guard records which account owns this device's cached data and wipes on a
mismatch. These tests run the real functions out of app.html under node with a
fake localStorage, rather than asserting that some string appears in the file.

Two things the guard must NOT do, both of which broke it while being written:
  * delete Supabase's own "sb-*" session token, which signs the user out
    during the sign-in that triggered the wipe;
  * delete pending_signup_name, which the signup form wrote seconds earlier
    and applyPendingSignupName() is about to consume -- that is the new user's
    own name, not the old user's data.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, os.pardir, "curbcall_netlify_v4", "app.html")


def _extract(src, start_marker, end_marker):
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


class DeviceHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node not available")
        with open(APP, encoding="utf-8") as f:
            cls.src = f.read()

    def _run(self, initial, email):
        """Run claimDeviceFor(email) over a fake localStorage; return the result."""
        store_js = _extract(self.src, "const store = {", "function deviceId(){")
        harness = """
const _data = %s;
let reloaded = false;
const localStorage = {
  get length(){ return Object.keys(_data).length; },
  key(i){ return Object.keys(_data)[i] ?? null; },
  getItem(k){ return k in _data ? _data[k] : null; },
  setItem(k,v){ _data[k] = String(v); },
  removeItem(k){ delete _data[k]; },
  clear(){ for (const k of Object.keys(_data)) delete _data[k]; },
};
const location = { reload(){ reloaded = true; } };
let storageWarned = false;
function toast(){}
%s
const stopped = claimDeviceFor(%s);
console.log(JSON.stringify({ data: _data, reloaded, stopped }));
""" % (json.dumps(initial), store_js, json.dumps(email))
        out = subprocess.run([shutil.which("node"), "-e", harness],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            self.fail("node failed: " + out.stderr[-2000:])
        return json.loads(out.stdout.strip().splitlines()[-1])

    # A browser mid-session for the first contractor.
    def _occupied(self):
        return {
            "last_feed": json.dumps({"bid1": {"title": "Sidewalk repair"}}),
            "upcoming_feed": json.dumps({"bid2": {}}),
            "leads_feed": json.dumps({"lead1": {}}),
            "lead_status": json.dumps({"bid1": "called"}),
            "feeds_updated_at": json.dumps("2026-09-01T00:00:00Z"),
            "saved": json.dumps(["bid1"]),
            "notes": json.dumps({"bid1": "call Tuesday"}),
            "pipeline": json.dumps({"bid1": "quoted"}),
            "company_profile": json.dumps({"contact": "First Contractor"}),
            "saved_searches": json.dumps([{"loc": "Joplin, MO"}]),
            "license_key": json.dumps("AAAA-BBBB"),
            "home_location": json.dumps("Joplin, MO"),
            "onboarded": json.dumps(True),
            "device_id": json.dumps("web-abc"),
            "has_account": json.dumps(True),
            "sb-xyz-auth-token": json.dumps({"access_token": "tok"}),
            "data_owner_email": json.dumps("first@example.com"),
        }

    def test_a_different_account_gets_a_clean_browser(self):
        r = self._run(self._occupied(), "second@example.com")
        for leaked in ("last_feed", "saved", "notes", "pipeline",
                       "company_profile", "saved_searches", "license_key",
                       "upcoming_feed", "leads_feed", "lead_status",
                       "home_location", "onboarded"):
            self.assertNotIn(leaked, r["data"], f"{leaked} carried over")

    def test_the_supabase_session_token_survives(self):
        """Deleting it would sign the new user out mid-sign-in."""
        r = self._run(self._occupied(), "second@example.com")
        self.assertIn("sb-xyz-auth-token", r["data"])

    def test_the_device_identity_survives(self):
        r = self._run(self._occupied(), "second@example.com")
        self.assertIn("device_id", r["data"])
        self.assertIn("has_account", r["data"])

    def test_the_new_owner_is_recorded(self):
        r = self._run(self._occupied(), "second@example.com")
        self.assertEqual(json.loads(r["data"]["data_owner_email"]),
                         "second@example.com")

    def test_it_reloads_and_tells_the_caller_to_stop(self):
        """In-memory copies are already populated; without the reload the
        cleared data goes straight back on screen."""
        r = self._run(self._occupied(), "second@example.com")
        self.assertTrue(r["reloaded"])
        self.assertTrue(r["stopped"])

    def test_the_same_account_keeps_everything(self):
        r = self._run(self._occupied(), "first@example.com")
        self.assertIn("last_feed", r["data"])
        self.assertIn("saved", r["data"])
        self.assertFalse(r["reloaded"])
        self.assertFalse(r["stopped"])

    def test_the_same_account_is_matched_case_insensitively(self):
        r = self._run(self._occupied(), "First@Example.com")
        self.assertIn("last_feed", r["data"])
        self.assertFalse(r["reloaded"])

    def test_a_brand_new_browser_does_not_bounce_through_a_reload(self):
        """Nothing to clear, so the first screen a new account sees must not
        be a page reload."""
        r = self._run({"device_id": json.dumps("web-new"),
                       "sb-xyz-auth-token": json.dumps({"access_token": "t"})},
                      "fresh@example.com")
        self.assertFalse(r["reloaded"])
        self.assertFalse(r["stopped"])
        self.assertEqual(json.loads(r["data"]["data_owner_email"]),
                         "fresh@example.com")

    def test_a_new_signups_own_name_is_not_thrown_away(self):
        """pending_signup_name is written by the signup form seconds before
        this runs and belongs to the person arriving."""
        data = self._occupied()
        data["pending_signup_name"] = json.dumps("Second Contractor")
        r = self._run(data, "second@example.com")
        self.assertEqual(json.loads(r["data"]["pending_signup_name"]),
                         "Second Contractor")

    def test_an_empty_email_changes_nothing(self):
        r = self._run(self._occupied(), "")
        self.assertIn("last_feed", r["data"])
        self.assertFalse(r["stopped"])


class GuardIsWiredInTests(unittest.TestCase):
    """The clear has to happen before anything can read or upload the data."""

    def test_claim_runs_before_the_app_is_shown(self):
        with open(APP, encoding="utf-8") as f:
            src = f.read()
        # Anchor on the session-management handler specifically. An earlier
        # onAuthStateChange registration exists purely to catch
        # PASSWORD_RECOVERY, and matching that one tests nothing.
        handler = _extract(src, "// \u2500\u2500 Session management \u2500\u2500",
                           "function showAuth()")
        # Comments out: the note above the call names showApp() in prose, and
        # matching that would compare a sentence against a call site.
        code = "\n".join(re.sub(r"//.*$", "", ln) for ln in handler.splitlines())
        self.assertIn("claimDeviceFor", code)
        self.assertLess(code.index("claimDeviceFor"), code.index("showApp()"),
                        "claimDeviceFor must run before showApp() reaches syncPullFeeds")


if __name__ == "__main__":
    unittest.main()
