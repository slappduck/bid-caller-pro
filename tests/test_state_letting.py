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
