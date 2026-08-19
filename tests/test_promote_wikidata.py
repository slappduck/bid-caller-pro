"""Tests for tools/promote_wikidata_portals.py.

This step is what carries a verified Wikidata find into the live directory
bid_portals.py reads, so a mistake here is invisible until scans quietly stop
matching a town. The two things worth pinning down are the (city, state) key
-- bid_portals._rows_to_seeds looks seeds up by it, so a label that kept its
", Missouri" suffix would never match -- and the found-only filter, since
data/wikidata_verified.csv deliberately keeps every failed probe.
"""
import csv
import importlib.util
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location(
    "promote_wikidata_portals",
    os.path.join(ROOT, "tools", "promote_wikidata_portals.py"))
promote = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(promote)

import bid_portals


VERIFIED_FIELDS = ["state", "place", "domain", "status", "owns", "bid_url",
                   "relevant"]


def _write_verified(path, rows):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=VERIFIED_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in VERIFIED_FIELDS})


def _row(**kw):
    base = {"state": "MO", "place": "Aurora", "domain": "aurora-cityhall.org",
            "status": "found", "owns": "domain", "relevant": "no",
            "bid_url": "https://aurora-cityhall.org/Bids.aspx"}
    base.update(kw)
    return base


class CityLabelTests(unittest.TestCase):
    def test_a_state_name_suffix_is_stripped(self):
        self.assertEqual(promote.clean_city("Springfield, Missouri", "MO"),
                         "Springfield")

    def test_a_state_abbreviation_suffix_is_stripped(self):
        self.assertEqual(promote.clean_city("Aurora, MO", "MO"), "Aurora")

    def test_a_plain_label_is_left_alone(self):
        self.assertEqual(promote.clean_city("Lee's Summit", "MO"),
                         "Lee's Summit")

    def test_a_different_states_suffix_is_not_stripped(self):
        """Kansas City, Kansas is in KS; the label must not be trimmed using
        Missouri's name just because the row says MO."""
        self.assertEqual(promote.clean_city("Kansas City, Kansas", "MO"),
                         "Kansas City, Kansas")

    def test_a_city_whose_name_contains_a_state_name_survives(self):
        self.assertEqual(promote.clean_city("Kansas City", "MO"),
                         "Kansas City")


class EntityTypeTests(unittest.TestCase):
    def test_counties_are_labelled_counties(self):
        self.assertEqual(promote.entity_type("Greene County"), "County")

    def test_townships_are_labelled_townships(self):
        self.assertEqual(promote.entity_type("Union Township"), "Township")

    def test_everything_else_is_a_city(self):
        self.assertEqual(promote.entity_type("Aurora"), "City")


class PlatformTests(unittest.TestCase):
    def test_bids_aspx_is_civicplus(self):
        self.assertEqual(promote.platform_of("https://x.org/Bids.aspx"),
                         "civicplus")
        self.assertEqual(promote.platform_of("https://x.org/bids.aspx"),
                         "civicplus")

    def test_anything_else_is_the_agencys_own_site(self):
        self.assertEqual(promote.platform_of("https://x.org/purchasing"),
                         "agency")


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.src = os.path.join(self.dir.name, "verified.csv")
        self.dst = os.path.join(self.dir.name, "portals.csv")

    def _run(self, extra=()):
        argv = sys.argv
        sys.argv = ["promote", "--in", self.src, "--out", self.dst, *extra]
        try:
            promote.main()
        finally:
            sys.argv = argv
        if not os.path.exists(self.dst):
            return []
        with open(self.dst, newline="") as fh:
            return list(csv.DictReader(fh))

    def test_only_found_rows_are_promoted(self):
        _write_verified(self.src, [
            _row(),
            _row(domain="b.org", place="B", status="no_bid_page", bid_url=""),
            _row(domain="c.org", place="C", status="unreachable", bid_url=""),
            _row(domain="d.org", place="D", status="not_this_town", bid_url=""),
        ])
        out = self._run()
        self.assertEqual([r["domain"] for r in out], ["aurora-cityhall.org"])

    def test_a_found_row_with_no_url_is_not_promoted(self):
        """status and bid_url disagreeing is a data bug, not a reason to write
        a seed the scanner would then try to fetch."""
        _write_verified(self.src, [_row(bid_url="")])
        self.assertEqual(self._run(), [])

    def test_the_promoted_row_is_keyed_the_way_bid_portals_reads_it(self):
        _write_verified(self.src, [_row(place="Springfield, Missouri",
                                        domain="springfieldmo.gov")])
        self._run()
        seeds = bid_portals._rows_to_seeds(self.dst)
        self.assertIn(("Springfield", "MO"), seeds)

    def test_duplicate_domains_are_collapsed(self):
        _write_verified(self.src, [_row(), _row(place="Aurora City")])
        self.assertEqual(len(self._run()), 1)

    def test_rerunning_preserves_the_original_checked_date(self):
        _write_verified(self.src, [_row()])
        self._run()
        with open(self.dst) as fh:
            fh.readline()
            first = fh.readline()
        with open(self.dst, newline="") as fh:
            rows = list(csv.DictReader(fh))
        rows[0]["checked_date"] = "2020-01-01"
        with open(self.dst, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=promote.COLUMNS)
            w.writeheader()
            w.writerows(rows)
        again = self._run()
        self.assertEqual(again[0]["checked_date"], "2020-01-01")
        self.assertTrue(first)

    def test_dry_run_writes_nothing(self):
        _write_verified(self.src, [_row()])
        self.assertEqual(self._run(["--dry-run"]), [])


if __name__ == "__main__":
    unittest.main()
