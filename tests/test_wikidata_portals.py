"""Tests for the non-.gov portal source (data/wikidata_portals.csv).

These towns exist in the directory for one reason: the national crawl is built
from the CISA .gov registry and structurally cannot see a city on a .com/.us
domain. Two things must stay true or they quietly stop counting:

  * they have to survive a re-crawl -- discover_bid_portals.py rewrites
    bid_portal_directory.csv wholesale, which is why this is a separate file;
  * a .gov page already known for a town must keep precedence, so this source
    can only ever ADD towns, never displace a verified one.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_portals


class WikidataSeedTests(unittest.TestCase):
    def test_the_file_is_present_and_parses(self):
        seeds = bid_portals._wikidata_seeds()
        self.assertGreater(len(seeds), 200,
                           "the non-.gov portal source is missing or empty")

    def test_every_seed_has_a_usable_url_and_platform(self):
        for (city, state), entries in bid_portals._wikidata_seeds().items():
            self.assertTrue(city and state, "a row lost its city or state")
            for e in entries:
                self.assertTrue(e["url"].startswith("http"),
                                f"{city}, {state}: {e['url']!r}")
                self.assertTrue(e["platform"])

    def test_no_domain_duplicates_the_national_crawl(self):
        """The real invariant. Not "nothing here is .gov" -- four of these ARE
        (frankfort.ky.gov, northlittlerock.ar.gov, mattoon.illinois.gov,
        paris.ky.gov). Those are STATE-delegated subdomains: the CISA registry
        the crawl is built from lists the state's ky.gov, never each city
        beneath it, so the crawl was blind to them too. Being on .gov is
        therefore not evidence of a duplicate -- sharing a domain is."""
        import csv
        crawled = set()
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "data",
                "bid_portal_directory.csv")) as fh:
            for row in csv.DictReader(fh):
                crawled.add(row["domain"].strip().lower())

        with open(bid_portals._WIKIDATA_CSV) as fh:
            for row in csv.DictReader(fh):
                self.assertNotIn(
                    row["domain"].strip().lower(), crawled,
                    f"{row['city']}, {row['state']} duplicates a crawled domain")

    def test_they_reach_the_seeded_directory(self):
        directory = {}
        bid_portals._seed(directory)
        contributed = [k for k, v in directory.items()
                       if v and v[0].get("source") == "wikidata"]
        self.assertGreater(len(contributed), 200)

    def test_a_dot_gov_page_keeps_precedence_for_the_same_town(self):
        """Seeding order is SEED_PORTALS, then the crawl, then this. A town the
        crawl already verified must not be overwritten."""
        directory = {}
        bid_portals._seed(directory)
        for (city, state) in bid_portals._national_seeds():
            entry = directory.get(bid_portals._key(city, state))
            if entry:
                self.assertNotEqual(
                    entry[0].get("source"), "wikidata",
                    f"{city}, {state}: wikidata displaced a crawl-verified page")

    def test_the_new_towns_are_geocoded(self):
        """A town with a bid page and no coordinates is invisible to
        towns_within_radius -- it sits in the directory doing nothing."""
        coords = {(c.lower(), s.upper()) for (c, s) in bid_portals._coords()}
        missing = [f"{c}, {s}" for (c, s) in bid_portals._wikidata_seeds()
                   if (c.lower(), s.upper()) not in coords]
        # A handful never resolve (renamed or unincorporated places); a large
        # number means the geocoding step was skipped.
        self.assertLess(len(missing), 30,
                        f"{len(missing)} towns have no coordinates: {missing[:10]}")


class RadiusReachTests(unittest.TestCase):
    """The point of all of it: a scan has to actually reach these towns."""

    def setUp(self):
        self.directory = {}
        bid_portals._seed(self.directory)
        self.coords = {(c.lower(), s.upper()): v
                       for (c, s), v in bid_portals._coords().items()}

    def _reach(self, city, state, radius):
        lat, lon = self.coords[(city.lower(), state.upper())]
        return bid_portals.towns_within_radius(
            self.directory, lat, lon, radius, exclude=())

    def test_southwest_missouri_reaches_more_than_before(self):
        # Springfield at 50mi was 9 towns on .gov alone. Guarding the floor
        # rather than the exact number so a future crawl can only improve it.
        self.assertGreaterEqual(len(self._reach("Springfield", "MO", 50)), 10)
        self.assertGreaterEqual(len(self._reach("Joplin", "MO", 50)), 12)

    def test_a_wide_radius_reaches_substantially_more(self):
        self.assertGreaterEqual(len(self._reach("Springfield", "MO", 125)), 50)

    def test_the_kansas_and_iowa_gains_are_real(self):
        self.assertGreaterEqual(len(self._reach("Wichita", "KS", 50)), 25)
        self.assertGreaterEqual(len(self._reach("Des Moines", "IA", 50)), 20)


if __name__ == "__main__":
    unittest.main()
