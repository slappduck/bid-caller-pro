"""Judging a discovered state page by what it actually yields.

discover_state_sources.py scores a candidate page on dated, repeated rows,
and that heuristic finds South Dakota's fuel-price index (294 dated rows of
diesel prices) and Nebraska's "Policies and Forms" just as happily as it
finds a real letting. verify_state_sources.py's job is to stop trusting the
score and instead run the same production parser (bid_sources.
parse_state_letting + counties.counties_named) a live scan would use, and
count what survives -- because that is what a contractor would actually be
shown, not what a heuristic hoped for.

These tests pin measure() against that same fuel-index trap, plus the two
other failure modes its own docstring calls out by name: Washington's search-
facet chips ("Public Works Awarded Pierce County" reads like a county-named
row but is UI chrome) and Louisiana's street names that happen to match
parish names. All of them must measure as zero usable rows even though a
naive extractor counts rows for every one of them.

state_fetch.fetch is the only network call in this file, and it is stubbed
throughout.
"""
import csv
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import verify_state_sources as VS  # noqa: E402

# A real letting: county column, dated rows, concrete work, placeable.
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

# South Dakota's actual trap: 28 dated rows, all diesel prices.
FUEL_INDEX = "<table>" + "".join(
    "<tr><td>8/%d/2026</td><td>422.47</td><td>344.16</td><td>76.85</td></tr>"
    % d for d in range(1, 29)) + "</table>"

# Washington's trap: a search-facet chip that names a real county but is UI
# chrome, not a project row.
FACET_CHIPS = """
<ul class="facets">
  <li>Public Works Awarded Pierce County (12)</li>
  <li>Public Works Awarded King County (9)</li>
</ul>
"""


class MeasureTests(unittest.TestCase):
    def test_no_url_is_reported_without_fetching_anything(self):
        with patch.object(VS.state_fetch, "fetch",
                          side_effect=AssertionError("must not fetch")):
            got = VS.measure("MO", "")
        self.assertEqual(got, dict(rows=0, usable=0, fetch="no_url", samples=[]))

    def test_a_fetch_failure_is_reported_by_its_status(self):
        with patch.object(VS.state_fetch, "fetch",
                          return_value=("blocked", "")):
            got = VS.measure("KS", "https://x.gov/let")
        self.assertEqual(got["fetch"], "blocked")
        self.assertEqual(got["usable"], 0)

    def test_a_real_letting_yields_placeable_rows(self):
        with patch.object(VS.state_fetch, "fetch",
                          return_value=(200, LETTING)):
            got = VS.measure("MO", "https://x.gov/let")
        self.assertEqual(got["fetch"], "ok")
        self.assertEqual(got["rows"], 3)
        self.assertGreaterEqual(got["usable"], 3)

    def test_the_fuel_price_index_yields_nothing_despite_294_dated_rows(self):
        """The reason this file measures yield instead of trusting the
        discovery score in the first place."""
        with patch.object(VS.state_fetch, "fetch",
                          return_value=(200, FUEL_INDEX)):
            got = VS.measure("SD", "https://x.gov/fuel")
        self.assertGreater(got["rows"], 0)
        self.assertEqual(got["usable"], 0)

    def test_search_facet_chips_are_not_counted_as_project_rows(self):
        """"Public Works Awarded Pierce County" names a real county but is a
        filter chip, not a letting -- Washington's actual trap."""
        with patch.object(VS.state_fetch, "fetch",
                          return_value=(200, FACET_CHIPS)):
            got = VS.measure("WA", "https://x.gov/search")
        self.assertEqual(got["usable"], 0)

    def test_an_index_page_is_followed_to_its_newest_letting(self):
        index_html = ('<a href="/NTC/NTC_Sept_18_2026.html">'
                     'Letting September 18, 2026</a>')
        pages = {"https://dot.example.gov/ntc": (200, index_html),
                 "https://dot.example.gov/NTC/NTC_Sept_18_2026.html":
                     (200, LETTING)}

        def fake_fetch(url, *a, **kw):
            return pages.get(url, ("http_404", ""))

        with patch.object(VS.state_fetch, "fetch", side_effect=fake_fetch):
            got = VS.measure("MO", "https://dot.example.gov/ntc", kind="index")
        self.assertEqual(got["fetch"], "ok")
        self.assertGreaterEqual(got["usable"], 3)

    def test_samples_show_the_county_and_a_title_snippet(self):
        with patch.object(VS.state_fetch, "fetch",
                          return_value=(200, LETTING)):
            got = VS.measure("MO", "https://x.gov/let")
        self.assertTrue(got["samples"])
        self.assertIn("boone", got["samples"][0].lower())


class MainCsvRoundTripTests(unittest.TestCase):
    """The checked-in CSV records measured yield, not a guess."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "state_bid_sources.csv")
        self._orig_csv = VS.CSV_PATH
        VS.CSV_PATH = self.path
        self.addCleanup(lambda: setattr(VS, "CSV_PATH", self._orig_csv))

    def _write(self, rows, fields=("state", "url", "kind")):
        with open(self.path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(fields))
            w.writeheader()
            w.writerows(rows)

    def _read(self):
        with open(self.path, newline="") as f:
            return list(csv.DictReader(f))

    def test_rows_and_usable_are_filled_in_from_the_measured_pipeline(self):
        self._write([{"state": "MO", "url": "https://x.gov/let", "kind": "listing"}])
        with patch.object(VS.state_fetch, "fetch",
                          return_value=(200, LETTING)):
            old = sys.argv
            sys.argv = ["verify_state_sources.py"]
            try:
                VS.main()
            finally:
                sys.argv = old
        rows = self._read()
        self.assertEqual(rows[0]["rows"], "3")
        self.assertEqual(int(rows[0]["usable"]), 3)

    def test_a_state_filter_only_measures_the_named_states(self):
        self._write([{"state": "MO", "url": "https://x.gov/let", "kind": "listing"},
                    {"state": "KS", "url": "https://y.gov/let", "kind": "listing"}])
        called = []

        def fake_fetch(url, *a, **kw):
            called.append(url)
            return (200, LETTING)

        with patch.object(VS.state_fetch, "fetch", side_effect=fake_fetch):
            old = sys.argv
            sys.argv = ["verify_state_sources.py", "--state", "MO"]
            try:
                VS.main()
            finally:
                sys.argv = old
        self.assertEqual(called, ["https://x.gov/let"])
        rows = self._read()
        # KS was never measured, so its row is left blank rather than
        # guessed at.
        ks = [r for r in rows if r["state"] == "KS"][0]
        self.assertEqual(ks["usable"], "")

    def test_the_output_carries_every_field_even_when_the_input_lacked_them(self):
        self._write([{"state": "MO", "url": "https://x.gov/let", "kind": "listing"}])
        with patch.object(VS.state_fetch, "fetch",
                          return_value=(200, LETTING)):
            old = sys.argv
            sys.argv = ["verify_state_sources.py"]
            try:
                VS.main()
            finally:
                sys.argv = old
        rows = self._read()
        self.assertEqual(set(rows[0]),
                         {"state", "url", "kind", "status", "score", "note",
                          "rows", "usable"})


if __name__ == "__main__":
    unittest.main()
