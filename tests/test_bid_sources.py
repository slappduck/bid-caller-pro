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

# Trimmed from a live fetch of springfieldmo.gov/Bids.aspx (Aug 2026). The
# fixture this replaced was reconstructed from memory and got the layout
# wrong in two ways a live site actually does: (1) a row lists both labels
# ("Status:", "Closes:") together, THEN both values ("Open", the date)
# together — never "Status: Open" adjacent; (2) the teaser text between the
# title link and that status block runs well past 600 characters, and often
# repeats a *different*, misleading date in prose ("THE REVISED DUE DATE IS
# ... AUGUST 18") before the real one ever appears.
CIVICPLUS_REAL_HTML = """
<div class="listItemsRow bid">
	<div class="bidTitle" style="vertical-align: top">
		<span><a href="bids.aspx?bidID=1643">SKATE AND PRO SHOP CONCESSIONAIRE</a></span><br>
<span style="font-size:0.75em;"><strong>Bid No.</strong> 009-2027RFP</span><br>						<span>ADDENDUM 1 ISSUED ON MONDAY, AUGUST 10, 2026.     THE REVISED DUE DATE IS TUESDAY, AUGUST 18, 2026.     LEGAL NOTICE:     REQUEST FOR PROPOSAL #009-2027   The City of Springfield will electronically accept submitted proposals through its e-bidding service provider, Euna OpenBids (formerly DemandStar) from qualified persons or firms interested in providing the following: SKATE AND PRO SHOP CONCESSIONAIRE at the Jordan Valley Ice Park. Proposal documents and any addendums are available via the Division of Purchases webpage. ... [<a href="bids.aspx?bidID=1643" style="font-style: normal; text-decoration: underline; font-family: arial">Read&nbsp;on<span class="visuallyHidden">: SKATE AND PRO SHOP CONCESSIONAIRE</span></a>]</span>
	</div>
	<div class="bidStatus">
	<div>						<span id="BidStatus164337">Status:</span><br>						<span id="BidCloses164337">Closes:</span>
	</div>					<div>						<span>Open</span><br>						<span>8/20/2026 3:00 PM</span>
	</div>
	</div>
</div>				<div class="listItemsRow bid alt">
	<div class="bidTitle" style="vertical-align: top">
		<span><a href="bids.aspx?bidID=1644">REPLACEMENT OF PUBLIC ADDRESS AND TERMINAL PAGING SYSTEM</a></span><br>
<span style="font-size:0.75em;"><strong>Bid No.</strong> 014-2027RFP</span><br>						<span>LEGAL NOTICE:     REQUEST FOR PROPOSAL #014-2027   The City of Springfield will electronically accept submitted proposals through its e-bidding service provider, Euna OpenBids... [<a href="bids.aspx?bidID=1644" style="font-style: normal; text-decoration: underline; font-family: arial">Read&nbsp;on<span class="visuallyHidden">: REPLACEMENT OF PUBLIC ADDRESS AND TERMINAL PAGING SYSTEM</span></a>]</span>
	</div>
	<div class="bidStatus">
	<div>						<span id="BidStatus164437">Status:</span><br>						<span id="BidCloses164437">Closes:</span>
	</div>					<div>						<span>Open</span><br>						<span>9/1/2026 3:00 PM</span>
	</div>
	</div>
</div>				<div class="listItemsRow bid">
	<div class="bidTitle" style="vertical-align: top">
		<span><a href="bids.aspx?bidID=1622">HOT ASPHALT MIX</a></span><br>
<span style="font-size:0.75em;"><strong>Bid No.</strong> 066-2026IFIB</span><br>						<span>LEGAL NOTICE: INVITATION FOR INFORMAL BID #066-2026 ... [<a href="bids.aspx?bidID=1622" style="font-style: normal; text-decoration: underline; font-family: arial">Read&nbsp;on<span class="visuallyHidden">: HOT ASPHALT MIX</span></a>]</span>
	</div>
	<div class="bidStatus">
	<div>						<span id="BidStatus162245">Status:</span><br>						<span id="BidCloses162245">Closes:</span>
	</div>					<div>						<span>Closed</span><br>						<span>5/5/2026 3:00 PM</span>
	</div>
	</div>
</div>
"""

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


class RealCivicPlusLayoutTests(unittest.TestCase):
    """The paired-label, paired-value layout a live CivicPlus site actually
    serves, and the oversized teaser text real rows carry."""

    def setUp(self):
        self.rows = bs.parse_civicplus_html(CIVICPLUS_REAL_HTML, "https://x.gov")
        self.by_title = {r["title"]: r for r in self.rows}

    def test_every_posting_is_found(self):
        self.assertEqual(len(self.rows), 3, [r["title"] for r in self.rows])

    def test_a_far_off_structured_closing_block_still_yields_a_deadline(self):
        # The status/closing block sits well past where a 600-char window
        # used to cut off, and behind a duplicate "Read on" link to the same
        # URL — both used to leave this blank.
        self.assertEqual(
            self.by_title["REPLACEMENT OF PUBLIC ADDRESS AND TERMINAL PAGING SYSTEM"]
            ["deadline"], "9/1/2026")

    def test_the_structured_field_wins_over_a_misleading_date_in_prose(self):
        # This row's teaser text says "THE REVISED DUE DATE IS ... AUGUST 18"
        # — a different date than the row's real "Closes:" field (8/20). The
        # structured field must win; picking whichever date appears first in
        # the text would silently return the wrong one.
        row = self.by_title["SKATE AND PRO SHOP CONCESSIONAIRE"]
        self.assertEqual(row["deadline"], "8/20/2026")
        self.assertNotEqual(row["deadline"], "8/18/2026")

    def test_the_listing_status_is_carried_through(self):
        self.assertEqual(self.by_title["SKATE AND PRO SHOP CONCESSIONAIRE"]["status"],
                         "Open")
        self.assertEqual(self.by_title["HOT ASPHALT MIX"]["status"], "Closed")

    def test_a_closed_postings_own_deadline_is_not_lost(self):
        self.assertEqual(self.by_title["HOT ASPHALT MIX"]["deadline"], "5/5/2026")


DETAIL_PAGE = """
<h1>2026 Sidewalk &amp; ADA Ramp Program</h1>
<p>Bid Number: 2026-014</p>
<p>Description: Removal and replacement of approximately 4,800 linear feet of
   deteriorated sidewalk, installation of 22 ADA-compliant curb ramps, and
   associated curb and gutter work at various locations citywide.</p>
<p>Closing Date/Time: 12/1/2026 2:00 PM</p>
<p>Contact Person: Marla Whitfield</p>
<p>Email: <a href="mailto:mwhitfield@example-city.gov">mwhitfield@example-city.gov</a></p>
<p>Phone: (417) 864-1districts</p>
<p>Phone: 417-864-1976</p>
<img src="/images/logo@2x.png">
<a href="mailto:webmaster@example-city.gov">Report a problem</a>
"""


class ContactExtractionTests(unittest.TestCase):
    """A bid with nobody to call is barely a lead. The listing page never has
    this — it is why the detail page is fetched at all."""

    def setUp(self):
        self.got = bs.parse_contact(DETAIL_PAGE)

    def test_finds_the_buyers_email(self):
        self.assertEqual(self.got["email"], "mwhitfield@example-city.gov")

    def test_skips_the_site_webmaster(self):
        # First email on the page by position is the buyer's; webmaster@ is a
        # site-furniture address and reaches nobody who can answer a question.
        self.assertNotIn("webmaster", self.got["email"])

    def test_finds_and_normalises_the_phone(self):
        self.assertEqual(self.got["phone"], "(417) 864-1976")

    def test_finds_the_contact_name(self):
        self.assertEqual(self.got["contact"], "Marla Whitfield")

    def test_a_label_is_not_mistaken_for_a_person(self):
        for text in ("Contact: The City Clerk", "Contact: Purchasing Department",
                     "Contact the City Of Springfield"):
            with self.subTest(text=text):
                self.assertEqual(bs.parse_contact(text)["contact"], "")

    def test_a_partial_number_is_never_returned(self):
        # Half a phone number is worse than none — it gets dialled.
        for text in ("Call 417-864", "ext. 1976", "Phone: 864-1976"):
            with self.subTest(text=text):
                self.assertEqual(bs.parse_contact(text)["phone"], "")

    def test_common_real_world_phone_shapes(self):
        for text, want in (("(417) 864-1976", "(417) 864-1976"),
                           ("417.864.1976", "(417) 864-1976"),
                           ("+1 417 864 1976", "(417) 864-1976"),
                           ("Tel: 417-864-1976 x22", "(417) 864-1976")):
            with self.subTest(text=text):
                self.assertEqual(bs.parse_contact(text)["phone"], want)

    def test_missing_details_come_back_blank_not_absent(self):
        got = bs.parse_contact("Sidewalk work. No contact given.")
        self.assertEqual(got, {"contact": "", "email": "", "phone": ""})

    def test_garbage_input_does_not_raise(self):
        for bad in ("", None, 12345, "<<<>>>"):
            with self.subTest(bad=bad):
                bs.parse_contact(bad)


class DetailScopeTests(unittest.TestCase):
    def test_pulls_the_labelled_description(self):
        scope = bs.detail_scope(DETAIL_PAGE)
        self.assertIn("4,800 linear feet", scope)
        self.assertIn("ADA-compliant curb ramps", scope)

    def test_stops_before_the_next_field(self):
        scope = bs.detail_scope(DETAIL_PAGE)
        self.assertNotIn("Marla", scope)
        self.assertNotIn("12/1/2026", scope)

    def test_no_labelled_description_yields_nothing(self):
        self.assertEqual(bs.detail_scope("<p>Just a title and a date.</p>"), "")

    def test_garbage_input_does_not_raise(self):
        for bad in ("", None, 12345):
            with self.subTest(bad=bad):
                self.assertEqual(bs.detail_scope(bad), "")


# Trimmed from a live fetch of a springfieldmo.gov bid detail page (Aug 2026).
# DETAIL_PAGE above was reconstructed from memory and, checked against the
# real page, got several things wrong at once: labels and values sit in
# separate table rows rather than "Label: value" inline; the site's
# Cloudflare email protection replaces a mailto with an obfuscated
# data-cfemail attribute and a literal "[email protected]" placeholder, so
# plain-text scanning finds nothing; and the free-text body mentions a
# different phone number and a different "contact" well before the actual
# labelled Contact Person and Closing Date fields. The buyer's real address
# was swapped for a synthetic one re-encoded with the same Cloudflare cipher.
LIVE_DETAIL_HTML = """
<table summary="Bid Details">
<tr><td style="background-color:#fafafa"><span class="BidListHeader">Description:</span></td></tr>
<tr><td><span class="BidDetail">ADDENDUM 1 ISSUED ON MONDAY, AUGUST 10, 2026. THE REVISED DUE DATE IS
TUESDAY, AUGUST 18, 2026. LEGAL NOTICE: REQUEST FOR PROPOSAL #009-2027 The City
of Springfield will electronically accept submitted proposals through its
e-bidding service provider, Euna OpenBids (formerly DemandStar), from
qualified persons or firms interested in providing the following: SKATE AND
PRO SHOP CONCESSIONAIRE at the Jordan Valley Ice Park. VENDOR NOTE 1: To
register with Euna OpenBids go to https://www.demandstar.com/registration.
VENDOR NOTE 2: For information on how to navigate Euna OpenBids go to
https://network.demandstar.com/agency-support-2/. VENDOR NOTE 3: If you have
issues registering or uploading a proposal, please contact Euna OpenBids
toll-free at (866) 273-1863. Euna OpenBids office hours are 8:00 A.M. to 7:00
P.M. Central Time, Monday through Friday. You can also contact the Division
of Purchases at (417) 864-1620 or the Buyer stated on the Title-Signature
Page of this solicitation between 8:00 A.M. and 5:00 P.M. Central Time,
Monday through Friday. It is strongly recommended that Offerors register as
soon as possible to ensure your ability to respond by the due date and
time.</span></td></tr>
<tr><td style="background-color:#fafafa"><span class="BidListHeader">Publication Date/Time:</span></td></tr>
<tr><td><span class="BidDetail">7/16/2026 10:30 AM</span></td></tr>
<tr><td style="background-color:#fafafa"><span class="BidListHeader">Closing Date/Time:</span></td></tr>
<tr><td><span class="BidDetail">8/20/2026 3:00 PM</span></td></tr>
<tr><td style="background-color:#fafafa"><span class="BidListHeader">Contact Person:</span></td></tr>
<tr><td><span class="BidDetail">Jordan Reyes <BR><a href="/cdn-cgi/l/email-protection" class="__cf_email__" data-cfemail="4220373b273002273a232f322e276f212b363b6c252d34">[email&#160;protected]</a> Phone: (417) 864-1955</span></td></tr>
</table>
"""


class RealDetailPageTests(unittest.TestCase):
    """The structural quirks a live CivicPlus detail page actually has, none
    of which the hand-reconstructed DETAIL_PAGE fixture above exhibited."""

    def test_decodes_a_cloudflare_obfuscated_email(self):
        # The visible text is literally the string "[email protected]" — a
        # plain-text scan of it can never find a real address.
        self.assertNotIn("@", "[email protected]")
        self.assertEqual(
            bs.parse_contact(LIVE_DETAIL_HTML)["email"], "buyer@example-city.gov")

    def test_the_labelled_contact_wins_over_a_prose_mention(self):
        # "please contact Euna OpenBids toll-free at ..." appears earlier in
        # the document than the real "Contact Person:" field.
        self.assertEqual(bs.parse_contact(LIVE_DETAIL_HTML)["contact"], "Jordan Reyes")

    def test_the_phone_near_the_contact_wins_over_ones_in_the_prose(self):
        # Two other phone numbers (Euna OpenBids, the Division of Purchases)
        # sit earlier in the description than the one printed right next to
        # the named contact.
        self.assertEqual(bs.parse_contact(LIVE_DETAIL_HTML)["phone"], "(417) 864-1955")

    def test_the_labelled_closing_date_wins_over_a_different_date_in_prose(self):
        # The description opens with "THE REVISED DUE DATE IS ... AUGUST 18"
        # — a different date than the real "Closing Date/Time:" field below.
        self.assertEqual(bs.detail_deadline(LIVE_DETAIL_HTML), "8/20/2026")

    def test_a_description_longer_than_600_characters_is_still_captured(self):
        scope = bs.detail_scope(LIVE_DETAIL_HTML)
        self.assertGreater(len(scope), 600)
        self.assertIn("SKATE AND PRO SHOP CONCESSIONAIRE", scope)
        self.assertNotIn("Publication Date", scope)


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


class DetailDeadlineTests(unittest.TestCase):
    """A bid with no deadline gets no urgency ranking and cannot be recognised
    as expired — which is how four 2025 listings showed as open in 2026."""

    def test_pulls_the_closing_date_from_a_posting(self):
        self.assertEqual(bs.detail_deadline(DETAIL_PAGE), "12/1/2026")

    def test_handles_the_label_shapes_real_sites_use(self):
        for html, want in (
            ("<p>Bid Opening Date/Time: March 3, 2026 10:00 AM</p>", "March 3, 2026"),
            ("<p>Due Date and Time: 2026-11-20</p>", "2026-11-20"),
            ("<p>Bids due by 12/15/2026 at 2:00 p.m.</p>", "12/15/2026"),
            ("<p>Proposals due January 8, 2027</p>", "January 8, 2027"),
        ):
            with self.subTest(html=html):
                self.assertEqual(bs.detail_deadline(html), want)

    def test_a_publication_date_is_not_a_deadline(self):
        self.assertEqual(
            bs.detail_deadline("<p>Publication Date/Time: 7/1/2026 8:00 AM</p>"), "")

    def test_no_date_yields_nothing(self):
        self.assertEqual(bs.detail_deadline("<p>Sidewalk work citywide.</p>"), "")

    def test_garbage_input_does_not_raise(self):
        for bad in ("", None, 12345):
            with self.subTest(bad=bad):
                self.assertEqual(bs.detail_deadline(bad), "")
