"""Offline tests for bid_portals.py — the persistent per-city bid-portal
directory. No network access: Upstash is force-disabled and each test gets
its own throwaway local file, so these are safe to run anywhere, anytime."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_portals as bp
import kv_backend


class BidPortalsTests(unittest.TestCase):
    def setUp(self):
        self._orig_url = bp.UPSTASH_URL
        self._orig_token = bp.UPSTASH_TOKEN
        # Storage moved behind kv_backend, so pinning bp._LOCAL_FILE no longer
        # isolates anything — writes were landing in kv_backend's own file and
        # leaking between tests. Swap the whole backend for a dict instead.
        self._store = {}
        self._kv_get = kv_backend.get
        self._kv_set = kv_backend.set
        kv_backend.get = lambda key, default=None: self._store.get(key, default)
        kv_backend.set = lambda key, value: (self._store.__setitem__(key, value), True)[1]

    def tearDown(self):
        kv_backend.get = self._kv_get
        kv_backend.set = self._kv_set

    def test_seed_present_on_first_load(self):
        d = bp.load_directory()
        got = bp.get_portals(d, "Springfield", "MO")
        self.assertTrue(got)
        # Asserting the behaviour rather than the exact string: it must point at
        # the bids module, and be labelled so the structured reader picks it up.
        self.assertIn("bids.aspx", got[0]["url"].lower())
        self.assertEqual(got[0]["platform"], "civicplus")

    def test_no_seed_points_at_the_meetings_module_alone(self):
        """AgendaCenter is council meetings, not bids. Two cities were seeded
        pointing only there, so they were scanned for bids on a page that never
        contains any."""
        d = bp.load_directory()
        for city, state in (("Aurora", "MO"), ("Joplin", "MO"), ("Springfield", "MO")):
            with self.subTest(city=city):
                urls = [e["url"].lower() for e in bp.get_portals(d, city, state)]
                self.assertTrue(any("bids.aspx" in u for u in urls),
                                f"{city} has no bids page seeded: {urls}")

    def test_lookup_is_case_and_whitespace_insensitive(self):
        d = bp.load_directory()
        self.assertTrue(bp.get_portals(d, "  SPRINGFIELD ", "mo"))

    def test_unknown_city_returns_empty(self):
        d = bp.load_directory()
        self.assertEqual(bp.get_portals(d, "Nowhereville", "ZZ"), [])

    def test_aggregator_url_is_not_learned(self):
        d = bp.load_directory()
        bp.learn_portal(d, "Test City", "MO", "https://www.bidnetdirect.com/mo/listing")
        bp.learn_portal(d, "Test City", "MO", "https://sub.demandstar.com/x")
        self.assertEqual(bp.get_portals(d, "Test City", "MO"), [])

    def test_real_url_is_learned_and_persists_across_loads(self):
        d = bp.load_directory()
        bp.learn_portal(d, "Test City", "MO", "https://www.testcity.gov/bids.aspx")
        bp.save_directory(d)

        d2 = bp.load_directory()
        got = bp.get_portals(d2, "Test City", "MO")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["url"], "https://www.testcity.gov/bids.aspx")
        self.assertEqual(got[0]["source"], "learned")

    def test_learning_same_url_twice_does_not_duplicate(self):
        d = bp.load_directory()
        url = "https://www.testcity.gov/bids.aspx"
        bp.learn_portal(d, "Test City", "MO", url)
        bp.learn_portal(d, "Test City", "MO", url)
        self.assertEqual(len(bp.get_portals(d, "Test City", "MO")), 1)

    def test_entry_ages_out_after_max_consecutive_failures(self):
        d = bp.load_directory()
        url = "https://www.failcity.gov/bids.aspx"
        bp.learn_portal(d, "Fail City", "MO", url)
        for _ in range(bp.MAX_FAIL):
            bp.record_result(d, "Fail City", "MO", url, False)
        self.assertEqual(bp.get_portals(d, "Fail City", "MO"), [])

    def test_single_transient_failure_does_not_drop_entry(self):
        d = bp.load_directory()
        url = "https://www.flakycity.gov/bids.aspx"
        bp.learn_portal(d, "Flaky City", "MO", url)
        bp.record_result(d, "Flaky City", "MO", url, False)
        self.assertTrue(bp.get_portals(d, "Flaky City", "MO"))

    def test_success_resets_failure_count(self):
        d = bp.load_directory()
        url = "https://www.recovering.gov/bids.aspx"
        bp.learn_portal(d, "Recovering City", "MO", url)
        for _ in range(bp.MAX_FAIL - 1):
            bp.record_result(d, "Recovering City", "MO", url, False)
        bp.record_result(d, "Recovering City", "MO", url, True)
        entries = d[bp._key("Recovering City", "MO")]
        self.assertEqual(entries[0]["fail_count"], 0)

    def test_is_aggregator_url(self):
        self.assertTrue(bp.is_aggregator_url("https://www.demandstar.com/x"))
        self.assertTrue(bp.is_aggregator_url("https://sub.bidnetdirect.com/x"))
        self.assertFalse(bp.is_aggregator_url("https://www.springfieldmo.gov/bids.aspx"))


class TownsWithinRadiusTests(unittest.TestCase):
    """A wide-radius scan used to only ever search the exact town typed plus
    a handful of geographically-guessed anchor points. towns_within_radius
    is the fix: answer "which towns we already have a real bid page for
    fall inside this radius" directly against pre-geocoded coordinates,
    instead of guessing points and hoping one lands near a known town."""

    def setUp(self):
        self._orig_url = bp.UPSTASH_URL
        self._orig_token = bp.UPSTASH_TOKEN
        self._store = {}
        self._kv_get = kv_backend.get
        self._kv_set = kv_backend.set
        kv_backend.get = lambda key, default=None: self._store.get(key, default)
        kv_backend.set = lambda key, value: (self._store.__setitem__(key, value), True)[1]
        self._orig_coords_cache = bp._coords_cache

    def tearDown(self):
        kv_backend.get = self._kv_get
        kv_backend.set = self._kv_set
        bp._coords_cache = self._orig_coords_cache

    # Springfield, MO is ~0mi from itself; Nixa, MO is ~11mi north;
    # Kansas City, MO is ~160mi northwest -- real coordinates, so distance
    # checks below exercise the real haversine math, not a stub.
    SPRINGFIELD = (37.2090, -93.2923)
    NIXA = (37.0428, -93.2926)
    KANSAS_CITY = (39.0997, -94.5786)

    def _directory_with(self, *towns):
        d = bp.load_directory()
        for city, state in towns:
            bp.learn_portal(d, city, state, f"https://www.{city.lower()}.gov/bids.aspx")
        return d

    def test_a_nearby_known_town_is_found(self):
        bp._coords_cache = {("Nixa", "MO"): self.NIXA}
        d = self._directory_with(("Nixa", "MO"))
        got = bp.towns_within_radius(d, *self.SPRINGFIELD, radius=25)
        self.assertEqual([(c, s) for c, s, _, _ in got], [("Nixa", "MO")])

    def test_a_town_outside_the_radius_is_not_found(self):
        bp._coords_cache = {("Kansas City", "MO"): self.KANSAS_CITY}
        d = self._directory_with(("Kansas City", "MO"))
        got = bp.towns_within_radius(d, *self.SPRINGFIELD, radius=25)
        self.assertEqual(got, [])
        # ...but it IS found once the radius is actually wide enough to reach it.
        got_wide = bp.towns_within_radius(d, *self.SPRINGFIELD, radius=200)
        self.assertEqual([(c, s) for c, s, _, _ in got_wide], [("Kansas City", "MO")])

    def test_excluded_towns_are_skipped_even_if_in_radius(self):
        bp._coords_cache = {("Nixa", "MO"): self.NIXA}
        d = self._directory_with(("Nixa", "MO"))
        got = bp.towns_within_radius(d, *self.SPRINGFIELD, radius=25,
                                     exclude={("nixa", "MO")})
        self.assertEqual(got, [])

    def test_a_town_with_coords_but_no_trusted_portal_is_skipped(self):
        # Geocoded, but never actually learned/seeded as a real bid page --
        # coordinates alone aren't enough to justify fetching it.
        bp._coords_cache = {("Ghost Town", "MO"): self.NIXA}
        d = bp.load_directory()
        got = bp.towns_within_radius(d, *self.SPRINGFIELD, radius=25)
        self.assertEqual(got, [])

    def test_a_town_that_aged_out_is_no_longer_returned(self):
        bp._coords_cache = {("Flakyville", "MO"): self.NIXA}
        d = self._directory_with(("Flakyville", "MO"))
        url = "https://www.flakyville.gov/bids.aspx"
        for _ in range(bp.MAX_FAIL):
            bp.record_result(d, "Flakyville", "MO", url, False)
        got = bp.towns_within_radius(d, *self.SPRINGFIELD, radius=25)
        self.assertEqual(got, [])

    def test_a_town_with_no_geocoded_coordinates_is_silently_skipped(self):
        # Real, trusted portal, just not geocoded yet -- degrades to "not
        # included this scan", never a crash.
        bp._coords_cache = {}
        d = self._directory_with(("Nixa", "MO"))
        got = bp.towns_within_radius(d, *self.SPRINGFIELD, radius=25)
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
