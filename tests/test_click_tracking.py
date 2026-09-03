"""Counting outreach link opens without renting the number from Cloudflare.

/go/<slug> serves the landing page, one slug per contractor emailed. Those
opens were counted by Cloudflare's Web Analytics beacon, which is a
third-party script: ad blockers strip it, so the figure is some unknown
fraction of the truth. Eight opens read as "nobody looked" when nobody could
actually say.

The page now posts its slug to this endpoint instead. It is public, because
the page reporting is public, so the tests below care mostly about what a
stranger can do to it: junk slugs, and filling the store.

It also stores no IP, no user agent and no per-visit timestamp. That is a
deliberate limit, not an oversight, and one test pins it -- these are third
parties who clicked a link in an email, and a count is all the outreach
needs.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


class ClickEndpointTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self._orig_get, self._orig_set = ls.kv_backend.get, ls.kv_backend.set
        ls.kv_backend.get = lambda k, d=None: self.store.get(k, d)
        ls.kv_backend.set = lambda k, v: self.store.__setitem__(k, v)
        self.app = ls.app.test_client()

    def tearDown(self):
        ls.kv_backend.get, ls.kv_backend.set = self._orig_get, self._orig_set

    def _click(self, slug):
        return self.app.post("/click", json={"slug": slug})

    def _rows(self):
        return self.store.get(ls._CLICK_KEY) or {}

    def test_a_click_is_counted(self):
        r = self._click("pcap")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["recorded"])
        self.assertEqual(self._rows()["pcap"]["n"], 1)

    def test_repeat_clicks_add_up(self):
        for _ in range(3):
            self._click("morici")
        self.assertEqual(self._rows()["morici"]["n"], 3)

    def test_slugs_are_counted_separately(self):
        self._click("pcap"); self._click("clauss"); self._click("clauss")
        self.assertEqual(self._rows()["pcap"]["n"], 1)
        self.assertEqual(self._rows()["clauss"]["n"], 2)

    def test_first_and_last_are_both_kept(self):
        self._click("plm"); self._click("plm")
        row = self._rows()["plm"]
        self.assertIn("first", row)
        self.assertIn("last", row)

    def test_case_does_not_split_a_slug(self):
        self._click("PCAP"); self._click("pcap")
        self.assertEqual(list(self._rows()), ["pcap"])
        self.assertEqual(self._rows()["pcap"]["n"], 2)

    def test_junk_is_refused(self):
        for bad in ("", "   ", "../../etc/passwd", "a" * 40, "has space",
                    "semi;colon", "<script>", "slug/with/slash"):
            r = self._click(bad)
            self.assertEqual(r.status_code, 400, f"accepted {bad!r}")
        self.assertEqual(self._rows(), {})

    def test_a_missing_body_does_not_500(self):
        r = self.app.post("/click", data="not json",
                          content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_the_store_cannot_be_filled_without_bound(self):
        """Public endpoint. A stranger must not be able to grow it forever."""
        orig = ls._CLICK_MAX_SLUGS
        ls._CLICK_MAX_SLUGS = 5
        try:
            for i in range(20):
                self._click(f"junk{i}")
            self.assertLessEqual(len(self._rows()), 5)
        finally:
            ls._CLICK_MAX_SLUGS = orig

    def test_a_full_store_still_counts_the_slugs_it_knows(self):
        """Hitting the cap must not stop real recipients being counted."""
        orig = ls._CLICK_MAX_SLUGS
        ls._CLICK_MAX_SLUGS = 2
        try:
            self._click("pcap"); self._click("clauss")
            self._click("stranger")          # refused, store is full
            self._click("pcap")              # known slug, still counts
            self.assertEqual(self._rows()["pcap"]["n"], 2)
            self.assertNotIn("stranger", self._rows())
        finally:
            ls._CLICK_MAX_SLUGS = orig

    def test_nothing_identifying_is_stored(self):
        self.app.post("/click", json={"slug": "pcap"},
                      headers={"User-Agent": "Mozilla/5.0 (spy)",
                               "X-Forwarded-For": "203.0.113.9"},
                      environ_base={"REMOTE_ADDR": "203.0.113.9"})
        blob = repr(self._rows())
        self.assertNotIn("203.0.113.9", blob)
        self.assertNotIn("Mozilla", blob)
        self.assertEqual(set(self._rows()["pcap"]), {"n", "first", "last"})

    def test_extra_fields_in_the_body_are_ignored(self):
        self.app.post("/click", json={"slug": "pcap", "ip": "203.0.113.9",
                                      "email": "someone@example.com"})
        self.assertNotIn("203.0.113.9", repr(self._rows()))
        self.assertNotIn("someone@example.com", repr(self._rows()))


class ClicksReachTheDashboardTests(unittest.TestCase):
    def test_diag_exposes_the_counts(self):
        src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "license_server.py"), encoding="utf-8").read()
        i = src.index('def diag():')
        self.assertIn('"go_clicks"', src[i:i + 4000])


class LandingPagePingTests(unittest.TestCase):
    """The page half: fires on /go/<slug> and nowhere else."""

    def setUp(self):
        with open(os.path.join(os.path.dirname(__file__), os.pardir,
                               "curbcall_netlify_v4", "index.html"),
                  encoding="utf-8") as f:
            self.src = f.read()

    def test_it_posts_to_our_own_backend(self):
        self.assertIn('/click', self.src)
        self.assertIn('keepalive: true', self.src)

    def test_it_only_fires_once_per_session(self):
        self.assertIn("sessionStorage", self.src)

    def test_it_sends_the_slug_and_nothing_else(self):
        i = self.src.index('body: JSON.stringify({ slug: slug })')
        self.assertGreater(i, 0)

    def test_failures_are_swallowed(self):
        """A dropped count is worth nothing; a landing page that throws is
        worth less."""
        i = self.src.index('/click')
        self.assertIn(".catch(function () {})", self.src[i - 400:i + 500])


if __name__ == "__main__":
    unittest.main()


class ClicksAreNotPublicTests(unittest.TestCase):
    """Who opened a cold email is the recipient's behaviour, not ours.

    This first went on /health, which is public and unauthenticated -- one
    curl and anyone could read which named contractors had opened Josh's
    outreach. Caught by a test rather than in the wild. It belongs behind the
    diag token, and this pins it there.
    """

    def setUp(self):
        self.store = {ls._CLICK_KEY: {"pcap": {"n": 3, "first": "x", "last": "y"}}}
        self._orig_get, self._orig_set = ls.kv_backend.get, ls.kv_backend.set
        ls.kv_backend.get = lambda k, d=None: self.store.get(k, d)
        ls.kv_backend.set = lambda k, v: self.store.__setitem__(k, v)
        self.app = ls.app.test_client()

    def tearDown(self):
        ls.kv_backend.get, ls.kv_backend.set = self._orig_get, self._orig_set

    def test_health_does_not_leak_who_clicked(self):
        body = self.app.get("/health").get_data(as_text=True)
        self.assertNotIn("go_clicks", body)
        self.assertNotIn("pcap", body)

    def test_diag_without_a_token_does_not_leak_it_either(self):
        r = self.app.get("/diag")
        self.assertIn(r.status_code, (403, 503))
        self.assertNotIn("pcap", r.get_data(as_text=True))

    def test_diag_with_the_token_does_show_it(self):
        orig = ls.DIAG_TOKEN
        ls.DIAG_TOKEN = "t" * 32
        try:
            r = self.app.get("/diag", headers={"X-Diag-Token": "t" * 32})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["go_clicks"]["pcap"]["n"], 3)
        finally:
            ls.DIAG_TOKEN = orig
