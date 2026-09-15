"""Running the real scan path over a fixed set of locations and reporting
the funnel a customer would actually see -- not the audit, which only
samples portals; this drives portal discovery, parsing, placement and the
radius check the way /scan itself does.

Two things the module's own comments call out as deliberate, and worth
pinning:

- The location set "deliberately spans metro and rural: the difference
  between them is the product's most important property and any average
  that hides it is a lie" -- so run_location() is tested per-location, never
  only on an aggregate.
- The per-town scan is concurrent "at production's own width" specifically
  because a serial loop would silently measure a smaller scan than
  production runs; one town raising must never sink the whole run, since a
  single bad portal page taking down a benchmark would hide the very
  funnel numbers this tool exists to report.

license_server._run_known_portals and bid_portals are stubbed throughout;
nothing here reaches a live bid portal.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import engine_benchmark as E  # noqa: E402
import bid_portals  # noqa: E402
import license_server as ls  # noqa: E402


class _Stubbed(unittest.TestCase):
    def stub(self, obj, name, value):
        original = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(lambda: setattr(obj, name, original))


class RunLocationTests(_Stubbed):
    def setUp(self):
        self.stub(bid_portals, "load_directory", lambda: {})
        self.stub(bid_portals, "towns_within_radius",
                  lambda pdb, lat, lon, radius: [("Nearby", "MO", 37.0, -93.7)])

    def test_placed_counts_everything_shown_counts_only_open(self):
        def fake_run(city, state, ai_label, grouped, center, radius, cdb,
                     coords, lock, pdb, default_city=None, town_coords=None,
                     stats=None):
            if city == "Aurora":
                grouped[city] = [{"title": "Aurora job", "miles": 0,
                                  "status": "open", "deadline": "9/20",
                                  "email": "a@x.com"}]
            elif city == "Nearby":
                grouped[city] = [{"title": "Nearby job", "miles": 5,
                                  "status": "Awarded"}]
            stats["town_ok"] = stats.get("town_ok", 0) + 1
        self.stub(ls, "_run_known_portals", fake_run)

        r = E.run_location("Aurora", "MO", 36.97, -93.72, 50, max_towns=5)
        self.assertEqual(r["towns_read"], 2)  # center + the one known town
        self.assertEqual(r["placed"], 2)
        self.assertEqual(r["shown"], 1)
        self.assertEqual(r["with_deadline"], 1)
        self.assertEqual(r["with_contact"], 1)
        self.assertEqual(r["funnel"], {"town_ok": 2})
        self.assertEqual(r["titles"], ["Aurora job"])

    def test_one_town_raising_does_not_sink_the_run(self):
        def raising(city, state, ai_label, grouped, center, radius, cdb,
                    coords, lock, pdb, default_city=None, town_coords=None,
                    stats=None):
            raise ValueError("boom")
        self.stub(ls, "_run_known_portals", raising)

        r = E.run_location("Aurora", "MO", 36.97, -93.72, 50, max_towns=5)
        self.assertEqual(r["placed"], 0)
        self.assertEqual(r["shown"], 0)
        self.assertEqual(r["funnel"]["town_error"], 2)

    def test_a_location_with_nothing_placed_reports_zero_not_a_divide_error(self):
        self.stub(ls, "_run_known_portals", lambda *a, **k: None)
        r = E.run_location("Aurora", "MO", 36.97, -93.72, 50, max_towns=5)
        self.assertEqual(r["placed"], 0)
        self.assertEqual(r["with_deadline"], 0)


class MainLocationFilterTests(_Stubbed):
    """--only narrows LOCATIONS by a case-insensitive city substring;
    --radius overrides every picked location's own radius."""

    def test_only_filters_by_substring(self):
        picks = [l for l in E.LOCATIONS if "aurora".lower() in l[0].lower()]
        self.assertEqual([p[0] for p in picks], ["Aurora"])

    def test_radius_override_replaces_every_picked_locations_radius(self):
        picks = [l for l in E.LOCATIONS if "kansas city".lower() in l[0].lower()]
        overridden = [(c, s, la, lo, 75) for c, s, la, lo, _r in picks]
        self.assertTrue(all(p[4] == 75 for p in overridden))

    def test_the_location_set_spans_more_than_one_state(self):
        """Metro and rural, several states -- an average across only one
        state would hide the exact gap the module exists to measure."""
        states = {l[1] for l in E.LOCATIONS}
        self.assertGreater(len(states), 1)


if __name__ == "__main__":
    unittest.main()
