"""Building the campaign payload that /campaign/send turns into mail.

Two things this tool promises and has to keep:

1. A prospect's market is measured, not asserted. If a real scan of a city
   turns up fewer open jobs than --min-bids, that prospect is DROPPED rather
   than mailed a claim about a feed that would show them nothing the moment
   they signed up. The tests here pin that drop, and that the JSON payload
   never contains a recipient whose market wasn't actually checked.

2. The salutation is never a scraped directory-listing title. Search results
   for small contractors are frequently Google-Business-listing titles --
   "Concrete Contractor Gladstone, MO" -- not a company name, and greeting
   someone with their own SEO keyword soup is worse than "Sir or Madam": it
   announces that nobody looked. company_name() and greeting() are what
   stand between a scraped row and the "Dear ..." line, so they're tested
   against the actual shapes company_name()'s own docstring says it has seen
   -- a dash-joined listing title, a self-concatenated scrape, a bare trade
   description with no business name in it at all.

Every network-shaped call this tool makes (bid_portals.load_directory,
bid_portals.towns_within_radius, license_server._run_known_portals,
license_server._city_coords) is stubbed. Nothing here scans the real web.
"""
import csv
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import build_campaign as bc  # noqa: E402
import bid_portals  # noqa: E402
import license_server as ls  # noqa: E402


class _Stubbed(unittest.TestCase):
    """Base class that patches the module-level collaborators build_campaign
    calls into, and always puts the real ones back -- these are shared
    modules the rest of the suite depends on."""

    def stub(self, obj, name, value):
        original = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(lambda: setattr(obj, name, original))


class CompanyNameTests(unittest.TestCase):
    def test_a_real_suffixed_name_is_kept(self):
        row = {"company": "Musselman Concrete LLC", "city": "Aurora", "state": "MO"}
        self.assertEqual(bc.company_name(row), "Musselman Concrete LLC")

    def test_a_bare_listing_title_names_nobody(self):
        """"Concrete Contractor Gladstone, MO" is every word of a search
        result and none of a business."""
        row = {"company": "Concrete Contractor Gladstone, MO",
               "city": "Gladstone", "state": "MO"}
        self.assertEqual(bc.company_name(row), "")

    def test_the_real_name_after_a_dash_separated_listing_title_is_found(self):
        """The exact shape from the file's own docstring: a Google Business
        title, then the actual company after " - "."""
        row = {"company": ("Decorative, Stamped Concrete Driveway & Floor "
                            "Contractor Chesterfield, Barnhart & St. Louis MO "
                            "- Hoffman Concrete LLC"),
               "city": "Chesterfield", "state": "MO"}
        self.assertEqual(bc.company_name(row), "Hoffman Concrete LLC")

    def test_a_self_concatenated_scrape_is_de_doubled(self):
        row = {"company": "E. Meier ContractingE. Meier Contracting",
               "city": "x", "state": "MO"}
        self.assertEqual(bc.company_name(row), "E. Meier Contracting")

    def test_an_empty_company_column_is_empty(self):
        self.assertEqual(bc.company_name({"company": "", "city": "x", "state": "MO"}), "")

    def test_a_trade_description_with_no_name_in_it_is_empty(self):
        row = {"company": "Residential & Commercial Concrete Contractors "
                           "serving Kansas City",
               "city": "Kansas City", "state": "MO"}
        self.assertEqual(bc.company_name(row), "")


class GreetingTests(unittest.TestCase):
    def test_a_real_contact_name_wins(self):
        row = {"contact": "John Smith", "company": "Musselman Concrete LLC"}
        self.assertEqual(bc.greeting(row), "John Smith")

    def test_a_placeholder_contact_value_falls_back_to_the_company(self):
        row = {"contact": "n/a", "company": "Musselman Concrete LLC"}
        self.assertEqual(bc.greeting(row), "Musselman Concrete LLC")

    def test_no_contact_and_no_real_company_name_is_sir_or_madam(self):
        row = {"contact": "", "company": "Concrete Contractor Gladstone, MO",
               "city": "Gladstone", "state": "MO"}
        self.assertEqual(bc.greeting(row), "Sir or Madam")


class ScanCityTests(_Stubbed):
    """scan_city() drives the real scan path (_run_known_portals) over the
    center plus every known town within radius, then keeps only open bids,
    nearest first -- the exact list that goes into the email as proof."""

    def test_bids_are_open_only_and_sorted_nearest_first(self):
        self.stub(bid_portals, "load_directory", lambda: {})
        self.stub(bid_portals, "towns_within_radius",
                  lambda pdb, lat, lon, radius: [("Nearby", "MO", 37.0, -93.7),
                                                  ("Farther", "MO", 37.5, -94.2)])

        def fake_run(city, state, ai_label, grouped, center, radius, cdb,
                     coords, lock, pdb, default_city=None, town_coords=None,
                     stats=None):
            if city == "Aurora":
                grouped[city] = [{"title": "Aurora job", "miles": 0,
                                  "status": "open"}]
            elif city == "Nearby":
                grouped[city] = [
                    {"title": "Nearby closed", "miles": 5, "status": "Awarded"},
                    {"title": "Nearby open", "miles": 5, "status": "Advertised"}]
            elif city == "Farther":
                grouped[city] = [{"title": "Farther job", "miles": 40,
                                  "status": ""}]
        self.stub(ls, "_run_known_portals", fake_run)

        bids, town_count = bc.scan_city("Aurora", "MO", 36.97, -93.72, 50)
        self.assertEqual(town_count, 3)  # center + the two known towns
        self.assertEqual([b["title"] for b in bids],
                         ["Aurora job", "Nearby open", "Farther job"])

    def test_max_towns_caps_how_many_known_towns_are_read(self):
        self.stub(bid_portals, "load_directory", lambda: {})
        self.stub(bid_portals, "towns_within_radius",
                  lambda pdb, lat, lon, radius: [
                      ("T%d" % i, "MO", 37.0 + i * 0.01, -93.7) for i in range(10)])
        self.stub(ls, "_run_known_portals", lambda *a, **k: None)
        _, town_count = bc.scan_city("Aurora", "MO", 36.97, -93.72, 50, max_towns=3)
        self.assertEqual(town_count, 4)  # center + 3, not center + 10


class MainBuildsAPayloadTests(_Stubbed):
    """End-to-end: a CSV in, a JSON payload out, nothing sent."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.csv_path = os.path.join(self.dir, "prospects.csv")
        self.out_path = os.path.join(self.dir, "out.json")
        self._argv = sys.argv

    def tearDown(self):
        sys.argv = self._argv

    def write_csv(self, rows):
        fields = ["email", "city", "state", "priority", "segment",
                  "company", "contact"]
        with open(self.csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fields})

    def run_main(self, *extra_args):
        sys.argv = ["build_campaign.py", self.csv_path, "-o", self.out_path,
                    *extra_args]
        bc.main()
        with open(self.out_path, encoding="utf-8") as f:
            return json.load(f)

    def test_a_market_below_min_bids_is_dropped_not_mailed(self):
        """The central guard: never promise a feed that would show empty."""
        self.write_csv([{"email": "a@x.com", "city": "Aurora", "state": "MO",
                         "priority": "1", "segment": "concrete contractor",
                         "company": "Musselman Concrete LLC"}])
        self.stub(ls, "_city_coords", lambda city, state, db: (36.97, -93.72))
        self.stub(bc, "scan_city",
                  lambda city, state, lat, lon, radius, max_towns=120: ([], 5))
        payload = self.run_main("--min-bids", "1")
        self.assertEqual(payload["recipients"], [])

    def test_a_market_with_enough_bids_produces_a_recipient(self):
        self.write_csv([{"email": "  A@X.com  ", "city": "Aurora",
                         "state": "MO", "priority": "1",
                         "segment": "concrete contractor",
                         "company": "Musselman Concrete LLC"}])
        self.stub(ls, "_city_coords", lambda city, state, db: (36.97, -93.72))
        self.stub(bc, "scan_city",
                  lambda city, state, lat, lon, radius, max_towns=120: (
                      [{"title": "Aurora curb job", "miles": 2,
                        "deadline": "9/20/2026"}], 5))
        payload = self.run_main()
        self.assertEqual(len(payload["recipients"]), 1)
        rec = payload["recipients"][0]
        self.assertEqual(rec["email"], "a@x.com")  # stripped and lowercased
        self.assertEqual(rec["vars"]["greeting"], "Musselman Concrete LLC")
        self.assertEqual(rec["vars"]["bids"], "1")
        self.assertIn("Aurora curb job", rec["vars"]["job_list"])

    def test_an_ungeocodable_city_is_dropped_not_guessed(self):
        self.write_csv([{"email": "b@x.com", "city": "Nowhere", "state": "ZZ",
                         "priority": "1", "segment": "concrete contractor",
                         "company": "Test"}])
        self.stub(ls, "_city_coords", lambda city, state, db: None)
        payload = self.run_main()
        self.assertEqual(payload["recipients"], [])

    def test_priority_and_segment_filters_narrow_the_rows(self):
        self.write_csv([
            {"email": "a@x.com", "city": "Aurora", "state": "MO",
             "priority": "1", "segment": "concrete contractor"},
            {"email": "b@x.com", "city": "Aurora", "state": "MO",
             "priority": "2", "segment": "concrete contractor"},
            {"email": "c@x.com", "city": "Aurora", "state": "MO",
             "priority": "1", "segment": "landscaping"},
            {"email": "", "city": "Aurora", "state": "MO",
             "priority": "1", "segment": "concrete contractor"},
        ])
        self.stub(ls, "_city_coords", lambda city, state, db: (36.97, -93.72))
        self.stub(bc, "scan_city",
                  lambda city, state, lat, lon, radius, max_towns=120: (
                      [{"title": "job", "miles": 1, "deadline": ""}], 5))
        payload = self.run_main("--priority", "1", "--segment",
                                "concrete contractor")
        self.assertEqual([r["email"] for r in payload["recipients"]], ["a@x.com"])

    def test_the_payload_always_carries_subject_and_body_templates(self):
        self.write_csv([])
        payload = self.run_main()
        self.assertIn("{{city}}", payload["subject"])
        self.assertIn("{{greeting}}", payload["body"])
        self.assertEqual(payload["recipients"], [])


if __name__ == "__main__":
    unittest.main()
