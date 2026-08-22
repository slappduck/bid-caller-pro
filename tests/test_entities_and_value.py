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


if __name__ == "__main__":
    unittest.main()
