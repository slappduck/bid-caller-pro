"""Which registry rows are worth probing for a bid page.

The registry lists every .gov domain a county has, and most counties have
several: a commission, a sheriff, a clerk, a probate court, a 911 centre.
Only the first of those ever puts work out to bid, but the crawler spent its
whole 24-path list on each of them and recorded another not_found. 780 of
12,711 registry rows are one of these; skipping them raised the hit rate in a
120-domain sample from 11% to 20%.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.discover_bid_portals import is_procurement_entity as keep


def row(org="", domain="", city=""):
    return {"org": org, "domain": domain, "city": city}


class SkipsOfficesWithNothingToBid(unittest.TestCase):
    def test_law_enforcement_and_courts(self):
        for org, dom in [
            ("Henry County Sheriff's Office", "henrycountysheriff-al.gov"),
            ("Montgomery County Probate Court", "montgomeryprobatecourtal.gov"),
            ("Stanislaus County District Attorney's Office", "standa.gov"),
            ("Crittenden County Circuit Clerk", "crittcircuitclerkar.gov"),
            ("25th Judicial Circuit of Alabama", "al25da.gov"),
        ]:
            self.assertFalse(keep(row(org, dom)), org)

    def test_the_giveaway_is_sometimes_only_in_the_hostname(self):
        """"HALE COUNTY SHERIFFS OFFICE SUITE 18" is caught by its org, but
        madco911al.gov and halecoso.gov need the domain to be read too."""
        self.assertFalse(keep(row("Huntsville-Madison County 911 Center",
                                  "madco911al.gov")))
        self.assertFalse(keep(row("Cuba Township", "cubaassessoril.gov")))

    def test_libraries_and_emergency_districts(self):
        self.assertFalse(keep(row("Orange Beach Public Library",
                                  "orangebeachlibrary.gov")))
        self.assertFalse(keep(row("DeKalb County E-911",
                                  "dekalbcountyal911.gov")))


class KeepsAnythingThatLetsWork(unittest.TestCase):
    def test_commissions_and_cities(self):
        for org, dom in [
            ("Barbour County Commission", "barbourcountyal.gov"),
            ("Mobile County Commission", "mobilecounty.gov"),
            ("City of Springfield", "springfieldmo.gov"),
            ("Jackson County Public Works", "jacksongov.org"),
        ]:
            self.assertTrue(keep(row(org, dom)), org)

    def test_a_place_name_is_not_an_office(self):
        """The pattern is word-bounded because it was not, once: "treasur"
        skipped the City of Treasure Island, and an unbounded "court" would
        take out every Courtland."""
        self.assertTrue(keep(row("City of Treasure Island",
                                 "mytreasureisland.gov", "Treasure Island")))
        self.assertTrue(keep(row("City of Courtland", "courtlandal.gov",
                                 "Courtland")))
        self.assertTrue(keep(row("Town of Clerkenwell", "clerkenwellva.gov")))

    def test_the_filter_only_skips_it_never_deletes(self):
        """A row that matches is not probed; nothing already discovered is
        touched. So a wrong pattern costs coverage, never data."""
        import inspect
        from tools import discover_bid_portals as d
        src = inspect.getsource(d._load_registry)
        self.assertIn("continue", src)
        self.assertNotIn("remove", src)


if __name__ == "__main__":
    unittest.main()


class ListingUrlTests(unittest.TestCase):
    """A single document is not a bid page.

    Discovery recorded twelve of these -- a 2023 PDF on a township site, a
    Google Doc, an uploaded RFP under /wp-content/uploads. Each passes the
    text test, because an RFP naturally contains procurement words. But a
    document never changes, so the scan re-reads one frozen solicitation
    forever while the town looks covered and its real bid page goes unread.
    """

    def test_real_listing_pages_are_kept(self):
        from tools.discover_bid_portals import is_listing_url
        for u in ("https://springfieldmo.gov/Bids.aspx",
                  "https://x.gov/purchasing",
                  "https://x.gov/bids?page=2",
                  "https://x.gov/business/bids-and-rfps"):
            self.assertTrue(is_listing_url(u), u)

    def test_single_documents_are_rejected(self):
        from tools.discover_bid_portals import is_listing_url
        for u in ("https://docs.google.com/document/d/abc",
                  "https://drive.google.com/drive/folders/x",
                  "https://x.gov/wp-content/uploads/2025/04/RFP-foo.pdf",
                  "https://x.gov/files/notice.PDF",
                  "https://x.gov/sites/default/files/2026-07/proc.pdf",
                  "https://y.govoffice2.com/uploads/dm/9417/FY2627.pdf"):
            self.assertFalse(is_listing_url(u), u)

    def test_the_shipped_directory_holds_no_single_documents(self):
        import csv, os
        from tools.discover_bid_portals import is_listing_url
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "data", "bid_portal_directory.csv")
        bad = [r["bid_url"] for r in csv.DictReader(open(path, encoding="utf-8"))
               if r["status"] == "found" and not is_listing_url(r["bid_url"])]
        self.assertEqual(bad, [], f"{len(bad)} document URLs recorded as bid pages")
