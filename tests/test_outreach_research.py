"""What the research tool is allowed to hand a person about to write an email.

The failure this guards against is subtle, because it produces an email that
looks researched. Columbia Curb & Gutter were emailed about their 2001 SBA
Regional Prime Contractor award -- true, checkable, and the third paragraph
of their homepage. It was twenty-five years old, and leading with it told the
reader exactly how far down the page the sender had got.

So the tests here are less about extraction working and more about extraction
being honest: a dated claim has to arrive labelled with its age, a quote has
to be a quote rather than a fragment ending mid-company-name, and a specific
trade has to beat a generic one when both are on the page.

The fetch layer is stubbed throughout. These pages are fixtures, not the
live web -- a test that depends on a contractor not redesigning their site
is a test that fails for reasons that teach nobody anything.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import outreach_research as R  # noqa: E402

# Trimmed from ccgmissouri.com, the page the 2001 mistake came from. The
# leading run-on is the real template's navigation: no punctuation anywhere,
# so a naive splitter treats the whole menu as one sentence.
CCG = """
<html><body>
Highlights by HTML5 UP Columbia Curb &amp; Gutter Co. Turnkey Solutions
Welcome Who We Are Founded in 1968, Columbia Curb &amp; Gutter Co. has
established itself in the Midwest as a reliable company offering turnkey
solutions for projects of all sizes.
Columbia Curb &amp; Gutter Co. was selected as the 2001 Regional Prime
Contractor of the Year for Region VII by the U.S. Small Business
Administration.
In addition, Columbia Curb &amp; Gutter Co. has built an impressive
construction equipment and trucking fleet allowing us to be mobile to
construction locations.
Columbia Curb &amp; Gutter Company specializes in concrete slip-formed
barrier for roads and bridges, as well as pavement cold milling.
Using our years of experience, we help to deliver quality projects on time
for contractors throughout Missouri and neighboring states.
</body></html>
"""


def _serving(html):
    """Stand in for the network: every path returns the same page."""
    return lambda url, **kw: (200, html)


class _Stubbed(unittest.TestCase):
    def serve(self, html):
        original = R.state_fetch.fetch
        R.state_fetch.fetch = _serving(html)
        self.addCleanup(lambda: setattr(R.state_fetch, "fetch", original))

    def research(self, html, **row):
        self.serve(html)
        base = {"slug": "ccg", "company": "Columbia Curb & Gutter",
                "city": "Columbia", "state": "MO", "email": "ccg@ccgmissouri.com"}
        base.update(row)
        return R.research(base)


class DatedClaimsAnnounceTheirAgeTests(_Stubbed):
    def test_the_2001_award_is_reported_as_stale(self):
        found = self.research(CCG)
        stale = [d for d in found["dated"] if d["stale"]]
        self.assertTrue(stale, "the 2001 award was not flagged")
        self.assertEqual(stale[0]["year"], 2001)
        self.assertEqual(stale[0]["age"], R.THIS_YEAR - 2001)

    def test_the_founding_year_is_not_itself_a_stale_claim(self):
        """1968 is the age of the company, not a fact that went off."""
        found = self.research(CCG)
        self.assertEqual(found["founded"], 1968)
        self.assertNotIn(1968, [d["year"] for d in found["dated"]])

    def test_a_recent_year_is_not_flagged(self):
        page = "<p>We completed the Broadway bridge deck in %d.</p>" % (
            R.THIS_YEAR - 1)
        found = self.research(page)
        self.assertEqual([d for d in found["dated"] if d["stale"]], [])

    def test_the_staleness_boundary_is_the_constant(self):
        page = "<p>Named contractor of the year in %d for the region.</p>" % (
            R.THIS_YEAR - R.STALE_YEARS - 1)
        self.assertTrue(self.research(page)["dated"][0]["stale"])


class QuotesAreWholeTests(_Stubbed):
    def test_a_company_suffix_does_not_end_the_sentence(self):
        """"Columbia Curb & Gutter Co." must not be cut after "Co."."""
        found = self.research(CCG)
        award = [d for d in found["dated"] if d["year"] == 2001][0]
        self.assertIn("Small Business Administration", award["sentence"])

    def test_us_is_not_two_sentences(self):
        found = self.research(CCG)
        award = [d for d in found["dated"] if d["year"] == 2001][0]
        self.assertIn("U.S.", award["sentence"])

    def test_an_unpunctuated_navigation_run_on_is_clipped(self):
        """The founding fact survives; the template credit does not tag along."""
        found = self.research(CCG)
        self.assertIn("Founded in 1968", found["founded_note"])
        self.assertLess(len(found["founded_note"]), 300)


class SpecificBeatsGenericTests(_Stubbed):
    def test_a_named_trade_is_preferred_over_the_broad_one(self):
        found = self.research(CCG)
        joined = " ".join(found["specialties"])
        self.assertIn("slip-formed", joined)

    def test_broad_trades_are_reported_when_nothing_narrower_exists(self):
        page = "<p>We pour driveways, patios and sidewalk for homeowners.</p>"
        self.assertTrue(self.research(page)["specialties"])

    def test_the_fleet_is_found_because_it_changes_the_pitch(self):
        """Own fleet means regional reach, which is the radius argument."""
        found = self.research(CCG)
        self.assertIn("scale", found["signals"])
        self.assertIn("fleet", " ".join(found["signals"]["scale"]).lower())

    def test_the_service_area_is_found(self):
        found = self.research(CCG)
        self.assertIn("service_area", found["signals"])


class NothingToReadIsSaidPlainlyTests(_Stubbed):
    def test_a_free_mail_row_is_reported_not_guessed(self):
        """No website column and a gmail address means no site, not a guess."""
        found = self.research(CCG, email="columbiaconcreteco@gmail.com")
        self.assertIn("free-mail", found["problem"])
        self.assertEqual(found["site"], "")

    def test_an_explicit_website_column_beats_the_email_domain(self):
        found = self.research(CCG, email="someone@gmail.com",
                              website="ccgmissouri.com")
        self.assertEqual(found["problem"], "")
        self.assertEqual(found["site"], "https://ccgmissouri.com")

    def test_an_unreachable_site_is_reported(self):
        original = R.state_fetch.fetch
        R.state_fetch.fetch = lambda url, **kw: (403, "")
        self.addCleanup(lambda: setattr(R.state_fetch, "fetch", original))
        found = R.research({"slug": "x", "company": "X", "city": "Columbia",
                            "state": "MO", "email": "x@example.com"})
        self.assertIn("nothing readable", found["problem"])

    def test_no_prose_is_ever_produced(self):
        """The tool reports facts. The email is typed by a person."""
        found = self.research(CCG)
        self.assertNotIn("angle", found)
        self.assertNotIn("intro", found)
        self.assertNotIn("subject", found)


if __name__ == "__main__":
    unittest.main()
