"""End-to-end test of _perform_scan with every network call stubbed.

Written after a live scan returned zero bids and unit tests all passed — which
means the units were fine and something about how they are wired together was
not. This drives the whole pipeline the way /scan does and asserts bids come
out the far end, so "the logic is sound, the problem is environmental" becomes
a fact rather than a hope.
"""
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls

CENTER = {"lat": 37.2090, "lon": -93.2923, "city": "Springfield", "state": "MO"}

# What Springfield's CivicPlus listing looks like to the structured parser.
CIVICPLUS_PAGE = """
  <a href="/Bids.aspx?bidID=412">FY26 Sidewalk Improvements &amp; ADA Ramps</a>
  <span>Bid Opening: December 1, 2026</span>
  <a href="/Bids.aspx?bidID=414">Curb &amp; Gutter Replacement - Phase 2</a>
  <span>Due: 12/15/2026</span>
"""


class ScanEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.cache = {}
        # bid_portals.load_directory() (called for real below, not mocked --
        # these tests want the actual SEED_PORTALS/national-crawl seeding
        # logic) persists through kv_backend, which falls back to a real
        # local JSON file when Upstash isn't configured. Without isolating
        # that storage layer, a "learned" entry left behind by an earlier
        # test run (in this file or another) silently changes how many bids
        # this test finds -- it did, until this was added: the assertions
        # were quietly passing because of leftover disk state, not because
        # of what this test actually sets up.
        self.pdb_store = {}
        self._patchers = [
            patch.object(ls, "_cache", side_effect=lambda: self.cache),
            patch.object(ls, "_save_cache"),
            patch.object(ls, "_resolve_center", return_value=CENTER),
            patch.object(ls, "OPENAI_API_KEY", "test-key"),
            patch.object(ls, "SAM_API_KEY", ""),
            patch.object(ls, "TAVILY_API_KEY", "test-key"),
            # Keep the geography small and deterministic. Without this,
            # data/bid_portal_coords.csv having a real nearby MO town (e.g.
            # Nixa/Republic/Ozark, all real SEED_PORTALS entries a short
            # drive from Springfield) would make towns_within_radius pull
            # one in — the mocked _fetch_page/_ai_extract below answer for
            # ANY url, so that town would silently double bid counts and
            # break the exact-count assertions in this class.
            patch.object(ls, "_nearby_anchor_towns", return_value=[]),
            patch.object(ls.bid_portals, "towns_within_radius", return_value=[]),
            patch.object(ls, "_geo_from_city",
                         side_effect=lambda c, s: {"lat": 37.209, "lon": -93.29,
                                                   "city": c, "state": s}
                         if s == "MO" else None),
            patch.object(ls.bid_portals.kv_backend, "get",
                         side_effect=lambda key, default=None: self.pdb_store.get(key, default)),
            patch.object(ls.bid_portals.kv_backend, "set",
                         side_effect=lambda key, value: self.pdb_store.__setitem__(key, value)),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def test_a_seeded_civicplus_portal_alone_produces_bids(self):
        """No search backend at all — the portal on its own must deliver."""
        with patch.object(ls, "_fetch_page", return_value=(CIVICPLUS_PAGE, "ok")), \
             patch.object(ls, "_tavily_search", return_value=[]), \
             patch.object(ls, "_ddg_search", return_value=[]), \
             patch.object(ls, "_ai_extract", return_value=[]):
            out = ls._perform_scan("Springfield, MO", 25)

        self.assertIsNotNone(out)
        titles = [b["title"] for v in out["bids"].values() for b in v]
        self.assertIn("FY26 Sidewalk Improvements & ADA Ramps", titles)
        self.assertIn("Curb & Gutter Replacement - Phase 2", titles)
        self.assertEqual(out["total_bids"], 2, out["debug"])

    def test_generic_queries_are_skipped_when_the_known_portal_already_hit(self):
        """A working direct read of the city's own bid page already answers
        "does this city have a sidewalk bid" -- the generic re-phrasings of
        that exact question are then pure Tavily-credit waste, not a recall
        gain. Aggregator/distinct-entity queries (school district, county,
        BidNet, ...) still run regardless, since a direct read of the CITY's
        page can't answer those."""
        queries_seen = []

        def record_search(q, max_results=6):
            queries_seen.append(q)
            return []

        with patch.object(ls, "_fetch_page", return_value=(CIVICPLUS_PAGE, "ok")), \
             patch.object(ls, "_tavily_search", side_effect=record_search), \
             patch.object(ls, "_ddg_search", return_value=[]), \
             patch.object(ls, "_ai_extract", return_value=[]):
            ls._perform_scan("Springfield, MO", 25)

        self.assertTrue(queries_seen, "no search ran at all")
        self.assertTrue(any("bidnetdirect.com" in q for q in queries_seen),
                        "an aggregator-targeted query should still run")
        self.assertFalse(any("ADA ramp curb gutter concrete bid opportunities" in q
                             for q in queries_seen),
                         "a generic re-phrasing should have been skipped")

    def test_generic_queries_still_run_when_no_known_portal_hits(self):
        """No working direct source -- search is the only signal available,
        so nothing gets trimmed."""
        queries_seen = []

        def record_search(q, max_results=6):
            queries_seen.append(q)
            return []

        with patch.object(ls, "_fetch_page", return_value=("", "ok")), \
             patch.object(ls, "_fetch_text", return_value=""), \
             patch.object(ls, "_tavily_search", side_effect=record_search), \
             patch.object(ls, "_ddg_search", return_value=[]), \
             patch.object(ls, "_ai_extract", return_value=[]):
            ls._perform_scan("Springfield, MO", 25)

        # Asserted by count rather than by exact wording: the generic set has
        # been reworded and trimmed before, and pinning a phrase makes this
        # test fail for an edit that changed nothing about the behaviour it
        # exists to protect.
        always = len(ls._center_always_count_for_test()) \
            if hasattr(ls, "_center_always_count_for_test") else 6
        self.assertGreater(
            len(queries_seen), always,
            "with no working direct source, the generic queries must run on "
            "top of the always-queries")
        # And they must be plain phrasings, not more site: filters -- the
        # point of the generic set is to reach pages no aggregator lists.
        self.assertTrue(any("site:" not in q for q in queries_seen),
                        "the generic queries should not all be site: filters")

    def test_the_search_path_alone_also_produces_bids(self):
        """Portal unreachable, search working — the other half must still run."""
        # Real niche-relevant filler, not "x"*500 -- looks_relevant() (run
        # before every search-result AI call, see license_server.py's
        # _run_local_queries) would otherwise correctly skip this page before
        # ever reaching the mocked _ai_extract below, for the same reason a
        # real page about janitorial services gets skipped.
        relevant_filler = "Sidewalk and ADA curb ramp replacement project. " + "x" * 450
        with patch.object(ls, "_fetch_page", return_value=("", "ok")), \
             patch.object(ls, "_fetch_text", return_value=relevant_filler), \
             patch.object(ls, "_tavily_search",
                          return_value=[{"url": "https://x.gov/bid/1", "content": ""}]), \
             patch.object(ls, "_ddg_search", return_value=[]), \
             patch.object(ls, "_bidnet_direct_urls", return_value=[]), \
             patch.object(ls, "_ai_extract",
                          return_value=[{"title": "Sidewalk repair", "scope": "s",
                                         "status": "Open", "city": "Springfield"}]):
            out = ls._perform_scan("Springfield, MO", 25)

        titles = [b["title"] for v in out["bids"].values() for b in v]
        self.assertIn("Sidewalk repair", titles, out["debug"])

    def test_both_paths_together_do_not_cancel_each_other_out(self):
        relevant_filler = "Sidewalk and ADA curb ramp replacement project. " + "x" * 450
        with patch.object(ls, "_fetch_page", return_value=(CIVICPLUS_PAGE, "ok")), \
             patch.object(ls, "_fetch_text", return_value=relevant_filler), \
             patch.object(ls, "_tavily_search",
                          return_value=[{"url": "https://x.gov/bid/1", "content": ""}]), \
             patch.object(ls, "_ddg_search", return_value=[]), \
             patch.object(ls, "_bidnet_direct_urls", return_value=[]), \
             patch.object(ls, "_ai_extract",
                          return_value=[{"title": "Sidewalk repair", "scope": "s",
                                         "status": "Open", "city": "Springfield"}]):
            out = ls._perform_scan("Springfield, MO", 25)
        self.assertGreaterEqual(out["total_bids"], 3, out["debug"])

    def test_the_funnel_explains_a_zero_result(self):
        """A scan that finds nothing must say why, not just return nothing."""
        with patch.object(ls, "_fetch_page", return_value=("", "ok")), \
             patch.object(ls, "_fetch_text", return_value=""), \
             patch.object(ls, "_tavily_search", return_value=[]), \
             patch.object(ls, "_ddg_search", return_value=[]), \
             patch.object(ls, "_bidnet_direct_urls", return_value=[]), \
             patch.object(ls, "_ai_extract", return_value=[]):
            out = ls._perform_scan("Springfield, MO", 25)
        self.assertEqual(out["total_bids"], 0)
        self.assertIn("funnel", out["debug"])


class ScanBudgetTests(unittest.TestCase):
    """A scan has to finish inside the app's own request timeout. Probing
    speculative URLs one after another, each with an 18s fetch timeout, is how
    a working scan turns into a client-side abort — which looks identical to
    "no bids" from the outside."""

    def test_portal_reads_do_not_run_one_after_another(self):
        overlap = {"now": 0, "max": 0}
        lock = threading.Lock()

        def slow_fetch(url, timeout=None):
            with lock:
                overlap["now"] += 1
                overlap["max"] = max(overlap["max"], overlap["now"])
            time.sleep(0.15)
            with lock:
                overlap["now"] -= 1
            return "", "ok"

        portals = [{"url": f"https://x{i}.gov/Bids.aspx", "platform": "civicplus"}
                   for i in range(6)]
        with patch.object(ls, "_fetch_page", side_effect=slow_fetch), \
             patch.object(ls, "_fetch_text", return_value=""), \
             patch.object(ls, "_geo_from_city", return_value=None), \
             patch.object(ls.bid_portals, "get_portals", return_value=portals), \
             patch.object(ls.bid_portals, "record_result"):
            started = time.time()
            ls._run_known_portals("X", "MO", "X, MO", {}, CENTER, 25, {}, {},
                                  threading.Lock(), {})
            elapsed = time.time() - started

        self.assertGreater(overlap["max"], 1,
                           "portals are fetched one at a time — six dead domains "
                           "at an 18s timeout each would blow the request budget")
        self.assertLess(elapsed, 0.7, f"took {elapsed:.2f}s for 6 portals")

    def test_detail_page_reads_are_capped_per_portal(self):
        """Contact details cost one fetch per bid. A listing with thirty
        matching postings must not turn into thirty more requests."""
        listing = "".join(
            f'<a href="/Bids.aspx?bidID={i}">Sidewalk Project {i}</a>'
            f'<span>Closing Date/Time: 12/1/2026</span>' for i in range(30))
        fetched = []

        def record(url, timeout=None):
            fetched.append(url)
            return (listing if url.endswith("/Bids.aspx") else "<p>x</p>"), "ok"

        portals = [{"url": "https://x.gov/Bids.aspx", "platform": "civicplus"}]
        with patch.object(ls, "_fetch_page", side_effect=record), \
             patch.object(ls, "_fetch_text", return_value=""), \
             patch.object(ls, "_geo_from_city", return_value=None), \
             patch.object(ls.bid_portals, "get_portals", return_value=portals), \
             patch.object(ls.bid_portals, "record_result"):
            ls._run_known_portals("X", "MO", "X, MO", {}, CENTER, 25, {}, {},
                                  threading.Lock(), {})

        details = [u for u in fetched if "bidID=" in u]
        self.assertLessEqual(len(details), ls.DETAIL_PAGES_PER_PORTAL,
                             f"{len(details)} detail fetches")
        self.assertGreater(len(details), 0, "no contact details were read at all")

    def test_a_guessed_url_gets_a_shorter_timeout_than_a_known_one(self):
        """Most speculative probes are dead. At the full timeout, a handful of
        them spends the whole request budget before search even starts."""
        seen = {}

        def record(url, timeout=None):
            seen[url] = timeout
            return "", "ok"

        portals = [{"url": "https://known.gov/Bids.aspx", "platform": "civicplus"},
                   {"url": "https://guess.gov/Bids.aspx", "platform": "civicplus",
                    "probe": True}]
        with patch.object(ls, "_fetch_page", side_effect=record), \
             patch.object(ls, "_fetch_text", return_value=""), \
             patch.object(ls, "_geo_from_city", return_value=None), \
             patch.object(ls.bid_portals, "get_portals", return_value=portals), \
             patch.object(ls.bid_portals, "record_result"):
            ls._run_known_portals("X", "MO", "X, MO", {}, CENTER, 25, {}, {},
                                  threading.Lock(), {})
        self.assertIsNone(seen["https://known.gov/Bids.aspx"])
        self.assertEqual(seen["https://guess.gov/Bids.aspx"], ls.PROBE_TIMEOUT)
        self.assertLess(ls.PROBE_TIMEOUT, ls.FETCH_TIMEOUT)


class HomepageLinkFallbackTests(unittest.TestCase):
    """A city with no known portal and no hit on the guessed common paths
    used to fall straight through to a generic web search -- which has no
    way to tell a result ABOUT this city apart from one merely mentioning
    it (see _place_bid's out_of_radius counter: a live Kansas City scan with
    no known portal came back with every single raw result out of radius).
    _run_known_portals should try an actual bid-shaped link off the town's
    own homepage first, same as tools/discover_bid_portals.py's offline
    crawl does."""

    HOMEPAGE = '<a href="/procurement/opportunities">Bid Opportunities</a>'

    def test_a_real_homepage_link_gets_tried_even_off_the_guessed_paths(self):
        # _read_portal only calls _fetch_page for URLs it recognizes as
        # CivicPlus (ends in Bids.aspx); everything else -- including the
        # homepage-derived link, which is tagged "custom" -- is read via
        # _fetch_text instead. Both need tracking to see everything that
        # was actually tried.
        fetched = []

        def record_page(url, timeout=None):
            fetched.append(url)
            if url == "https://x.gov":
                return (self.HOMEPAGE, "ok")
            return ("", "ok")

        def record_text(url, timeout=None):
            fetched.append(url)
            return ""

        with patch.object(ls, "_fetch_page", side_effect=record_page), \
             patch.object(ls, "_fetch_text", side_effect=record_text), \
             patch.object(ls, "_geo_from_city", return_value=None), \
             patch.object(ls.bid_portals, "get_portals", return_value=[]), \
             patch.object(ls.bid_portals, "record_result"), \
             patch.object(ls.gov_directory, "lookup",
                          return_value=[{"domain": "x.gov", "type": "City",
                                        "org": "City of X", "city": "X", "state": "MO"}]):
            ls._run_known_portals("X", "MO", "X, MO", {}, CENTER, 25, {}, {},
                                  threading.Lock(), {})

        self.assertIn("https://x.gov", fetched, "the homepage itself was never fetched")
        self.assertIn("https://x.gov/procurement/opportunities", fetched,
                      "the bid-shaped link found on the homepage was never tried")

    def test_a_dead_homepage_does_not_crash_the_scan(self):
        with patch.object(ls, "_fetch_page", return_value=("", "http_500")), \
             patch.object(ls, "_fetch_text", return_value=""), \
             patch.object(ls, "_geo_from_city", return_value=None), \
             patch.object(ls.bid_portals, "get_portals", return_value=[]), \
             patch.object(ls.bid_portals, "record_result"), \
             patch.object(ls.gov_directory, "lookup",
                          return_value=[{"domain": "x.gov", "type": "City",
                                        "org": "City of X", "city": "X", "state": "MO"}]):
            got = ls._run_known_portals("X", "MO", "X, MO", {}, CENTER, 25, {}, {},
                                        threading.Lock(), {})
        self.assertEqual(got, 0)


class KnownPortalRadiusExpansionTests(unittest.TestCase):
    """A wide-radius scan used to only ever search the exact town typed plus
    a handful of geographically-guessed anchor points, capped at 6 no
    matter how large the radius actually was. This is the fix: every town
    within radius that we already have a real, verified bid page for (the
    national crawl -- see bid_portals.towns_within_radius) gets read
    directly too, at no search-credit cost. This exercises that path
    through the full _perform_scan pipeline, not just the unit in
    isolation (see tests/test_bid_portals.py for that)."""

    def setUp(self):
        self.cache = {}
        self.pdb_store = {}
        self._patchers = [
            patch.object(ls, "_cache", side_effect=lambda: self.cache),
            patch.object(ls, "_save_cache"),
            patch.object(ls, "_resolve_center", return_value=CENTER),
            patch.object(ls, "OPENAI_API_KEY", "test-key"),
            patch.object(ls, "SAM_API_KEY", ""),
            patch.object(ls, "TAVILY_API_KEY", "test-key"),
            patch.object(ls, "_nearby_anchor_towns", return_value=[]),
            patch.object(ls, "_tavily_search", return_value=[]),
            patch.object(ls, "_ddg_search", return_value=[]),
            patch.object(ls, "_ai_extract", return_value=[]),
            patch.object(ls, "_bidnet_direct_urls", return_value=[]),
            # The center town has no known portal in these tests, so it runs
            # the full query list and hits the real DDG_QUERY_PAUSE between
            # each one (production pacing for the free scraped backend,
            # correctly exercised by test_portal_reads_do_not_run_one_after_
            # another elsewhere) -- not what these tests are checking, and it
            # turns 2 fast tests into an 18s pair for no assertion-relevant
            # reason.
            patch.object(ls, "DDG_QUERY_PAUSE", 0),
            # Real coordinates for every city these tests touch, not a live
            # network call — _place_bid geocodes a bid's default_city to
            # apply the radius check, same reason ScanEndToEndTests mocks
            # this. Missing "Springfield" here (the center itself) was the
            # actual bug: _geo_from_city returning None for it didn't error,
            # it silently fell through to a real, slow network geocode.
            patch.object(ls, "_geo_from_city", side_effect=lambda c, s: {
                "Springfield": {"lat": 37.2090, "lon": -93.2923, "city": c, "state": s},
                "Nixa": {"lat": 37.0428, "lon": -93.2926, "city": c, "state": s},
                "Kansas City": {"lat": 39.0997, "lon": -94.5786, "city": c, "state": s},
            }.get(c)),
            patch.object(ls.bid_portals.kv_backend, "get",
                         side_effect=lambda key, default=None: self.pdb_store.get(key, default)),
            patch.object(ls.bid_portals.kv_backend, "set",
                         side_effect=lambda key, value: self.pdb_store.__setitem__(key, value)),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def test_a_known_town_within_radius_contributes_bids_with_no_search(self):
        nearby_town = ("Nixa", "MO", 37.0428, -93.2926)  # ~11mi from CENTER

        def fetch(url, timeout=None):
            if "nixa" in url:
                return (CIVICPLUS_PAGE, "ok")
            return ("", "ok")

        with patch.object(ls.bid_portals, "towns_within_radius", return_value=[nearby_town]), \
             patch.object(ls.bid_portals, "get_portals",
                          side_effect=lambda d, city, state: (
                              [{"url": "https://www.nixa.gov/Bids.aspx", "platform": "civicplus"}]
                              if city == "Nixa" else [])), \
             patch.object(ls, "_fetch_page", side_effect=fetch):
            out = ls._perform_scan("Springfield, MO", 25)

        titles = [b["title"] for v in out["bids"].values() for b in v]
        self.assertIn("FY26 Sidewalk Improvements & ADA Ramps", titles, out["debug"])

    def test_a_known_town_outside_the_result_radius_is_excluded_by_the_per_bid_filter(self):
        """towns_within_radius already filters by radius, but this pins the
        belt-and-suspenders behavior: even if a known town somehow slipped
        through too far away, individual bids from it still can't bypass
        the existing out_of_radius check on the final result."""
        far_town = ("Kansas City", "MO", 39.0997, -94.5786)  # ~160mi from CENTER

        def fetch(url, timeout=None):
            if "kansascity" in url:
                return (CIVICPLUS_PAGE, "ok")
            return ("", "ok")

        with patch.object(ls.bid_portals, "towns_within_radius", return_value=[far_town]), \
             patch.object(ls.bid_portals, "get_portals",
                          side_effect=lambda d, city, state: (
                              [{"url": "https://www.kansascity.gov/Bids.aspx", "platform": "civicplus"}]
                              if city == "Kansas City" else [])), \
             patch.object(ls, "_fetch_page", side_effect=fetch):
            out = ls._perform_scan("Springfield, MO", 25)

        self.assertEqual(out["total_bids"], 0, out["debug"])


if __name__ == "__main__":
    unittest.main()
