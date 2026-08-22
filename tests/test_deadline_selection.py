"""A posting states several dates. Only one of them is the bid's.

A page carries a question deadline, a pre-bid meeting, a documents-available
date, a publication date and a closing date, and the extractor took whichever
sat closest to a deadline-ish word. Measured against 183 live postings, that
picked the wrong date on four of them -- and in every case it picked the
QUESTIONS deadline, which falls one to three weeks before the bid is due:

  Seagoville TX   "Deadline for Questions: November 10"  ->  Nov 24 submissions
  Leominster MA   "sub bidders due date 9/3"             ->  Closing 9/20
  Emporia KS      "Questions ... August 26"              ->  Closing 9/1
  Montpelier VT   "Question Deadline: April 9"           ->  Submission Apr 15
  Jersey Village  "Questions Due: September 11"          ->  Deadline Sep 29

Reading a bid as closed two weeks before it is, is the single worst thing
this app can do to a contractor, so the extractor now works in two tiers: the
labelled closing field first, prose only when the page has no labelled field,
and a lead-in check that rejects a date attached to questions, a site visit
or a publication notice.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_sources as bs


class LabelledFieldWinsTests(unittest.TestCase):
    """Verbatim from the live pages named above."""

    def test_the_questions_deadline_is_not_the_bid_deadline(self):
        text = ("Deadline for Questions: November 10, 2025 "
                "SOQ Submissions due: November 24, 2025")
        self.assertEqual(bs.detail_deadline(text), "November 24, 2025")

    def test_a_closing_field_outranks_an_earlier_prose_date(self):
        text = ("Questions should be submitted no later than August 26, 2026. "
                "Publication Date/Time: 8/4/2026 12:00 AM "
                "Closing Date/Time: 9/1/2026")
        self.assertEqual(bs.detail_deadline(text), "9/1/2026")

    def test_a_documents_available_date_is_not_the_deadline(self):
        """Belmont MA: "no later than 08/19/2026" was the day the documents
        went up, two weeks before the bid closed."""
        text = ("Bids will be available no later than 08/19/2026 @ 10:00AM. "
                "Publication Date/Time: 8/12/2026 10:00 AM "
                "Documents Available 8/19/2026 10:00AM "
                "Closing Date/Time: 9/2/2026 10:00 AM")
        self.assertEqual(bs.detail_deadline(text), "9/2/2026")

    def test_a_question_deadline_before_a_submission_deadline(self):
        text = ("Question Deadline: April 9, 2025 "
                "Proposal Submission Deadline: April 15, 2025")
        self.assertEqual(bs.detail_deadline(text), "April 15, 2025")


class PublicationDateIsNotADeadlineTests(unittest.TestCase):
    def test_the_field_before_it_does_not_disqualify_it(self):
        """The lead-in check is anchored to the label it precedes. An
        unanchored one saw "Publication" 40 characters earlier and threw the
        real closing date away."""
        text = ("Publication Date/Time: 7/1/2026 8:00 AM "
                "Closing Date/Time: 12/1/2026 2:00 PM")
        self.assertEqual(bs.detail_deadline(text), "12/1/2026")


class ProseFallbackTests(unittest.TestCase):
    """Used only when the page states no labelled closing field."""

    def test_a_legal_notice_with_a_time_before_the_date(self):
        """The digits in "10:00" broke the old gap, so the whole notice read
        as undated."""
        text = ("Sealed bids will be received no later than 10:00 AM on "
                "July 15, 2026, at which time bids will be opened.")
        self.assertEqual(bs.detail_deadline(text), "July 15, 2026")

    def test_a_bid_opening_is_the_effective_deadline(self):
        """"Bid Opening Information: 8/25/26" is how CivicPlus prints it when
        the closing field says "Open Until Contracted"."""
        text = ("Closing Date/Time: Open Until Contracted "
                "Bid Opening Information: 8/25/26")
        self.assertEqual(bs.detail_deadline(text), "8/25/26")

    def test_an_address_after_bid_opening_is_not_a_date(self):
        text = ("Closing Date/Time: 9/2/2026 10:00 AM "
                "Bid Opening Information: 19 Moore Street Belmont, MA 02478")
        self.assertEqual(bs.detail_deadline(text), "9/2/2026")

    def test_a_page_with_no_date_stays_blank(self):
        self.assertEqual(bs.detail_deadline("Sealed bids will be received."), "")


class ExistingLayoutsStillWorkTests(unittest.TestCase):
    def test_the_common_labels(self):
        for text, want in (
                ("Closing Date/Time: 12/1/2026 2:00 PM", "12/1/2026"),
                ("Bid Opening Date/Time: March 3, 2026 10:00 AM", "March 3, 2026"),
                ("Due Date and Time: 2026-11-20 2:00 PM", "2026-11-20"),
                ("Bids Due: 12/01/2026", "12/01/2026"),
                ("Proposals due September 3, 2026", "September 3, 2026")):
            self.assertEqual(bs.detail_deadline(text), want, text)


if __name__ == "__main__":
    unittest.main()
