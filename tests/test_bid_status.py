"""Tests for which bids count as open.

Written after a live scan reported funnel `kept=7` and a total of `0`: seven
real, in-radius solicitations were placed into the result and every one of them
was then ruled not-open and hidden. The old rule demanded the status string be
exactly "open", so anything an agency or the extraction model phrased
differently — "Accepting Bids", "Active", "Open - Bids Due 12/1" — silently
vanished between being found and being shown.

This is the single most expensive class of bug in the product: it costs a bid
that was already found and paid for, and it looks from the outside exactly like
"there is no work in your area".
"""
import datetime
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


class OpenStatusTests(unittest.TestCase):
    def test_the_plain_cases(self):
        self.assertTrue(ls._is_open_bid({"status": "Open"}))
        self.assertFalse(ls._is_open_bid({"status": "Closed"}))

    def test_the_ways_a_live_bid_is_really_written(self):
        for status in ("open", " Open ", "OPEN", "Accepting Bids", "Active",
                       "Advertised", "Open - Bids Due 12/1/2026", "Currently Open",
                       "Bidding", "Open for Bids", "Issued", "Posted"):
            with self.subTest(status=status):
                self.assertTrue(ls._is_open_bid({"status": status}), status)

    def test_the_ways_a_dead_bid_is_really_written(self):
        for status in ("Closed", "closed", "CLOSED", "Bid Closed", "Awarded",
                       "Award to Acme Concrete", "Cancelled", "Canceled",
                       "Expired", "Withdrawn", "Archived", "No Longer Accepting",
                       "Not Accepting Bids", "Complete"):
            with self.subTest(status=status):
                self.assertFalse(ls._is_open_bid({"status": status}), status)

    def test_an_unstated_status_is_not_evidence_of_a_closed_bid(self):
        # Structured listings often carry no status at all. Treating that as
        # closed would throw away the most reliable source in the pipeline.
        for bid in ({}, {"status": ""}, {"status": None}, {"status": "   "}):
            with self.subTest(bid=bid):
                self.assertTrue(ls._is_open_bid(bid))

    def test_malformed_input_does_not_raise(self):
        for bid in (None, {"status": 12345}):
            with self.subTest(bid=bid):
                ls._is_open_bid(bid)

    def test_a_closed_bid_still_ranks_last(self):
        closed = {"title": "Sidewalk repair", "status": "Awarded"}
        open_ = {"title": "Sidewalk repair", "status": "Accepting Bids"}
        self.assertLess(ls._score_bid(closed), ls._score_bid(open_))


class KeptAndTotalAgreeTests(unittest.TestCase):
    """The funnel's `kept` and the reported total must not be able to disagree
    without the funnel saying why."""

    CENTER = {"lat": 37.209, "lon": -93.292, "city": "Springfield", "state": "MO"}

    def _place(self, bids):
        grouped, stats, cdb = {}, {}, {}
        for b in bids:
            ls._place_bid(grouped, dict(b), self.CENTER, 25, cdb,
                          default_city="Springfield", default_state="MO",
                          fallback_coords=(37.209, -93.292), stats=stats)
        # Same closing step the scan runs, so the funnel here means what it
        # means in production rather than drifting from it.
        ls._count_kept_closed(grouped, stats)
        total = sum(1 for v in grouped.values() for b in v if ls._is_open_bid(b))
        return grouped, stats, total

    def test_bids_phrased_as_accepting_are_counted(self):
        _, stats, total = self._place([
            {"title": "2026 Sidewalk Program", "status": "Accepting Bids"},
            {"title": "ADA Ramp Replacement", "status": "Active"},
        ])
        self.assertEqual(stats.get("kept"), 2, stats)
        self.assertEqual(total, 2, stats)

    def test_a_kept_but_closed_bid_is_called_out_in_the_funnel(self):
        _, stats, total = self._place([
            {"title": "Old Curb Job", "status": "Awarded"},
            {"title": "New Sidewalk Job", "status": "Open"},
        ])
        self.assertEqual(stats.get("kept"), 2)
        self.assertEqual(stats.get("kept_but_closed"), 1, stats)
        self.assertEqual(total, 1)

    def test_an_expired_deadline_shows_up_as_kept_but_closed(self):
        # Not a silent zero: the funnel says the work was found and had lapsed.
        _, stats, total = self._place([
            {"title": "Sidewalk Program", "status": "Open", "deadline": "3/1/2019"},
        ])
        self.assertEqual(stats.get("kept"), 1)
        self.assertEqual(stats.get("kept_but_closed"), 1, stats)
        self.assertEqual(total, 0)


class LastScanDiagnosticsTests(unittest.TestCase):
    """/health has to show what a scan found, not only how many."""

    GROUPED = {
        "Springfield": [
            {"title": "2026 Sidewalk & ADA Ramp Program", "status": "Open",
             "deadline": "12/1/2026", "email": "buyer@springfieldmo.gov"},
            {"title": "Curb and Gutter - Grant Ave", "status": "Awarded",
             "deadline": "3/3/2026", "phone": "417-555-0100"},
        ],
        "Nixa": [{"title": "Concrete Flatwork Contract", "deadline": ""}],
    }

    def test_statuses_are_broken_down(self):
        self.assertEqual(ls._status_breakdown(self.GROUPED),
                         {"Open": 1, "Awarded": 1, "(none)": 1})

    def test_the_sample_says_whether_each_bid_counted_as_open(self):
        sample = ls._scan_sample(self.GROUPED)
        by_title = {s["title"]: s for s in sample}
        self.assertTrue(by_title["2026 Sidewalk & ADA Ramp Program"]["open"])
        self.assertFalse(by_title["Curb and Gutter - Grant Ave"]["open"])
        self.assertTrue(by_title["Concrete Flatwork Contract"]["open"])

    def test_the_sample_carries_no_contact_details(self):
        import json
        raw = json.dumps(ls._scan_sample(self.GROUPED))
        self.assertNotIn("buyer@springfieldmo.gov", raw)
        self.assertNotIn("417-555-0100", raw)

    def test_the_sample_is_bounded(self):
        big = {"X": [{"title": f"Job {i}", "status": "Open"} for i in range(50)]}
        self.assertLessEqual(len(ls._scan_sample(big)), 8)

    def test_empty_input_is_handled(self):
        self.assertEqual(ls._status_breakdown({}), {})
        self.assertEqual(ls._scan_sample(None), [])


class SearchTownFallbackTests(unittest.TestCase):
    """A live 125mi scan of Russellville MO returned bids in Binghamton NY,
    Barrington IL and Aledo IL. Ordinary cities that don't exist in Missouri
    failed to geocode, were stamped with a Missouri anchor town's coordinates,
    and passed the radius check on borrowed location."""

    CENTER = {"lat": 38.515, "lon": -92.343, "city": "Russellville", "state": "MO"}
    ANCHOR = (38.6, -92.5)

    def _place(self, city, **kw):
        grouped, stats = {}, {}
        # Nothing geocodes — this is the path where the fallback is reached.
        with patch.object(ls, "_city_coords", return_value=None):
            ls._place_bid(grouped, {"title": "Sidewalk work", "city": city},
                          self.CENTER, 125, {}, default_state="MO",
                          fallback_coords=self.ANCHOR, stats=stats, **kw)
        return grouped, stats

    def test_an_out_of_area_city_is_not_pinned_to_the_search_town(self):
        for city in ("Binghamton", "Barrington", "Aledo"):
            with self.subTest(city=city):
                grouped, stats = self._place(city)
                self.assertEqual(grouped, {}, f"{city} was kept")
                self.assertEqual(stats.get("unresolvable_place"), 1)
                self.assertIsNone(stats.get("placed_by_search_town"))

    def test_a_county_is_still_anchored(self):
        # The whole reason the fallback exists: counties let a lot of curb and
        # road work and no gazetteer resolves them.
        for city in ("Greene County", "Cole County Public Works",
                     "Ozark R-VI School District", "Boone County Road District",
                     "Springfield Airport Authority", "Wayne Township"):
            with self.subTest(city=city):
                grouped, stats = self._place(city)
                self.assertEqual(stats.get("placed_by_search_town"), 1, city)
                self.assertTrue(grouped, city)

    def test_the_town_we_searched_is_still_anchored(self):
        grouped, stats = self._place("Russellville", default_city="Russellville")
        self.assertEqual(stats.get("placed_by_search_town"), 1)
        self.assertTrue(grouped)

    def test_an_explicitly_out_of_state_bid_is_still_never_anchored(self):
        grouped, stats = self._place("Greene County, NY")
        self.assertEqual(grouped, {})
        self.assertEqual(stats.get("unresolvable_place"), 1)


class StaleYearTests(unittest.TestCase):
    """A live Springfield scan reported four open bids in August 2026, all of
    them "2025 Sidewalk Program - Scope of Work SW-1..4" with no deadline. An
    undated bid is assumed open, which is right for a new posting and wrong for
    last year's programme still sitting on a portal."""

    def test_last_years_programme_is_not_open(self):
        for title in ("2025 Sidewalk Program – Scope of Work SW - 1",
                      "2024 ADA Curb Ramp Project",
                      "FY2023 Concrete Flatwork Contract"):
            with self.subTest(title=title):
                bid = {"title": title, "status": "Open", "deadline": ""}
                ls._apply_stale_year(bid)
                self.assertFalse(ls._is_open_bid(bid), title)

    def test_this_year_and_next_are_left_open(self):
        year = datetime.datetime.now().year
        for title in (f"{year} Sidewalk Program", f"{year + 1} ADA Improvements"):
            with self.subTest(title=title):
                bid = {"title": title, "status": "Open", "deadline": ""}
                ls._apply_stale_year(bid)
                self.assertTrue(ls._is_open_bid(bid), title)

    def test_a_multi_year_programme_is_left_alone(self):
        # "2025-2026" names a past year but is still current.
        bid = {"title": "2025-2026 Sidewalk Replacement Program",
               "status": "Open", "deadline": ""}
        ls._apply_stale_year(bid)
        self.assertTrue(ls._is_open_bid(bid))

    def test_a_stated_deadline_always_wins_over_the_title(self):
        # An old programme number with a live deadline is a real open bid.
        future = f"12/1/{datetime.datetime.now().year + 1}"
        bid = {"title": "2019PW0028 Sidewalk Repair", "status": "Open",
               "deadline": future}
        ls._apply_stale_year(bid)
        self.assertTrue(ls._is_open_bid(bid))

    def test_a_title_with_no_year_is_untouched(self):
        bid = {"title": "Sidewalk Replacement Program", "status": "Open",
               "deadline": ""}
        ls._apply_stale_year(bid)
        self.assertTrue(ls._is_open_bid(bid))

    def test_garbage_input_does_not_raise(self):
        for bad in (None, {}, {"title": None}, "not a bid"):
            with self.subTest(bad=bad):
                ls._apply_stale_year(bad)


class EnrichEveryPlacedBidTests(unittest.TestCase):
    """Enrichment used to live inside the structured CivicPlus reader only, so
    a bid that arrived via search kept its blank contact fields. A live scan
    placed eleven bids and reported no contacts_found at all."""

    PAGE = ("<p>Description: Replace 900 LF of sidewalk.</p>"
            "<p>Contact Person: Dale Prentice</p>"
            "<p>dprentice@example-city.gov</p><p>(417) 555-0143</p>"
            "<p>Closing Date/Time: 12/1/2026 2:00 PM</p>")

    def test_a_search_path_bid_gets_its_contacts(self):
        grouped = {"Springfield": [
            {"title": "2026 Sidewalk Program", "url": "https://x.gov/bid/9",
             "status": "Open", "deadline": ""}]}
        stats = {}
        with patch.object(ls, "_fetch_page", return_value=(self.PAGE, "ok")):
            ls._enrich_placed_bids(grouped, stats)
        bid = grouped["Springfield"][0]
        self.assertEqual(bid["email"], "dprentice@example-city.gov")
        self.assertEqual(bid["phone"], "(417) 555-0143")
        self.assertEqual(stats.get("contacts_found"), 1, stats)

    def test_a_bid_that_already_has_contacts_is_not_refetched(self):
        grouped = {"X": [{"title": "A", "url": "https://x.gov/1",
                          "email": "known@city.gov"}]}
        with patch.object(ls, "_fetch_page", side_effect=AssertionError("fetched!")):
            ls._enrich_placed_bids(grouped, {})

    def test_undated_bids_are_enriched_first(self):
        # A missing deadline is worse than a missing phone number: it also
        # stops an expired listing being recognised as expired.
        order = []

        def record(url, timeout=None):
            order.append(url)
            return "", "ok"

        grouped = {"X": [
            {"title": "dated", "url": "https://x.gov/dated", "deadline": "12/1/2026"},
            {"title": "undated", "url": "https://x.gov/undated", "deadline": ""}]}
        with patch.object(ls, "_fetch_page", side_effect=record), \
             patch.object(ls, "ENRICH_MAX", 1):
            ls._enrich_placed_bids(grouped, {})
        self.assertEqual(order, ["https://x.gov/undated"], order)

    def test_a_deadline_found_late_still_closes_the_bid(self):
        page = "<p>Closing Date/Time: 3/1/2019 2:00 PM</p><p>x@city.gov</p>"
        grouped = {"X": [{"title": "Sidewalk", "url": "https://x.gov/1",
                          "status": "Open", "deadline": ""}]}
        stats = {}
        with patch.object(ls, "_fetch_page", return_value=(page, "ok")):
            ls._enrich_placed_bids(grouped, stats)
        ls._count_kept_closed(grouped, stats)
        self.assertFalse(ls._is_open_bid(grouped["X"][0]))
        self.assertEqual(stats.get("kept_but_closed"), 1, stats)

    def test_the_work_is_bounded(self):
        fetched = []
        grouped = {"X": [{"title": f"J{i}", "url": f"https://x.gov/{i}"}
                         for i in range(40)]}
        with patch.object(ls, "_fetch_page",
                          side_effect=lambda u, timeout=None: (fetched.append(u), ("", "ok"))[1]):
            ls._enrich_placed_bids(grouped, {})
        self.assertLessEqual(len(fetched), ls.ENRICH_MAX, len(fetched))

    def test_empty_input_is_handled(self):
        for bad in (None, {}, {"X": []}):
            with self.subTest(bad=bad):
                ls._enrich_placed_bids(bad, {})


class OutOfStateAuthorityTests(unittest.TestCase):
    """A Missouri scan returned "DuPage County Sidewalks, curb ramps and
    lighting". DuPage is in Illinois, 350 miles away. It survived the
    plain-town check because "County" makes a name look unmappable."""

    CENTER = {"lat": 38.515, "lon": -92.343, "city": "Russellville", "state": "MO"}

    def _place(self, city, **kw):
        grouped, stats = {}, {}
        with patch.object(ls, "_city_coords", return_value=None):
            ls._place_bid(grouped, {"title": "Sidewalk work", "city": city},
                          self.CENTER, 125, {}, default_state="MO",
                          fallback_coords=(38.6, -92.5), stats=stats, **kw)
        return grouped, stats

    def test_a_county_that_only_exists_elsewhere_is_dropped(self):
        grouped, stats = self._place("DuPage County")
        self.assertEqual(grouped, {})
        self.assertEqual(stats.get("authority_in_another_state"), 1, stats)

    def test_a_county_that_exists_here_is_kept(self):
        for city in ("Greene County", "Cole County"):
            with self.subTest(city=city):
                grouped, stats = self._place(city)
                self.assertEqual(stats.get("placed_by_search_town"), 1, stats)

    def test_an_ambiguous_county_name_is_left_alone(self):
        # Christian County is registered in IL, KY and MO. A name in several
        # states proves nothing, so it must not be dropped.
        grouped, stats = self._place("Christian County")
        self.assertIsNone(stats.get("authority_in_another_state"), stats)
        self.assertTrue(grouped)

    def test_a_county_the_registry_has_never_heard_of_still_anchors(self):
        grouped, stats = self._place("Frobnitz County")
        self.assertEqual(stats.get("placed_by_search_town"), 1, stats)

    def test_the_rule_does_not_reach_townships_and_districts(self):
        # The registry covers these far too thinly for "known in one state" to
        # mean anything: Ohio owning the only registered Wayne Township says
        # nothing about Missouri.
        for city in ("Wayne Township", "Boone County Road District"):
            with self.subTest(city=city):
                _, stats = self._place(city)
                self.assertIsNone(stats.get("authority_in_another_state"), stats)

    def test_the_town_we_searched_is_never_second_guessed(self):
        # Russellville is registered in AR and KY but not MO — the registry
        # holds only 194 of Missouri's ~950 incorporated places. Absence from
        # it is not evidence of anything.
        grouped, stats = self._place("Russellville", default_city="Russellville")
        self.assertEqual(stats.get("placed_by_search_town"), 1, stats)
        self.assertTrue(grouped)


class ReverseGeocodeCenterTests(unittest.TestCase):
    """A live scan came back centred on "Township of Rock Prairie, MO" — a
    rural civil township with no procurement office and no registered domain.
    The centre's name is not cosmetic: it becomes every search query the scan
    builds and the key the .gov directory is asked for, so the structured
    portal path never ran at all."""

    @staticmethod
    def _payload(city="", locality="", admin=()):
        return {"countryCode": "US", "principalSubdivisionCode": "US-MO",
                "city": city, "locality": locality,
                "localityInfo": {"administrative": list(admin)}}

    def _resolve(self, payload):
        with patch.object(ls, "_get_json", return_value=payload):
            return ls._reverse_geocode_city(37.1, -93.4)

    def test_a_township_is_not_accepted_as_the_centre(self):
        got = self._resolve(self._payload(
            locality="Township of Rock Prairie",
            admin=[{"name": "Greene County", "adminLevel": 6},
                   {"name": "Township of Rock Prairie", "adminLevel": 7}]))
        self.assertEqual(got, ("Greene County", "MO"))

    def test_a_real_city_still_wins_outright(self):
        got = self._resolve(self._payload(
            city="Springfield",
            admin=[{"name": "Greene County", "adminLevel": 6},
                   {"name": "Springfield", "adminLevel": 8}]))
        self.assertEqual(got, ("Springfield", "MO"))

    def test_the_municipality_beats_the_township_it_sits_in(self):
        got = self._resolve(self._payload(
            locality="Township of Campbell",
            admin=[{"name": "Greene County", "adminLevel": 6},
                   {"name": "Township of Campbell", "adminLevel": 7},
                   {"name": "Springfield", "adminLevel": 8}]))
        self.assertEqual(got, ("Springfield", "MO"))

    def test_an_unregistered_town_is_still_preferred_over_a_township(self):
        # Most small towns have no .gov domain; that must not push the scan
        # up to the county.
        got = self._resolve(self._payload(
            city="Fair Grove",
            admin=[{"name": "Township of Franklin", "adminLevel": 7}]))
        self.assertEqual(got, ("Fair Grove", "MO"))

    def test_nothing_usable_returns_nothing(self):
        self.assertIsNone(self._resolve(self._payload(
            locality="Unincorporated Area",
            admin=[{"name": "Unorganized Territory", "adminLevel": 7}])))

    def test_a_non_us_point_is_rejected(self):
        payload = self._payload(city="Toronto")
        payload["countryCode"] = "CA"
        self.assertIsNone(self._resolve(payload))

    def test_a_dead_provider_does_not_raise(self):
        for bad in (None, {}, {"countryCode": "US"}):
            with self.subTest(bad=bad):
                self.assertIsNone(self._resolve(bad))

    def test_malformed_admin_entries_are_survived(self):
        got = self._resolve(self._payload(
            city="Springfield", admin=[None, "junk", {"name": None}, 42]))
        self.assertEqual(got, ("Springfield", "MO"))


class AuthorityDetectionTests(unittest.TestCase):
    def test_organisations_are_recognised(self):
        for name in ("Greene County", "St. Louis County", "Wayne Township",
                     "Ozark R-VI School District", "Metro Transit Authority",
                     "Public Water Supply District No. 2", "Levee Commission",
                     "Housing Authority", "Regional Airport", "Port Authority",
                     "Board of Education", "University of Missouri"):
            with self.subTest(name=name):
                self.assertTrue(ls._looks_like_authority(name), name)

    def test_plain_towns_are_not(self):
        for name in ("Binghamton", "Barrington", "Aledo", "Springfield",
                     "O'Fallon", "Kansas City", "Lees Summit", "Nixa"):
            with self.subTest(name=name):
                self.assertFalse(ls._looks_like_authority(name), name)

    def test_garbage_input_does_not_raise(self):
        for bad in (None, "", 12345):
            with self.subTest(bad=bad):
                ls._looks_like_authority(bad)


class DetailPageEnrichmentTests(unittest.TestCase):
    """Listing pages carry no contact details, so every bid from the most
    reliable source in the pipeline used to arrive with nothing to call."""

    PAGE = ("<p>Description: Replace 4,800 LF of sidewalk and 22 ADA ramps.</p>"
            "<p>Contact Person: Marla Whitfield</p>"
            "<p>mwhitfield@example-city.gov</p><p>(417) 864-1976</p>")

    def test_contacts_are_filled_in_from_the_posting(self):
        rows = [{"title": "Sidewalk Program", "url": "https://x.gov/Bids.aspx?bidID=1"}]
        with patch.object(ls, "_fetch_page", return_value=(self.PAGE, "ok")):
            ls._enrich_from_detail_pages(rows)
        self.assertEqual(rows[0]["email"], "mwhitfield@example-city.gov")
        self.assertEqual(rows[0]["phone"], "(417) 864-1976")
        self.assertEqual(rows[0]["contact"], "Marla Whitfield")
        self.assertIn("4,800 LF", rows[0]["scope"])

    def test_an_unreachable_posting_costs_its_contacts_not_the_bid(self):
        rows = [{"title": "Sidewalk Program", "url": "https://x.gov/b/1"}]
        with patch.object(ls, "_fetch_page", side_effect=RuntimeError("boom")):
            ls._enrich_from_detail_pages(rows)
        self.assertEqual(rows[0]["title"], "Sidewalk Program")
        self.assertEqual(rows[0].get("email", ""), "")

    def test_details_already_known_are_not_overwritten(self):
        rows = [{"title": "X", "url": "https://x.gov/b/1", "email": "real@city.gov"}]
        with patch.object(ls, "_fetch_page", return_value=(self.PAGE, "ok")):
            ls._enrich_from_detail_pages(rows)
        self.assertEqual(rows[0]["email"], "real@city.gov")

    def test_the_funnel_reports_how_many_bids_have_someone_to_call(self):
        rows = [{"title": "A", "url": "https://x.gov/1"},
                {"title": "B", "url": "https://x.gov/2"}]
        stats = {}
        pages = {"https://x.gov/1": (self.PAGE, "ok"), "https://x.gov/2": ("", "ok")}
        with patch.object(ls, "_fetch_page", side_effect=lambda u, timeout=None: pages[u]):
            ls._enrich_from_detail_pages(rows, stats=stats)
        self.assertEqual(stats.get("contacts_found"), 1, stats)
        self.assertEqual(stats.get("contacts_missing"), 1, stats)

    def test_postings_are_read_concurrently(self):
        # Sequential detail fetches are how a working scan turns into a
        # client-side abort, which looks identical to "no bids".
        overlap = {"now": 0, "max": 0}
        guard = threading.Lock()

        def slow(url, timeout=None):
            with guard:
                overlap["now"] += 1
                overlap["max"] = max(overlap["max"], overlap["now"])
            time.sleep(0.12)
            with guard:
                overlap["now"] -= 1
            return "", "ok"

        rows = [{"title": f"J{i}", "url": f"https://x.gov/{i}"} for i in range(6)]
        with patch.object(ls, "_fetch_page", side_effect=slow):
            started = time.time()
            ls._enrich_from_detail_pages(rows)
            elapsed = time.time() - started
        self.assertGreater(overlap["max"], 1, "detail pages fetched one at a time")
        self.assertLess(elapsed, 0.6, f"took {elapsed:.2f}s for 6 postings")

    def test_a_short_timeout_is_used(self):
        seen = {}

        def record(url, timeout=None):
            seen[url] = timeout
            return "", "ok"

        with patch.object(ls, "_fetch_page", side_effect=record):
            ls._enrich_from_detail_pages([{"title": "X", "url": "https://x.gov/1"}])
        self.assertEqual(seen["https://x.gov/1"], ls.PROBE_TIMEOUT)
        self.assertLess(ls.PROBE_TIMEOUT, ls.FETCH_TIMEOUT)

    def test_rows_without_a_url_are_skipped_quietly(self):
        rows = [{"title": "X"}, {"title": "Y", "url": ""}]
        with patch.object(ls, "_fetch_page", side_effect=AssertionError("fetched!")):
            ls._enrich_from_detail_pages(rows)

    def test_empty_input_is_handled(self):
        for bad in (None, []):
            with self.subTest(bad=bad):
                ls._enrich_from_detail_pages(bad)


if __name__ == "__main__":
    unittest.main()
