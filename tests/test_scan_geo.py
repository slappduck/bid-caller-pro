"""Tests for the geo/placement half of the scan pipeline.

These cover three bugs that quietly cost the scan real bids rather than
throwing anything:

  * every bid was geocoded against the search centre's state, so a wide
    radius crossing a state line dropped all the out-of-state bids it found;
  * a transient geocoder outage was cached as a permanent failure, silently
    blacklisting that city for every future scan;
  * deadlines written as prose ("Due by 12/01/2026 at 2:00 PM") were
    unparseable, costing the bid its urgency ranking and letting expired
    listings keep showing as open.
"""
import datetime
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


# Aurora MO sits close enough to the state line that a 100mi radius reaches
# well into Arkansas — the exact situation the old code got wrong.
CENTER = {"lat": 36.9709, "lon": -93.7180, "city": "Aurora", "state": "MO"}
REAL_PLACES = {
    ("Aurora", "MO"): (36.9709, -93.7180),
    ("Bentonville", "AR"): (36.3729, -94.2088),
    ("Springfield", "MO"): (37.2090, -93.2923),
    ("Chicago", "IL"): (41.8781, -87.6298),
}


def _fake_geo(city, state):
    """Stand-in for zippopotam: only real (city, state) pairs resolve."""
    hit = REAL_PLACES.get((city, state))
    if not hit:
        return None
    return {"lat": hit[0], "lon": hit[1], "city": city, "state": state}


class SplitCityStateTests(unittest.TestCase):
    def test_abbreviation(self):
        self.assertEqual(ls._split_city_state("Bentonville, AR"), ("Bentonville", "AR"))

    def test_full_state_name(self):
        self.assertEqual(ls._split_city_state("Bentonville, Arkansas"), ("Bentonville", "AR"))

    def test_bare_city(self):
        self.assertEqual(ls._split_city_state("Bentonville"), ("Bentonville", ""))

    def test_trailing_noise_is_not_a_state(self):
        self.assertEqual(ls._split_city_state("Bentonville, somewhere"), ("Bentonville", ""))

    def test_empty(self):
        self.assertEqual(ls._split_city_state(""), ("", ""))


class PlaceBidStateTests(unittest.TestCase):
    def setUp(self):
        self.grouped, self.coords, self.db = {}, {}, {}

    def _place(self, bid, **kw):
        with patch.object(ls, "_geo_from_city", side_effect=_fake_geo):
            ls._place_bid(self.grouped, bid, CENTER, 100, self.db,
                          city_coords=self.coords, **kw)

    def test_out_of_state_bid_is_kept_when_the_ai_states_the_state(self):
        self._place({"title": "Bentonville Sidewalk & ADA Ramps",
                     "city": "Bentonville, AR", "status": "Open"})
        self.assertEqual(list(self.grouped), ["Bentonville, AR"])

    def test_out_of_state_bid_is_kept_via_the_anchor_towns_state(self):
        # The AI didn't restate the location; the anchor town it came from did.
        self._place({"title": "Sidewalk program", "status": "Open"},
                    default_city="Bentonville", default_state="AR")
        self.assertEqual(list(self.grouped), ["Bentonville, AR"])

    def test_in_state_bid_keeps_a_bare_city_label(self):
        self._place({"title": "Sidewalk program", "city": "Springfield", "status": "Open"})
        self.assertEqual(list(self.grouped), ["Springfield"])

    def test_out_of_state_label_carries_the_state(self):
        self._place({"title": "x", "city": "Bentonville, AR", "status": "Open"})
        self.assertIn("Bentonville, AR", self.coords)

    def test_bid_outside_the_radius_is_still_dropped(self):
        self._place({"title": "x", "city": "Chicago, IL", "status": "Open"})
        self.assertEqual(self.grouped, {})

    def test_unresolvable_city_is_dropped(self):
        self._place({"title": "x", "city": "Nowheresville, ZZ", "status": "Open"})
        self.assertEqual(self.grouped, {})

    def test_bid_with_no_location_at_all_is_dropped(self):
        self._place({"title": "x", "status": "Open"})
        self.assertEqual(self.grouped, {})

    def test_stated_state_wins_over_the_centre(self):
        # "Springfield" exists in both MO and IL. Saying IL must not silently
        # resolve to the Missouri one just because that's the centre's state.
        self._place({"title": "x", "city": "Springfield, IL", "status": "Open"})
        self.assertEqual(self.grouped, {})  # the IL one is >100mi away


class PlaceNameResolutionTests(unittest.TestCase):
    """Public bodies rarely call themselves by a bare city name, and the AI is
    told to copy the location exactly as written. zippopotam only knows names
    that appear in ZIP data, so counties, townships and school districts —
    core buyers for this trade — resolved to nothing and their bids were
    thrown away."""

    def test_normalizes_authority_names_to_a_place(self):
        cases = {
            "City of O'Fallon": "O'Fallon",
            "Town of Cary": "Cary",
            "Village of Oak Park": "Oak Park",
            "Township of Montclair": "Montclair",
            "County of Greene": "Greene",
            "Aurora R-VIII School District": "Aurora",
            "Springfield Public Schools": "Springfield",
            "Cambridge Housing Authority": "Cambridge",
            "Greene County Road District 3": "Greene County",
            "Springfield": "Springfield",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(ls._normalize_place(raw), want)

    def test_normalizing_junk_yields_nothing(self):
        for raw in ("", "   ", None, "City of "):
            with self.subTest(raw=raw):
                self.assertEqual(ls._normalize_place(raw), "")

    def test_falls_back_to_the_normalized_form(self):
        # zippopotam knows "O'Fallon" but not "City of O'Fallon".
        seen = []

        def zippo(city, state):
            seen.append(city)
            return (38.81, -90.70) if city == "O'Fallon" else None

        with patch.object(ls, "_zippopotam_city", side_effect=zippo), \
             patch.object(ls, "_nominatim_place", return_value=None):
            got = ls._geo_from_city("City of O'Fallon", "MO")
        self.assertIsNotNone(got)
        self.assertEqual(seen, ["City of O'Fallon", "O'Fallon"])

    def test_counties_resolve_through_the_second_geocoder(self):
        with patch.object(ls, "_zippopotam_city", return_value=None), \
             patch.object(ls, "_nominatim_place", return_value=(37.25, -93.35)) as nom:
            got = ls._geo_from_city("Greene County", "MO")
        self.assertEqual((got["lat"], got["lon"]), (37.25, -93.35))
        self.assertTrue(nom.called)

    def test_the_fast_geocoder_is_preferred_when_it_answers(self):
        with patch.object(ls, "_zippopotam_city", return_value=(37.2, -93.3)), \
             patch.object(ls, "_nominatim_place") as nom:
            ls._geo_from_city("Springfield", "MO")
        nom.assert_not_called()

    def test_unresolvable_everywhere_is_still_none(self):
        with patch.object(ls, "_zippopotam_city", return_value=None), \
             patch.object(ls, "_nominatim_place", return_value=None):
            self.assertIsNone(ls._geo_from_city("Nowhere At All", "MO"))


class SearchTownFallbackTests(unittest.TestCase):
    """A bid found by searching a specific town, naming a place no gazetteer
    resolves, is anchored to that town rather than discarded."""

    def setUp(self):
        self.grouped, self.coords, self.db, self.stats = {}, {}, {}, {}

    def _place(self, bid, **kw):
        with patch.object(ls, "_geo_from_city", side_effect=_fake_geo):
            ls._place_bid(self.grouped, bid, CENTER, 100, self.db,
                          city_coords=self.coords, stats=self.stats, **kw)

    def test_unresolvable_place_is_kept_when_a_search_town_is_known(self):
        self._place({"title": "Road district curb work", "city": "Greene County Road District 3",
                     "status": "Open"},
                    default_state="MO", fallback_coords=(36.97, -93.71))
        self.assertEqual(list(self.grouped), ["Greene County Road District 3"])
        self.assertEqual(self.stats.get("placed_by_search_town"), 1)

    def test_unresolvable_place_is_still_dropped_without_a_search_town(self):
        self._place({"title": "x", "city": "Nowheresville", "status": "Open"})
        self.assertEqual(self.grouped, {})
        self.assertEqual(self.stats.get("unresolvable_place"), 1)

    def test_a_bid_naming_another_state_is_never_anchored_here(self):
        # Pinning an explicitly-Illinois bid to a Missouri town would present
        # work hundreds of miles away as local.
        self._place({"title": "x", "city": "Nowheresville, IL", "status": "Open"},
                    default_state="MO", fallback_coords=(36.97, -93.71))
        self.assertEqual(self.grouped, {})
        self.assertEqual(self.stats.get("unresolvable_place"), 1)

    def test_the_funnel_records_why_bids_were_lost(self):
        self._place({"title": "kept", "city": "Springfield", "status": "Open"})
        self._place({"title": "no location", "status": "Open"})
        self._place({"title": "too far", "city": "Chicago, IL", "status": "Open"})
        self._place({"title": "kept", "city": "Springfield", "status": "Open"})  # duplicate
        self.assertEqual(self.stats.get("kept"), 1)
        self.assertEqual(self.stats.get("no_location"), 1)
        self.assertEqual(self.stats.get("out_of_radius"), 1)
        self.assertEqual(self.stats.get("duplicate"), 1)


class PageBudgetTests(unittest.TestCase):
    """A fixed page budget has to buy the widest coverage it can. Left in
    discovery order, the site: queries meant one aggregator could swallow the
    whole allowance while the agency's own posting was never opened."""

    @staticmethod
    def _items(*urls):
        return [{"url": u, "content": ""} for u in urls]

    def test_bidnet_keeps_precedence(self):
        out = ls._prioritize_pages(self._items(
            "https://city.example/bids",
            "https://www.bidnetdirect.com/x/solicitations/open-bids/1"))
        self.assertIn("bidnetdirect", out[0]["url"])

    def test_government_domains_outrank_aggregators(self):
        out = ls._prioritize_pages(self._items(
            "https://aggregator.example/1",
            "https://springfield.mo.gov/bids",
            "https://greene.co.us/rfp"))
        self.assertTrue(ls._GOV_DOMAIN_RE.search(ls._page_domain(out[0]["url"])))

    def test_one_domain_cannot_take_the_whole_budget(self):
        urls = [f"https://aggregator.example/{i}" for i in range(10)]
        urls.append("https://someblog.example/post")
        out = ls._prioritize_pages(self._items(*urls))
        head = out[:ls.MAX_PAGES_PER_DOMAIN + 1]
        self.assertIn("someblog.example",
                      [ls._page_domain(i["url"]) for i in head])

    def test_over_quota_pages_are_deferred_not_discarded(self):
        urls = [f"https://aggregator.example/{i}" for i in range(10)]
        out = ls._prioritize_pages(self._items(*urls))
        self.assertEqual(len(out), len(urls))

    def test_unparseable_urls_do_not_break_ordering(self):
        out = ls._prioritize_pages(self._items("not a url", "https://x.gov/a"))
        self.assertEqual(len(out), 2)


class AnchorTownTests(unittest.TestCase):
    CENTER = {"lat": 36.97, "lon": -93.72, "city": "Aurora", "state": "MO"}

    def _anchors(self, radius):
        # Name each probe point after where it landed so none collide.
        def rev(lat, lon):
            return (f"T{round(lat, 3)}_{round(lon, 3)}", "MO")
        with patch.object(ls, "_reverse_geocode_city", side_effect=rev):
            return ls._nearby_anchor_towns(self.CENTER, radius)

    def test_tight_radius_searches_only_the_centre(self):
        self.assertEqual(self._anchors(25), [])

    def test_anchors_carry_their_coordinates(self):
        for town in self._anchors(75):
            self.assertEqual(len(town), 4)
            self.assertIsInstance(town[2], float)

    def test_a_wide_radius_uses_two_rings(self):
        anchors = self._anchors(125)
        dists = {round(ls._miles_between(self.CENTER["lat"], self.CENTER["lon"], a[2], a[3]))
                 for a in anchors}
        self.assertGreater(len(dists), 1, f"all anchors at one distance: {dists}")

    def test_wider_radius_searches_more_towns(self):
        self.assertGreater(len(self._anchors(125)), len(self._anchors(50)))

    def test_anchor_count_is_capped(self):
        self.assertLessEqual(len(self._anchors(500)), ls.MAX_ANCHOR_TOWNS)

    def test_the_centre_city_is_never_also_an_anchor(self):
        with patch.object(ls, "_reverse_geocode_city", return_value=("Aurora", "MO")):
            self.assertEqual(ls._nearby_anchor_towns(self.CENTER, 125), [])


class DuplicateBidTests(unittest.TestCase):
    """The same solicitation is routinely found on an aggregator AND the
    agency's own site. Both copies used to be kept, and since the client
    derives a bid's id from city + title + scope they rendered as two cards
    sharing one id — starring one appeared to star the other."""

    def setUp(self):
        self.grouped, self.db = {}, {}

    def _place(self, bid):
        with patch.object(ls, "_geo_from_city", side_effect=_fake_geo):
            ls._place_bid(self.grouped, bid, CENTER, 100, self.db)

    def test_same_title_and_deadline_in_one_town_is_kept_once(self):
        for url in ("https://aggregator.example/a", "https://city.example/b"):
            self._place({"title": "Sidewalk & ADA Ramp Program", "city": "Springfield",
                         "deadline": "2026-12-01", "status": "Open", "url": url})
        self.assertEqual(len(self.grouped["Springfield"]), 1)

    def test_whitespace_and_case_differences_still_count_as_duplicates(self):
        self._place({"title": "Sidewalk  Program", "city": "Springfield",
                     "deadline": "2026-12-01", "status": "Open"})
        self._place({"title": "sidewalk program", "city": "Springfield",
                     "deadline": "2026-12-01", "status": "Open"})
        self.assertEqual(len(self.grouped["Springfield"]), 1)

    def test_same_title_with_a_different_deadline_is_a_different_bid(self):
        # Annual re-lets share a title; the deadline is what separates them.
        self._place({"title": "Annual Sidewalk Program", "city": "Springfield",
                     "deadline": "2026-12-01", "status": "Open"})
        self._place({"title": "Annual Sidewalk Program", "city": "Springfield",
                     "deadline": "2026-12-15", "status": "Open"})
        self.assertEqual(len(self.grouped["Springfield"]), 2)

    def test_same_title_in_two_towns_is_not_deduplicated(self):
        self._place({"title": "Sidewalk Program", "city": "Springfield",
                     "deadline": "2026-12-01", "status": "Open"})
        self._place({"title": "Sidewalk Program", "city": "Aurora",
                     "deadline": "2026-12-01", "status": "Open"})
        self.assertEqual(sum(len(v) for v in self.grouped.values()), 2)

    def test_untitled_bids_are_not_collapsed_together(self):
        for scope in ("first job", "second job"):
            self._place({"title": "", "scope": scope, "city": "Springfield",
                         "deadline": "2026-12-01", "status": "Open"})
        self.assertEqual(len(self.grouped["Springfield"]), 2)


class OpenBidCountTests(unittest.TestCase):
    def test_closed_bids_are_not_counted_as_findings(self):
        self.assertTrue(ls._is_open_bid({"status": "Open"}))
        self.assertTrue(ls._is_open_bid({}))            # unstated means open
        self.assertFalse(ls._is_open_bid({"status": "Closed"}))
        self.assertFalse(ls._is_open_bid({"status": "closed"}))

    def test_planned_upcoming_items_are_not_open_bids(self):
        self.assertFalse(ls._is_open_bid({"status": "Planned"}))


class CityCoordsCacheTests(unittest.TestCase):
    def test_success_is_cached_and_not_refetched(self):
        db, calls = {}, []

        def counting(city, state):
            calls.append((city, state))
            return _fake_geo(city, state)

        with patch.object(ls, "_geo_from_city", side_effect=counting):
            first = ls._city_coords("Springfield", "MO", db)
            second = ls._city_coords("Springfield", "MO", db)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)

    def test_a_fresh_miss_is_not_immediately_refetched(self):
        db, calls = {}, []

        def always_fail(city, state):
            calls.append((city, state))
            return None

        with patch.object(ls, "_geo_from_city", side_effect=always_fail):
            ls._city_coords("Springfield", "MO", db)
            ls._city_coords("Springfield", "MO", db)
        self.assertEqual(len(calls), 1)

    def test_an_expired_miss_is_retried(self):
        stale = (datetime.datetime.now()
                 - datetime.timedelta(hours=ls.GEO_MISS_RETRY_HOURS + 1)).isoformat()
        db = {"geo_cache": {"springfield|MO": {"missed_at": stale}}}
        with patch.object(ls, "_geo_from_city", side_effect=_fake_geo):
            self.assertIsNotNone(ls._city_coords("Springfield", "MO", db))

    def test_a_legacy_none_entry_heals_instead_of_blacklisting_forever(self):
        # Caches written by the previous version stored a bare None and
        # returned it forever, permanently dropping every bid in that city.
        db = {"geo_cache": {"springfield|MO": None}}
        with patch.object(ls, "_geo_from_city", side_effect=_fake_geo):
            self.assertIsNotNone(ls._city_coords("Springfield", "MO", db))


class DeadlineParsingTests(unittest.TestCase):
    def test_parses_dates_embedded_in_prose(self):
        for text in ("2026-12-01",
                     "12/01/2026",
                     "December 1, 2026",
                     "Due by 12/01/2026 at 2:00 PM",
                     "Bids due December 1, 2026 at 2:00 p.m.",
                     "Thursday, December 1, 2026",
                     "12/01/2026 2:00 PM CST",
                     "Submit no later than 3:00 PM on 12/1/2026",
                     "Sept. 1, 2026"):
            with self.subTest(text=text):
                self.assertIsNotNone(ls._parse_deadline(text))

    def test_a_bare_year_is_still_not_a_date(self):
        # _apply_deadline_status handles these separately; parsing one as a
        # real date would break its stale-listing check.
        for text in ("FY2024", "Due 2025", "2025 cycle", "Closed as of 2024"):
            with self.subTest(text=text):
                self.assertIsNone(ls._parse_deadline(text))

    def test_prose_deadline_scores_the_same_as_the_iso_one(self):
        due = datetime.date.today() + datetime.timedelta(days=3)
        iso = {"title": "Sidewalk repair", "status": "Open", "deadline": due.isoformat()}
        prose = {"title": "Sidewalk repair", "status": "Open",
                 "deadline": f"Due by {due.strftime('%m/%d/%Y')} at 2:00 PM"}
        self.assertEqual(ls._score_bid(iso), ls._score_bid(prose))

    def test_expired_prose_deadline_marks_the_bid_closed(self):
        past = datetime.date.today() - datetime.timedelta(days=45)
        bid = {"status": "Open", "deadline": f"Due by {past.strftime('%m/%d/%Y')} at 2:00 PM"}
        ls._apply_deadline_status(bid)
        self.assertEqual(bid["status"], "Closed")


if __name__ == "__main__":
    unittest.main()


class CivicPlusPortalReadTests(unittest.TestCase):
    """A recognised platform is read structurally instead of being scraped and
    handed to the AI. That is what makes reading every known portal on every
    scan affordable, and it is not subject to a search budget at all."""

    LISTING = """
      <a href="/Bids.aspx?bidID=412">FY26 Sidewalk Improvements &amp; ADA Ramps</a>
      <span>Bid Opening: December 1, 2026</span>
      <a href="/Bids.aspx?bidID=413">Janitorial Services</a>
      <span>Closes: 11/02/2026</span>
      <a href="/Bids.aspx?bidID=414">Curb &amp; Gutter Replacement - Phase 2</a>
      <span>Due: 12/15/2026</span>
    """

    def setUp(self):
        self.grouped, self.coords, self.stats = {}, {}, {}
        self.cdb = {}
        self.pdb = {}

    def _run(self):
        import threading
        with patch.object(ls, "_fetch_page", return_value=(self.LISTING, "ok")), \
             patch.object(ls, "_ai_extract") as ai, \
             patch.object(ls, "_geo_from_city", side_effect=_fake_geo), \
             patch.object(ls.bid_portals, "get_portals", return_value=[
                 {"url": "https://www.springfieldmo.gov/Bids.aspx", "platform": "civicplus"}]), \
             patch.object(ls.bid_portals, "record_result"):
            got = ls._run_known_portals(
                "Springfield", "MO", "Springfield, MO", self.grouped, CENTER, 100,
                self.cdb, self.coords, threading.Lock(), self.pdb,
                default_city="Springfield", town_coords=(37.2, -93.3), stats=self.stats)
        return got, ai

    def test_bids_are_extracted_without_any_ai_call(self):
        got, ai = self._run()
        ai.assert_not_called()
        self.assertGreater(got, 0)

    def test_only_the_relevant_listings_are_kept(self):
        self._run()
        titles = [b["title"] for v in self.grouped.values() for b in v]
        self.assertEqual(len(titles), 2, titles)
        self.assertTrue(any("Sidewalk" in t for t in titles))
        self.assertTrue(any("Curb" in t for t in titles))
        self.assertFalse(any("Janitorial" in t for t in titles))

    def test_the_skipped_listing_is_counted_in_the_funnel(self):
        self._run()
        self.assertEqual(self.stats.get("filtered_not_niche"), 1)

    def test_closing_dates_survive_into_the_bid(self):
        self._run()
        bids = [b for v in self.grouped.values() for b in v]
        self.assertTrue(all(b["deadline"] for b in bids), bids)

    def test_links_are_absolute_so_open_works(self):
        self._run()
        bids = [b for v in self.grouped.values() for b in v]
        self.assertTrue(all(b["url"].startswith("https://") for b in bids), bids)

    def test_an_empty_page_records_a_failure_and_keeps_going(self):
        import threading
        with patch.object(ls, "_fetch_page", return_value=("", "ok")), \
             patch.object(ls, "_geo_from_city", side_effect=_fake_geo), \
             patch.object(ls.bid_portals, "get_portals", return_value=[
                 {"url": "https://x.gov/Bids.aspx", "platform": "civicplus"}]), \
             patch.object(ls.bid_portals, "record_result") as rec:
            ls._run_known_portals("X", "MO", "X, MO", self.grouped, CENTER, 100,
                                  self.cdb, self.coords, threading.Lock(), self.pdb,
                                  stats=self.stats)
        self.assertEqual(self.grouped, {})
        # Marked as a failure so MAX_FAIL eventually retires a dead entry.
        self.assertFalse(rec.call_args[0][-1])


class StructuredReadNeverLosesBidsTests(unittest.TestCase):
    """Structured reading is an optimisation over the AI path, never a
    replacement. A parser that matches nothing must fall through, not silently
    turn a working portal into zero bids — which is exactly what it did once.
    """

    def setUp(self):
        self.grouped, self.coords, self.stats = {}, {}, {}
        self.cdb, self.pdb = {}, {}

    # Realistic page text: the known-portal path now runs looks_relevant
    # before spending an AI call (the search path always has), so a filler
    # string of x's would be skipped as having no concrete work on it -- and
    # this test is about the parser-miss fall-through, not about the gate.
    PAGE_TEXT = ("Invitation to Bid — 2026 Sidewalk and ADA Ramp Replacement "
                 "Program. Sealed bids for concrete curb and gutter work "
                 "will be received by the City Clerk. " * 6)

    def _run(self, page_html, ai_returns, page_text=None):
        import threading
        rec = None
        with patch.object(ls, "_fetch_page", return_value=(page_html, "ok")), \
             patch.object(ls, "_fetch_text",
                          return_value=page_text or self.PAGE_TEXT), \
             patch.object(ls, "_ai_extract", return_value=ai_returns) as ai, \
             patch.object(ls, "_geo_from_city", side_effect=_fake_geo), \
             patch.object(ls.bid_portals, "get_portals", return_value=[
                 {"url": "https://www.springfieldmo.gov/Bids.aspx", "platform": "civicplus"}]), \
             patch.object(ls.bid_portals, "record_result") as rec:
            ls._run_known_portals(
                "Springfield", "MO", "Springfield, MO", self.grouped, CENTER, 100,
                self.cdb, self.coords, threading.Lock(), self.pdb,
                default_city="Springfield", town_coords=(37.2, -93.3), stats=self.stats)
        return ai, rec

    def test_a_parser_miss_falls_through_to_the_ai_path(self):
        # Markup the CivicPlus parser doesn't recognise, but a real page.
        ai, _ = self._run("<html><body>Bids are listed below</body></html>",
                          [{"title": "Sidewalk repair", "city": "Springfield",
                            "status": "Open"}])
        ai.assert_called_once()
        titles = [b["title"] for v in self.grouped.values() for b in v]
        self.assertEqual(titles, ["Sidewalk repair"])

    def test_a_page_with_no_concrete_work_never_reaches_the_ai(self):
        """21 of 33 sampled agency portals have no niche term anywhere on
        them. Extracting those spends a call, and seconds of the scan's town
        budget, to find nothing."""
        ai, _ = self._run("<html><body>Bids are listed below</body></html>", [],
                          page_text="Janitorial services and copier leasing. " * 20)
        ai.assert_not_called()
        self.assertEqual(self.stats.get("portal_no_niche_content"), 1)

    def test_a_skipped_page_is_still_a_working_portal(self):
        _, rec = self._run("<html><body>Bids are listed below</body></html>", [],
                           page_text="Janitorial services and copier leasing. " * 20)
        self.assertTrue(rec.call_args[0][-1],
                        "nothing for us today is not a dead source")

    def test_a_parser_miss_is_not_recorded_as_a_dead_portal(self):
        # Recording it as a failure would retire a perfectly good seeded URL
        # after MAX_FAIL scans.
        _, rec = self._run("<html><body>Bids are listed below</body></html>", [])
        self.assertTrue(rec.call_args[0][-1], "a live page was marked as failed")

    def test_a_parser_miss_is_visible_in_the_funnel(self):
        self._run("<html><body>nothing matchable</body></html>", [])
        self.assertEqual(self.stats.get("civicplus_parse_miss"), 1)

    def test_a_successful_parse_still_skips_the_ai_call(self):
        good = '<a href="/Bids.aspx?bidID=1">Sidewalk Improvements</a>'
        ai, _ = self._run(good, [])
        ai.assert_not_called()
        self.assertEqual(len([b for v in self.grouped.values() for b in v]), 1)


class LocalQueryPrefilterTests(unittest.TestCase):
    """_run_local_queries processes unverified search hits, unlike a known
    portal's own listing -- there's no "a parser gap is our problem, not a
    dead site" trust to lean on here, so looks_relevant() must run before
    paying for an AI call. Deliberately does NOT touch _run_known_portals'
    fallback path (see StructuredReadNeverLosesBidsTests above), which stays
    ungated on purpose."""

    def setUp(self):
        import threading
        self.grouped, self.coords, self.stats = {}, {}, {}
        self.cdb, self.pdb = {}, {}
        self.seen = set()
        self.lock = threading.Lock()
        self.center = {"lat": 37.209, "lon": -93.29, "city": "Springfield", "state": "MO"}

    def _run(self, page_text, ai_returns=None):
        with patch.object(ls, "TAVILY_API_KEY", "test-key"), \
             patch.object(ls, "_tavily_search",
                          return_value=[{"url": "https://x.gov/bid/1", "content": ""}]), \
             patch.object(ls, "_ddg_search", return_value=[]), \
             patch.object(ls, "_bidnet_direct_urls", return_value=[]), \
             patch.object(ls, "_fetch_text", return_value=page_text), \
             patch.object(ls, "_ai_extract", return_value=ai_returns or []) as ai, \
             patch.object(ls, "_geo_from_city",
                          side_effect=lambda c, s: {"lat": 37.2, "lon": -93.3, "city": c,
                                                    "state": s} if s == "MO" else None):
            raw = ls._run_local_queries(
                ["sidewalk bid"], "Springfield, MO", 10, self.grouped, self.center, 25,
                self.cdb, self.coords, self.seen, self.lock, self.pdb,
                default_city="Springfield", state="MO", stats=self.stats)
        return raw, ai

    def test_a_page_with_no_niche_terms_never_reaches_the_ai_call(self):
        raw, ai = self._run("x" * 500)
        ai.assert_not_called()
        self.assertEqual(raw, 0)

    def test_the_skip_is_counted_in_the_funnel(self):
        self._run("x" * 500)
        self.assertEqual(self.stats.get("filtered_not_niche"), 1)

    def test_a_page_that_actually_mentions_the_trade_still_gets_extracted(self):
        raw, ai = self._run(
            "Sidewalk and ADA curb ramp replacement project, bids due soon." + "x" * 400,
            ai_returns=[{"title": "Sidewalk Repair", "scope": "s", "status": "Open",
                        "city": "Springfield"}])
        ai.assert_called_once()
        self.assertEqual(raw, 1)
        titles = [b["title"] for v in self.grouped.values() for b in v]
        self.assertEqual(titles, ["Sidewalk Repair"])
