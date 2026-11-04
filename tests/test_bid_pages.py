"""The public "concrete bids in <state>" pages: one per state, never stale
against the data, never giving away which agency pages are watched, and
filled with open bids at the edge from /bids-in-state."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_bid_pages as P  # noqa: E402
import build_cost_pages as C  # noqa: E402
import license_server as ls  # noqa: E402


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class CommittedBidPagesTests(unittest.TestCase):
    def test_every_state_and_dc_has_a_page_in_the_sitemap(self):
        sitemap = read(C.SITEMAP)
        for st, name in P.N.NAMES.items():
            fname = f"{C.slug(name)}.html"
            self.assertTrue(os.path.exists(os.path.join(P.OUT, fname)), fname)
            self.assertIn(f"{C.BASE_URL}/concrete-bids/{fname}", sitemap)

    def test_the_committed_pages_are_what_the_data_builds(self):
        tmp = tempfile.mkdtemp()
        try:
            out, sm = os.path.join(tmp, "concrete-bids"), os.path.join(tmp, "sitemap.xml")
            shutil.copytree(P.OUT, out)
            shutil.copy(C.SITEMAP, sm)
            with patch.object(P, "OUT", out), patch.object(C, "SITEMAP", sm), patch("builtins.print"):
                P.main()
            for name in sorted(os.listdir(P.OUT)):
                self.assertEqual(read(os.path.join(P.OUT, name)), read(os.path.join(out, name)),
                                 f"{name} is stale: run tools/build_bid_pages.py")
            self.assertEqual(read(C.SITEMAP), read(sm), "sitemap.xml is stale: run tools/build_bid_pages.py")
        finally:
            shutil.rmtree(tmp)

    def test_each_page_has_the_live_list_placeholder_for_its_state(self):
        page = read(os.path.join(P.OUT, "missouri.html"))
        self.assertIn('<div id="open-bids" data-state="MO">', page)
        self.assertIn("Concrete and sidewalk bids in Missouri", page)
        self.assertRegex(page, r"[\d,]+ agency bid pages")

    def test_no_agency_bid_page_address_is_given_away(self):
        allowed = ("https://curbcallpro.com", "https://bid-caller-pro.onrender.com", "https://fonts.googleapis.com",
                   "https://schema.org")
        for name in os.listdir(P.OUT):
            for url in re.findall(r'(?:href|action)="(https?://[^"]+)"', read(os.path.join(P.OUT, name))):
                self.assertTrue(url.startswith(allowed), f"{name} links out to {url}")

    def test_price_and_bid_pages_link_to_each_other(self):
        self.assertIn('/concrete-bids/missouri.html', read(os.path.join(C.OUT, "missouri.html")))
        self.assertIn('/concrete-cost/missouri.html', read(os.path.join(P.OUT, "missouri.html")))


class BidsInStateTests(unittest.TestCase):
    ROWS = {"a": {"title": "Elm St Sidewalk", "town": "Aurora, MO", "lat": 1, "lon": 1, "due": "2099-01-05", "seen": "2026-11-01"},
            "b": {"title": "Ramps", "town": "Joplin, MO", "lat": 1, "lon": 1, "due": "2099-01-02", "seen": "2026-11-01"},
            "c": {"title": "Kansas job", "town": "Lawrence, KS", "lat": 1, "lon": 1, "due": "2099-01-01", "seen": "2026-11-01"},
            "d": {"title": "Old", "town": "Aurora, MO", "lat": 1, "lon": 1, "due": "2020-01-01", "seen": "2020-01-01"}}

    def setUp(self):
        ls._bids_in_state_cache.update(at=0.0, rows=None)

    def test_open_bids_in_the_state_soonest_first(self):
        with patch.object(ls, "_open_bid_rows", return_value=self.ROWS):
            d = ls.app.test_client().get("/bids-in-state?state=mo").get_json()
        self.assertEqual(d["count"], 2)
        self.assertEqual([b["title"] for b in d["bids"]], ["Ramps", "Elm St Sidewalk"])
        self.assertEqual(set(d["bids"][0]), {"title", "town", "due"})

    def test_a_bad_state_is_refused(self):
        self.assertEqual(ls.app.test_client().get("/bids-in-state?state=Missouri").status_code, 400)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class WorkerListTests(unittest.TestCase):
    """The list the Worker puts on a page, from /bids-in-state's answer."""

    def render(self, data):
        tmp = tempfile.mkdtemp()
        try:
            mod = os.path.join(tmp, "w.mjs")
            shutil.copy(os.path.join(ROOT, "worker", "index.js"), mod)
            js = (f"import('{mod}').then(m=>process.stdout.write(String(m.bidsHTML({data}))))")
            return subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30).stdout
        finally:
            shutil.rmtree(tmp)

    def test_titles_are_escaped_and_dates_are_short(self):
        out = self.render('{ok:true,count:3,bids:[{title:"Elm <script>",town:"Aurora, MO",due:"2026-11-20"}]}')
        self.assertIn("Elm &lt;script&gt;", out)
        self.assertIn("due Nov 20", out)
        self.assertIn("and 2 more", out)

    def test_nothing_found_says_so_and_a_failure_keeps_the_placeholder(self):
        self.assertIn("No open bids found", self.render("{ok:true,count:0,bids:[]}"))
        self.assertEqual(self.render("null"), "null")


if __name__ == "__main__":
    unittest.main()
