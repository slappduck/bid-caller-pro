#!/usr/bin/env python3
"""Counts only, never contents.

"How's the list doing" kept getting answered by opening
data/outreach_prospects.csv directly, which puts a screen full of third-party
email addresses and a home postal address' worth of context in front of
whoever's looking -- exactly the exposure OUTREACH.md exists to avoid. This
prints only aggregate counts: how many rows, how many per status, and how
many ready rows are missing the one field outreach_draft.py refuses to send
without.
"""
import collections
import csv
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROSPECTS = os.path.join(_ROOT, "data", "outreach_prospects.csv")


def report(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    counts = collections.Counter(r.get("status", "") for r in rows)
    ready_no_intro = sum(
        1 for r in rows if r.get("status") == "ready" and not r.get("intro"))
    return len(rows), counts, ready_no_intro


def main():
    if not os.path.exists(PROSPECTS):
        print(f"no prospect list at {PROSPECTS}\n"
              "It is deliberately not in the repo -- business contact data, "
              "public repo.", file=sys.stderr)
        return 2
    total, counts, ready_no_intro = report(PROSPECTS)
    print(f"{total} row(s)")
    for status, n in sorted(counts.items()):
        print(f"  {status or '(blank)'}: {n}")
    print(f"ready with blank intro: {ready_no_intro}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
