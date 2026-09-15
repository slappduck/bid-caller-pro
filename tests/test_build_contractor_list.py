"""Building a prospect CSV from contractors' own websites, and only theirs.

Two guards this file exists to pin, taken straight from the module's own
comments:

- **robots.txt decides, and a missing or unreadable one means allowed.**
  _robots_ok() deliberately does not use urllib.robotparser's default
  fetch, because RobotFileParser.read() sends Python-urllib's own
  User-Agent, which the same small-business bot filters that broke this
  codebase's other tools answer with a captcha page -- and RobotFileParser
  then parses that HTML as if it were rules. The comment says this produced
  a 21-of-43 false "disallowed" rate on sites that actually say
  `Disallow:` (empty -- allow everything). So the tests check the exact
  cases that comment describes: no robots.txt, a challenge page standing in
  for one, and an explicit empty Disallow, all read as allowed; only a real
  `Disallow: /` blocks.

- **A directory-listing shell is not a contractor.** Yelp/Angi/BBB/etc. are
  excluded by domain before a single request goes out, never scraped and
  filtered after the fact.

Every HTTP call (_get) is stubbed with canned pages -- nothing here reaches
the live web.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import build_contractor_list as B  # noqa: E402


class _Stubbed(unittest.TestCase):
    def stub(self, obj, name, value):
        original = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(lambda: setattr(obj, name, original))


class TitleTests(unittest.TestCase):
    def test_the_company_name_is_split_off_the_seo_suffix(self):
        page = ("<title>Musselman Concrete LLC | Concrete Contractor "
                "in Aurora MO</title>")
        self.assertEqual(B._title(page), "Musselman Concrete LLC")

    def test_no_title_tag_is_empty_not_an_error(self):
        self.assertEqual(B._title("<html><body>hi</body></html>"), "")


class EmailExtractionTests(unittest.TestCase):
    def test_platform_and_placeholder_addresses_are_filtered(self):
        page = ("<body>contact@sentry.io noreply@foo.com "
                "real@musselmanconcrete.com</body>")
        self.assertEqual(B._emails(page, "www.musselmanconcrete.com"),
                         ["real@musselmanconcrete.com"])

    def test_an_address_on_the_business_own_domain_is_ranked_first(self):
        """A testimonial's gmail address or a web-designer's credit should
        not outrank the business's own domain."""
        page = ("<body>Reach out at feedback@gmail.com or "
                "info@acmeconcrete.com any time.</body>")
        found = B._emails(page, "acmeconcrete.com")
        self.assertEqual(found[0], "info@acmeconcrete.com")

    def test_no_real_email_on_the_page_is_an_empty_list(self):
        self.assertEqual(B._emails("<body>no addresses here</body>", "x.com"), [])


class PhoneExtractionTests(unittest.TestCase):
    def test_a_formatted_phone_number_is_found(self):
        page = "<body>Call us at (417) 555-1234 today.</body>"
        self.assertEqual(B._phone(page), "(417) 555-1234")

    def test_digits_inside_a_script_tag_are_not_mistaken_for_a_phone(self):
        page = ("<script>var trackingId = 4175551234;</script>"
                "<body>no phone here</body>")
        self.assertEqual(B._phone(page), "")


class RobotsOkTests(_Stubbed):
    """The exact cases the module's own comment names: a missing file and a
    challenge page must both read as allowed, and only a real Disallow
    blocks."""

    def serve_robots(self, text):
        self.stub(B, "_get", lambda url, timeout=10, limit=100000: text)

    def test_a_missing_robots_txt_means_allowed(self):
        self.serve_robots(None)
        self.assertTrue(B._robots_ok("http://acme.com"))

    def test_a_bot_challenge_page_is_not_read_as_rules(self):
        self.serve_robots("<html>please enable javascript</html>")
        self.assertTrue(B._robots_ok("http://acme.com"))

    def test_an_explicit_empty_disallow_means_allow_everything(self):
        self.serve_robots("User-agent: *\nDisallow:\n")
        self.assertTrue(B._robots_ok("http://acme.com"))

    def test_a_real_disallow_all_is_honoured(self):
        self.serve_robots("User-agent: *\nDisallow: /\n")
        self.assertFalse(B._robots_ok("http://acme.com"))

    def test_a_partial_disallow_still_allows_the_homepage(self):
        self.serve_robots("User-agent: *\nDisallow: /private\n")
        self.assertTrue(B._robots_ok("http://acme.com"))


class ProbeTests(_Stubbed):
    def test_an_aggregator_domain_is_skipped_before_any_request(self):
        r = B.probe(("MO", "Aurora", "http://yelp.com/biz/acme"))
        self.assertEqual(r["status"], "aggregator")
        self.assertEqual(r["company"], "")

    def test_a_robots_disallowed_site_is_recorded_not_dropped_silently(self):
        self.stub(B, "_robots_ok", lambda base: False)
        r = B.probe(("MO", "Aurora", "http://acme.com"))
        self.assertEqual(r["status"], "robots_disallow")

    def test_a_reachable_site_with_contact_details_is_ok(self):
        self.stub(B, "_robots_ok", lambda base: True)
        pages = {"http://acme.com": (
            "<html><head><title>Acme Concrete Co | Home</title></head>"
            "<body>Call (555) 123-4567. info@acme.com</body></html>")}
        self.stub(B, "_get",
                  lambda url, timeout=12, limit=250000: pages.get(url))
        r = B.probe(("MO", "Aurora", "http://acme.com"))
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["company"], "Acme Concrete Co")
        self.assertEqual(r["email"], "info@acme.com")
        self.assertEqual(r["phone"], "(555) 123-4567")

    def test_a_site_with_a_name_but_no_email_is_no_email_not_ok(self):
        self.stub(B, "_robots_ok", lambda base: True)
        pages = {"http://acme.com": (
            "<html><head><title>Acme Concrete Co</title></head>"
            "<body>no contact info published</body></html>")}
        self.stub(B, "_get",
                  lambda url, timeout=12, limit=250000: pages.get(url))
        r = B.probe(("MO", "Aurora", "http://acme.com"))
        self.assertEqual(r["status"], "no_email")

    def test_a_site_that_never_answers_is_unreachable(self):
        self.stub(B, "_robots_ok", lambda base: True)
        self.stub(B, "_get", lambda url, timeout=12, limit=250000: None)
        r = B.probe(("MO", "Aurora", "http://acme.com"))
        self.assertEqual(r["status"], "unreachable")
        self.assertEqual(r["company"], "")


if __name__ == "__main__":
    unittest.main()
