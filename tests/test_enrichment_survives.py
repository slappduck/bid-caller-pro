"""Everything read off a posting must reach the bid card.

_enrich_from_detail_pages fetches each posting and fills in value, published,
bid_number, prebid, addenda and documents alongside the contact details. The
CivicPlus branch of _run_known_portals then built a NEW dict from a fixed
list of nine keys -- and hardcoded "value": "" -- so all six of those fields
were read off the page and dropped one line later.

CivicPlus is the platform behind ~2,400 of the portals in the directory, so
this emptied the Est. Value, Posted, Bid Number, Pre-Bid, Addenda and
Documents rows of the bid card for most of the board. Losing `published` cost
more than a blank row: _age_out_undated uses it to retire a dateless bid on
its real age instead of waiting out a 60-day first-seen clock.

The non-CivicPlus branch passes the model's dict straight through and was
never affected, which is why the two branches disagreed in the first place.
"""
import os
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls

LISTING_URL = "https://emporiaks.gov/Bids.aspx"

LISTING = """<html><body>
<a href="bids.aspx?bidID=41">2026 Sidewalk Replacement Program</a>
<span>Status:</span><span>Closes:</span><span>Open</span><span>12/01/2026</span>
</body></html>"""

POSTING = """<html><body>
<h1>2026 Sidewalk Replacement Program</h1>
<p>Bid Number: 2026-014</p>
<p>Published: 11/03/2026</p>
<p>Bid Opening: December 1, 2026 at 2:00 PM CST</p>
<p>Engineer's Estimate: $220,000</p>
<p>A mandatory pre-bid meeting will be held November 18, 2026.</p>
<p>Addendum No. 1 has been issued.</p>
<p>Contact: Dana Reyes, purchasing@emporiaks.gov, (620) 555-0142</p>
<a href="/docs/2026-014-plans.pdf">Plan Set</a>
</body></html>"""


def _fetch_page(url, timeout=None):
    return (LISTING if url == LISTING_URL else POSTING), "ok"


class EnrichedFieldsReachTheCardTests(unittest.TestCase):
    def _scan(self):
        grouped, stats, coords = {}, {}, {}
        center = {"city": "Emporia", "state": "KS",
                  "lat": 38.4039, "lon": -96.1817}
        pdb = {"portals": {"emporia|KS": [{"url": LISTING_URL,
                                           "platform": "civicplus"}]}}
        with patch.object(ls, "_fetch_page", _fetch_page), \
             patch.object(ls.bid_portals, "get_portals",
                          lambda *_a, **_k: [{"url": LISTING_URL,
                                              "platform": "civicplus"}]), \
             patch.object(ls.bid_portals, "record_result",
                          lambda *_a, **_k: None):
            ls._run_known_portals(
                "Emporia", "KS", "Emporia, KS", grouped, center, 25, {},
                coords, threading.Lock(), pdb, default_city="Emporia",
                town_coords=(38.4039, -96.1817), stats=stats)
        bids = [b for v in grouped.values() for b in v]
        self.assertEqual(len(bids), 1, f"expected one bid, got {bids} {stats}")
        return bids[0]

    def test_the_stated_value_is_not_blanked(self):
        """The exact regression: "value": "" was written over $220,000."""
        self.assertIn("220,000", self._scan().get("value", ""))

    def test_the_publication_date_survives(self):
        """_age_out_undated needs it to retire a dateless bid on real age."""
        self.assertTrue(self._scan().get("published"))

    def test_the_bid_number_survives(self):
        self.assertIn("2026-014", self._scan().get("bid_number", ""))

    def test_the_prebid_meeting_survives(self):
        self.assertTrue(self._scan().get("prebid"))

    def test_the_addendum_flag_survives(self):
        self.assertTrue(self._scan().get("addenda"))

    def test_the_documents_survive(self):
        self.assertTrue(self._scan().get("documents"))

    def test_the_contact_details_still_survive(self):
        """Guard the fields the old allowlist did carry."""
        bid = self._scan()
        self.assertTrue(bid.get("email") or bid.get("phone"))
        self.assertTrue(bid.get("deadline"))


class PublicationDateLabelTests(unittest.TestCase):
    """A posting states when it went up in a dozen different ways.

    Only CivicPlus's own "Publication Date" was matched, so a posting reading
    "Posted: 11/03/2026" counted as undated -- and an undated bid has to wait
    out a 60-day first-seen clock before it can be retired, instead of being
    aged against the date printed on it.
    """

    def _pub(self, text):
        import bid_sources
        return bid_sources.detail_published(text)

    def test_the_civicplus_label_still_matches(self):
        self.assertEqual(self._pub("Publication Date: 11/03/2026"), "11/03/2026")

    def test_the_common_variants_match(self):
        for text in ("Published: 11/03/2026", "Posted: 11/03/2026",
                     "Issued: 11/03/2026", "Date Posted 11/03/2026",
                     "Date Published: 11/03/2026", "Issue Date: 11/03/2026",
                     "Posting Date: 11/03/2026", "Published on 11/03/2026",
                     "Release Date: 11/03/2026"):
            self.assertEqual(self._pub(text), "11/03/2026", text)

    def test_a_long_form_date_matches(self):
        self.assertEqual(self._pub("Posted: November 3, 2026"),
                         "November 3, 2026")

    def test_prose_without_a_date_is_not_a_publication_date(self):
        for text in ("Bids are posted here as they become available.",
                     "Addenda will be issued to all planholders.",
                     "Bid Opening: 12/01/2026"):
            self.assertEqual(self._pub(text), "", text)



if __name__ == "__main__":
    unittest.main()
