"""Google was splitting the brand name in half.

A search for "curbcallpro" returned "These are results for curb callpro" and
"Missing: callpro", with an AI Overview about Housecall Pro and The Callpro --
two other companies. The site had no structured data at all, so nothing on it
ever told Google that CurbCall Pro is one named thing rather than a string to
guess at.

/favicon.ico was also a 404. The declared PNG covers most surfaces, but
browsers and several crawlers request that path directly regardless of what is
declared, and a search result renders 16px, which is better shipped than
downscaled from 512 on the fly.

These are not ranking tricks. They are the difference between an entity Google
can attach a name, a logo and a price to, and one it has to infer.
"""
import json
import os
import re
import unittest
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, os.pardir, "curbcall_netlify_v4")


def read(name):
    with open(os.path.join(WEB, name), encoding="utf-8") as f:
        return f.read()


def graph():
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>',
                  read("index.html"), re.S)
    assert m, "no structured data on the landing page"
    return {n["@type"]: n for n in json.loads(m.group(1))["@graph"]}


class StructuredDataTests(unittest.TestCase):
    def test_the_landing_page_carries_valid_json_ld(self):
        self.assertTrue(graph())

    def test_the_brand_is_declared_as_one_name(self):
        """The specific failure: Google read 'curbcallpro' as two words."""
        org = graph()["Organization"]
        self.assertEqual(org["name"], "CurbCall Pro")
        self.assertIn("CurbCallPro", org["alternateName"])

    def test_the_logo_is_declared_so_a_result_can_show_it(self):
        logo = graph()["Organization"]["logo"]
        self.assertTrue(logo["url"].endswith(".png"))
        self.assertGreaterEqual(logo["width"], 112)   # Google's minimum

    def test_the_logo_file_actually_exists(self):
        url = graph()["Organization"]["logo"]["url"]
        self.assertTrue(os.path.exists(os.path.join(WEB, url.rsplit("/", 1)[-1])))

    def test_both_plans_are_priced(self):
        """A price in a search result is a qualifier: somebody who will not
        pay $49 does not click, which is a click saved on both sides."""
        offers = {o["name"]: o["price"] for o in
                  graph()["SoftwareApplication"]["offers"]}
        self.assertEqual(offers, {"Monthly": "49.00", "Annual": "399.00"})

    def test_the_prices_match_the_page_they_are_on(self):
        page = read("index.html")
        self.assertIn("$49", page)
        self.assertIn("$399", page)

    def test_the_software_is_published_by_the_organisation(self):
        g = graph()
        self.assertEqual(g["SoftwareApplication"]["publisher"]["@id"],
                         g["Organization"]["@id"])


class FaviconTests(unittest.TestCase):
    def test_there_is_an_ico_at_the_path_browsers_ask_for(self):
        self.assertTrue(os.path.exists(os.path.join(WEB, "favicon.ico")))

    def test_it_contains_the_size_a_search_result_renders(self):
        from PIL import Image
        im = Image.open(os.path.join(WEB, "favicon.ico"))
        self.assertIn((16, 16), im.info.get("sizes", []))

    def test_the_page_declares_it_first(self):
        head = read("index.html")[:4000]
        ico = head.index('href="/favicon.ico"')
        png = head.index('href="/icon-192.png"')
        self.assertLess(ico, png)


class SitemapTests(unittest.TestCase):
    def setUp(self):
        self.locs = [e.text for e in
                     ET.parse(os.path.join(WEB, "sitemap.xml")).iter()
                     if e.tag.endswith("loc")]

    def test_it_is_valid_xml_with_entries(self):
        self.assertGreater(len(self.locs), 4)

    def test_every_listed_page_exists(self):
        for loc in self.locs:
            path = loc.replace("https://curbcallpro.com/", "") or "index.html"
            with self.subTest(loc=loc):
                self.assertTrue(os.path.exists(os.path.join(WEB, path)),
                                "sitemap lists a page that is not there")

    def test_the_policy_archive_is_listed(self):
        """The versions somebody accepted should be findable, not orphaned."""
        self.assertTrue(any("/legal/" in l for l in self.locs))

    def test_robots_points_at_it(self):
        self.assertIn("Sitemap: https://curbcallpro.com/sitemap.xml",
                      read("robots.txt"))
