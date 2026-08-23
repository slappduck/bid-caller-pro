"""State DOT letting pages: reading them, and refusing to guess where they are.

A city bid page says where it is by whose site it is on. A state letting page
does not -- one table carries work from every corner of the state, and the
only location a row gives you is a county name inside it. That makes two
things dangerous, and every test here came from actually hitting one of them
against a live page:

  * A nav menu looks like a listing. Arkansas's page produced 633 "rows", 32
    of which passed the trade filter: "ADA", "Asphalt Binder Price Index",
    "Historic Structures Bridge Demolition Movie Clips". All menu entries.
  * A county name is not a location. TxDOT's facilities table has a DISTRICT
    column whose values are also county names, so a building at "6601 Boucher
    Drive Edmond, OK" was tagged Houston County, Texas. Louisiana's rows
    matched parishes off street names ("St. Mary Street"). Washington's
    matched search-facet chips ("Public Works Awarded Pierce County").

A bid pinned to the wrong place is worse than one never found: the contractor
drives to it. So the rule is explicit evidence or nothing.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_sources
import counties


def table(rows, header=None):
    out = ["<table>"]
    if header:
        out.append("<tr>" + "".join("<th>%s</th>" % h for h in header) + "</tr>")
    for r in rows:
        out.append("<tr>" + "".join("<td>%s</td>" % c for c in r) + "</tr>")
    return "\n".join(out + ["</table>"])


class CountyLookupTests(unittest.TestCase):
    def test_table_loaded(self):
        self.assertGreater(counties.county_count(), 3000)
        self.assertIn("MO", counties.loaded_states())

    def test_saint_and_st_are_the_same_place(self):
        self.assertEqual(counties.lookup("MO", "Saint Louis"),
                         counties.lookup("MO", "St. Louis"))

    def test_trailing_words_are_the_name(self):
        # "(1): Job JSR0028 Route 18 HENRY County" -- the name is HENRY, not
        # the whole cell. Capturing the whole cell matched nothing, and a
        # stray short cell elsewhere in the row then supplied a wrong county.
        got = counties.counties_named(
            ["(1): Job JSR0028 Route 18 HENRY County. Coldmill and resurface"],
            "MO")
        self.assertEqual([g[0] for g in got], ["henry"])

    def test_comma_list_of_counties(self):
        got = counties.counties_named(
            ["Route Various CALLAWAY, CAMDEN, MARIES, MONITEAU, OSAGE, "
             "PHELPS, PULASKI, WASHINGTON County. ADA improvements"], "MO")
        self.assertEqual(len(got), 8)
        self.assertIn("callaway", [g[0] for g in got])

    def test_parish_counts_as_a_label(self):
        got = counties.counties_named(["work in St. Mary Parish"], "LA")
        self.assertEqual([g[0] for g in got], ["st mary"])

    def test_bare_name_needs_a_header_to_be_trusted(self):
        cells = ["121851-26-01", "Houston", "6601 Boucher Drive Edmond, OK"]
        self.assertEqual(counties.counties_named(cells, "TX"), [])
        got = counties.counties_named(cells, "TX", county_column=1)
        self.assertEqual([g[0] for g in got], ["houston"])

    def test_street_name_is_not_a_parish(self):
        # Louisiana: "St. Mary Street Sidewalks" is in a project-name column,
        # with no parish column anywhere on the page.
        self.assertEqual(
            counties.counties_named(
                ["H.011833", "St. Mary Street Sidewalks", "8/19/2026"], "LA"),
            [])

    def test_substring_is_not_a_match(self):
        self.assertEqual(counties.counties_in("work in Cassville", "MO"), [])


class RowShapeTests(unittest.TestCase):
    def test_nav_menu_is_not_a_listing(self):
        nav = "".join("<li>%s</li>" % t for t in (
            "ADA", "Asphalt Binder Price Index", "Keep Arkansas Beautiful",
            "Historic Structures Bridge Demolition Movie Clips",
            "Construction Program Stormwater Pollution Prevention Plan"))
        self.assertEqual(bid_sources.letting_rows("<ul>%s</ul>" % nav), [])

    def test_a_dated_row_is_a_record(self):
        html = table([["A1", "Sidewalk replacement on Main St", "9/18/2026"]])
        self.assertEqual(len(bid_sources.letting_rows(html)), 1)

    def test_header_row_is_not_emitted_as_a_record(self):
        html = table([["T1922", "Manatee", "Resurfacing", "9/1/2026"]],
                     header=["Project", "County", "Work", "Letting"])
        rows = bid_sources.letting_rows(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], 1)          # county column found
        self.assertNotIn("Project", rows[0][0])

    def test_district_header_does_not_mark_a_county_column(self):
        html = table([["121851", "Houston", "Roof repair", "9/1/2026"]],
                     header=["Project", "District", "Work", "Letting"])
        self.assertIsNone(bid_sources.letting_rows(html)[0][2])


class ParseStateLettingTests(unittest.TestCase):
    def test_missouri_prose_shape(self):
        html = table([[
            "D05",
            "(1): Job JCD0190 Route 163 BOONE County. Resurface from Route K "
            "to Route 63 outer road, the total length being 5.916 miles.",
            "9/18/2026"]])
        rows = bid_sources.parse_state_letting(
            html, "MO", "https://x.mo.gov/L", counties.counties_named)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["county"], "boone")
        self.assertEqual(rows[0]["source"], "state_dot")
        self.assertTrue(rows[0]["lat"] and rows[0]["lon"])

    def test_florida_column_shape(self):
        html = table([["T1922", "Manatee", "Resurfacing", "9/1/2026"],
                      ["T3988", "Leon", "Resurfacing", "9/1/2026"]],
                     header=["Project", "County", "Work", "Letting"])
        rows = bid_sources.parse_state_letting(
            html, "FL", "https://x.fl.gov/L", counties.counties_named)
        self.assertEqual(sorted(r["county"] for r in rows), ["leon", "manatee"])

    def test_texas_district_row_is_refused(self):
        html = table([["121851-26-01", "Houston",
                       "6601 Boucher Drive Edmond, OK 73034", "9/1/2026"]],
                     header=["Project", "District", "Location", "Letting"])
        self.assertEqual(bid_sources.parse_state_letting(
            html, "TX", "https://x.tx.gov/L", counties.counties_named), [])

    def test_unplaceable_row_is_dropped_not_shown_statewide(self):
        html = table([["H.011833", "Doucet Rd Sidewalks", "8/19/2026",
                       "CE&I services to construct ADA compliant ramps"]])
        self.assertEqual(bid_sources.parse_state_letting(
            html, "LA", "https://x.la.gov/L", counties.counties_named), [])

    def test_off_trade_row_is_dropped(self):
        html = table([["X1", "Audit services for FINNEY County", "9/1/2026"]])
        self.assertEqual(bid_sources.parse_state_letting(
            html, "KS", "https://x.ks.gov/L", counties.counties_named), [])

    def test_duplicate_rows_collapse(self):
        row = ["D05", "Route 163 BOONE County. Resurface from Route K to "
               "Route 63 outer road, total length 5.916 miles.", "9/18/2026"]
        rows = bid_sources.parse_state_letting(
            table([row, row]), "MO", "u", counties.counties_named)
        self.assertEqual(len(rows), 1)

    def test_multi_county_job_keeps_the_full_list(self):
        html = table([["D07", "Route Various CALLAWAY, CAMDEN, OSAGE County. "
                       "ADA improvements at 10 locations.", "9/18/2026"]])
        rows = bid_sources.parse_state_letting(
            html, "MO", "u", counties.counties_named)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["all_counties"]), 3)

    def test_empty_input_is_safe(self):
        self.assertEqual(bid_sources.parse_state_letting("", "MO", "", None), [])
        self.assertEqual(bid_sources.letting_rows(None), [])


if __name__ == "__main__":
    unittest.main()


class BundledJobTests(unittest.TestCase):
    """A state "call" is a procurement unit, not a job.

    MoDOT numbers several jobs inside one cell: "(1): Job JSR0028 Route 18
    HENRY County... (2): Job JSR0033 Route 54 CEDAR, ST CLAIR County." Read
    whole, that row names four counties and gets placed at whichever is
    nearest the contractor -- so a Springfield scan showed a card headed
    "Polk County, 28mi" whose description was about work in Henry.
    """

    def test_bundle_splits_into_one_job_each(self):
        got = bid_sources.split_bundled_jobs(
            "(1): Job A HENRY County. Resurface. (2): Job B CEDAR County. Mill.")
        self.assertEqual(len(got), 2)
        self.assertTrue(got[0].startswith("Job A"))

    def test_single_marker_is_stripped(self):
        self.assertEqual(
            bid_sources.split_bundled_jobs("(1): Job X COLE County. Repair."),
            ["Job X COLE County. Repair."])

    def test_unnumbered_text_is_untouched(self):
        self.assertEqual(bid_sources.split_bundled_jobs("Resurfacing"),
                         ["Resurfacing"])

    def test_empty_is_empty(self):
        self.assertEqual(bid_sources.split_bundled_jobs(""), [])
        self.assertEqual(bid_sources.split_bundled_jobs(None), [])

    def test_each_split_job_keeps_its_own_county(self):
        html = table([["G02",
                       "(1): Job JSR0028 Route 18 HENRY County. Coldmill and "
                       "resurface on Ohio Street, 2.059 miles. "
                       "(2): Job JSR0442 Route 32 CEDAR County. Coldmill and "
                       "resurface on Route 32, 4.1 miles.",
                       "9/18/2026"]])
        rows = bid_sources.parse_state_letting(
            html, "MO", "u", counties.counties_named)
        by_county = {r["county"]: r for r in rows}
        self.assertEqual(sorted(by_county), ["cedar", "henry"])
        self.assertIn("HENRY", by_county["henry"]["scope"])
        self.assertNotIn("CEDAR", by_county["henry"]["scope"])

    def test_off_trade_half_of_a_bundle_is_dropped(self):
        html = table([["G03",
                       "(1): Job A Route 5 HENRY County. Sidewalk and curb "
                       "ramp replacement along Main Street. "
                       "(2): Job B Route 9 CEDAR County. Legal services for "
                       "right of way acquisition consulting.",
                       "9/18/2026"]])
        rows = bid_sources.parse_state_letting(
            html, "MO", "u", counties.counties_named)
        self.assertEqual([r["county"] for r in rows], ["henry"])

    def test_county_column_survives_a_split(self):
        # Florida's county lives in its own column, so it stays authoritative
        # no matter how the description is broken up.
        html = table([["T1922", "Manatee", "Resurfacing", "9/1/2026"]],
                     header=["Project", "County", "Work", "Letting"])
        rows = bid_sources.parse_state_letting(
            html, "FL", "u", counties.counties_named)
        self.assertEqual([r["county"] for r in rows], ["manatee"])

    def test_places_carries_every_county_for_nearest_pick(self):
        html = table([["D07", "Route Various CALLAWAY, CAMDEN, OSAGE County. "
                       "ADA improvements at 10 locations.", "9/18/2026"]])
        rows = bid_sources.parse_state_letting(
            html, "MO", "u", counties.counties_named)
        self.assertEqual(len(rows[0]["places"]), 3)
        for _name, lat, lon in rows[0]["places"]:
            self.assertTrue(lat and lon)


class StateRouteLanguageTests(unittest.TestCase):
    """State DOTs write addresses as route numbers, and the filter did not.

    The roadway word list was written for municipal postings -- street, road,
    avenue, boulevard -- and had no state-route designations in it at all. So
    "Coldmill and resurface on Ohio Street" passed and the otherwise identical
    "Coldmill and resurface on Route 32" did not. Missouri's letting page went
    from 6 usable rows to 18 when that was fixed; a 125-mile Springfield scan
    went from 4 state bids to 9.
    """

    def test_numbered_state_route(self):
        self.assertTrue(bid_sources.looks_relevant(
            "Coldmill and resurface on Route 32, 4.1 miles"))

    def test_lettered_state_route(self):
        self.assertTrue(bid_sources.looks_relevant(
            "Resurface Route K from I-49 near Nevada to County Road 1800"))

    def test_interstate(self):
        self.assertTrue(bid_sources.looks_relevant(
            "Rehabilitation of I-70 mainline pavement"))

    def test_us_highway(self):
        self.assertTrue(bid_sources.looks_relevant(
            "Widening of US 63 through the county"))

    def test_a_multi_digit_route_number_is_not_cut_short(self):
        # The first version matched a single character after "Route" and then
        # required a word boundary, so "Route K" passed and "Route 32" failed
        # on its second digit.
        for n in ("5", "32", "160", "1800"):
            self.assertTrue(
                bid_sources.looks_relevant("Resurfacing of Route %s" % n),
                "Route %s should be road work" % n)

    def test_bus_route_is_not_this_trade(self):
        self.assertFalse(bid_sources.looks_relevant(
            "Bus route improvement study for the transit authority"))

    def test_transit_words_do_not_block_a_real_concrete_job(self):
        # The transit guard must not veto a job that names concrete outright.
        self.assertTrue(bid_sources.looks_relevant(
            "Concrete bus pad and sidewalk replacement along the bus route"))

    def test_route_optimisation_is_not_road_work(self):
        self.assertFalse(bid_sources.looks_relevant(
            "Route optimization consulting services"))


class PlanHolderTests(unittest.TestCase):
    """Who is bidding a state job as prime -- i.e. who needs a concrete sub.

    This is the answer to "why show me a highway contract I cannot win". The
    letting publishes the contractors who pulled plans, and two of the eight
    on one live MoDOT call were themselves concrete companies, which is the
    clearest evidence that subs already work this list.
    """

    HEADER = ["Prime", "Name - Vendor #", "Organization", "Address",
              "Phone", "Email", "Fax"]

    def _page(self, rows):
        return table(rows, header=self.HEADER)

    def test_reads_company_contact_phone_email(self):
        got = bid_sources.parse_plan_holders(self._page([
            ["", "Rhea, Don 0010907", "Don Schnieders Excavating Company, Inc.",
             "1307 Fairgrounds Road Jefferson City, MO", "573-893-2251",
             "drhea@dsecompany.com", ""]]))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["company"],
                         "Don Schnieders Excavating Company, Inc.")
        self.assertEqual(got[0]["contact"], "Don Rhea")
        self.assertEqual(got[0]["email"], "drhea@dsecompany.com")

    def test_vendor_number_column_is_not_the_company(self):
        # "Name - Vendor #" holds the state's id for the PERSON. Matching
        # "vendor" as a company word returned "Rhea, Don 0010907" as the firm
        # and never read the Organization column at all.
        got = bid_sources.parse_plan_holders(self._page([
            ["", "Watson, Mark 0028254", "Watson Concrete, Inc.", "Columbia MO",
             "573-228-6678", "mark@watsonconcreteinc.com", ""]]))
        self.assertEqual(got[0]["company"], "Watson Concrete, Inc.")
        self.assertNotIn("0028254", got[0]["contact"])

    def test_a_row_with_no_way_to_reach_anyone_is_not_a_lead(self):
        self.assertEqual(bid_sources.parse_plan_holders(self._page([
            ["", "Doe, Jane", "Ghost Contracting LLC", "Nowhere", "", "", ""]])),
            [])

    def test_company_is_required(self):
        self.assertEqual(bid_sources.parse_plan_holders(self._page([
            ["", "Doe, Jane", "", "Nowhere", "555-1212", "j@x.com", ""]])), [])

    def test_duplicate_companies_collapse(self):
        row = ["", "A, B", "Same Co", "X", "555-000-1111", "b@same.com", ""]
        self.assertEqual(len(bid_sources.parse_plan_holders(
            self._page([row, row]))), 1)

    def test_webmaster_addresses_are_dropped_but_the_row_survives(self):
        got = bid_sources.parse_plan_holders(self._page([
            ["", "A, B", "Real Co", "X", "555-000-1111", "webmaster@x.gov", ""]]))
        self.assertEqual(got[0]["email"], "")
        self.assertEqual(got[0]["phone"], "555-000-1111")

    def test_limit_is_respected(self):
        rows = [["", "P%d, Q" % i, "Co %d" % i, "X", "555-000-000%d" % i,
                 "a%d@x.com" % i, ""] for i in range(9)]
        self.assertEqual(len(bid_sources.parse_plan_holders(
            self._page(rows), limit=4)), 4)

    def test_name_without_a_comma_is_left_alone(self):
        got = bid_sources.parse_plan_holders(self._page([
            ["", "Estimating Dept", "Ti-Zack Concrete, LLC", "MN",
             "507-357-6463", "estimating@tizack.com", ""]]))
        self.assertEqual(got[0]["contact"], "Estimating Dept")

    def test_index_link_is_found_on_the_letting_page(self):
        html = '<a href="/BidLettingPlansRoom/PlanHolder/Index/6128">' \
               'Plan Holder List (MoDOT Plans Room)</a>'
        self.assertEqual(
            bid_sources.plan_holder_index(
                html, "https://x.mo.gov/BidLettingPlansRoom/Letting"),
            "https://x.mo.gov/BidLettingPlansRoom/PlanHolder/Index/6128")

    def test_call_url_is_built_from_the_index(self):
        self.assertEqual(
            bid_sources.plan_holder_url_for_call(
                "https://x.mo.gov/BidLettingPlansRoom/PlanHolder/Index/6128",
                "G02"),
            "https://x.mo.gov/BidLettingPlansRoom/PlanHolder/Call/6128?call=G02")

    def test_call_url_needs_both_parts(self):
        self.assertEqual(bid_sources.plan_holder_url_for_call("", "G02"), "")
        self.assertEqual(bid_sources.plan_holder_url_for_call(
            "https://x.mo.gov/PlanHolder/Index/1", ""), "")
        # A URL that is not the shape this function knows must decline rather
        # than invent one -- a plausible 404 is worse than no link.
        self.assertEqual(bid_sources.plan_holder_url_for_call(
            "https://other.gov/bids", "G02"), "")

    def test_state_rows_carry_their_call_number(self):
        html = table([["G02", "Route 18 HENRY County. Coldmill and resurface "
                       "on Ohio Street, 2.059 miles.", "9/18/2026"]])
        rows = bid_sources.parse_state_letting(
            html, "MO", "u", counties.counties_named)
        self.assertEqual(rows[0]["call"], "G02")

    def test_empty_page_is_safe(self):
        self.assertEqual(bid_sources.parse_plan_holders(""), [])
        self.assertEqual(bid_sources.parse_plan_holders(None), [])
        self.assertEqual(bid_sources.plan_holder_index(None), "")


class NoticeToContractorsTests(unittest.TestCase):
    """Some states publish the letting as prose, with no table at all.

    Alabama's Notice to Contractors is numbered blocks:
      1. DEMOF-RPF-NHF-PRF-A210(943), TUSCALOOSA COUNTY  Contract Time: 620
      Working Days for constructing the Bridge Replacement and Approaches...
    Everything needed is there and a table-only reader sees none of it.
    """

    NOTICE = (
        "1. DEMOF-A210(943) , TUSCALOOSA COUNTY Contract Time: 620 Working "
        "Days for constructing the Bridge Replacement and Approaches "
        "(Grading, Drainage, Pavement and Traffic Stripe). "
        "15. HRRR-1126(250) , CHILTON COUNTY Contract Time: 30 Working Days "
        "for constructing sidewalk and curb ramp work on County Road 42 near "
        "the Shelby County line. "
        "26. STPSU-3525(253) , HOUSTON COUNTY Contract Time: 45 Working Days "
        "for resurfacing on Route 84 including curb and gutter.")

    def _rows(self):
        return bid_sources.parse_notice_to_contractors(
            "<p>%s</p>" % self.NOTICE, "AL", "u", counties.counties_named)

    def test_each_numbered_block_is_a_job(self):
        self.assertEqual(len(self._rows()), 3)

    def test_the_county_is_the_one_next_to_the_project_number(self):
        # The Chilton block also names Shelby, which is far larger. Picking by
        # population put a Chilton County job in Shelby.
        by_id = {r["call"]: r["county"] for r in self._rows()}
        self.assertEqual(by_id["HRRR-1126(250)"], "chilton")

    def test_item_number_is_not_shown(self):
        for r in self._rows():
            self.assertFalse(r["scope"].startswith(("1.", "15.", "26.")))

    def test_project_id_becomes_the_call_number(self):
        self.assertIn("DEMOF-A210(943)", [r["call"] for r in self._rows()])

    def test_the_bare_index_at_the_top_is_not_mistaken_for_jobs(self):
        # The page repeats the list as a short index before the real entries.
        index = "1. DEMOF-A210(943), TUSCALOOSA 15. HRRR-1126(250), CHILTON"
        self.assertEqual(
            bid_sources.notice_to_contractors_items(index, min_len=60), [])

    def test_prose_reader_only_runs_when_tables_gave_nothing(self):
        html = table([["T1922", "Manatee", "Resurfacing", "9/1/2026"]],
                     header=["Project", "County", "Work", "Letting"])
        rows = bid_sources.parse_state_letting(
            html, "FL", "u", counties.counties_named)
        self.assertEqual([r["county"] for r in rows], ["manatee"])


class LettingIndexTests(unittest.TestCase):
    """Alabama's letting URL changes every letting, so we store the index.

    .../NTC/2026/NTC_August_28_2026.html works today and 404s next month.
    Storing that address means the source dies silently when it rotates.
    """

    INDEX = '''
      <a href="/DW_Pages/NTC/2026/NTC_August_28_2026.html">Notice to Contractors</a>
      <a href="/DW_Pages/NTC/2026/NTC_July_31_2026.html">Notice to Contractors</a>
      <a href="/WEBPROPS/2026/August 28, 2026/BidAugust2826.pdf">August 28, 2026 Letting</a>
      <a href="/DW_Pages/Prior_Lettings/Prior_Letting_2025.html">Prior Lettings 2025</a>
    '''

    def _pick(self):
        return bid_sources.newest_letting_link(
            self.INDEX, "https://al.gov/", today=(2026, 8, 23))

    def test_picks_the_newest(self):
        self.assertIn("August_28_2026", self._pick())

    def test_prefers_a_page_over_a_pdf(self):
        # Both are the same letting; we cannot read the PDF, so choosing it
        # would make a working source look dead.
        self.assertTrue(self._pick().endswith(".html"))

    def test_skips_prior_lettings(self):
        self.assertNotIn("Prior", self._pick())

    def test_far_future_dates_are_ignored(self):
        html = '<a href="/ntc/NTC_January_5_2031.html">Letting</a>' + self.INDEX
        self.assertNotIn("2031", bid_sources.newest_letting_link(
            html, "https://al.gov/", today=(2026, 8, 23)))

    def test_no_dated_letting_link_gives_nothing(self):
        self.assertEqual(bid_sources.newest_letting_link(
            '<a href="/about">About us</a>', "https://al.gov/"), "")

    def test_numeric_dates_are_understood(self):
        html = '<a href="/l?letting=9/18/2026">Letting Details</a>'
        self.assertIn("9/18/2026", bid_sources.newest_letting_link(
            html, "https://x.gov/", today=(2026, 8, 23)))


class LettingDateTests(unittest.TestCase):
    """A state row rarely states a due date -- the letting date IS the deadline.

    Without it every state bid arrived undated, could never be recognised as
    expired, and sat on the board forever. Worse, Florida publishes every
    letting from January onward on ONE page, so a Tampa scan was showing 51
    state jobs of which 43 had already been held.
    """

    def test_missouri_states_it_as_a_bid_opening(self):
        self.assertEqual(
            bid_sources.page_letting_date(
                "<p>Bid Opening Date: 09/18/2026</p>"), "09/18/2026")

    def test_alabama_states_it_only_in_the_url(self):
        self.assertEqual(
            bid_sources.page_letting_date(
                "<p>Notice to Contractors</p>",
                "https://x.al.gov/NTC/2026/NTC_August_28_2026.html"),
            "8/28/2026")

    def test_letting_wording(self):
        self.assertEqual(
            bid_sources.page_letting_date("<h2>September 30, 2026 Letting</h2>"),
            "September 30, 2026")

    def test_no_date_anywhere_is_empty(self):
        self.assertEqual(bid_sources.page_letting_date("<p>Bids</p>"), "")

    def test_the_date_becomes_the_deadline(self):
        html = ("<p>Bid Opening Date: 09/18/2026</p>" +
                table([["D05", "Route 163 BOONE County. Resurface from Route K "
                        "to Route 63 outer road, 5.916 miles.", "x"]]))
        rows = bid_sources.parse_state_letting(
            html, "MO", "u", counties.counties_named)
        self.assertEqual(rows[0]["deadline"], "09/18/2026")

    def test_a_row_with_its_own_date_keeps_it(self):
        html = ("<p>Bid Opening Date: 09/18/2026</p>" +
                table([["D05", "Route 163 BOONE County. Resurface Route K to "
                        "Route 63, 5.916 miles. Bids due 10/02/2026", "x"]]))
        self.assertEqual(bid_sources.parse_state_letting(
            html, "MO", "u", counties.counties_named)[0]["deadline"],
            "10/02/2026")

    def test_each_letting_section_gets_its_own_date(self):
        # Florida's shape: a date heading, then a one-row documents table,
        # then the projects table. The date does NOT sit directly above the
        # projects table, so it has to carry forward from the last heading.
        section = ('<h2>%s</h2>' + table([["Important Letting Documents"]]) +
                   table([["T19%d2", "Manatee", "Resurfacing"]],
                         header=["Proposal ID", "County", "Major Work Type"]))
        html = (section % ("February 25, 2026", 1)) + \
               (section % ("September 30, 2026", 2))
        rows = bid_sources.parse_state_letting(
            html, "FL", "u", counties.counties_named)
        self.assertEqual(sorted(r["deadline"] for r in rows),
                         ["February 25, 2026", "September 30, 2026"])


class SupplyAndServicesTests(unittest.TestCase):
    """Two kinds of thing that read as concrete work and are not.

    Both reached a live Nashville board. "Emulsified Asphalt for the Wilson
    County Road Commission" is a commodity order -- it names this trade's
    materials so every keyword fires, but there is nothing to build. And
    Construction Engineering & Inspection is the agency hiring a firm to WATCH
    somebody else build it; its scope describes concrete work in detail.
    """

    def test_material_orders_are_not_work(self):
        for t in ("Emulsified Asphalt for the Wilson County Road Commission",
                  "Metal Culverts for the Wilson County Road Commission",
                  "Purchase of Ready Mix Concrete for the Street Department",
                  "Annual Materials Contract - crushed stone",
                  "Supply of aggregate for the county"):
            self.assertFalse(bid_sources.looks_relevant(t), t)

    def test_furnish_and_install_is_work(self):
        # The supply shape alone must not veto a real job.
        self.assertTrue(bid_sources.looks_relevant(
            "Furnish and install 400 LF of curb and gutter on Main Street"))

    def test_buying_materials_to_build_with_is_work(self):
        self.assertTrue(bid_sources.looks_relevant(
            "Purchase of Ready Mix Concrete and installation of new sidewalk ramps"))

    def test_replacing_a_culvert_is_work(self):
        self.assertTrue(bid_sources.looks_relevant(
            "Replace corrugated metal culvert and restore roadway"))

    def test_cei_in_parentheses_is_caught(self):
        # "Inspection (CEI) Services" -- the parenthetical defeats the
        # adjacency the professional-services rule needs.
        self.assertFalse(bid_sources.looks_relevant(
            "Drakes Creek Road Widening - Construction Engineering & "
            "Inspection (CEI) Services"))

    def test_ce_and_i_shorthand_is_caught(self):
        self.assertFalse(bid_sources.looks_relevant(
            "CE&I services to construct sidewalks along both sides of "
            "St. Mary Street"))

    def test_construction_inspection_services_is_caught(self):
        self.assertFalse(bid_sources.looks_relevant(
            "Construction Inspection Services for the 2026 program"))

    def test_a_real_sidewalk_job_still_passes(self):
        for t in ("OLIVE ROAD SIDEWALK PROJECT",
                  "On-call contracting services for concrete sidewalk and curb work",
                  "ADA improvements, 10 Locations at various sites"):
            self.assertTrue(bid_sources.looks_relevant(t), t)


class ProjectCodeIsNotAYearTests(unittest.TestCase):
    """A project code is not a date.

    Alabama numbers a job "ATRP2-52-2024-263". The stale-year rule reads every
    year in a title and closes an undated bid when they are all past, so the
    2024 sitting in the middle of that sequence closed a live August 2026 job
    on sight -- it showed on a Nashville board as Closed with no deadline.

    Letter-led hyphenated codes are stripped before the year scan. Purely
    numeric ones like "2024-17" are deliberately left alone: after "Bid No."
    that usually IS the year it was issued, and closing it is right.
    """
    import license_server as ls_mod

    def _status(self, title):
        bid = {"title": title, "scope": "", "deadline": "", "status": "Open"}
        self.ls_mod._apply_stale_year(bid)
        return bid["status"]

    def test_alabama_project_code_does_not_close_a_live_job(self):
        self.assertEqual(self._status(
            "ATRP2-52-2024-263 , MORGAN COUNTY Contract Time: 90 Working Days "
            "for constructing the Roadway Improvements"), "Open")

    def test_other_state_code_shapes(self):
        for code in ("HRRR-0426(250)", "STPSU-3525(253)", "IM-IMGR-I065(571)",
                     "DEMOF-RPF-NHF-PRF-A210(943)"):
            self.assertEqual(self._status("%s BIBB COUNTY sidewalk work" % code),
                             "Open", code)

    def test_a_genuinely_stale_programme_still_closes(self):
        self.assertEqual(
            self._status("2025 Sidewalk Program - Scope of Work SW-1"), "Closed")
        self.assertEqual(
            self._status("FY2024 Street Maintenance Program"), "Closed")

    def test_a_year_range_spanning_now_is_left_open(self):
        self.assertEqual(self._status("2025-2026 Concrete Programme"), "Open")

    def test_this_year_is_open(self):
        self.assertEqual(self._status("2026 Curb and Gutter Replacement"), "Open")

    def test_a_dated_bid_is_never_touched_by_this_rule(self):
        bid = {"title": "2025 Sidewalk Program", "scope": "",
               "deadline": "12/01/2026", "status": "Open"}
        self.ls_mod._apply_stale_year(bid)
        self.assertEqual(bid["status"], "Open")


class UrlAgeTests(unittest.TestCase):
    """Where a posting is FILED is often the only statement of how old it is.

    A live board showed "Concrete Sidewalks and ADA Ramps Project" as OPEN
    with no closing date. Neither its title nor its scope named a year. The
    only evidence it was from July 2025 sat in the URL:
        .../fairfield/Purchasing/2025/2025-07 ITB Southport Community...
    """
    import license_server as ls_mod

    def _status(self, url, title="Concrete Sidewalks and ADA Ramps Project"):
        bid = {"title": title, "scope": "new concrete sidewalks, curbs, "
               "ADA compliant ramps", "deadline": "", "status": "Open",
               "url": url}
        self.ls_mod._apply_stale_year(bid)
        return bid["status"]

    def test_the_reported_listing_closes(self):
        self.assertEqual(self._status(
            "https://cms3.revize.com/revize/fairfield/Purchasing/2025/"
            "2025-07%20ITB%20Southport%20Community%20Connectivity.pdf"),
            "Closed")

    def test_this_years_folder_stays_open(self):
        self.assertEqual(self._status(
            "https://cms3.revize.com/revize/fairfield/Purchasing/2026/"
            "2026-03%20ITB%20Curb.pdf"), "Open")

    def test_a_bid_id_is_not_a_year(self):
        # CivicPlus addresses a posting as Bids.aspx?bidID=2024, where 2024 is
        # a row id. Reading that as a year would close a brand new bid.
        self.assertEqual(self._status("https://x.gov/Bids.aspx?bidID=2024"),
                         "Open")

    def test_only_date_shaped_path_segments_count(self):
        self.assertEqual(self._status("https://x.gov/bids/2026-street-program"),
                         "Open")
        self.assertEqual(self._status("https://x.gov/purchasing/current/rfp.pdf"),
                         "Open")

    def test_a_year_folder_alone_is_enough(self):
        self.assertEqual(self._status("https://x.gov/bids/2024/sidewalk.pdf"),
                         "Closed")

    def test_a_dated_bid_is_never_touched(self):
        bid = {"title": "x", "scope": "", "deadline": "12/01/2026",
               "status": "Open", "url": "https://x.gov/bids/2020/old.pdf"}
        self.ls_mod._apply_stale_year(bid)
        self.assertEqual(bid["status"], "Open")

    def test_no_url_is_safe(self):
        self.assertEqual(self._status(""), "Open")
        self.assertEqual(self.ls_mod._url_path_years(None), [])
