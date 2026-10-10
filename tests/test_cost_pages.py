"""The public concrete-price pages: one per state with rates, numbers that
match the data, findable from the sitemap, and never stale against the data."""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import build_cost_pages as C  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "curbcall_netlify_v4")


def rates():
    out = {}
    for name in os.listdir(C.RATES):
        if re.fullmatch(r"[a-z]{2}\.json", name):
            with open(os.path.join(C.RATES, name), encoding="utf-8") as f:
                out[name[:2].upper()] = json.load(f)
    return out


class CommittedPagesTests(unittest.TestCase):
    def setUp(self):
        self.rates = rates()

    def test_every_state_with_rates_has_a_page_and_a_sitemap_entry(self):
        with open(C.SITEMAP, encoding="utf-8") as f:
            sitemap = f.read()
        for d in self.rates.values():
            fname = f"{C.slug(d['state_name'])}.html"
            self.assertTrue(os.path.exists(os.path.join(C.OUT, fname)), fname)
            self.assertIn(f"{C.BASE_URL}/concrete-cost/{fname}", sitemap)
        self.assertIn(f"{C.BASE_URL}/concrete-cost/index.html", sitemap)

    def test_the_committed_pages_are_what_the_data_builds(self):
        # A data change that skipped the page build would leave the public
        # pages quoting old prices; a rebuild into a scratch copy must match.
        tmp = tempfile.mkdtemp()
        try:
            out, sm = os.path.join(tmp, "concrete-cost"), os.path.join(tmp, "sitemap.xml")
            shutil.copytree(C.OUT, out)
            shutil.copy(C.SITEMAP, sm)
            with patch.object(C, "OUT", out), patch.object(C, "SITEMAP", sm), patch("builtins.print"):
                C.main()
            for name in sorted(os.listdir(C.OUT)) + ["../sitemap.xml"]:
                with open(os.path.join(C.OUT, name), encoding="utf-8") as a, \
                     open(os.path.join(out, name), encoding="utf-8") as b:
                    self.assertEqual(a.read(), b.read(), f"{name} is stale: run tools/build_cost_pages.py")
        finally:
            shutil.rmtree(tmp)

    def test_missouri_quotes_modot_sidewalk_price(self):
        mo = self.rates["MO"]
        code = mo["cats"]["sidewalk"]
        by_y = mo["prices"][code]["STATEWIDE"]
        y = max(by_y, key=int)
        with open(os.path.join(C.OUT, "missouri.html"), encoding="utf-8") as f:
            page = f.read()
        self.assertIn(f"<b>{C.money(by_y[y][0])}</b> a sq yd", page)
        self.assertIn("MoDOT", page)
        faq = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', page).group(1))
        self.assertEqual(faq["@type"], "FAQPage")
        self.assertTrue(any("sidewalk" in q["name"].lower() for q in faq["mainEntity"]))

    def test_the_calculator_uses_the_apps_ready_mix_figures(self):
        with open(os.path.join(SITE, "app.js"), encoding="utf-8") as f:
            m = re.search(r"const READY_MIX=\{([^}]*)\}", f.read())
        app = {k: float(v) for k, v in re.findall(r"(\w+):([\d.]+)", m.group(1))}
        self.assertEqual(app, {k: float(v) for k, v in C.READY_MIX.items()})

    def test_pages_load_nothing_but_fonts(self):
        for name in os.listdir(C.OUT):
            with open(os.path.join(C.OUT, name), encoding="utf-8") as f:
                page = f.read()
            self.assertNotRegex(page, r"<script[^>]+src=", name)
            self.assertIn('<link rel="canonical" href="https://curbcallpro.com/concrete-cost/', page)

    def test_the_landing_page_links_here(self):
        with open(os.path.join(SITE, "index.html"), encoding="utf-8") as f:
            self.assertIn('href="/concrete-cost/index.html"', f.read())


class BuildTests(unittest.TestCase):
    def test_names_from_data_are_escaped(self):
        d = {"state": "ZZ", "state_name": "Test", "basis": "all_bids", "years": [2025],
             "source": "<b>DOT</b>", "source_url": "https://example.org/?a=1&b=2",
             "districts": {"STATEWIDE": "All"}, "cats": {"sidewalk": "A"},
             "items": {"A": {"name": "Walk <script>", "unit": "sq yd"}},
             "prices": {"A": {"STATEWIDE": {"2025": [50.0, 40.0, 60.0, 12, 100]}}}}
        d["items"]["A"]["name"] = "Walk </script><script>alert(1)</script>"
        title, desc, body, ld = C.state_page(d, [d])
        self.assertNotIn("alert(1)</script>", body)
        self.assertIn("Walk &lt;/script&gt;", body)
        full = C.page(title, desc, "concrete-cost/test.html", body, ld)
        ld_block = re.search(r'<script type="application/ld\+json">(.*?)</script>', full, re.S).group(1)
        self.assertNotIn("<", ld_block)
        self.assertIn("alert(1)", json.loads(ld_block)["mainEntity"][0]["acceptedAnswer"]["text"])
        self.assertIn("&lt;b&gt;DOT&lt;/b&gt;", body)
        self.assertIn("$50 a sq yd", desc)


if __name__ == "__main__":
    unittest.main()
