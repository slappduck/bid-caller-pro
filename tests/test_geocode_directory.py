"""Joining directory towns to a Wikidata coordinate.

/scan works outward from a point, so a "found" bid-portal row whose town has
no entry in bid_portal_coords.csv is not merely unranked -- it is invisible,
and no radius will ever return it. That is exactly what happened to the
municipal crawl: 1,796 verified pages added, 1,574 of them for towns the
coordinate file had never heard of.

Because this is a join against Wikidata's P625 rather than a real geocoder,
the one thing that can actually go wrong is matching the wrong entity -- a
QID resolved to a coordinate on the other side of the planet. The bounding
box check exists for exactly that, and it is the main thing pinned down here,
along with the SPARQL-then-REST fallback (SPARQL has been the flaky one) and
the "already have it" skip that keeps a rerun from re-resolving every town
every time.

Both wikidata services are stubbed -- these tests never reach query.wikidata
.org or Special:EntityData.
"""
import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import geocode_directory as G  # noqa: E402


class ParsePointTests(unittest.TestCase):
    def test_longitude_comes_first_in_wkt_but_lat_lon_comes_out(self):
        self.assertEqual(G._parse_point("Point(-86.1581 39.7684)"),
                         (39.7684, -86.1581))

    def test_a_malformed_value_is_none_not_a_crash(self):
        self.assertIsNone(G._parse_point("not a point"))
        self.assertIsNone(G._parse_point(""))


class _MainFixture(unittest.TestCase):
    """Wires DIRECTORY/COORDS/CANDIDATES to a scratch directory so main()
    never touches the real data files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "data"), exist_ok=True)
        self._orig = dict(DIRECTORY=G.DIRECTORY, COORDS=G.COORDS,
                          _ROOT=G._ROOT, _sparql=G._sparql,
                          _entity_point=G._entity_point)
        G._ROOT = self.tmp
        G.DIRECTORY = os.path.join(self.tmp, "data", "directory.csv")
        G.COORDS = os.path.join(self.tmp, "data", "coords.csv")
        self.addCleanup(self._restore)

    def _restore(self):
        G.DIRECTORY = self._orig["DIRECTORY"]
        G.COORDS = self._orig["COORDS"]
        G._ROOT = self._orig["_ROOT"]
        G._sparql = self._orig["_sparql"]
        G._entity_point = self._orig["_entity_point"]

    def _write_directory(self, rows):
        with open(G.DIRECTORY, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["status", "city", "state", "domain"])
            w.writeheader()
            w.writerows(rows)

    def _write_coords(self, rows=()):
        with open(G.COORDS, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["city", "state", "lat", "lon"])
            w.writerows(rows)

    def _write_candidates(self, name, rows):
        path = os.path.join(self.tmp, "data", name)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["qid", "domain"])
            w.writeheader()
            w.writerows(rows)

    def _read_coords(self):
        with open(G.COORDS, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def _run(self, *extra):
        old = sys.argv
        sys.argv = ["geocode_directory.py"] + list(extra)
        try:
            G.main()
        finally:
            sys.argv = old


class MainTests(_MainFixture):
    def test_a_town_already_in_the_coordinate_file_is_never_re_resolved(self):
        self._write_directory([{"status": "found", "city": "Boone",
                                "state": "MO", "domain": "boone.mo.gov"}])
        self._write_coords([["Boone", "MO", "38.9", "-92.3"]])
        self._write_candidates("municipal_candidates.csv",
                               [{"qid": "Q1", "domain": "boone.mo.gov"}])

        def boom(query, tries=2):
            raise AssertionError("SPARQL should never be called")
        G._sparql = boom
        self._run()
        rows = self._read_coords()
        self.assertEqual(len(rows), 1)  # unchanged, not duplicated

    def test_a_domain_with_no_qid_stays_unresolved(self):
        """Only rows whose candidate file recorded a QID can be joined at
        all -- everything else has no lever to pull."""
        self._write_directory([{"status": "found", "city": "Neosho",
                                "state": "MO", "domain": "neosho.mo.gov"}])
        self._write_coords()
        # No candidates file at all: qid_of stays empty.
        G._sparql = lambda query, tries=2: (_ for _ in ()).throw(
            AssertionError("no qid means nothing to look up"))
        self._run()
        self.assertEqual(self._read_coords(), [])

    def test_sparql_resolves_a_good_point_and_it_is_appended(self):
        self._write_directory([{"status": "found", "city": "Boone",
                                "state": "MO", "domain": "boone.mo.gov"}])
        self._write_coords()
        self._write_candidates("municipal_candidates.csv",
                               [{"qid": "Q1", "domain": "boone.mo.gov"}])
        G._sparql = lambda query, tries=2: [
            {"x": {"value": "http://www.wikidata.org/entity/Q1"},
             "coord": {"value": "Point(-92.3 38.9)"}}]
        self._run()
        rows = self._read_coords()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["city"], "Boone")
        self.assertAlmostEqual(float(rows[0]["lat"]), 38.9)
        self.assertAlmostEqual(float(rows[0]["lon"]), -92.3)

    def test_a_coordinate_outside_the_continental_us_is_dropped(self):
        """A QID resolved to the wrong entity is worse than no coordinate at
        all -- it would place a bid somewhere the customer never looks."""
        self._write_directory([{"status": "found", "city": "Boone",
                                "state": "MO", "domain": "boone.mo.gov"}])
        self._write_coords()
        self._write_candidates("municipal_candidates.csv",
                               [{"qid": "Q1", "domain": "boone.mo.gov"}])
        # London, not Boone -- the wrong-entity trap this check exists for.
        G._sparql = lambda query, tries=2: [
            {"x": {"value": "http://www.wikidata.org/entity/Q1"},
             "coord": {"value": "Point(-0.1276 51.5072)"}}]
        self._run()
        self.assertEqual(self._read_coords(), [])

    def test_a_sparql_failure_is_covered_by_the_rest_fallback(self):
        self._write_directory([{"status": "found", "city": "Boone",
                                "state": "MO", "domain": "boone.mo.gov"}])
        self._write_coords()
        self._write_candidates("municipal_candidates.csv",
                               [{"qid": "Q1", "domain": "boone.mo.gov"}])

        def boom(query, tries=2):
            raise TimeoutError("wdqs is down")
        G._sparql = boom
        G._entity_point = lambda qid: (38.9, -92.3)
        self._run()
        rows = self._read_coords()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["city"], "Boone")

    def test_a_town_that_resolves_nowhere_is_left_out_and_reported(self):
        self._write_directory([{"status": "found", "city": "Boone",
                                "state": "MO", "domain": "boone.mo.gov"}])
        self._write_coords()
        self._write_candidates("municipal_candidates.csv",
                               [{"qid": "Q1", "domain": "boone.mo.gov"}])
        G._sparql = lambda query, tries=2: []
        G._entity_point = lambda qid: None
        self._run()
        self.assertEqual(self._read_coords(), [])

    def test_dry_run_touches_nothing(self):
        self._write_directory([{"status": "found", "city": "Boone",
                                "state": "MO", "domain": "boone.mo.gov"}])
        self._write_coords()
        self._write_candidates("municipal_candidates.csv",
                               [{"qid": "Q1", "domain": "boone.mo.gov"}])

        def boom(*a, **kw):
            raise AssertionError("dry-run must not touch the network")
        G._sparql = boom
        G._entity_point = boom
        self._run("--dry-run")
        self.assertEqual(self._read_coords(), [])

    def test_only_found_rows_are_ever_considered(self):
        """A directory row that failed probing has no business getting a
        coordinate -- it will never be shown to anyone."""
        self._write_directory([{"status": "no_bid_page", "city": "Neosho",
                                "state": "MO", "domain": "neosho.mo.gov"}])
        self._write_coords()
        self._write_candidates("municipal_candidates.csv",
                               [{"qid": "Q9", "domain": "neosho.mo.gov"}])

        def boom(*a, **kw):
            raise AssertionError("no found rows means nothing to resolve")
        G._sparql = boom
        self._run()
        self.assertEqual(self._read_coords(), [])


if __name__ == "__main__":
    unittest.main()
