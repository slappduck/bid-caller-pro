"""Promoting verified Wikidata finds into the live portal directory.

tools/verify_wikidata_candidates.py writes every probe result to
data/wikidata_verified.csv, pass or fail, so failures stay auditable. This
step is the only thing standing between that raw probe log and
bid_portals._rows_to_seeds, which expects a specific column set -- and it
has to run repeatedly without churning the diff, because a national
re-harvest used to require doing this reshaping by hand.

Two things matter enough to pin:
  * `checked_date` for a domain already in the directory must not change on
    a re-run -- otherwise every promotion run would touch every row's date
    and the diff would never shrink to just what's new;
  * a Wikidata place label carries its own state suffix inconsistently
    ("Springfield" next to "Cairo, Illinois"), and bid_portals keys seeds on
    a plain (city, state) pair, so a label that keeps its suffix would never
    match a scan looking for the bare city name.

No network calls exist in this file at all -- it is pure CSV reshaping --
so these are all direct, unstubbed unit tests.
"""
import csv
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import promote_wikidata_portals as P  # noqa: E402


class CleanCityTests(unittest.TestCase):
    def test_a_full_state_name_suffix_is_stripped(self):
        self.assertEqual(P.clean_city("Springfield, Missouri", "MO"),
                         "Springfield")

    def test_a_two_letter_abbreviation_suffix_is_stripped(self):
        self.assertEqual(P.clean_city("Aurora, MO", "MO"), "Aurora")

    def test_a_plain_city_name_is_left_alone(self):
        self.assertEqual(P.clean_city("Springfield", "MO"), "Springfield")

    def test_the_match_is_case_insensitive(self):
        self.assertEqual(P.clean_city("Cairo, illinois", "IL"), "Cairo")

    def test_an_unknown_state_code_does_not_raise(self):
        """STATE_NAMES.get(state, "\\0") is the sentinel for this -- an
        unmapped code must fall through safely, not KeyError."""
        self.assertEqual(P.clean_city("Somewhere", "ZZ"), "Somewhere")


class EntityTypeTests(unittest.TestCase):
    def test_a_county_suffix_is_recognized(self):
        self.assertEqual(P.entity_type("Boone County"), "County")

    def test_a_county_of_prefix_is_recognized(self):
        self.assertEqual(P.entity_type("County of Boone"), "County")

    def test_a_township_suffix_is_recognized(self):
        self.assertEqual(P.entity_type("Union Township"), "Township")

    def test_a_plain_city_name_defaults_to_city(self):
        self.assertEqual(P.entity_type("Springfield"), "City")

    def test_a_borough_suffix_is_currently_typed_as_township(self):
        """Pinning current behavior, not endorsing it: entity_type() has no
        separate "Borough" bucket, so a name ending " Borough" is typed
        "Township" (tools/promote_wikidata_portals.py, entity_type()). This
        looks like a plausible mislabel -- flagged for a human to confirm
        rather than changed here, since it feeds the live portal directory."""
        self.assertEqual(P.entity_type("Matawan Borough"), "Township")


class PlatformOfTests(unittest.TestCase):
    def test_a_civicplus_bids_aspx_path_is_recognized(self):
        self.assertEqual(
            P.platform_of("https://cityofexample.org/Bids.aspx"), "civicplus")

    def test_the_match_is_case_insensitive(self):
        self.assertEqual(
            P.platform_of("https://cityofexample.org/BIDS.ASPX"), "civicplus")

    def test_anything_else_is_the_agencys_own_site(self):
        self.assertEqual(
            P.platform_of("https://cityofexample.org/bids/current"), "agency")


class ExistingDatesTests(unittest.TestCase):
    def test_a_missing_file_returns_an_empty_mapping(self):
        self.assertEqual(P.existing_dates("/no/such/file.csv"), {})

    def test_reads_domain_to_checked_date(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "portals.csv")
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=P.COLUMNS)
            w.writeheader()
            w.writerow({"domain": "example.gov", "checked_date": "2026-01-05"})
        self.assertEqual(P.existing_dates(path), {"example.gov": "2026-01-05"})


class MainPromotionTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.src = os.path.join(self.d, "wikidata_verified.csv")
        self.dst = os.path.join(self.d, "wikidata_portals.csv")

    def _write_src(self, rows, fields=("domain", "place", "state", "status",
                                       "bid_url", "owns")):
        with open(self.src, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)

    def _run(self, extra_args=None):
        argv = ["--in", self.src, "--out", self.dst] + (extra_args or [])
        with patch.object(sys, "argv", ["promote_wikidata_portals.py"] + argv):
            P.main()

    def _dst_rows(self):
        with open(self.dst, newline="") as fh:
            return list(csv.DictReader(fh))

    def test_only_found_rows_with_a_bid_url_are_promoted(self):
        self._write_src([
            {"domain": "found.gov", "place": "Rocheport, MO", "state": "MO",
             "status": "found", "bid_url": "https://found.gov/bids", "owns": ""},
            {"domain": "notfound.gov", "place": "Nowhere, MO", "state": "MO",
             "status": "not_found", "bid_url": "", "owns": ""},
            {"domain": "brokenurl.gov", "place": "Someplace, MO", "state": "MO",
             "status": "found", "bid_url": "", "owns": ""},
        ])
        self._run()
        out = self._dst_rows()
        self.assertEqual([r["domain"] for r in out], ["found.gov"])

    def test_the_city_is_cleaned_of_its_state_suffix(self):
        self._write_src([
            {"domain": "rocheportmo.us", "place": "Rocheport, Missouri",
             "state": "MO", "status": "found",
             "bid_url": "https://rocheportmo.us/bids", "owns": ""},
        ])
        self._run()
        out = self._dst_rows()
        self.assertEqual(out[0]["city"], "Rocheport")

    def test_a_re_run_keeps_the_original_checked_date(self):
        with open(self.dst, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=P.COLUMNS)
            w.writeheader()
            w.writerow({"domain": "found.gov", "city": "Rocheport",
                       "state": "MO", "type": "City", "org": "Rocheport",
                       "status": "found", "bid_url": "https://found.gov/bids",
                       "platform": "agency", "checked_date": "2026-01-01",
                       "source": "wikidata", "verified_by": ""})
        self._write_src([
            {"domain": "found.gov", "place": "Rocheport, MO", "state": "MO",
             "status": "found", "bid_url": "https://found.gov/bids", "owns": ""},
        ])
        self._run()
        out = self._dst_rows()
        self.assertEqual(out[0]["checked_date"], "2026-01-01")

    def test_a_brand_new_row_gets_todays_date(self):
        with patch.object(P.datetime, "date") as fake_date:
            fake_date.today.return_value.isoformat.return_value = "2026-09-15"
            self._write_src([
                {"domain": "newtown.us", "place": "New Town, MO", "state": "MO",
                 "status": "found", "bid_url": "https://newtown.us/bids", "owns": ""},
            ])
            argv = ["--in", self.src, "--out", self.dst]
            with patch.object(sys, "argv", ["promote_wikidata_portals.py"] + argv):
                P.main()
        out = self._dst_rows()
        self.assertEqual(out[0]["checked_date"], "2026-09-15")

    def test_duplicate_domains_keep_only_the_first(self):
        self._write_src([
            {"domain": "dup.gov", "place": "Placeone, MO", "state": "MO",
             "status": "found", "bid_url": "https://dup.gov/bids1", "owns": ""},
            {"domain": "dup.gov", "place": "Placetwo, MO", "state": "MO",
             "status": "found", "bid_url": "https://dup.gov/bids2", "owns": ""},
        ])
        self._run()
        out = self._dst_rows()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["bid_url"], "https://dup.gov/bids1")

    def test_a_row_missing_domain_city_or_state_is_skipped(self):
        self._write_src([
            {"domain": "", "place": "Rocheport, MO", "state": "MO",
             "status": "found", "bid_url": "https://x.us/bids", "owns": ""},
            {"domain": "x.us", "place": "", "state": "MO",
             "status": "found", "bid_url": "https://x.us/bids", "owns": ""},
        ])
        self._run()
        self.assertEqual(self._dst_rows(), [])

    def test_dry_run_writes_nothing(self):
        self._write_src([
            {"domain": "found.gov", "place": "Rocheport, MO", "state": "MO",
             "status": "found", "bid_url": "https://found.gov/bids", "owns": ""},
        ])
        self._run(["--dry-run"])
        self.assertFalse(os.path.exists(self.dst))

    def test_a_missing_input_file_exits_rather_than_writing_junk(self):
        argv = ["--in", os.path.join(self.d, "no-such-file.csv"),
                "--out", self.dst]
        with patch.object(sys, "argv", ["promote_wikidata_portals.py"] + argv):
            with self.assertRaises(SystemExit):
                P.main()


if __name__ == "__main__":
    unittest.main()
