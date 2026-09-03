"""Choosing which page IS a state's letting listing.

The crawl finds a dozen bid-shaped pages per state and has to pick one. It
used to pick on appearance -- dated, repeated, project-shaped rows -- and
appearance is exactly what a fuel price index has. South Dakota's carries 295
dated rows of diesel prices and outscored the real letting page; Nebraska's
"Policies and Forms" won its state the same way. Twelve states ended up
pointed at pages that could never produce a bid, and nothing downstream could
tell, because each of those pages genuinely is a list of dated rows.

So the decision is made by running the production parser and counting rows
that survive placement. These tests hold that line: a page only wins by
yielding work.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import discover_state_sources as dsc


def page(*links):
    """A menu page linking to each url with a bid-shaped label."""
    return "<html><body>" + "".join(
        '<a href="%s">%s</a>' % (u, label) for u, label in links
    ) + "</body></html>"


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

# South Dakota's actual trap: more dated rows than the letting page, all of
# them numbers. Scores well, yields nothing.
FUEL_INDEX = "<table>" + "".join(
    "<tr><td>8/%d/2026</td><td>422.47</td><td>344.16</td><td>76.85</td></tr>"
    % d for d in range(1, 29)) + "</table>"


class MeasuredYieldTests(unittest.TestCase):
    def test_real_letting_yields_rows(self):
        self.assertGreaterEqual(
            dsc.measured_yield("MO", "https://x.gov/let", LETTING), 3)

    def test_fuel_price_index_yields_nothing(self):
        self.assertEqual(
            dsc.measured_yield("SD", "https://x.gov/fuel", FUEL_INDEX), 0)

    def test_fuel_index_still_beats_the_letting_on_the_old_heuristic(self):
        """The reason yield had to take over -- not a hypothetical."""
        self.assertGreaterEqual(dsc.listing_score(FUEL_INDEX)[0],
                                dsc.listing_score(LETTING)[0])

    def test_a_malformed_page_scores_zero_rather_than_raising(self):
        self.assertEqual(dsc.measured_yield("MO", "https://x.gov", "<table"), 0)


class DiscoverPicksByYieldTests(unittest.TestCase):
    ROOT = "https://dot.example.gov/"

    def _crawl(self, pages):
        def fake_fetch(url, *a, **kw):
            if url in pages:
                return 200, pages[url]
            return "http_404", ""
        with patch.object(dsc.state_fetch, "fetch", side_effect=fake_fetch):
            # LETTING names Missouri counties; the decoy is South Dakota's
            # real one. The state only has to match the letting's counties.
            return dsc.discover("MO", self.ROOT, hops=1)

    def test_letting_wins_over_a_higher_scoring_decoy(self):
        got = self._crawl({
            self.ROOT: page(("/fuel", "Fuel price index bid letting"),
                            ("/letting", "Letting")),
            "https://dot.example.gov/fuel": FUEL_INDEX,
            "https://dot.example.gov/letting": LETTING,
        })
        self.assertEqual(got["url"], "https://dot.example.gov/letting")
        self.assertGreaterEqual(got["usable"], 3)

    def test_a_decoy_is_kept_for_review_but_never_called_ok(self):
        """The URL stays so a human can see what the crawl landed on. The
        status does not say "ok", and production gates on usable anyway."""
        got = self._crawl({
            self.ROOT: page(("/fuel", "Fuel price index bid letting")),
            "https://dot.example.gov/fuel": FUEL_INDEX,
        })
        self.assertEqual(got["usable"], 0)
        self.assertEqual(got["status"], "no_yield")
        self.assertNotEqual(got["status"], "ok")

    def test_a_state_with_nothing_bid_shaped_reports_no_listing(self):
        got = self._crawl({self.ROOT: "<html><body>Welcome</body></html>"})
        self.assertEqual(got["url"], "")
        self.assertEqual(got["status"], "no_listing")

    def test_an_unreachable_root_is_reported_not_guessed(self):
        with patch.object(dsc.state_fetch, "fetch",
                          side_effect=lambda u, *a, **k: ("blocked", "")):
            got = dsc.discover("KS", self.ROOT)
        self.assertEqual(got["url"], "")
        self.assertIn("blocked", got["note"])


class IndexPageTests(unittest.TestCase):
    """Alabama's listing URL changes every letting; the index is the source."""
    ROOT = "https://dot.example.gov/"
    INDEX = page(("/NTC/NTC_August_28_2026.html", "Letting August 28, 2026"))

    def test_an_index_over_a_letting_is_found_and_labelled(self):
        pages = {
            self.ROOT: page(("/ntc", "Notice to Contractors")),
            "https://dot.example.gov/ntc": self.INDEX,
            "https://dot.example.gov/NTC/NTC_August_28_2026.html": LETTING,
        }
        with patch.object(dsc.state_fetch, "fetch",
                          side_effect=lambda u, *a, **k:
                          (200, pages[u]) if u in pages else ("http_404", "")):
            got = dsc.discover("MO", self.ROOT, hops=1,
                               today=(2026, 8, 30))
        self.assertEqual(got["kind"], "index")
        self.assertEqual(got["url"], "https://dot.example.gov/ntc")
        self.assertGreaterEqual(got["usable"], 3)

    def test_a_plain_listing_is_not_labelled_an_index(self):
        pages = {self.ROOT: page(("/letting", "Letting")),
                 "https://dot.example.gov/letting": LETTING}
        with patch.object(dsc.state_fetch, "fetch",
                          side_effect=lambda u, *a, **k:
                          (200, pages[u]) if u in pages else ("http_404", "")):
            got = dsc.discover("MO", self.ROOT, hops=1)
        self.assertEqual(got["kind"], "listing")


class CsvColumnTests(unittest.TestCase):
    def test_kind_is_written_out(self):
        """It was not, so every run blanked the column -- and kind=index is
        the only thing keeping Alabama and Kentucky resolvable."""
        path = os.path.join(os.path.dirname(__file__), os.pardir,
                            "tools", "discover_state_sources.py")
        with open(path) as f:
            src = f.read()
        line = [l for l in src.splitlines() if l.strip().startswith("fields = [")]
        self.assertTrue(line, "fields list not found")
        self.assertIn('"kind"', line[0] + src.split("fields = [")[1][:120])


if __name__ == "__main__":
    unittest.main()
