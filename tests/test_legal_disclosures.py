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
