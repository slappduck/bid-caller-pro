"""Town bid notices re-posted on a state local-programs page.

Arkansas DOT posts each town's own notice for federal-aid local work
(sidewalks, trails) on one page. Two of the towns on the captured copy,
Tyronza and Lake Village, have no bid page of their own that discovery could
find, so this page is the only place their sidewalk jobs show up online.
The fixture is the real page body captured 2026-11-02.
"""
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bid_sources  # noqa: E402
import license_server as ls  # noqa: E402


def setUpModule():
    ls._local_source_cache.clear()


class _FreshCache(unittest.TestCase):
    def setUp(self):
        ls._local_source_cache.clear()

FIXTURE = os.path.join(ROOT, "data", "recall_fixtures", "ardot_local_projects.html")
URL = ls.LOCAL_NOTICE_SOURCES["AR"][0][0]


def page():
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


def rows():
    return bid_sources.parse_local_agency_notices(page(), "AR", URL)


class ParseTests(unittest.TestCase):
    def by_place(self):
        return {r.get("town") or r.get("county"): r for r in rows()}

    def test_the_sidewalk_jobs_are_read(self):
        got = self.by_place()
        self.assertEqual(got["Lake Village"]["title"],
                         "Lake Village Rodney Angel Sports Complex Sidewalks")
        self.assertEqual(got["Lake Village"]["deadline"], "November 17, 2026")
        self.assertEqual(got["Tyronza"]["title"],
                         "Tyronza Hwy. 118 Sidewalk Phase 1 (Main St)")
        self.assertEqual(got["Tyronza"]["deadline"], "November 10, 2026")
        self.assertEqual(got["Alma"]["title"], "Alma Southside Sidewalk Improvements")

    def test_abbreviations_are_spelled_out_so_the_trade_filter_sees_them(self):
        """"Ped. Impvts." is a sidewalk job the filter could not recognise."""
        self.assertEqual(self.by_place()["Jonesboro"]["title"],
                         "Jonesboro Marion Berry Parkway Pedestrian Improvements")

    def test_a_county_letting_is_kept_as_a_county(self):
        r = self.by_place()["Craighead"]
        self.assertNotIn("town", r)
        self.assertEqual(r["deadline"], "October 27, 2026")   # "October 27 th , 2026"
        self.assertEqual(r["title"],
                         "Crowley’s Ridge Gravel Trail Respite Hub (Craighead County)")

    def test_other_trades_are_left_out(self):
        self.assertNotIn("Lowell", self.by_place())          # traffic signals

    def test_an_addendum_joins_the_notice_above_it(self):
        lr = self.by_place()["Little Rock"]
        self.assertEqual([d["name"] for d in lr["documents"]],
                         ["Bid notice", "Addendum 1", "Addendum 2"])
        self.assertFalse(any(r["agency"].lower().startswith("addend") for r in rows()))

    def test_each_row_links_to_its_own_notice(self):
        for r in rows():
            self.assertTrue(r["url"].startswith("https://media.ark.org/ardot/"), r["url"])
            self.assertEqual(r["source"], "state_local")


class PlacementTests(unittest.TestCase):
    CENTER = {"lat": 35.3859, "lon": -94.3985, "state": "AR"}   # Fort Smith

    def place(self, row, radius=50):
        grouped, coords = {}, {}
        with patch("license_server._city_coords", return_value=[35.4776, -94.2219]):
            kept = ls._place_local_notice(grouped, row, self.CENTER, radius, coords, {})
        return kept, grouped, coords

    def alma(self, **kw):
        r = next(x for x in rows() if x.get("town") == "Alma")
        r.update(kw)
        return r

    def test_placed_at_the_town(self):
        with patch("license_server._is_open_bid", return_value=True):
            kept, grouped, coords = self.place(self.alma())
        self.assertIsNotNone(kept)
        self.assertIn("Alma", grouped)
        self.assertAlmostEqual(kept["miles"], 12, delta=1)
        self.assertIn("Alma", coords)

    def test_out_of_radius_is_dropped(self):
        with patch("license_server._is_open_bid", return_value=True):
            kept, grouped, _ = self.place(self.alma(), radius=5)
        self.assertIsNone(kept)
        self.assertEqual(grouped, {})

    def test_a_closed_notice_is_dropped(self):
        kept, grouped, _ = self.place(self.alma(deadline="October 1, 2020"))
        self.assertIsNone(kept)

    def test_another_states_town_is_labelled_with_its_state(self):
        center = {"lat": 37.209, "lon": -93.2923, "state": "MO"}
        with patch("license_server._city_coords", return_value=[36.3353, -93.4524]), \
             patch("license_server._is_open_bid", return_value=True):
            grouped = {}
            r = next(x for x in rows() if x.get("town") == "Green Forest")
            ls._place_local_notice(grouped, r, center, 100, {}, {})
        self.assertIn("Green Forest, AR", grouped)


class ScanWiringTests(_FreshCache):
    def test_a_scan_touching_arkansas_reads_the_page(self):
        fetched = []

        def fake_fetch(url, timeout=None):
            fetched.append(url)
            return page(), "ok"

        center = {"lat": 35.3859, "lon": -94.3985, "state": "AR"}
        grouped, stats = {}, {}
        with patch("license_server._state_sources", return_value={}), \
             patch("license_server._fetch_page", side_effect=fake_fetch), \
             patch("license_server._city_coords", return_value=[35.4776, -94.2219]), \
             patch("license_server._is_open_bid", return_value=True), \
             patch("license_server.counties.states_within", return_value=["AR"]):
            ls._run_state_sources(center, 50, grouped, {}, stats)
        self.assertEqual(fetched, [URL])
        self.assertEqual(stats.get("state_local_rows_read"), len(rows()))
        self.assertIn("Alma Southside Sidewalk Improvements",
                      [b["title"] for v in grouped.values() for b in v])

    def test_a_scan_elsewhere_does_not_fetch_it(self):
        with patch("license_server._state_sources", return_value={}), \
             patch("license_server._fetch_page") as f, \
             patch("license_server.counties.states_within", return_value=["NE"]):
            ls._run_state_sources({"lat": 41.25, "lon": -95.93, "state": "NE"}, 25, {}, {}, {})
        f.assert_not_called()


if __name__ == "__main__":
    unittest.main()


MO_FIXTURE = os.path.join(ROOT, "data", "recall_fixtures", "modot_local_projects.html")
MO_URL = ls.LOCAL_NOTICE_SOURCES["MO"][0][0]


def mo_rows(today=None):
    import datetime
    with open(MO_FIXTURE, encoding="utf-8") as f:
        html = f.read()
    return bid_sources.parse_lpa_table(html, "MO", MO_URL,
                                       today=today or datetime.date(2026, 11, 2))


class MissouriTableTests(unittest.TestCase):
    """MoDOT's table of locally administered federal-aid bid notices."""

    def by_place(self):
        out = {}
        for r in mo_rows():
            out.setdefault(r.get("town") or (r.get("county") + " County"), []).append(r)
        return out

    def test_the_open_sidewalk_jobs_are_read(self):
        got = self.by_place()
        self.assertEqual(got["Creve Coeur"][0]["title"], "New Ballas Sidewalk Project")
        self.assertEqual(got["Creve Coeur"][0]["deadline"], "November 19, 2026")
        self.assertEqual(got["Desloge"][0]["title"], "Sidewalk improvements to N. Desloge Drive")
        self.assertIn("Milan", got)
        self.assertIn("Ozark", got)

    def test_the_district_is_not_mistaken_for_the_agency(self):
        """"NW District, City of Milan, ..." is Milan's job, not a district's."""
        for r in mo_rows():
            self.assertNotIn("district", (r.get("town") or r.get("county") or "").lower())

    def test_a_bare_town_name_is_a_town(self):
        """"Central District, New Bloomfield, construction of ...\""""
        self.assertIn("New Bloomfield", self.by_place())

    def test_the_split_county_name_is_rejoined(self):
        """The table prints "St, Francois County"."""
        self.assertIn("St. Francois County", self.by_place())

    def test_addenda_join_their_notice(self):
        nb = self.by_place()["New Bloomfield"][0]
        self.assertEqual([d["name"] for d in nb["documents"]],
                         ["Bid notice", "Addendum 1", "Addendum 2"])
        self.assertFalse(any(r["title"].lower().startswith("addendum") for r in mo_rows()))

    def test_a_shared_use_path_counts_as_this_trade(self):
        self.assertIn("Kearney", self.by_place())

    def test_other_trades_are_left_out(self):
        titles = " ".join(r["title"] for r in mo_rows()).lower()
        self.assertNotIn("flashing beacon", titles)

    def test_an_old_undated_notice_is_dropped(self):
        """Smith Park Trail was posted 3/30/25 with no letting date."""
        self.assertFalse(any("Smith Park" in r["title"] for r in mo_rows()))

    def test_entities_are_decoded(self):
        for r in mo_rows():
            self.assertNotIn("&amp;", r["title"])
            self.assertNotIn("&nbsp;", r["title"])

    def test_each_row_links_to_its_notice(self):
        for r in mo_rows():
            self.assertTrue(r["url"].startswith("https://www.modot.org/media/"), r["url"])

    def test_closed_rows_are_dropped_before_any_geocoding(self):
        """Hundreds of past notices sit in the table; geocoding each would
        spend the scan's budget."""
        closed = dict(mo_rows()[0], deadline="October 1, 2020")
        with patch("license_server._city_coords") as geo:
            ls._place_local_notice({}, closed, {"lat": 37.2, "lon": -93.3, "state": "MO"},
                                   100, {}, {})
        geo.assert_not_called()


IA_FIXTURE = os.path.join(ROOT, "data", "recall_fixtures", "iowadot_notice_to_bidders.txt")
IA_URL = ls.LOCAL_NOTICE_SOURCES["IA"][0][0]


def ia_rows():
    with open(IA_FIXTURE, encoding="utf-8") as f:
        return bid_sources.parse_iowa_notice_to_bidders(f.read(), "IA", IA_URL)


class IowaNoticeToBiddersTests(_FreshCache):
    """Iowa DOT's letting, which carries county and city work with quantities."""

    def find(self, ident):
        return next(r for r in ia_rows() if r["call"] == ident)

    def test_the_letting_date_is_the_due_date(self):
        self.assertTrue(all(r["deadline"] == "November 17, 2026" for r in ia_rows()))

    def test_a_sidewalk_job_carries_its_quantities(self):
        r = self.find("52-3715-674")
        self.assertEqual(r["county"], "Johnson")
        self.assertIn("Iowa City", r["title"])
        self.assertIn("5,231 sq yd Recreational Trail, PCC", r["scope"])
        self.assertIn("609 sq yd Sidewalk, PCC", r["scope"])

    def test_county_projects_are_included(self):
        """Proposal IDs with C in the middle are let for a county."""
        r = self.find("08-C008-091")
        self.assertEqual(r["county"], "Boone")
        self.assertIn("22,084 sq yd Pavement, PCC", r["scope"])

    def test_a_job_with_no_flatwork_is_left_out(self):
        """A paved shoulder alone is usually asphalt, not this trade."""
        ids = {r["call"] for r in ia_rows()}
        self.assertNotIn("10-1503-079", ids)      # deck overlay, paved shoulders only
        self.assertNotIn("40-1757-024", ids)      # HMA resurfacing, paved shoulders only

    def test_asphalt_items_do_not_count(self):
        for r in ia_rows():
            self.assertNotIn("HMA", r["scope"].split("Flatwork items:")[1], r["call"])

    def test_abbreviations_read_cleanly(self):
        for r in ia_rows():
            self.assertNotIn("P.c.c", r["scope"])
            self.assertNotIn("Ccs", r["title"])

    def test_documents_point_at_the_notice_and_bid_express(self):
        docs = self.find("52-3715-674")["documents"]
        self.assertEqual(docs[0]["url"], IA_URL)
        self.assertIn("bidx.com", docs[1]["url"])

    def test_a_scan_reads_the_pdf_through_the_document_fetcher(self):
        with open(IA_FIXTURE, encoding="utf-8") as f:
            text = f.read()
        grouped, stats = {}, {}
        with patch("license_server._state_sources", return_value={}), \
             patch("license_server._fetch_document", return_value=(b"%PDF-", "application/pdf", "ok")), \
             patch("license_server._pdf_all_text", return_value=text), \
             patch("license_server._is_open_bid", return_value=True), \
             patch("license_server.counties.states_within", return_value=["IA"]):
            ls._run_state_sources({"lat": 41.661, "lon": -91.530, "state": "IA"}, 30,
                                  grouped, {}, stats)
        self.assertEqual(stats.get("state_local_rows_read"), len(ia_rows()))
        self.assertIn("Johnson County, IA", grouped)


IL_FIXTURE = os.path.join(ROOT, "data", "recall_fixtures", "idot_contractors_bulletins.txt")


def il_rows():
    with open(IL_FIXTURE, encoding="utf-8") as f:
        text = f.read().split("\n", 1)[1]           # drop the provenance line
    return bid_sources.parse_il_contractors_bulletin(text, "IL", "x")


class IllinoisBulletinTests(unittest.TestCase):
    """IDOT's weekly bulletin of contracts towns and counties let themselves."""

    def by_place(self):
        out = {}
        for r in il_rows():
            out.setdefault(r.get("town") or r.get("county") + " County", []).append(r)
        return out

    def test_town_jobs_are_read_with_their_letting_date(self):
        got = self.by_place()
        self.assertEqual(got["Joliet"][0]["deadline"], "November 5, 2026")
        self.assertIn("PCC curb", got["Joliet"][0]["title"])
        self.assertIn("Kinmundy", got)                   # curb & gutter at a crossing

    def test_a_county_is_told_from_a_town_by_the_district_list(self):
        got = self.by_place()
        self.assertIn("Rock Island County", got)         # printed bare, "Rock Island"
        self.assertIn("Kane County", got)                # printed "Kane County DOT"

    def test_each_row_links_the_bulletin_it_came_from(self):
        for r in il_rows():
            self.assertTrue(r["url"].endswith(".pdf"), r["url"])
        joliet = self.by_place()["Joliet"][0]
        self.assertIn("october-22", joliet["url"])

    def test_a_notice_in_two_bulletins_is_kept_once(self):
        self.assertEqual(len(self.by_place()["Elgin"]), 1)

    def test_other_trades_are_left_out(self):
        titles = " ".join(r["title"].lower() for r in il_rows())
        self.assertNotIn("tree remov", titles)
        self.assertNotIn("traffic signal detection", titles)
        self.assertNotIn("sign material", titles)

    def test_pdf_glyph_noise_is_removed(self):
        for r in il_rows():
            self.assertFalse(any(ord(c) < 32 or 0x7f <= ord(c) <= 0x9f for c in r["title"]), r["title"])
            self.assertNotIn("¶", r["scope"])

    def test_the_scan_reads_the_newest_bulletins_from_the_index(self):
        index = ('<a href="/content/dam/x/local-roads/bulletins/october-29.pdf">a</a>'
                 '<a href="/content/dam/x/local-roads/bulletins/october-22.pdf">b</a>'
                 '<a href="/content/dam/x/local-roads/bulletins/october-15.pdf">c</a>'
                 '<a href="/content/dam/x/local-roads/bulletins/october-8.pdf">d</a>'
                 '<a href="/content/dam/x/local-roads/bulletins/october-1.pdf">e</a>')
        got = []
        with patch("license_server._fetch_document",
                   side_effect=lambda u: (got.append(u) or (b"%PDF", "application/pdf", "ok"))), \
             patch("license_server._pdf_all_text", return_value="PART II"):
            text = ls._il_bulletin_text(index, "https://idot.illinois.gov/a/b.html")
        self.assertEqual(len(got), ls.IL_BULLETINS_READ)
        self.assertTrue(got[0].startswith("https://idot.illinois.gov/content/dam/"))
        self.assertIn(bid_sources.IL_BULLETIN_MARK + got[0], text)


class LocalSourceCacheTests(_FreshCache):
    def test_a_second_scan_reuses_the_fetched_text(self):
        calls = []

        def fake_fetch(url, timeout=None):
            calls.append(url)
            return page(), "ok"

        with patch("license_server._state_sources", return_value={}), \
             patch("license_server._fetch_page", side_effect=fake_fetch), \
             patch("license_server._city_coords", return_value=[35.4776, -94.2219]), \
             patch("license_server.counties.states_within", return_value=["AR"]):
            for _ in range(2):
                ls._run_state_sources({"lat": 35.39, "lon": -94.40, "state": "AR"}, 50, {}, {}, {})
        self.assertEqual(calls, [URL])
