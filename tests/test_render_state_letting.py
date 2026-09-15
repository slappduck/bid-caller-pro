"""Rendering a state letting page with a real browser, then handing the HTML
to the SAME parser the live scan uses.

The one rule this file guards is right there in the module docstring:
nothing about relevance or county placement gets re-implemented or relaxed
for a rendered page. bid_sources.parse_state_letting and counties.
counties_named are called unmodified -- a row still has to pass
looks_relevant() and still has to name a real county. So the tests below
run measure() against the identical LETTING fixture test_state_discovery.py
uses for the live-fetch path, and check it scores exactly the same way.

The browser itself (playwright) is never touched: measure() calls this
module's own render() function, which is stubbed out here to return canned
HTML or raise, so no test needs Chromium and none makes a live navigation.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import render_state_letting as R  # noqa: E402
import state_fetch  # noqa: E402

# Same fixture as test_state_discovery.py: county column, dated rows,
# concrete work -- a real letting page the production parser can place.
LETTING = """
<table>
  <tr><th>Call</th><th>County</th><th>Letting</th><th>Description</th></tr>
  <tr><td>A01</td><td>Boone</td><td>9/18/2026</td>
      <td>Sidewalk, curb ramp and ADA detectable warning replacement
          on Route 63 through Columbia</td></tr>
  <tr><td>A02</td><td>Greene</td><td>9/18/2026</td>
      <td>Concrete pavement repair and joint sealing, Route 13 from
          Kansas Expressway to Glenstone</td></tr>
  <tr><td>A03</td><td>Jasper</td><td>9/18/2026</td>
      <td>Curb and gutter replacement with ADA ramps at twelve
          intersections in Joplin</td></tr>
</table>
"""


class _Stubbed(unittest.TestCase):
    def stub(self, obj, name, value):
        original = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(lambda: setattr(obj, name, original))


class MeasureTests(_Stubbed):
    def setUp(self):
        self.stub(state_fetch, "robots_allows", lambda url: True)

    def test_a_real_letting_page_yields_placeable_rows(self):
        self.stub(R, "render", lambda page, url: LETTING)
        r = R.measure(None, "MO", "https://x.gov/let")
        self.assertEqual(r["fetch"], "ok")
        self.assertEqual(r["rows"], 3)
        self.assertEqual(r["usable"], 3)
        self.assertEqual(len(r["samples"]), 3)
        self.assertIn("boone", r["samples"][0])

    def test_no_url_is_reported_without_touching_the_browser(self):
        self.stub(R, "render", lambda page, url: (_ for _ in ()).throw(
            AssertionError("render() should never be called with no url")))
        r = R.measure(None, "MO", "")
        self.assertEqual(r, dict(fetch="no_url", rows=0, usable=0, samples=[]))

    def test_a_robots_disallowed_state_is_never_rendered(self):
        self.stub(state_fetch, "robots_allows", lambda url: False)
        self.stub(R, "render", lambda page, url: (_ for _ in ()).throw(
            AssertionError("render() should never run past a robots block")))
        r = R.measure(None, "MO", "https://x.gov/let")
        self.assertEqual(r["fetch"], "robots_disallow")

    def test_a_page_that_never_finishes_loading_is_reported_not_raised(self):
        def boom(page, url):
            raise TimeoutError("navigation timeout")
        self.stub(R, "render", boom)
        r = R.measure(None, "MO", "https://x.gov/let")
        self.assertEqual(r["fetch"], "render_TimeoutError")
        self.assertEqual(r["usable"], 0)

    def test_empty_html_is_reported_as_empty_not_zero_rows(self):
        self.stub(R, "render", lambda page, url: "")
        r = R.measure(None, "MO", "https://x.gov/let")
        self.assertEqual(r["fetch"], "empty")

    def test_a_page_still_shaped_like_the_south_dakota_fuel_trap_scores_zero(self):
        """Same decoy discover_state_sources.py guards against: a dated,
        repeated, numeric table that is not a letting."""
        fuel = "<table>" + "".join(
            "<tr><td>8/%d/2026</td><td>422.47</td><td>344.16</td>"
            "<td>76.85</td></tr>" % d for d in range(1, 15)) + "</table>"
        self.stub(R, "render", lambda page, url: fuel)
        r = R.measure(None, "SD", "https://x.gov/fuel")
        self.assertEqual(r["fetch"], "ok")
        self.assertEqual(r["usable"], 0)


class ExecutableLookupTests(_Stubbed):
    def test_the_first_existing_hint_is_used(self):
        self.stub(os.path, "exists", lambda p: p == R.CHROME_HINTS[1])
        self.assertEqual(R._executable(), R.CHROME_HINTS[1])

    def test_no_existing_hint_falls_back_to_none(self):
        """None tells Playwright to use its own managed download rather
        than a path that does not exist."""
        self.stub(os.path, "exists", lambda p: False)
        self.assertIsNone(R._executable())


class LoadSourcesTests(unittest.TestCase):
    """_load_sources() is a small helper around the same CSV main() reads
    directly -- not currently called by main() itself, but it should still
    read the file correctly if something calls it."""

    def test_it_reads_the_configured_csv(self):
        import csv
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "sources.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["state", "url", "usable"])
            w.writeheader()
            w.writerow({"state": "MO", "url": "https://x.gov", "usable": "3"})
        original = R.SOURCES_CSV
        R.SOURCES_CSV = path
        try:
            rows, fh = R._load_sources()
            fh.close()
        finally:
            R.SOURCES_CSV = original
        self.assertEqual(rows, [{"state": "MO", "url": "https://x.gov",
                                 "usable": "3"}])


class MainWithoutPlaywrightTests(unittest.TestCase):
    """No test here drives a real browser. When playwright truly is not
    importable, main() must say so and exit cleanly rather than crash."""

    def test_a_missing_playwright_install_exits_cleanly(self):
        original = sys.modules.get("playwright", "unset")
        sys.modules["playwright"] = None  # forces ImportError on the import
        self.addCleanup(lambda: (sys.modules.pop("playwright", None)
                                  if original == "unset"
                                  else sys.modules.__setitem__("playwright", original)))
        sys.argv = ["render_state_letting.py", "--state", "MO"]
        rc = R.main()
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
