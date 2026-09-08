"""Every version a consent record can cite must still be readable.

terms_acceptances stores which version an account accepted. That is only
evidence while the named document can be produced -- and editing terms.html or
privacy.html overwrites the only copy. The gap was already real, not
hypothetical: rows exist citing privacy version 2026-09-07, and the page had
been rewritten by the time anyone thought to ask what it said.

So each published version is snapshotted under legal/, and these tests fail if
a policy is edited without archiving it. That is the whole point: the archive
has to be impossible to forget, because the moment it is needed is years after
the moment it would have been easy.

The comparison ignores the archive banner and the "Previous versions" link,
which exist only in one copy or the other. Everything else must match, so an
archived file cannot quietly drift from what was actually published.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, os.pardir)
WEB = os.path.join(ROOT, "curbcall_netlify_v4")
ARCHIVE = os.path.join(WEB, "legal")

MONTHS = ("January February March April May June July August September "
          "October November December").split()


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def published_version(page):
    m = re.search(r"Last updated:\s*([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})",
                  read(WEB, page))
    assert m, page
    return "%s-%02d-%02d" % (m.group(3), MONTHS.index(m.group(1)) + 1,
                             int(m.group(2)))


def comparable(html):
    """The policy text, minus the bits that legitimately differ."""
    html = re.sub(r'<div class="archived-banner".*?</div>\s*', "", html, flags=re.S)
    # Normalise the subdirectory's relative links BEFORE stripping anything
    # matched by href: an archived copy carries href="../legal/index.html",
    # so stripping first silently missed it and reported a false mismatch.
    html = re.sub(r'href="\.\./([^"]+)"', r'href="\1"', html)
    html = re.sub(r'<div class="updated"><a href="legal/index\.html">'
                  r'Previous versions</a></div>\s*', "", html)
    return re.sub(r"\s+", " ", html).strip()


class CurrentVersionIsArchivedTests(unittest.TestCase):
    PAGES = {"terms.html": "terms", "privacy.html": "privacy"}

    def test_a_snapshot_exists_for_what_is_published_now(self):
        for page, stem in self.PAGES.items():
            v = published_version(page)
            path = os.path.join(ARCHIVE, "%s-%s.html" % (stem, v))
            self.assertTrue(os.path.exists(path),
                            "%s says %s and legal/%s-%s.html does not exist -- "
                            "archive it before shipping the edit"
                            % (page, v, stem, v))

    def test_the_snapshot_matches_what_is_published(self):
        """An archive that drifts from the live page is worse than none."""
        for page, stem in self.PAGES.items():
            v = published_version(page)
            snap = os.path.join(ARCHIVE, "%s-%s.html" % (stem, v))
            if not os.path.exists(snap):
                continue                      # the test above already says so
            with self.subTest(page=page):
                self.assertEqual(comparable(read(snap)),
                                 comparable(read(WEB, page)),
                                 "legal/%s-%s.html no longer matches %s"
                                 % (stem, v, page))


class ArchiveIsUsableTests(unittest.TestCase):
    def snapshots(self):
        return sorted(f for f in os.listdir(ARCHIVE)
                      if re.match(r"(terms|privacy)-\d{4}-\d{2}-\d{2}\.html$", f))

    def test_there_is_at_least_one_of_each(self):
        names = self.snapshots()
        self.assertTrue(any(n.startswith("terms-") for n in names))
        self.assertTrue(any(n.startswith("privacy-") for n in names))

    def test_each_snapshot_says_it_is_archived(self):
        """Served publicly, so an unlabelled copy reads as the current policy."""
        for name in self.snapshots():
            with self.subTest(name=name):
                self.assertIn("Archived version", read(ARCHIVE, name))

    def test_each_snapshots_filename_matches_the_date_inside_it(self):
        for name in self.snapshots():
            with self.subTest(name=name):
                stem, date = name[:-5].split("-", 1)
                m = re.search(r"Last updated:\s*([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})",
                              read(ARCHIVE, name))
                self.assertIsNotNone(m)
                inside = "%s-%02d-%02d" % (m.group(3), MONTHS.index(m.group(1)) + 1,
                                           int(m.group(2)))
                self.assertEqual(date, inside,
                                 "%s contains a policy dated %s" % (name, inside))

    def test_every_snapshot_is_listed_on_the_index(self):
        index = read(ARCHIVE, "index.html")
        for name in self.snapshots():
            with self.subTest(name=name):
                self.assertIn(name, index, name + " is not linked from the index")

    def test_the_index_links_nothing_that_is_missing(self):
        index = read(ARCHIVE, "index.html")
        for href in re.findall(r'href="((?:terms|privacy)-[^"]+\.html)"', index):
            with self.subTest(href=href):
                self.assertTrue(os.path.exists(os.path.join(ARCHIVE, href)),
                                "index links " + href + ", which does not exist")

    def test_the_live_policies_point_at_the_archive(self):
        """An archive nobody can find does not answer the question."""
        for page in ("terms.html", "privacy.html"):
            with self.subTest(page=page):
                self.assertIn("legal/index.html", read(WEB, page))


class VersionsTheServerCanStampTests(unittest.TestCase):
    """The constants written into every consent row must name real documents."""

    def constant(self, name):
        m = re.search(name + r'\s*=\s*os\.environ\.get\(\s*"' + name +
                      r'"\s*,\s*"([\d-]+)"\s*\)', read(ROOT, "license_server.py"))
        self.assertIsNotNone(m, name + " is not a dated constant any more")
        return m.group(1)

    def test_the_terms_version_it_stamps_is_archived(self):
        v = self.constant("TERMS_VERSION")
        self.assertTrue(os.path.exists(os.path.join(ARCHIVE, "terms-%s.html" % v)),
                        "consent rows cite terms %s with nothing to show" % v)

    def test_the_privacy_version_it_stamps_is_archived(self):
        v = self.constant("PRIVACY_VERSION")
        self.assertTrue(os.path.exists(os.path.join(ARCHIVE, "privacy-%s.html" % v)),
                        "consent rows cite privacy %s with nothing to show" % v)


if __name__ == "__main__":
    unittest.main()
