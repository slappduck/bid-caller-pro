#!/usr/bin/env python3
"""Who's due for the one follow-up, and the facts to write it from.

The data backing this: a single follow-up sent a few days after the first,
never sooner, lifts replies substantially -- the first message only ever
captures part of the eventual replies, the rest come from that one nudge.
Sooner than a few days reads as impatient rather than persistent, and this
account already has a hard rule against reading as a campaign, so there is
exactly one follow-up, not a sequence.

This does not touch what "sent" already means. It reads the same prospect
list outreach_draft.py does and asks a narrower question: of the rows marked
sent, which ones are old enough for the one follow-up and haven't had one
yet. It writes nothing to the CSV -- sent_date is filled in by hand today,
and a follow-up column follows the same practice: after you actually send
the nudge, add today's date to that prospect's row yourself.

FOLLOWUP_AFTER_DAYS is a judgement call, not a fact to verify like the
agency count is, so it is a constant you can see and change, not something
this script decides for you.

A follow-up counts against the same 5/day this account already runs on. It
is not an extra 5 -- five outbound emails a day total, whether new or
follow-up, is the number the operator set for this account and this script
does not get to loosen that on its own.

Nothing here sends anything, same as outreach_draft.py. It prints who is due
and what the first email said, so the follow-up references it honestly
instead of guessing what was already sent.

Expects one additional column beyond what outreach_draft.py documents:

  followup_date   blank until you send the follow-up, then filled in by
                  hand the same way sent_date is. A row with this already
                  set is done and will not be listed again.

  python3 tools/outreach_followup_draft.py                # due today
  python3 tools/outreach_followup_draft.py --limit 1
  python3 tools/outreach_followup_draft.py --json
"""
import argparse
import csv
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.outreach_draft import DO_NOT_CONTACT, SITE  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROSPECTS = os.path.join(_ROOT, "data", "outreach_prospects.csv")

# Sooner reads as impatient; the data this account is following says a few
# days, not a few hours. See this file's own docstring for the reasoning.
FOLLOWUP_AFTER_DAYS = 3

DAILY = 5


def _parse_date(value):
    try:
        return datetime.date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def due_for_followup(rows, today, after_days=FOLLOWUP_AFTER_DAYS):
    """(due, skipped) -- due is oldest-sent-first, skipped explains why not.

    today is passed in rather than read from the clock so this is testable
    without waiting three days for a fixture to age.
    """
    due, skipped = [], []
    for r in rows:
        if r.get("status") in DO_NOT_CONTACT:
            continue
        if r.get("status") != "sent":
            continue
        if (r.get("followup_date") or "").strip():
            continue
        sent = _parse_date(r.get("sent_date"))
        if sent is None:
            skipped.append((r, "sent_date missing or unreadable"))
            continue
        age = (today - sent).days
        if age < after_days:
            skipped.append((r, f"only {age} day(s) since sent, needs {after_days}"))
            continue
        due.append((r, age))
    due.sort(key=lambda pair: -pair[1])
    return due, skipped


def brief(row, age):
    return {
        "slug": row["slug"],
        "company": row["company"],
        "to": row["email"],
        "greeting": row["greeting"],
        "days_since_sent": age,
        "link": f"{SITE}/go/{row['slug']}",
        "original_angle": row.get("intro", ""),
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=DAILY,
                    help=f"how many to list (default {DAILY})")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    if not os.path.exists(PROSPECTS):
        print(f"no prospect list at {PROSPECTS}\n"
              "It is deliberately not in the repo -- business contact data, "
              "public repo.", file=sys.stderr)
        return 2

    with open(PROSPECTS, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    due, skipped = due_for_followup(rows, datetime.date.today())
    due = due[:args.limit]
    briefs = [brief(r, age) for r, age in due]

    if args.json:
        print(json.dumps({"due": briefs, "count_skipped": len(skipped)}, indent=2))
        return 0

    for d in briefs:
        print("=" * 72)
        print(f"{d['company']}   ({d['days_since_sent']} days since sent)")
        print(f"  to           {d['to']}")
        print(f"  open with    {d['greeting']},")
        print(f"  link         {d['link']}")
        print(f"  first email  said: {d['original_angle']}")
        print()
    print(f"\n{len(briefs)} due for a follow-up, {len(skipped)} not yet due or "
          "already followed up.\nWrite a short one referencing the first "
          'email -- "following up on this, no worries if not relevant" is '
          "the shape that works, not a second pitch. Counts against the\n"
          "same 5/day as new prospects.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
