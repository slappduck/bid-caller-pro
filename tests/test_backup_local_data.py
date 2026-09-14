"""Copying the two files that exist on exactly one disk.

Everything else in this project lives in at least three places: the working
copy, GitHub, and whatever Render and Cloudflare are serving. data/
outreach_prospects.csv and data/sender.env live in one, deliberately -- one
is third-party contact data, the other holds a home postal address, and the
repository is public.

Losing the prospect list would not just lose the leads. It would lose the
opt-outs, and those are the part that cannot be rebuilt by searching again:
every row marked unsubscribed is somebody who asked once and would have to be
emailed a second time to discover they had asked.

Which makes verification the interesting behaviour rather than copying. A
backup nobody checked is a backup nobody has, so a copy that does not match
its source is deleted rather than left looking like protection. And pruning
gets its own tests, because a cleanup that deletes one file too many in a
folder full of irreplaceable ones is worse than never pruning at all.
"""
import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import backup_local_data as B  # noqa: E402


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.dest = os.path.join(tempfile.mkdtemp(), "nested", "backup")
        self._data = B.DATA
        B.DATA = self.data

    def tearDown(self):
        B.DATA = self._data

    def write(self, name, text="secret\n"):
        with open(os.path.join(self.data, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_both_precious_files_are_copied(self):
        for name in B.PRECIOUS:
            self.write(name)
        copied, missing = B.backup(self.dest, today="2026-09-14")
        self.assertEqual(len(copied), len(B.PRECIOUS))
        self.assertEqual(missing, [])

    def test_the_copy_is_dated_so_history_accumulates(self):
        self.write(B.PRECIOUS[0])
        copied, _ = B.backup(self.dest, today="2026-09-14")
        self.assertTrue(os.path.basename(copied[0]).startswith("2026-09-14__"))

    def test_the_destination_is_created_if_absent(self):
        self.write(B.PRECIOUS[0])
        B.backup(self.dest, today="2026-09-14")
        self.assertTrue(os.path.isdir(self.dest))

    def test_the_contents_actually_arrive(self):
        self.write(B.PRECIOUS[0], "slug,email\nacme,a@x.com\n")
        copied, _ = B.backup(self.dest, today="2026-09-14")
        with open(copied[0], encoding="utf-8") as f:
            self.assertIn("a@x.com", f.read())

    def test_a_missing_file_is_reported_not_fatal(self):
        """sender.env is absent on a machine that has never sent outreach."""
        self.write(B.PRECIOUS[0])
        copied, missing = B.backup(self.dest, today="2026-09-14")
        self.assertEqual(len(copied), 1)
        self.assertEqual(missing, B.PRECIOUS[1:])

    def test_a_copy_that_does_not_verify_is_not_left_behind(self):
        """A backup nobody checked is a backup nobody has."""
        self.write(B.PRECIOUS[0])
        original = B._digest
        B._digest = lambda p: os.path.basename(p)   # never matches
        self.addCleanup(lambda: setattr(B, "_digest", original))
        with self.assertRaises(IOError):
            B.backup(self.dest, today="2026-09-14")
        self.assertEqual(
            [n for n in os.listdir(self.dest) if n.startswith("2026")], [])

    def test_two_runs_on_different_days_both_survive(self):
        self.write(B.PRECIOUS[0])
        B.backup(self.dest, today="2026-09-13")
        B.backup(self.dest, today="2026-09-14")
        self.assertEqual(len(os.listdir(self.dest)), 2)


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp()

    def touch(self, name):
        open(os.path.join(self.dest, name), "w").close()

    def test_old_copies_go(self):
        self.touch("2026-01-01__outreach_prospects.csv")
        removed = B.prune(self.dest, 30, today=datetime.date(2026, 9, 14))
        self.assertEqual(len(removed), 1)

    def test_recent_copies_stay(self):
        self.touch("2026-09-13__outreach_prospects.csv")
        B.prune(self.dest, 30, today=datetime.date(2026, 9, 14))
        self.assertEqual(len(os.listdir(self.dest)), 1)

    def test_the_boundary_keeps_the_last_day(self):
        self.touch("2026-08-15__x.csv")   # exactly 30 days
        B.prune(self.dest, 30, today=datetime.date(2026, 9, 14))
        self.assertEqual(len(os.listdir(self.dest)), 1)

    def test_unstamped_files_are_never_touched(self):
        """The folder may be a synced drive holding somebody else's things."""
        self.touch("tax-return.pdf")
        self.touch("notes.txt")
        B.prune(self.dest, 0, today=datetime.date(2026, 9, 14))
        self.assertEqual(sorted(os.listdir(self.dest)),
                         ["notes.txt", "tax-return.pdf"])

    def test_a_nonsense_date_is_left_alone_rather_than_guessed(self):
        self.touch("2026-13-45__x.csv")
        B.prune(self.dest, 0, today=datetime.date(2026, 9, 14))
        self.assertEqual(len(os.listdir(self.dest)), 1)


if __name__ == "__main__":
    unittest.main()
