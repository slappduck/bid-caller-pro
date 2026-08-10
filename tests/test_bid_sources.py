"""Tests for bid_sources.py — the direct, structured bid readers.

Fixtures are trimmed copies of the real page shapes. Nothing here touches the
network, so a parser can be tuned without waiting on a live site.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_sources as bs


CIVICPLUS_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Bid Postings</title>
  <item>
    <title>FY26 Sidewalk Improvements - Mt Vernon &amp; Miller</title>
    <link>https://www.springfieldmo.gov/Bids.aspx?bidID=412</link>
    <description>Approximately 13,500 SF of concrete sidewalk, 1,000 SF of
      concrete ADA ramp and 1,000 LF of curb and gutter.
      Bid Opening: December 1, 2026</description>
  </item>
  <item>
    <title>Janitorial Services - Municipal Buildings</title>
    <link>https://www.springfieldmo.gov/Bids.aspx?bidID=413</link>
    <description>Nightly cleaning of city offices.</description>
  </item>
  <item>
    <title>ADA Improvement Project - Sunshine &amp; Battlefield</title>
    <link>https://www.springfieldmo.gov/Bids.aspx?bidID=414</link>
    <description>5,000 SY concrete ramps, 3,000 SY ADA sidewalk.
      Closing: 12/15/2026</description>
  </item>
</channel></rss>"""

CIVICPLUS_HTML = """
<div class="listItems">
  <a href="/Bids.aspx?bidID=412">FY26 Sidewalk Improvements &amp; ADA Ramps</a>
  <span class="date">Bid Opening: December 1, 2026</span>
  <a href="/Bids.aspx?bidID=413">Janitorial Services</a>
  <span class="date">Closes: 11/02/2026</span>
  <a href="https://www.springfieldmo.gov/Bids.aspx?bidID=414">Curb &amp; Gutter Replacement</a>
  <span class="date">Due: 2026-12-15</span>
  <a href="/Bids.aspx?bidID=412">FY26 Sidewalk Improvements &amp; ADA Ramps</a>
  <a href="/SomethingElse.aspx">Not a bid link</a>
</div>"""

RSS_INDEX = """
<ul>
  <li><a href="/RSSFeed.aspx?ModID=65&amp;CID=All-calendar.xml">Calendar</a></li>
  <li><a href="/RSSFeed.aspx?ModID=76&amp;CID=All-0">Bid Postings</a></li>
  <li><a href="/RSSFeed.aspx?ModID=58&amp;CID=news">News Flash</a></li>
</ul>"""


class PlatformIdentificationTests(unittest.TestCase):
    def test_recognises_the_major_platforms(self):
        cases = {
            "https://www.springfieldmo.gov/Bids.aspx": "civicplus",
            "https://mo-springfield.civicplus.com/bids.aspx": "civicplus",
            "https://www.demandstar.com/app/agencies/x": "demandstar",
            "https://vendors.planetbids.com/portal/1234/bo/bo-search": "planetbids",
            "https://cityofx.bonfirehub.com/opportunities": "bonfire",
            "https://procurement.opengov.com/portal/springfield": "opengov",
            "https://www.bidnetdirect.com/public/solicitations/open": "bidnetdirect",
        }
        for url, want in cases.items():
            with self.subTest(url=url):
                self.assertEqual(bs.identify_platform(url), want)

    def test_an_unknown_agency_page_is_still_worth_keeping(self):
        self.assertEqual(
            bs.identify_platform("https://greenecountymo.gov/purchasing/rfp"), "agency")
        self.assertEqual(
            bs.identify_platform("https://www.co.christian.mo.us/bids"), "agency")

    def test_an_ordinary_page_is_not_a_source(self):
        for url in ("https://example.com/blog/post", "https://news.site/story", ""):
            with self.subTest(url=url):
                self.assertEqual(bs.identify_platform(url), "")

    def test_malformed_input_does_not_raise(self):
        for url in (None, 12345, "http://[", "not a url at all"):
            with self.subTest(url=url):
                bs.identify_platform(url)


class CivicPlusEndpointTests(unittest.TestCase):
    def test_builds_urls_from_a_bare_domain(self):
        urls = bs.civicplus_endpoints("www.springfieldmo.gov")
        self.assertTrue(any(u.endswith("/Bids.aspx") for u in urls))
        self.assertTrue(all(u.startswith("https://") for u in urls))

    def test_accepts_a_full_url_too(self):
        urls = bs.civicplus_endpoints("https://www.springfieldmo.gov/")
        self.assertIn("https://www.springfieldmo.gov/Bids.aspx", urls)

    def test_includes_the_rss_index_for_freshness(self):
        self.assertTrue(any(u.endswith("/rss.aspx")
                            for u in bs.civicplus_endpoints("x.gov")))

    def test_empty_domain_yields_nothing(self):
        self.assertEqual(bs.civicplus_endpoints(""), [])


class RssParsingTests(unittest.TestCase):
    def test_extracts_every_item(self):
        rows = bs.parse_civicplus_rss(CIVICPLUS_RSS)
        self.assertEqual(len(rows), 3)

    def test_carries_title_url_and_scope(self):
        row = bs.parse_civicplus_rss(CIVICPLUS_RSS)[0]
        self.assertIn("Mt Vernon & Miller", row["title"])
        self.assertTrue(row["url"].endswith("bidID=412"))
        self.assertIn("ADA ramp", row["scope"])

    def test_pulls_a_closing_date_out_of_the_description(self):
        rows = bs.parse_civicplus_rss(CIVICPLUS_RSS)
        self.assertEqual(rows[0]["deadline"], "December 1, 2026")
        self.assertEqual(rows[2]["deadline"], "12/15/2026")

    def test_malformed_feed_returns_nothing_rather_than_raising(self):
        for bad in ("", "<rss>", "not xml at all", None, "<html>nope</html>"):
            with self.subTest(bad=bad):
                self.assertEqual(bs.parse_civicplus_rss(bad), [])


class HtmlParsingTests(unittest.TestCase):
    def test_finds_each_distinct_bid_link(self):
        rows = bs.parse_civicplus_html(CIVICPLUS_HTML, "https://www.springfieldmo.gov")
        self.assertEqual(len(rows), 3, [r["title"] for r in rows])

    def test_makes_relative_links_absolute(self):
        rows = bs.parse_civicplus_html(CIVICPLUS_HTML, "https://www.springfieldmo.gov")
        self.assertTrue(all(r["url"].startswith("https://") for r in rows))

    def test_ignores_links_that_are_not_bids(self):
        rows = bs.parse_civicplus_html(CIVICPLUS_HTML, "https://x.gov")
        self.assertFalse(any("SomethingElse" in r["url"] for r in rows))

    def test_unescapes_titles(self):
        rows = bs.parse_civicplus_html(CIVICPLUS_HTML, "https://x.gov")
        self.assertIn("Sidewalk Improvements & ADA Ramps", rows[0]["title"])

    def test_picks_up_the_closing_date_near_the_link(self):
        rows = bs.parse_civicplus_html(CIVICPLUS_HTML, "https://x.gov")
        self.assertEqual(rows[0]["deadline"], "December 1, 2026")

    def test_garbage_input_returns_nothing_rather_than_raising(self):
        for bad in ("", None, "<html", 12345):
            with self.subTest(bad=bad):
                self.assertEqual(bs.parse_civicplus_html(bad), [])


class FeedDiscoveryTests(unittest.TestCase):
    def test_picks_the_bids_feed_out_of_the_index(self):
        feed = bs.find_bid_feed(RSS_INDEX, "https://www.springfieldmo.gov")
        self.assertIn("ModID=76", feed)
        self.assertTrue(feed.startswith("https://"))

    def test_no_bids_module_means_no_feed(self):
        self.assertEqual(bs.find_bid_feed('<a href="/RSSFeed.aspx?ModID=1">News</a>'), "")


class RelevanceFilterTests(unittest.TestCase):
    """Runs before any AI call, so it must be generous — a false negative is a
    lost bid, a false positive costs a fraction of a cent."""

    def test_keeps_anything_plausibly_concrete(self):
        keep = [
            "FY26 Sidewalk Improvements",
            "ADA Curb Ramp Replacement Program",
            "Curb and Gutter Repair - Phase 2",
            "Street Improvement Project - Main St",
            "Safe Routes to School Pedestrian Improvements",
            "Parking Lot Reconstruction",
            "2026 Concrete Flatwork Contract",
            "Greenway Trail Extension",
            "Downtown Streetscape Improvements",
            "CDBG Neighborhood Infrastructure",
        ]
        for title in keep:
            with self.subTest(title=title):
                self.assertTrue(bs.looks_relevant(title), title)

    def test_drops_the_obviously_unrelated(self):
        drop = ["Janitorial Services", "Employee Health Insurance Broker",
                "Annual Financial Audit", "Police Uniform Supply",
                "Copier Lease Agreement"]
        for title in drop:
            with self.subTest(title=title):
                self.assertFalse(bs.looks_relevant(title), title)

    def test_a_mixed_scope_still_counts(self):
        # Concrete work buried in a bigger job is exactly what we must not miss.
        self.assertTrue(bs.looks_relevant(
            "Fire Station No. 4 Construction",
            "Includes site work, parking lot paving and ADA access ramps."))

    def test_empty_input_is_not_relevant(self):
        self.assertFalse(bs.looks_relevant("", None))

    def test_rejection_reason_names_the_cause(self):
        self.assertTrue(bs.rejection_reason("Janitorial Services").startswith("unrelated:"))
        self.assertEqual(bs.rejection_reason("Widget Procurement"), "no_niche_keyword")


if __name__ == "__main__":
    unittest.main()


class GovDirectoryTests(unittest.TestCase):
    """CISA's .gov registry, shipped as data. The scanner's weak point was
    never parsing — it was not knowing where to look, and paying a search API
    per query for an answer that never changes."""

    def setUp(self):
        import gov_directory
        self.gd = gov_directory

    def test_the_whole_country_is_loaded(self):
        self.assertGreater(self.gd.loaded_count(), 10000)

    def test_the_town_you_asked_for_ranks_above_its_neighbours(self):
        # Fremont Hills is registered with Nixa as its mailing city, so a naive
        # sort put a different town's domain first.
        self.assertEqual(self.gd.domains_for("Nixa", "MO")[0], "nixamo.gov")

    def test_a_citys_own_domain_outranks_its_county(self):
        got = self.gd.domains_for("Springfield", "MO")
        self.assertEqual(got[0], "springfieldmo.gov")
        self.assertIn("greenecountymo.gov", got)

    def test_counties_are_reachable_at_all(self):
        # Counties let a lot of curb and road work and were previously invisible.
        for city, state, want in (("Springfield", "MO", "greenecountymo.gov"),
                                  ("Ozark", "MO", "christiancountymo.gov"),
                                  ("Bolivar", "MO", "polkcountymo.gov")):
            with self.subTest(city=city):
                self.assertIn(want, self.gd.domains_for(city, state))

    def test_lookup_is_case_and_space_insensitive(self):
        self.assertEqual(self.gd.domains_for("  sPrInGfIeLd ", "mo")[0],
                         "springfieldmo.gov")

    def test_unknown_places_return_nothing_rather_than_raising(self):
        for city, state in (("Nowhereville", "ZZ"), ("", "MO"), (None, None)):
            with self.subTest(city=city):
                self.assertEqual(self.gd.domains_for(city, state), [])

    def test_counties_can_be_excluded(self):
        got = self.gd.domains_for("Springfield", "MO", include_county=False)
        self.assertNotIn("greenecountymo.gov", got)

    def test_coverage_is_national_not_just_missouri(self):
        for city, state in (("Twentynine Palms", "CA"), ("Detroit", "MI")):
            with self.subTest(city=city):
                self.assertTrue(self.gd.domains_for(city, state))


class CandidateUrlTests(unittest.TestCase):
    def test_civicplus_path_is_tried_first(self):
        self.assertTrue(bs.candidate_bid_urls("nixamo.gov")[0].endswith("/Bids.aspx"))

    def test_urls_are_absolute_https(self):
        self.assertTrue(all(u.startswith("https://")
                            for u in bs.candidate_bid_urls("nixamo.gov")))

    def test_the_probe_can_be_kept_short(self):
        # Each candidate is a live fetch, so callers cap it.
        self.assertEqual(len(bs.candidate_bid_urls("x.gov", limit=2)), 2)

    def test_a_blank_domain_probes_nothing(self):
        for bad in ("", None, "   "):
            with self.subTest(bad=bad):
                self.assertEqual(bs.candidate_bid_urls(bad), [])
