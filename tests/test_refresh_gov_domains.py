"""Rebuilding data/gov_domains.csv from CISA's national .gov registry.

Two things worth pinning, both stated directly in the module's own
comments:

- Only local government is kept -- City, County, Special district, School
  district -- because "Federal, state, tribal and election domains don't
  let municipal concrete work." A federal or state row in the output would
  point the scanner at an agency that structurally cannot issue the kind of
  bid this product tracks.
- A truncated download must never silently shrink national coverage: the
  file refuses to write when fewer than 5,000 rows survive the filter,
  since a network hiccup or a schema change upstream should fail loudly
  rather than quietly erase most of the country's coverage.

urllib.request.urlopen is stubbed with an in-memory CSV throughout --
nothing here fetches the real registry.
"""
import csv
import os
import sys
import tempfile
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import refresh_gov_domains as G  # noqa: E402


class _FakeResponse:
    def __init__(self, text):
        self._text = text.encode("utf-8")

    def read(self):
        return self._text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Stubbed(unittest.TestCase):
    def stub(self, obj, name, value):
        original = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(lambda: setattr(obj, name, original))

    def serve(self, csv_text):
        self.stub(urllib.request, "urlopen",
                  lambda url, timeout=120: _FakeResponse(csv_text))

    def make_rows(self, n, domain_type="City", state="MO"):
        header = "Domain name,Domain type,Organization name,City,State\n"
        body = "".join(
            f"town{i}.gov,{domain_type},City of {i},Town{i},{state}\n"
            for i in range(n))
        return header + body


class RefreshTests(_Stubbed):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.stub(G, "OUT", os.path.join(self.dir, "gov_domains.csv"))

    def read_out(self):
        with open(G.OUT, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_only_local_government_types_survive(self):
        text = self.make_rows(5000) + "fed.gov,Federal,US Gov,Washington,DC\n"
        self.serve(text)
        G.main()
        rows = self.read_out()
        self.assertEqual(len(rows), 5000)
        self.assertFalse(any(r["type"] == "Federal" for r in rows))

    def test_a_row_missing_domain_or_state_is_dropped(self):
        text = self.make_rows(5000)
        text += ",City,No Domain,Nowhere,MO\n"       # blank domain
        text += "noname.gov,City,No State,Nowhere,\n"  # blank state
        self.serve(text)
        G.main()
        rows = self.read_out()
        self.assertEqual(len(rows), 5000)

    def test_a_truncated_download_refuses_to_write(self):
        """The exact guard the module's comment names: fewer than 5000 rows
        must fail loudly, not silently shrink national coverage."""
        self.serve(self.make_rows(4))
        with self.assertRaises(SystemExit):
            G.main()
        self.assertFalse(os.path.exists(G.OUT))

    def test_rows_are_sorted_by_state_then_city_then_domain(self):
        text = ("Domain name,Domain type,Organization name,City,State\n"
                "z.gov,City,Z Town,Zeta,MO\n"
                "a.gov,City,A Town,Alpha,KS\n")
        text += self.make_rows(4998, state="MO")
        self.serve(text)
        G.main()
        rows = self.read_out()
        states = [r["state"] for r in rows]
        self.assertEqual(states, sorted(states))
        self.assertEqual(rows[0]["state"], "KS")

    def test_state_is_upper_cased_and_stray_whitespace_is_trimmed(self):
        text = ("Domain name,Domain type,Organization name,City,State\n"
                " town.gov ,City, City of Town , Town , mo \n")
        text += self.make_rows(4999)
        self.serve(text)
        G.main()
        rows = self.read_out()
        row = [r for r in rows if r["domain"] == "town.gov"][0]
        self.assertEqual(row["state"], "MO")
        self.assertEqual(row["city"], "Town")


if __name__ == "__main__":
    unittest.main()
