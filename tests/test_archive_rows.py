"""An archived CivicPlus row must not be shown as live work.

CivicPlus sites keep their entire history on the same Bids.aspx page and
retitle an entry when the job is let: "Roadway Improvements 2019" becomes
"Award - Roadway Improvements 2019". Nothing else about the row changes --
no Closes: date is added, and the Status chip is usually absent -- so
_status_near returned "", _run_known_portals defaulted the status to "Open",
and a job awarded in 2019 was presented to a contractor as current.

Measured across 400 real CivicPlus portals: 88 niche rows reached the feed
and 88 of them were displayed as open. Sixty-nine were awards, published bid
results or cancellations. This is the "awarded jobs shown as open" the app
was reported for, at its source.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_sources as bs
import license_server as ls

# Verbatim from the sampled portals.
AWARDED = (
    "Award - Roadway Improvements 2019",
    "Award: IFB: Roadway Improvements (2024) - MDOT Prequalification",
    "Award/Amendment 1 - Engineering Services/South Main Street Project",
    "Notice of Award - 2026 Sidewalk Program",
)
CLOSED = (
    "Bid Results - Roadway Improvements 2025",
    "Unofficial 2026 Paving Program Bid Results",
    "Results - RFR- Insulation/Drywall - 299 301 Bacon Street",
    "Registry of Proposals - 246 North Main Street Property",
)
CANCELLED = (
    "Cancellation of Bids - ReBid (1) Camp Arrowhead Reconstruction",
    "Cancellation of Procurement - Brown School Sidewalks",
)
LIVE = (
    "2026 Sidewalk Replacement Program",
    "RFP 1168 - Concrete Sidewalk Remove and Replace - Various Locations",
    "Request For Bids: 2026 Milling And Resurfacing Of Various City Streets",
    "HARRISON STREET INTERSECTION IMPROVEMENTS",
    # The bare word is not the label. A solicitation may mention an award.
    "Award Winning Streetscape Design Standards Update",
)


class TitleStatusTests(unittest.TestCase):
    def test_award_labels_are_read_as_awarded(self):
        for t in AWARDED:
            self.assertEqual(bs.status_from_title(t), "Awarded", t)

    def test_published_results_are_read_as_closed(self):
        """Results exist only after bidding has closed."""
        for t in CLOSED:
            self.assertEqual(bs.status_from_title(t), "Closed", t)

    def test_cancellations_are_read_as_cancelled(self):
        for t in CANCELLED:
            self.assertEqual(bs.status_from_title(t), "Cancelled", t)

    def test_a_live_solicitation_is_left_alone(self):
        for t in LIVE:
            self.assertEqual(bs.status_from_title(t), "", t)


class ItReachesTheFeedTests(unittest.TestCase):
    """The status has to survive all the way to what the client displays."""

    def _row(self, title):
        html = (f'<html><body><a href="bids.aspx?bidID=1">{title}</a>'
                f'</body></html>')
        rows = bs.parse_civicplus_html(html, base_url="https://x.gov/Bids.aspx")
        self.assertEqual(len(rows), 1, title)
        return rows[0]

    def test_the_parser_stamps_the_status_on_the_row(self):
        self.assertEqual(self._row("Award - Roadway Improvements 2019")["status"],
                         "Awarded")

    def test_an_archived_row_is_not_an_open_bid(self):
        for t in AWARDED + CLOSED + CANCELLED:
            bid = dict(self._row(t))
            bid["status"] = bid["status"] or "Open"
            self.assertFalse(ls._is_open_bid(bid),
                             f"still displayed as open: {t}")

    def test_a_live_row_is_still_an_open_bid(self):
        for t in LIVE:
            bid = dict(self._row(t))
            bid["status"] = bid["status"] or "Open"
            self.assertTrue(ls._is_open_bid(bid), t)

    def test_the_title_outranks_a_stale_status_chip(self):
        """An archived row often keeps whatever chip it was posted with."""
        html = ('<html><body><a href="bids.aspx?bidID=1">'
                'Award - Roadway Improvements 2019</a>'
                '<span>Status:</span><span>Open</span></body></html>')
        rows = bs.parse_civicplus_html(html, base_url="https://x.gov/Bids.aspx")
        self.assertEqual(rows[0]["status"], "Awarded")


class NicheGateTests(unittest.TestCase):
    def test_an_asphalt_overlay_programme_is_this_trade(self):
        """Let with curb, gutter and ADA ramp repair as pay items. It matched
        none of the niche terms, so it was dropped."""
        for t in ("2026 Asphalt Overlay Program",
                  "RFP 25-143 State Street Mill and Overlay Project"):
            self.assertTrue(bs.looks_relevant(t), t)

    def test_a_zoning_overlay_is_not(self):
        """Why "overlay" is not a term on its own."""
        self.assertFalse(bs.looks_relevant("Zoning Overlay District Study"))

    def test_consultant_work_is_not_construction_work(self):
        """These carry a STRONG term, so they skipped CLEARLY_UNRELATED and
        reached the feed. A concrete contractor cannot bid any of them."""
        for t in ("Pavement Management Program Engineering Services",
                  "Traffic Study Engineering Services - Intersections",
                  "Professional Services for Sidewalk Design",
                  "ROW Appraisal - South Main Street",
                  "Consulting Services for the Roadway Master Plan"):
            self.assertFalse(bs.looks_relevant(t), t)

    def test_real_work_that_says_services_is_kept(self):
        """The rule names professions, not the word "services"."""
        for t in ("On-Call Public Works Services",
                  "Concrete Sidewalk Replacement Services",
                  "Curb and Gutter Repair Services"):
            self.assertTrue(bs.looks_relevant(t), t)


class RoadWorkRecallTests(unittest.TestCase):
    """"<road name> ... Improvements" is how municipalities title street work.

    NICHE_TERMS carries "street improvement" and "road improvement" as exact
    substrings, so any word in between defeated them. Auditing what the gate
    REJECTED across six metros on the live board turned up five real jobs lost
    to nothing but word order, three of them from the same county in one
    sweep. Every title here is verbatim from that audit.
    """

    def test_a_road_name_separated_from_improvements(self):
        for t in ("2026 - RFQ - Bear Creek Road Safety Improvements - STBG-9902(609)",
                  "2026 - RFQ - Lonedell Road Safety Improvements STBG-9902(610)",
                  "2026 - RFQ - Saline Road Safety Improvements- Phase 4",
                  "Canton Ave Improvement Project - Phase 2",
                  "Commercial Street (8th Ave to 10th Ave) Stormsewer Improvements",
                  "Hackett Boulevard Stormwater Improvements Project"):
            self.assertTrue(bs.looks_relevant(t), t)

    def test_the_reverse_word_order_too(self):
        for t in ("Reconstruction of Maple Avenue",
                  "Rehabilitation of the Elm Street Corridor",
                  "Widening of Highway 60"):
            self.assertTrue(bs.looks_relevant(t), t)

    def test_work_with_no_roadway_in_it_is_still_rejected(self):
        """The rule needs a roadway word AND a work word. Neither alone."""
        for t in ("PFAS Treatment at Water Treatment Plant",
                  "Community Center AHU 1 & 2 Replacement",
                  "Shoal Creek Wastewater Treatment Plant Clarifier Addition",
                  "Interior Renovations at Overland Police Department",
                  "Request for Proposal - 101 West Main Street"):
            self.assertFalse(bs.looks_relevant(t), t)


class ParkingTests(unittest.TestCase):
    """A parking lot is flatwork. The meters and the permit software are not."""

    def test_a_parking_surface_is_this_trade(self):
        for t in ("MOmentum Bike Park Parking and Concessions",
                  "Public Safety Parking Lot Improvements"):
            self.assertTrue(bs.looks_relevant(t), t)

    def test_parking_administration_is_not(self):
        for t in ("Parking Meter Replacement",
                  "Parking Enforcement Management Software",
                  "Downtown Parking Study",
                  "Parking Permit Citation Processing"):
            self.assertFalse(bs.looks_relevant(t), t)


if __name__ == "__main__":
    unittest.main()
