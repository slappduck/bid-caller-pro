"""HTML entities must be decoded before anything tries to read the text.

_unescape was a hand-written table of six entities. It did not include
&rsquo; or &apos; -- which is how procurement pages overwhelmingly write the
apostrophe in "Engineer's Estimate" -- so the raw entity survived into the
cleaned text and defeated the regex looking for it. A page printing
"Engineer&rsquo;s Estimate: $1,000,000" reported no value at all.

The gap was not limited to values: every title, contact name, scope and date
carrying a curly quote, an em dash or a numeric character reference came
through with the markup still in it. The stdlib knows all ~2,000 entities.

Measured on 198 live postings: 11% state a dollar amount anywhere on the
page, and value extraction went from 4 of those to 8. The remaining misses
are insurance limits, bid security and bond amounts -- correctly not reported
as the project's value, since a contractor pricing against a wrong number is
worse off than one seeing a blank.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_sources as bs


class UnescapeTests(unittest.TestCase):
    def test_the_six_that_were_already_handled_still_are(self):
        self.assertEqual(bs._unescape("Bids &amp; RFPs"), "Bids & RFPs")
        self.assertEqual(bs._unescape("&lt;b&gt;"), "<b>")
        self.assertEqual(bs._unescape("&quot;x&quot;"), '"x"')
        self.assertEqual(bs._unescape("&#39;"), "'")

    def test_the_apostrophes_that_broke_value_extraction(self):
        self.assertEqual(bs._unescape("Engineer&rsquo;s"), "Engineer’s")
        self.assertEqual(bs._unescape("ENGINEER&apos;S"), "ENGINEER'S")

    def test_dashes_and_numeric_references(self):
        self.assertEqual(bs._unescape("Bids &#8212; RFPs"), "Bids — RFPs")
        self.assertEqual(bs._unescape("caf&eacute;"), "café")


class ValueTests(unittest.TestCase):
    """Every case here is verbatim from a live posting."""

    def test_the_curly_apostrophe_case(self):
        self.assertEqual(
            bs.detail_value("Engineer&rsquo;s Estimate: $1,000,000"),
            "$1,000,000")

    def test_the_entity_apostrophe_case(self):
        self.assertEqual(bs.detail_value("ENGINEER&apos;S ESTIMATE $236,000"),
                         "$236,000")

    def test_the_engineering_estimate_wording(self):
        self.assertEqual(bs.detail_value("Engineering Estimate is $400,000.00"),
                         "$400,000.00")

    def test_a_plain_estimate_label_still_works(self):
        self.assertEqual(bs.detail_value("Estimated Cost: $85,000"), "$85,000")

    def test_insurance_limits_are_not_the_project_value(self):
        """The commonest false positive: nearly every construction
        solicitation carries these two numbers."""
        for text in ("commercial general liability with limits of "
                     "$1,000,000 each occurrence",
                     "general aggregate $2,000,000 estimated value",
                     "combined single limit of $1,000,000 estimated amount"):
            self.assertEqual(bs.detail_value(text), "", text)

    def test_bid_security_is_not_the_project_value(self):
        self.assertEqual(
            bs.detail_value("Bid security shall be in the amount of ten "
                            "percent (10%) of the total amount of the bid or "
                            "Twenty-Thousand Dollars ($20,000.00)"), "")

    def test_a_page_with_no_amount_stays_blank(self):
        self.assertEqual(bs.detail_value("Sealed bids will be received."), "")


class PlanetBidsValueTests(unittest.TestCase):
    """The field a customer photographed and asked about.

    A City of Duarte posting on PlanetBids printed "Estimated Bid Value
    $130,000.00" and the app showed an empty Est. Value box. Two separate
    faults, both here:

    The label. "Estimated Bid Value" has a word between "estimated" and the
    noun, and the pattern required them adjacent.

    The exclusion window. It searched 60 characters back from the match and
    hit "Liquidated Damages $1,000 per calendar day" -- the PREVIOUS field --
    so even once the label matched, the amount was thrown away. A
    disqualifier now only counts where it is attached to the figure:
    immediately before the label, or immediately after the number, which is
    where the rate qualifiers actually sit.
    """

    # Verbatim, in page order, from the posting in the screenshot.
    DUARTE = ("Offer Valid Liquidated Damages $1,000 per calendar day "
              "Estimated Bid Value $130,000.00 Start/Delivery Date "
              "Project Duration 25 Working Days")

    def test_the_estimated_bid_value_is_read(self):
        self.assertEqual(bs.detail_value(self.DUARTE), "$130,000.00")

    def test_the_liquidated_damages_on_the_same_page_are_not(self):
        self.assertNotIn("1,000", bs.detail_value(self.DUARTE))

    def test_a_word_between_estimated_and_the_noun(self):
        for text, want in (("Estimated Project Cost: $85,000", "$85,000"),
                           ("Estimated Contract Value $1,250,000", "$1,250,000"),
                           ("Estimated Bid Value $95,000", "$95,000")):
            self.assertEqual(bs.detail_value(text), want, text)

    def test_a_rate_immediately_after_the_figure_is_not_a_project_value(self):
        for text in ("Liquidated damages estimated cost $1,500 per calendar day",
                     "Estimated cost $500 per occurrence",
                     "Estimated amount $12 per linear foot"):
            self.assertEqual(bs.detail_value(text), "", text)

    def test_a_disqualifier_immediately_before_the_label_still_wins(self):
        for text in ("Bid Bond estimated amount $25,000",
                     "Bid security estimated value $20,000"):
            self.assertEqual(bs.detail_value(text), "", text)

    def test_a_previous_field_no_longer_disqualifies_the_next_one(self):
        """The exact regression: an unrelated earlier field killed a real
        value 40 characters later."""
        text = ("Publication Date/Time: 7/1/2026 8:00 AM "
                "Estimated Bid Value $95,000")
        self.assertEqual(bs.detail_value(text), "$95,000")


if __name__ == "__main__":
    unittest.main()
