"""An existing customer on a new phone was given a scan they did not ask for.

showApp() starts autoFillZip() and syncPullFeeds() in the same tick. The
first-scan guard decided by looking at bidData, which on a new device is empty
for the first second or two of every launch simply because the server has not
answered yet -- and every OTHER guard it consults is device-local, so on new
hardware they are all absent for the same reason. The geolocation callback
usually wins the race, so a paying customer signing in on a second phone got
an unrequested 90-second scan of wherever they happened to be standing, and
paid for it in search-API budget.

The function's own comment promised this could not happen: "an existing
customer opening the app must never trigger a surprise 90-second scan, and
neither must a second device". It did.

The fix is to decide on the server's answer rather than on an empty cache.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, os.pardir, "curbcall_netlify_v4", "app.html")


def js():
    with open(APP, encoding="utf-8") as f:
        html = f.read()
    body = "\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                                html, re.S))
    # Comments here describe the bug by name; a raw scan would match the prose.
    return re.sub(r"^\s*//.*$", "", body, flags=re.M)


def guard_body(src):
    i = src.index("function maybeFirstScan(){")
    return src[i:src.index("\n}", i)]


class TheGuardWaitsForAnAnswerTests(unittest.TestCase):
    def setUp(self):
        self.src = js()

    def test_there_is_a_sync_state_to_consult(self):
        self.assertIn('let feedSyncState="pending"', self.src)

    def test_it_refuses_while_the_answer_is_still_in_flight(self):
        self.assertIn('feedSyncState==="pending"', guard_body(self.src))

    def test_it_refuses_when_the_account_already_has_a_feed(self):
        """The whole point: a customer with bids is not a new signup."""
        self.assertIn('feedSyncState==="has-bids"', guard_body(self.src))

    def test_that_check_comes_before_the_device_local_ones(self):
        """Those are the checks that are absent on new hardware, which is
        why they let this through."""
        g = guard_body(self.src)
        self.assertLess(g.index("feedSyncState"), g.index("bidData"))

    def test_the_old_device_local_guards_are_still_there(self):
        """They still catch the ordinary cases and cost nothing."""
        g = guard_body(self.src)
        self.assertIn("FIRST_SCAN_KEY", g)
        self.assertIn("last_scan_debug", g)
        self.assertIn("navigator.onLine", g)


class EveryPathSettlesTheFlagTests(unittest.TestCase):
    """A flag stuck on "pending" would disable the feature entirely, which is
    a quieter failure than the bug and just as wrong."""

    def setUp(self):
        i = js().index("async function syncPullFeeds(){")
        self.fn = js()[i:i + 2600]

    def test_a_signed_out_call_settles_it(self):
        self.assertIn('if(!sb||!currentUser){feedSyncState="failed";return;}',
                      self.fn)

    def test_a_query_error_settles_it(self):
        self.assertIn('if(res.error){feedSyncState="failed";return;}', self.fn)

    def test_a_thrown_error_settles_it(self):
        self.assertIn('catch(e){feedSyncState="failed";return;}', self.fn)

    def test_a_successful_read_settles_it_from_all_three_feeds(self):
        for feed in ("data.bids", "data.upcoming", "data.leads"):
            self.assertIn(feed, self.fn)
        self.assertIn('"has-bids":"empty"', self.fn)


class TheDecisionIsRetriedOnceTheAnswerArrivesTests(unittest.TestCase):
    """Waiting is only half of it. Something has to ask again afterwards, or
    the first scan simply never happens for anybody."""

    def setUp(self):
        i = js().index("async function syncPullFeeds(){")
        self.fn = js()[i:i + 4000]

    def test_a_brand_new_account_still_gets_its_first_scan(self):
        """No server row at all -- the case the feature exists for."""
        i = self.fn.index("if(!data){")
        self.assertIn("maybeFirstScan()", self.fn[i:i + 500])

    def test_it_is_reconsidered_after_a_feed_is_loaded(self):
        self.assertGreaterEqual(self.fn.count("maybeFirstScan()"), 2)
