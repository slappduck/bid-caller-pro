"""Deciding whether a coverage answer really describes the prospect's patch.

The number in a cold email is the only claim the recipient can check in
thirty seconds, so a wrong one is worse than no email. Frankfort is the case
this guard exists for: /coverage answered 8 agencies for Frankfort, IL
because three Illinois places share the name and the geocoder averaged them
into a field 150 miles away.

The first version tested whether the prospect's own town appeared among the
three nearest agencies. That proxy failed in both directions and cost real
prospects: Milwaukee's three closest entries are Whitefish Bay, South
Milwaukee and New Berlin -- suburbs within fifteen miles -- and Skippack, PA
has no bid page of its own though Lansdale is eight miles away. Both had
resolved perfectly and both were held.

The question was always "how far is the nearest work", which is a number.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.outreach_draft import _looks_like_the_right_town, NEAR_ENOUGH_MI


class DistanceDecidesTests(unittest.TestCase):
    def test_a_nearby_agency_passes_even_in_another_town(self):
        """Skippack, PA -- no bid page of its own, Lansdale eight miles off."""
        data = {"agencies": 479, "nearest_mi": 8.2,
                "nearest": ["Lansdale, PA", "Conshohocken, PA", "Souderton, PA"]}
        self.assertTrue(_looks_like_the_right_town(data, "Skippack"))

    def test_suburbs_of_the_prospects_own_city_pass(self):
        """Milwaukee -- its three closest entries are all suburbs."""
        data = {"agencies": 223, "nearest_mi": 5.4,
                "nearest": ["Whitefish Bay, WI", "South Milwaukee, WI",
                            "New Berlin, WI"]}
        self.assertTrue(_looks_like_the_right_town(data, "Milwaukee"))

    def test_the_frankfort_case_is_still_caught(self):
        data = {"agencies": 8, "nearest_mi": 151.0,
                "nearest": ["Quincy, IL", "Macomb, IL"]}
        self.assertFalse(_looks_like_the_right_town(data, "Frankfort"))

    def test_the_boundary_is_inclusive(self):
        self.assertTrue(_looks_like_the_right_town(
            {"nearest_mi": NEAR_ENOUGH_MI, "nearest": ["Anywhere, XX"]}, "Town"))
        self.assertFalse(_looks_like_the_right_town(
            {"nearest_mi": NEAR_ENOUGH_MI + 0.1, "nearest": ["Anywhere, XX"]}, "Town"))

    def test_zero_miles_passes(self):
        """The prospect's own town has a bid page."""
        self.assertTrue(_looks_like_the_right_town(
            {"nearest_mi": 0.0, "nearest": ["Waukesha, WI"]}, "Waukesha"))


class FallsBackWhenTheServerIsOlderTests(unittest.TestCase):
    """nearest_mi arrives with a deploy. Until then, the old name test."""

    def test_name_match_still_works_without_a_distance(self):
        self.assertTrue(_looks_like_the_right_town(
            {"nearest": ["Waukesha, WI", "Pewaukee, WI"]}, "Waukesha"))

    def test_no_distance_and_no_name_match_is_held(self):
        self.assertFalse(_looks_like_the_right_town(
            {"nearest": ["Quincy, IL", "Macomb, IL"]}, "Frankfort"))

    def test_an_empty_answer_is_held(self):
        self.assertFalse(_looks_like_the_right_town({}, "Anywhere"))
        self.assertFalse(_looks_like_the_right_town({"nearest": []}, "Anywhere"))

    def test_a_non_numeric_distance_does_not_crash(self):
        self.assertFalse(_looks_like_the_right_town(
            {"nearest_mi": "close", "nearest": ["Quincy, IL"]}, "Frankfort"))


if __name__ == "__main__":
    unittest.main()


class DoNotContactTests(unittest.TestCase):
    """"Please remove me from your email list" has to stick.

    A.C. Moate sent exactly that the morning after the first send. Every other
    rule in this tool is a judgement call the operator can override with
    --slug, which deliberately ignores status so a wrongly-held row can be
    forced through. This is the one status that must survive that override,
    because the cost of getting it wrong lands on someone who already asked
    once and is not a mistake you can take back.
    """

    def test_the_statuses_that_mean_never_again(self):
        from tools.outreach_draft import DO_NOT_CONTACT
        self.assertIn("unsubscribed", DO_NOT_CONTACT)
        self.assertIn("bounced", DO_NOT_CONTACT)

    def test_slug_cannot_force_an_unsubscribed_prospect(self):
        """--slug overrides status on purpose. Not this status."""
        import inspect
        from tools import outreach_draft as od
        src = inspect.getsource(od.main)
        # The filter has to happen before the --slug branch selects rows.
        self.assertIn("DO_NOT_CONTACT", src)
        self.assertLess(src.index("DO_NOT_CONTACT"), src.index('if args.slug:'),
                        "the block must be applied before --slug picks rows")

    def test_a_refusal_is_reported_rather_than_silent(self):
        """Silently dropping a slug the operator asked for looks like a bug
        and invites them to work around it."""
        import inspect
        from tools import outreach_draft as od
        self.assertIn("refusing", inspect.getsource(od.main))


class LocationEvidenceTests(unittest.TestCase):
    """A.C. Moate is in Auburn, Washington. They were emailed about Toledo.

    Candidates come from searching "<city> concrete contractor sidewalk curb",
    and a contractor with per-city landing pages ranks for cities they merely
    advertise into. The search answered the question it was asked; nothing
    checked whether the company had an address near the town whose number the
    email was about to quote. They replied asking to be removed, which is the
    correct response to mail about a market you do not work in.
    """

    def test_a_site_naming_other_states_is_held(self):
        from tools.verify_prospect_location import evidence
        import tools.verify_prospect_location as vp
        orig = vp.state_fetch.fetch
        vp.state_fetch.fetch = lambda u, **kw: (
            200, "<p>Serving Auburn, WA and Portland, OR and Reno, NV</p>")
        try:
            verdict, note = evidence({"email": "x@acmoate.com",
                                      "city": "Toledo", "state": "OH"})
        finally:
            vp.state_fetch.fetch = orig
        self.assertEqual(verdict, "no_state")
        self.assertIn("WA", note)

    def test_the_right_state_passes(self):
        from tools.verify_prospect_location import evidence
        import tools.verify_prospect_location as vp
        orig = vp.state_fetch.fetch
        vp.state_fetch.fetch = lambda u, **kw: (
            200, "<p>Rockford, IL 61101 — serving northern Illinois</p>")
        try:
            verdict, _ = evidence({"email": "x@concretesystemsinc.net",
                                   "city": "Rockford", "state": "IL"})
        finally:
            vp.state_fetch.fetch = orig
        self.assertEqual(verdict, "ok")

    def test_a_neighbouring_town_still_passes(self):
        """Willow Grove serving Skippack, twenty miles off, is the same
        market and the same coverage number. Only a wrong STATE is fatal."""
        from tools.verify_prospect_location import evidence
        import tools.verify_prospect_location as vp
        orig = vp.state_fetch.fetch
        vp.state_fetch.fetch = lambda u, **kw: (
            200, "<p>2401 Wyandotte Rd, Willow Grove, PA 19090</p>")
        try:
            verdict, note = evidence({"email": "x@claussbrothers.com",
                                      "city": "Skippack", "state": "PA"})
        finally:
            vp.state_fetch.fetch = orig
        self.assertEqual(verdict, "ok")
        self.assertIn("not named", note)

    def test_a_free_mail_address_without_a_website_is_held(self):
        """gmail.com tells you nothing about who they are."""
        from tools.verify_prospect_location import evidence
        verdict, _ = evidence({"email": "someone@gmail.com",
                               "city": "Philadelphia", "state": "PA"})
        self.assertEqual(verdict, "no_site")


class SignatureTests(unittest.TestCase):
    """Every outreach email is commercial mail from a business.

    They went out signed "Josh" and nothing else. That reads as a note from a
    person rather than a company, and it omits the one thing US commercial
    email is required to carry: a valid physical postal address.

    These set the module's own values rather than mutating os.environ and
    reloading. The reload version passed on its own and failed once in a full
    run: a reloaded module and a global environment are both shared state,
    and whichever test imported outreach_draft next got whatever was left
    behind.
    """

    def setUp(self):
        from tools import outreach_draft as od
        self.od = od
        self._saved = (od.SIGNER, od.ADDRESS, od.PHONE)

    def tearDown(self):
        self.od.SIGNER, self.od.ADDRESS, self.od.PHONE = self._saved

    def _set(self, signer="", address="", phone=""):
        self.od.SIGNER, self.od.ADDRESS, self.od.PHONE = signer, address, phone
        return self.od

    def test_no_address_means_no_signature(self):
        od = self._set(signer="Josh Surname")
        self.assertIsNone(od.build_signature())

    def test_no_address_stops_the_tool_rather_than_warning(self):
        """A warning gets scrolled past. This has to be a refusal."""
        od = self._set(signer="Josh Surname")
        self.assertIn("CURBCALL_ADDRESS", od.signature_problem())

    def test_no_signer_is_also_refused(self):
        od = self._set(address="PO Box 1, Town, MO 65605")
        self.assertIn("CURBCALL_SIGNER", od.signature_problem())

    def test_a_configured_signature_carries_the_postal_address(self):
        od = self._set(signer="Josh Surname",
                       address="PO Box 1, Town, MO 65605")
        sig = od.build_signature()
        self.assertIn("PO Box 1, Town, MO 65605", sig)
        self.assertIn("CurbCall Pro", sig)
        self.assertIn("curbcallpro.com", sig)
        self.assertEqual(od.signature_problem(), "")

    def test_the_opt_out_is_explicit_not_implied(self):
        od = self._set(signer="J", address="PO Box 1")
        self.assertIn("take you off the list", od.build_signature())

    def test_the_phone_is_optional_but_used_when_given(self):
        od = self._set(signer="J", address="PO Box 1")
        self.assertNotIn("555", od.build_signature())
        od = self._set(signer="J", address="PO Box 1", phone="(417) 555-0143")
        self.assertIn("(417) 555-0143", od.build_signature())


class ResearchHappensBeforeDraftingTests(unittest.TestCase):
    """The location check has to run inside drafting, not beside it.

    It existed as a separate tool that nobody was obliged to run, which is the
    same as not having it -- and A.C. Moate is what that costs: a Washington
    contractor told about bid pages near Toledo, and a contact burned for
    good. A check you have to remember is not a check.
    """

    def setUp(self):
        import inspect
        from tools import outreach_draft as od
        self.od = od
        self.src = inspect.getsource(od.main)

    def test_the_check_runs_in_the_draft_loop(self):
        self.assertIn("_location_evidence", self.src)

    def test_it_runs_before_the_coverage_lookup(self):
        """Cheapest correct order: do not even ask for a number for a
        contractor who is in the wrong state."""
        self.assertLess(self.src.index("_location_evidence"),
                        self.src.index("coverage(row["))

    def test_a_bad_location_is_held_not_drafted(self):
        i = self.src.index("_location_evidence")
        window = self.src[i:i + 400]
        self.assertIn("held.append", window)
        self.assertIn("continue", window)

    def test_the_reason_names_the_problem(self):
        i = self.src.index("_location_evidence")
        self.assertIn("location unconfirmed", self.src[i:i + 400])


class SenderFileTests(unittest.TestCase):
    """The signer's postal address lives locally, never in the repo.

    Commercial email has to carry a physical address, this repo is public,
    and the address in question is a home. Those two facts must not meet, so
    the values come from data/sender.env, which .gitignore excludes.
    """

    def test_the_sender_file_is_gitignored(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, os.pardir, ".gitignore"),
                  encoding="utf-8") as f:
            self.assertIn("data/sender.env", f.read())

    def test_no_postal_address_is_hardcoded_in_the_tool(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, os.pardir, "tools", "outreach_draft.py"),
                  encoding="utf-8") as f:
            src = f.read()
        # Anything shaped like a street address belongs in the gitignored
        # sender file, never in a file that gets pushed. Writing the real one
        # into this comment as an example would have defeated the test it is
        # explaining -- which is exactly what happened on the first draft.
        hits = re.findall(r"\d{2,6}\s+[A-Z]\.?\s?[A-Za-z]+\s+"
                          r"(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Ln|Lane|Blvd)\b",
                          src)
        self.assertEqual(hits, [], f"postal address in a committed file: {hits}")

    def test_a_real_environment_variable_wins(self):
        """So a one-off send can override without editing the file."""
        from tools import outreach_draft as od
        os.environ["CURBCALL_SIGNER"] = "Someone Else"
        try:
            self.assertEqual(od._sender("CURBCALL_SIGNER"), "Someone Else")
        finally:
            os.environ.pop("CURBCALL_SIGNER", None)

    def test_a_missing_file_is_not_an_error(self):
        from tools.outreach_draft import _load_sender_env
        self.assertEqual(_load_sender_env("/nonexistent/sender.env"), {})

    def test_comments_and_blank_lines_are_ignored(self):
        import tempfile
        from tools.outreach_draft import _load_sender_env
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("# a note\n\nCURBCALL_SIGNER=Example Name\nbroken line\n")
            path = f.name
        try:
            got = _load_sender_env(path)
            self.assertEqual(got.get("CURBCALL_SIGNER"), "Example Name")
            self.assertNotIn("broken line", got)
        finally:
            os.unlink(path)
