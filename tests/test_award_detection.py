"""Tests for _looks_awarded, the filter that keeps finished work out of the feed.

Local news coverage of a council awarding a contract is nearly indistinguishable
from a bid notice: same project, same agency, same dollar figure, often the same
week. It is the opposite of a lead. A real one reached a customer -- Jefferson,
Iowa's Westwood Drive sidewalk, awarded to Cardenas Concrete for $748,908 --
and was shown as an open bid because the extraction model left the status blank
and _is_open_bid treats an unstated status as open.

The counter-case matters just as much: a city's live bid page routinely lists
recent awards next to current solicitations, and closing those would be a much
worse bug than the one being fixed.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


# The article that prompted this, as it actually reads.
JEFFERSON_AWARD = """
A project for a new sidewalk in Jefferson may start later this fall.
The City Council awarded the contract for a new sidewalk to be installed at
Westwood Drive at their July 28th meeting. The winning bid was $748,908 from
Cardenas Concrete in Fort Dodge, which engineering firm Bolton and Menk said
was 17 percent below their original estimated cost. Along with that bid, the
Council approved an alternate of $37,594 to re-pave the east alleyway of the
downtown square.
Peterson adds the contractor has indicated that they may start after Labor Day.
"""

REAL_SOLICITATION = """
INVITATION TO BID -- 2026 Sidewalk and ADA Ramp Replacement Program.
Sealed bids will be received by the City Clerk until 2:00 p.m. on September 15,
2026, at which time bids will be publicly opened and read aloud. The work
includes approximately 4,200 square feet of concrete sidewalk removal and
replacement and twelve ADA curb ramps.
"""

MIXED_PAGE = """
Current Bid Opportunities
2026 Street Improvements -- bids due September 30, 2026.
Recently Awarded: the 2025 Sidewalk Program contract was awarded to
Cardenas Concrete on June 2, 2025.
"""


class LooksAwardedTests(unittest.TestCase):
    def test_the_article_that_shipped_is_recognised_as_awarded(self):
        self.assertTrue(ls._looks_awarded(JEFFERSON_AWARD))

    def test_a_real_solicitation_is_not_awarded(self):
        self.assertFalse(ls._looks_awarded(REAL_SOLICITATION))

    def test_a_page_with_both_stays_open(self):
        """A live bid page listing a past award alongside a current
        solicitation must not be closed -- suppressing real open bids is a
        worse failure than showing one stale award."""
        self.assertFalse(ls._looks_awarded(MIXED_PAGE))

    def test_empty_text_is_not_awarded(self):
        self.assertFalse(ls._looks_awarded(""))
        self.assertFalse(ls._looks_awarded(None))

    def test_ordinary_bid_language_alone_does_not_trigger_it(self):
        self.assertFalse(ls._looks_awarded(
            "The city seeks bids for concrete sidewalk replacement."))

    def test_low_bidder_phrasing_counts(self):
        self.assertTrue(ls._looks_awarded(
            "Council named the apparent low bidder at Monday's meeting."))

    def test_notice_of_award_counts(self):
        self.assertTrue(ls._looks_awarded(
            "A notice of award was issued for the curb and gutter project."))


class ExtractionAppliesTheFilterTests(unittest.TestCase):
    """_ai_extract must override the model rather than trust it: the shipped
    bug was the model returning a bid with no status at all."""

    def setUp(self):
        self._key = ls.OPENAI_API_KEY
        ls.OPENAI_API_KEY = "test-key"
        self.addCleanup(lambda: setattr(ls, "OPENAI_API_KEY", self._key))

    def _fake_openai(self, payload):
        import io
        import json as _json

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return _json.dumps({
                    "choices": [{"message": {"content": _json.dumps(payload)}}]
                }).encode()

        self._orig_open = ls.urllib.request.urlopen
        ls.urllib.request.urlopen = lambda *a, **k: _Resp()
        self.addCleanup(
            lambda: setattr(ls.urllib.request, "urlopen", self._orig_open))

    def test_a_bid_with_no_status_on_an_award_page_becomes_awarded(self):
        self._fake_openai([{"title": "Westwood Drive Sidewalk", "status": ""}])
        bids = ls._ai_extract("Jefferson, IA", JEFFERSON_AWARD)
        self.assertEqual(bids[0]["status"], "Awarded")
        self.assertFalse(ls._is_open_bid(bids[0]))

    def test_a_bid_the_model_called_open_on_an_award_page_is_overridden(self):
        self._fake_openai([{"title": "Westwood Drive Sidewalk",
                            "status": "Open"}])
        bids = ls._ai_extract("Jefferson, IA", JEFFERSON_AWARD)
        self.assertEqual(bids[0]["status"], "Awarded")

    def test_a_genuine_solicitation_is_left_alone(self):
        self._fake_openai([{"title": "2026 Sidewalk Program",
                            "status": "Open"}])
        bids = ls._ai_extract("Aurora, MO", REAL_SOLICITATION)
        self.assertEqual(bids[0]["status"], "Open")
        self.assertTrue(ls._is_open_bid(bids[0]))

    def test_an_already_closed_bid_is_not_relabelled(self):
        self._fake_openai([{"title": "Old job", "status": "Closed"}])
        bids = ls._ai_extract("Jefferson, IA", JEFFERSON_AWARD)
        self.assertEqual(bids[0]["status"], "Closed")


if __name__ == "__main__":
    unittest.main()
