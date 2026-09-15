"""Shortlisting new residential_permits.py SOURCES candidates from Socrata.

The tool's own docstring names the failure mode it exists to avoid: Fort
Worth's permit dataset has a field that sounds promising ("permit_type") but
whose actual values are generic buckets, so text-matching "driveway" against
a free-text description field mostly pulled in unrelated plumbing repairs
that happened to mention "driveway" as a location. A field name is never
enough -- only a GROUP BY on the field's real distinct values, with a
dedicated driveway/sidewalk/curb value and real volume behind it, is.

These tests pin the two filters that make that true (a field must look
promising by name AND not be one of the excluded generic ones; a value must
say driveway/sidewalk/curb specifically, and clear a minimum row count so one
mislabeled stray can't pass) and the shortlist behaviour around them: no
location fields means the dataset is never even worth a GROUP BY call, and a
dataset already seen under one search query is not probed again under
another.

The catalog and SoQL calls are stubbed by replacing _search_catalog and
_distinct_values -- the network never gets touched.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import discover_permit_sources as P  # noqa: E402


class CandidateFieldTests(unittest.TestCase):
    def test_a_field_needs_a_hint_to_be_worth_checking(self):
        self.assertEqual(P._candidate_fields(["contractor_name", "zip"]), [])

    def test_a_promising_name_is_kept(self):
        self.assertEqual(P._candidate_fields(["permit_type"]), ["permit_type"])

    def test_an_excluded_word_wins_even_when_a_hint_also_matches(self):
        """"county_type" contains the hint "type" but is excluded outright --
        a per-county breakdown is not a work category."""
        self.assertEqual(P._candidate_fields(["county_type"]), [])

    def test_worktype_is_recognised_despite_running_the_hints_together(self):
        self.assertEqual(P._candidate_fields(["worktype"]), ["worktype"])


class LocationFieldTests(unittest.TestCase):
    def test_lat_and_lon_together_count(self):
        self.assertTrue(P._has_location_fields(["latitude", "longitude"]))

    def test_latitude_alone_is_not_enough(self):
        self.assertFalse(P._has_location_fields(["latitude"]))

    def test_an_address_field_alone_counts(self):
        self.assertTrue(P._has_location_fields(["site_address"]))

    def test_no_location_signal_at_all(self):
        self.assertFalse(P._has_location_fields(["contractor_name", "fee"]))


class DistinctValuesTests(unittest.TestCase):
    def setUp(self):
        self._orig = P._fetch_json
        self.addCleanup(lambda: setattr(P, "_fetch_json", self._orig))

    def test_a_bad_count_is_coerced_to_zero_rather_than_raising(self):
        P._fetch_json = lambda url: [{"permit_type": "Driveway", "n": "oops"}]
        got = P._distinct_values("data.city.gov", "abcd-1234", "permit_type")
        self.assertEqual(got, [("Driveway", 0)])

    def test_a_row_with_no_value_is_skipped(self):
        P._fetch_json = lambda url: [{"permit_type": None, "n": 5}]
        self.assertEqual(P._distinct_values("d", "id", "permit_type"), [])

    def test_a_non_list_response_yields_nothing(self):
        P._fetch_json = lambda url: {"error": "timeout"}
        self.assertEqual(P._distinct_values("d", "id", "permit_type"), [])


class CheckDatasetTests(unittest.TestCase):
    def setUp(self):
        self._orig = P._distinct_values
        self.addCleanup(lambda: setattr(P, "_distinct_values", self._orig))

    def test_no_location_fields_means_no_network_call_at_all(self):
        """A dataset with nowhere to place a bid on a map is not worth the
        cost of even one GROUP BY."""
        def boom(*a, **kw):
            raise AssertionError("_distinct_values must not be called")
        P._distinct_values = boom
        got = P._check_dataset("d", "id", "Some Permits", ["permit_type"])
        self.assertIsNone(got)

    def test_the_fort_worth_trap_a_generic_category_is_rejected(self):
        """"permit_type" looks promising by name; its actual values are
        generic buckets with nothing about driveways in them."""
        P._distinct_values = lambda domain, did, field, limit=50: (
            [("Residential", 900), ("Commercial", 400)]
            if field == "permit_type" else [])
        got = P._check_dataset("d", "id", "Fort Worth Permits",
                               ["permit_type", "address"])
        self.assertIsNone(got)

    def test_a_single_stray_row_below_the_minimum_is_rejected(self):
        P._distinct_values = lambda domain, did, field, limit=50: (
            [("Driveway / Sidewalks", 1)] if field == "permit_type" else [])
        got = P._check_dataset("d", "id", "X", ["permit_type", "address"])
        self.assertIsNone(got)

    def test_a_genuine_dedicated_category_with_volume_is_accepted(self):
        P._distinct_values = lambda domain, did, field, limit=50: (
            [("Driveway / Sidewalks", 812), ("Building", 5000)]
            if field == "permit_type" else [])
        got = P._check_dataset("data.austintexas.gov", "abcd-1234",
                               "Austin Permits", ["permit_type", "latitude",
                                                   "longitude"])
        self.assertEqual(got["field"], "permit_type")
        self.assertEqual(got["value"], "Driveway / Sidewalks")
        self.assertEqual(got["count"], 812)

    def test_curb_ramp_counts_as_a_dedicated_category_too(self):
        P._distinct_values = lambda domain, did, field, limit=50: (
            [("Curb Ramp", 40)] if field == "work_class" else [])
        got = P._check_dataset("d", "id", "X", ["work_class", "address"])
        self.assertIsNotNone(got)


class MainShortlistTests(unittest.TestCase):
    """The discovery loop across several catalog queries."""

    def setUp(self):
        self._search = P._search_catalog
        self._check = P._check_dataset
        self.addCleanup(lambda: setattr(P, "_search_catalog", self._search))
        self.addCleanup(lambda: setattr(P, "_check_dataset", self._check))

    def _dataset(self, domain, did, columns, name="Permits"):
        return {"resource": {"id": did, "name": name,
                             "columns_field_name": columns},
                "metadata": {"domain": domain}}

    def test_the_same_dataset_returned_by_two_queries_is_checked_once(self):
        d = self._dataset("data.austintexas.gov", "abcd-1234",
                          ["permit_type", "address"])
        P._search_catalog = lambda q, limit=40: [d]
        checked = []

        def fake_check(domain, did, name, columns):
            checked.append((domain, did))
            return None
        P._check_dataset = fake_check

        old_argv = sys.argv
        sys.argv = ["discover_permit_sources.py", "--queries",
                    "building permits", "residential permits"]
        try:
            P.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(checked, [("data.austintexas.gov", "abcd-1234")])

    def test_a_dataset_with_no_columns_is_never_checked(self):
        d = self._dataset("data.example.gov", "zzzz-0000", [])
        P._search_catalog = lambda q, limit=40: [d]

        def boom(*a, **kw):
            raise AssertionError("must not check a columnless dataset")
        P._check_dataset = boom

        old_argv = sys.argv
        sys.argv = ["discover_permit_sources.py", "--queries", "permits"]
        try:
            P.main()  # would raise via boom() if this regressed
        finally:
            sys.argv = old_argv


if __name__ == "__main__":
    unittest.main()
