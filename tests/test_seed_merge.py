"""A town's second bid page must not be thrown away.

_seed used to skip a place entirely if it already had any entry --
`if k not in directory`. The intent was precedence, so a hand-verified page
would not be displaced by a crawled one. The effect was exclusion.

What it cost: county government is keyed by its county SEAT, so a county's
bid page and its seat city's share one key, and the city almost always got
there first. 430 verified bid pages sat in the CSV unreachable by any scan --
Mobile County Commission behind City of Mobile, Morgan County Commission
behind City of Decatur, Fairbanks North Star Borough behind City of
Fairbanks.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_portals


class MergeTests(unittest.TestCase):
    def test_a_second_portal_is_added_not_dropped(self):
        d = {}
        today = "2026-08-26"
        bid_portals._merge_seeds(
            d, {("Mobile", "AL"): [{"url": "https://cityofmobile.gov/Bids.aspx",
                                    "platform": "civicplus"}]}, "seed", today)
        bid_portals._merge_seeds(
            d, {("Mobile", "AL"): [{"url": "https://mobilecountyal.gov/Bids.aspx",
                                    "platform": "civicplus"}]},
            "national_crawl", today)
        urls = [e["url"] for e in d[bid_portals._key("Mobile", "AL")]]
        self.assertEqual(len(urls), 2)
        self.assertIn("https://mobilecountyal.gov/Bids.aspx", urls)

    def test_precedence_is_order_not_exclusion(self):
        """The hand-verified page must still be read FIRST -- callers take
        the first few entries -- but the crawled one is still there."""
        d = {}
        bid_portals._merge_seeds(
            d, {("X", "MO"): [{"url": "https://hand.example/bids"}]},
            "seed", "2026-08-26")
        bid_portals._merge_seeds(
            d, {("X", "MO"): [{"url": "https://crawled.example/bids"}]},
            "national_crawl", "2026-08-26")
        bucket = d[bid_portals._key("X", "MO")]
        self.assertEqual(bucket[0]["url"], "https://hand.example/bids")
        self.assertEqual(bucket[0]["source"], "seed")
        self.assertEqual(bucket[1]["source"], "national_crawl")

    def test_the_same_url_is_not_added_twice(self):
        d = {}
        for source in ("seed", "national_crawl", "wikidata"):
            bid_portals._merge_seeds(
                d, {("X", "MO"): [{"url": "https://same.example/bids"}]},
                source, "2026-08-26")
        self.assertEqual(len(d[bid_portals._key("X", "MO")]), 1)
        self.assertEqual(d[bid_portals._key("X", "MO")][0]["source"], "seed")

    def test_merged_entries_get_the_normal_bookkeeping(self):
        """A merged entry has to age out through MAX_FAIL like any other, so
        it needs the same fields."""
        d = {}
        bid_portals._merge_seeds(
            d, {("X", "MO"): [{"url": "https://a.example/bids"}]},
            "national_crawl", "2026-08-26")
        e = d[bid_portals._key("X", "MO")][0]
        for field in ("source", "added", "last_ok", "last_checked", "fail_count"):
            self.assertIn(field, e)
        self.assertEqual(e["fail_count"], 0)


class LiveDirectoryTests(unittest.TestCase):
    """Against the real shipped data, because the whole point is the pages
    this makes reachable."""

    @classmethod
    def setUpClass(cls):
        cls.d = bid_portals.load_directory()

    def test_the_county_page_is_reachable_at_its_county_seat(self):
        for city, state, host in [("Mobile", "AL", "mobilecountyal.gov"),
                                  ("Decatur", "AL", "morgancounty-al.gov"),
                                  ("Fairbanks", "AK", "fnsb.gov"),
                                  ("Tampa", "FL", "hcfl.gov")]:
            urls = [e["url"] for e in
                    bid_portals.get_portals(self.d, city, state)]
            self.assertTrue(any(host in u for u in urls),
                            f"{host} not reachable at {city}, {state}: {urls}")

    def test_the_city_page_still_comes_first(self):
        urls = [e["url"] for e in
                bid_portals.get_portals(self.d, "Mobile", "AL")]
        self.assertIn("cityofmobile.gov", urls[0])

    def test_no_town_exceeds_the_scan_cap(self):
        """A town holding more portals than a scan reads means a verified bid
        page that is never fetched. This caught exactly that when school
        districts were added: Cincinnati and Houston went to seven against a
        cap of six, so the cap moved rather than the pages being dropped."""
        import license_server as ls
        worst = max(len(v) for v in self.d.values() if isinstance(v, list))
        self.assertLessEqual(worst, ls.PORTALS_PER_TOWN)


if __name__ == "__main__":
    unittest.main()
