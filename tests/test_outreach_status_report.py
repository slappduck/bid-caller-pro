"""These counts are the one safe way to look at the prospect list on a
screen that isn't only yours -- so the thing worth pinning is that the
report really does stay aggregate: no email, no company, no intro text,
just how many and which status.
"""
import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import outreach_status_report as R  # noqa: E402

FIELDS = ["slug", "company", "greeting", "email", "city", "state", "intro",
          "status", "sent_date"]


def row(slug, status="ready", intro="x"):
    return {"slug": slug, "company": slug.title(), "greeting": "team",
            "email": f"{slug}@example.com", "city": "Columbia", "state": "MO",
            "intro": intro, "status": status, "sent_date": ""}


def write_csv(rows):
    path = os.path.join(tempfile.mkdtemp(), "prospects.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return path


class ReportTests(unittest.TestCase):
    def test_total_matches_row_count(self):
        path = write_csv([row("a"), row("b"), row("c")])
        total, _, _ = R.report(path)
        self.assertEqual(total, 3)

    def test_counts_are_grouped_by_status(self):
        path = write_csv([row("a", status="ready"),
                           row("b", status="sent"),
                           row("c", status="sent")])
        _, counts, _ = R.report(path)
        self.assertEqual(counts["ready"], 1)
        self.assertEqual(counts["sent"], 2)

    def test_ready_rows_missing_intro_are_counted(self):
        path = write_csv([row("a", status="ready", intro=""),
                           row("b", status="ready", intro="has one"),
                           row("c", status="sent", intro="")])
        _, _, ready_no_intro = R.report(path)
        # only "a" is both ready and blank -- "c" is blank but not ready
        self.assertEqual(ready_no_intro, 1)

    def test_report_never_touches_email_or_intro_text(self):
        """The whole point of a separate report: nothing PII-shaped leaks."""
        path = write_csv([row("a", intro="secret pitch line")])
        total, counts, ready_no_intro = R.report(path)
        for value in (total, counts, ready_no_intro):
            self.assertNotIn("secret pitch line", repr(value))
            self.assertNotIn("a@example.com", repr(value))


if __name__ == "__main__":
    unittest.main()
