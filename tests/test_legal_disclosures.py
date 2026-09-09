"""Disclosures that have to keep matching the software.

Three failures found by reading the published policies against the code, all
of the same kind: the documents said something that used to be true.

  * The privacy policy named seven processors. The code calls fifteen. Brave,
    DuckDuckGo, Zippopotam, Nominatim, BigDataCloud, Resend, Upstash and
    Cloudflare were all undisclosed, and Netlify was listed after the site had
    moved off it. BigDataCloud is the one that matters most: it reverse-geocodes
    a device location, the most sensitive thing the product touches.

  * The word "renew" appeared nowhere a buyer could see it. Plans renew
    automatically and the Terms said so, but the purchase screen said only
    "$49 / month" and "Cancel anytime" -- a benefit, not a disclosure that the
    card is charged again.

  * The coverage claim was a hand-typed 6,869 against a real 6,868.

The tests below are deliberately about the SHAPE of the disclosure rather than
its wording, so the copy can be rewritten without breaking them, but a
processor cannot be added in code and forgotten in the policy.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, os.pardir)
WEB = os.path.join(ROOT, "curbcall_netlify_v4")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


class ProcessorsAreDisclosedTests(unittest.TestCase):
    """Every third party the backend sends data to appears in the policy."""

    # host fragment -> the name a reader would recognise
    PROCESSORS = {
        "supabase": "Supabase", "stripe": "Stripe", "openai": "OpenAI",
        "tavily": "Tavily", "brave": "Brave", "duckduckgo": "DuckDuckGo",
        "zippopotam": "Zippopotam", "nominatim": "Nominatim",
        "bigdatacloud": "BigDataCloud", "resend": "Resend",
    }

    def setUp(self):
        self.policy = read(WEB, "privacy.html")
        self.backend = read(ROOT, "license_server.py")

    def test_every_service_the_code_calls_is_named_in_the_policy(self):
        missing = []
        for host, name in self.PROCESSORS.items():
            if re.search(host, self.backend, re.I) and not re.search(
                    name, self.policy, re.I):
                missing.append(name)
        self.assertEqual(missing, [],
                         f"called in code, absent from privacy policy: {missing}")

    def test_the_host_is_current(self):
        """The site moved to Cloudflare; the policy still credited Netlify."""
        self.assertIn("Cloudflare", self.policy)
        self.assertNotIn("Netlify", self.policy)

    def test_device_location_is_explained_not_just_vendor_named(self):
        """It is the most sensitive thing collected, and it is opt-in."""
        self.assertIn("Use My", self.policy)
        self.assertRegex(self.policy, r"(?i)location")


class AutomaticRenewalIsDisclosedTests(unittest.TestCase):
    """Said where the buyer decides, not only in the Terms.

    Federal and several state auto-renewal rules want the renewal terms clear
    and conspicuous before the transaction. "Cancel anytime" is a benefit
    claim; it does not tell anyone the card is charged again.
    """

    def _near_buttons(self, html):
        """Text within a few hundred characters after each Subscribe link."""
        out = []
        for m in re.finditer(r'(?i)>Subscribe [A-Za-z]+</a>', html):
            out.append(html[m.end():m.end() + 400])
        return out

    def test_every_subscribe_button_is_followed_by_renewal_terms(self):
        for page in ("index.html", "app.html"):
            html = read(WEB, page)
            windows = self._near_buttons(html)
            self.assertTrue(windows, f"no Subscribe button found in {page}")
            for w in windows:
                self.assertRegex(
                    w, r"(?i)renews automatically",
                    f"a Subscribe button in {page} has no renewal disclosure")

    def test_the_price_and_period_are_restated_with_it(self):
        for page in ("index.html", "app.html"):
            html = read(WEB, page)
            for w in self._near_buttons(html):
                self.assertRegex(w, r"\$\d+\s*/\s*(month|year)",
                                 f"{page}: renewal note omits price and period")

    def test_cancelling_is_told_where_to_happen(self):
        for page in ("index.html", "app.html"):
            for w in self._near_buttons(read(WEB, page)):
                self.assertRegex(w, r"(?i)cancel")


class CoverageClaimTests(unittest.TestCase):
    """An advertising claim has to stay true without being retyped."""

    def test_no_exact_hand_typed_count(self):
        html = read(WEB, "index.html")
        # A bare four-digit figure next to "agency bid pages" is the shape that
        # went stale. A floor ("more than 6,800") does not.
        bad = re.findall(r"(?<!than )\b\d,\d{3}\s+verified agency bid pages", html)
        self.assertEqual(bad, [], f"exact coverage claim will drift: {bad}")

    def test_the_claim_is_a_floor(self):
        html = read(WEB, "index.html")
        self.assertRegex(html, r"(?i)more than [\d,]+ verified agency bid pages")

    def test_the_floor_is_actually_true(self):
        """Checked against the directory rather than trusted."""
        import csv
        html = read(WEB, "index.html")
        m = re.search(r"(?i)more than ([\d,]+) verified agency bid pages", html)
        claimed = int(m.group(1).replace(",", ""))
        with open(os.path.join(ROOT, "data", "bid_portal_directory.csv"),
                  newline="", encoding="utf-8") as f:
            held = sum(1 for r in csv.DictReader(f)
                       if r.get("status") == "found" and r.get("bid_url"))
        # The directory file is only part of it; seeds in bid_portals.py make up
        # the rest. The claim must not exceed what the file alone plus a sane
        # allowance can support, and must be a floor rather than a ceiling.
        self.assertGreater(claimed, held,
                           "claim should exceed the CSV alone (seeds add more)")
        self.assertLess(claimed, held * 2,
                        "claim is far above anything the data supports")


if __name__ == "__main__":
    unittest.main()


class ConsentVersionsMatchThePublishedPagesTests(unittest.TestCase):
    """The version stamped on a consent record has to name a real document.

    Every acceptance row stores TERMS_VERSION and PRIVACY_VERSION. That is the
    whole point of the record -- "they agreed to the Terms" is weak once the
    Terms change, and "they accepted 2026-06-17" is only evidence while
    2026-06-17 is genuinely what terms.html said that day. If the page is
    edited and the constant is not, every row written afterwards cites a
    version that never existed.
    """

    MONTHS = ("January February March April May June July August September "
              "October November December").split()

    def _published_date(self, page):
        html = read(WEB, page)
        m = re.search(r"Last updated:\s*([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})",
                      html)
        self.assertIsNotNone(m, "no 'Last updated' date on " + page)
        month, day, year = m.group(1), int(m.group(2)), m.group(3)
        self.assertIn(month, self.MONTHS, "unreadable month on " + page)
        return "%s-%02d-%02d" % (year, self.MONTHS.index(month) + 1, day)

    def _constant(self, name):
        src = read(ROOT, "license_server.py")
        m = re.search(name + r'\s*=\s*os\.environ\.get\(\s*"' + name +
                      r'"\s*,\s*"([\d-]+)"\s*\)', src)
        self.assertIsNotNone(m, name + " is not a dated constant any more")
        return m.group(1)

    def test_the_terms_version_is_the_date_on_terms_html(self):
        self.assertEqual(self._constant("TERMS_VERSION"),
                         self._published_date("terms.html"),
                         "TERMS_VERSION and terms.html disagree; consent rows "
                         "would cite a version nobody can read")

    def test_the_privacy_version_is_the_date_on_privacy_html(self):
        self.assertEqual(self._constant("PRIVACY_VERSION"),
                         self._published_date("privacy.html"),
                         "PRIVACY_VERSION and privacy.html disagree")


class RetentionAfterDeletionIsDisclosedTests(unittest.TestCase):
    """Keeping data after someone asks to be deleted is lawful only if said.

    Consent records outlive the account. Retention to defend a legal claim is
    a recognised exception, but it is an exception to a promise the Privacy
    Policy makes in writing -- so the policy has to describe it, and describe
    it accurately, or the retention itself becomes the violation.

    An earlier design hashed the address instead of keeping it. That was
    dropped: the salt was the service-role key, so rotating a credential would
    have made every retained record permanently unverifiable while still
    looking intact, and a hash is worse evidence than a plain address in the
    one situation the record exists for. The address is kept, and the
    retention is bounded instead.
    """

    NUMBER_WORDS = {"ten": 10, "five": 5, "seven": 7, "three": 3, "twelve": 12}

    def policy_text(self):
        return re.sub(r"<[^>]+>", " ", read(WEB, "privacy.html")).lower()

    def retention_years_in_code(self):
        m = re.search(r'TERMS_RETENTION_YEARS\s*=\s*int\(os\.environ\.get\('
                      r'\s*"TERMS_RETENTION_YEARS"\s*,\s*"(\d+)"\s*\)\)',
                      read(ROOT, "license_server.py"))
        self.assertIsNotNone(m, "the retention period is not a constant")
        return int(m.group(1))

    def test_the_policy_says_a_consent_record_is_kept(self):
        text = self.policy_text()
        self.assertIn("after deletion", text)
        self.assertIn("accepted", text)

    def test_the_policy_states_how_long_and_agrees_with_the_code(self):
        """"We keep it for a while" is not a retention notice."""
        text = self.policy_text()
        years = self.retention_years_in_code()
        said = [n for word, n in self.NUMBER_WORDS.items()
                if re.search(r"\b%s years?\b" % word, text)]
        said += [int(m) for m in re.findall(r"\b(\d+) years?\b", text)]
        self.assertIn(years, said,
                      "the policy does not state the %d-year period the code "
                      "actually enforces" % years)

    def test_the_policy_still_promises_the_rest_is_deleted(self):
        """A retention notice must not read as 'we keep everything'."""
        self.assertIn("is deleted", self.policy_text())

    def test_the_app_tells_the_user_before_they_confirm(self):
        """Finding out from the policy afterwards is not consent."""
        app = read(WEB, "app.html")
        i = app.index("Permanently removes your account")
        panel = app[i:i + 600].lower()
        self.assertRegex(panel, r"\b(ten|10) years\b",
                         "the delete screen does not say how long the "
                         "consent record is kept")

    def test_the_cascade_that_destroyed_the_record_is_gone(self):
        sql = read(ROOT, "supabase_sync_schema.sql")
        sql = "\n".join(l for l in sql.splitlines()
                        if not l.strip().startswith("--"))
        self.assertRegex(
            sql, r"alter table terms_acceptances\s+drop constraint if exists",
            "terms_acceptances still cascades from auth.users, so deleting "
            "an account still destroys the consent record")

    def test_deletion_stamps_the_record_rather_than_removing_it(self):
        src = re.sub(r"^\s*#.*$", "", read(ROOT, "license_server.py"), flags=re.M)
        body = src[src.index("def account_delete("):]
        body = body[:body.index("\ndef ")] if "\ndef " in body else body
        self.assertIn("_mark_terms_account_deleted", body)

    def test_the_scheduled_purge_is_the_only_thing_that_deletes_them(self):
        """A bounded promise is only true if exactly one thing enforces it,
        and nothing else can quietly remove a record early."""
        src = re.sub(r"^\s*#.*$", "", read(ROOT, "license_server.py"), flags=re.M)
        deleters = re.findall(
            r'def (\w+)\([^)]*\):(?:(?!\ndef ).)*?terms_acceptances'
            r'(?:(?!\ndef ).)*?method="DELETE"', src, re.S)
        self.assertEqual(deleters, ["_purge_expired_terms_acceptances"],
                         "unexpected deleter(s) of terms_acceptances: %s"
                         % deleters)

    def test_something_actually_runs_the_purge(self):
        """Otherwise the policy promises a deletion that never happens."""
        wf = os.path.join(ROOT, ".github", "workflows",
                          "purge-expired-consent.yml")
        self.assertTrue(os.path.exists(wf), "no scheduled purge job")
        text = read(wf)
        self.assertIn("/terms/purge", text)
        self.assertIn("schedule:", text)


class TermsCoverTheBasicsTests(unittest.TestCase):
    """Clauses whose absence costs nothing until the day it costs everything.

    Found by reading the Terms against the code. None of these existed:

      * No licence grant. Section 5 forbade reselling the data while nothing
        above it ever granted a licence -- you cannot restrict what you never
        conveyed, and the product IS a compiled dataset.
      * No severability. If a court voided one provision -- the liability cap
        being the likeliest -- nothing said the rest survived.
      * No indemnification, so a customer's misuse was our cost.
      * No forum-selection clause. Choosing Missouri LAW without choosing
        Missouri COURTS leaves a dispute to be heard wherever the customer
        lives, under that state's procedure.
    """

    def text(self):
        return re.sub(r"<[^>]+>", " ", read(WEB, "terms.html")).lower()

    def test_a_licence_is_actually_granted(self):
        t = self.text()
        self.assertIn("licence", t)
        self.assertRegex(t, r"limited,?\s+revocable")

    def test_ownership_of_the_compiled_feed_is_claimed(self):
        """The postings are public. The compilation is the product."""
        self.assertIn("compiled", self.text())

    def test_there_is_a_severability_clause(self):
        self.assertRegex(self.text(), r"unenforceable")

    def test_there_is_an_indemnity(self):
        self.assertRegex(self.text(), r"claim against us")

    def test_disputes_have_a_forum_not_just_a_governing_law(self):
        t = self.text()
        self.assertIn("missouri", t)
        self.assertRegex(t, r"courts located in missouri|missouri courts")

    def test_a_price_change_has_a_notice_period(self):
        """"We'll give notice" without a number is whatever the strictest
        state says it is."""
        self.assertRegex(self.text(), r"\b(30|thirty) days\b")

    def test_the_changes_clause_describes_the_prompt_that_exists(self):
        """The software asks for re-acceptance. The clause should say so,
        or the document and the behaviour are two different promises."""
        self.assertIn("accept the new version", self.text())

    def test_the_section_numbers_have_no_gaps(self):
        heads = re.findall(r"<h2>(\d+)\.", read(WEB, "terms.html"))
        nums = [int(n) for n in heads]
        self.assertEqual(nums, list(range(1, len(nums) + 1)),
                         "renumbering left a gap: %s" % nums)


class PrivacyCoversEveryDataFlowTests(unittest.TestCase):
    """Three flows the policy never mentioned, all of them live in the code."""

    def text(self):
        return re.sub(r"<[^>]+>", " ", read(WEB, "privacy.html")).lower()

    def test_published_reviews_are_disclosed(self):
        """reviews stores display_name, company and quote, and approved rows
        appear on the marketing site."""
        t = self.text()
        self.assertIn("review", t)
        self.assertRegex(t, r"shown publicly|public")

    def test_contacts_shown_on_a_bid_are_disclosed(self):
        self.assertRegex(self.text(), r"contracting officer|purchasing agent")

    def test_plan_holders_are_disclosed(self):
        """_attach_plan_holders() shows other contractors who took plans."""
        self.assertIn("plan holder", self.text())

    def test_the_outreach_list_is_disclosed_with_a_way_off_it(self):
        t = self.text()
        self.assertIn("opt out", t)
        self.assertIn("support@curbcallpro.com", t)

    def test_there_is_a_security_statement(self):
        self.assertRegex(self.text(), r"encrypted")

    def test_breach_notification_is_promised(self):
        self.assertIn("breach", self.text())

    def test_billing_retention_has_a_number(self):
        self.assertRegex(self.text(), r"seven years|7 years")

    def test_the_section_numbers_have_no_gaps(self):
        heads = re.findall(r"<h2>(\d+)\.", read(WEB, "privacy.html"))
        nums = [int(n) for n in heads]
        self.assertEqual(nums, list(range(1, len(nums) + 1)),
                         "renumbering left a gap: %s" % nums)
