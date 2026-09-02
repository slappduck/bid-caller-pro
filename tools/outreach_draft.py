#!/usr/bin/env python3
"""Write the next few outreach emails, each carrying that contractor's own
coverage number.

The pitch that works is not a claim about the product, it is a fact about
their town: "there are 94 agencies inside 50 miles of Wilmington on it right
now." So every draft is built from a live /coverage call rather than from
adjectives, and a prospect whose number cannot be trusted is skipped instead
of guessed at.

Two guards, both learned the hard way.

A thin number is worse than no email. Eight agencies reads as "this does not
cover me" and burns a lead permanently, so anything under MIN_AGENCIES is
held rather than sent.

And the number has to be for the right town. /coverage answered 8 for
Frankfort, IL when the honest answer was 113, because three Illinois places
share that name and the geocoder averaged them into a field 150 miles away.
That specific bug is fixed, but the shape of it will happen again with the
next duplicated name, so the response is checked: the nearest agency it
reports has to actually be in the prospect's own city, or the row is held for
a human to look at.

Nothing here sends anything. It prints drafts for review.

The prospect list is NOT in the repo and must not be: it is third-party
business contact data and this repo is public, so .gitignore excludes
data/*prospects*.csv. Keep it locally with these columns:

  slug      unique, becomes the /go/<slug> tracking link -- never reuse one
  company   for your own reference in the held/sent report
  greeting  how the email opens: a first name, or "<Company> team"
  email     one address
  city      plain name, no ZIP -- this is what /coverage is asked about
  state     two letters
  intro     the one personal line; a row without it is held, on purpose,
            because the whole pitch is that this is not a mail merge
  status    ready | hold | sent
  sent_date filled in by you after sending

  python3 tools/outreach_draft.py                 # next 5 ready prospects
  python3 tools/outreach_draft.py --limit 1
  python3 tools/outreach_draft.py --slug marshall
  python3 tools/outreach_draft.py --json          # machine-readable
"""
import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
PROSPECTS = os.path.join(_ROOT, "data", "outreach_prospects.csv")
API = os.environ.get("CURBCALL_API", "https://bid-caller-pro.onrender.com")
SITE = "https://curbcallpro.com"
RADIUS = 50

# Below this, the honest number argues against us. Chosen from the real
# spread: Springfield MO sits at 12 and reads as sparse but real; Frankfort's
# bogus 8 read as "not covered".
MIN_AGENCIES = 10

DAILY = 5


def coverage(city, state):
    """Live agency count for a place, or None if the API will not answer."""
    body = json.dumps({"location": f"{city}, {state}", "radius": RADIUS}).encode()
    req = urllib.request.Request(
        API.rstrip("/") + "/coverage", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.load(r)
    except Exception:
        return None


def _looks_like_the_right_town(data, city):
    """The nearest agency should be in the prospect's own city.

    This is the check that would have caught Frankfort: the count came back
    fine, but `nearest` was full of towns 150 miles away.
    """
    nearest = data.get("nearest") or []
    if not nearest:
        return False
    want = city.strip().lower()
    return any(want == n.split(",")[0].strip().lower() for n in nearest[:3])


def compose(row, agencies):
    city = row["city"]
    subject = (f"{agencies} agencies near {city} post sidewalk and ADA bids")
    body = (
        f"Hi {row['greeting']},\n\n"
        f"{row['intro']}\n\n"
        "I built CurbCall Pro — it watches the bid pages of every city, county, "
        "school district and special district near you and pulls out the "
        "sidewalk, ADA-ramp and curb & gutter work. There are "
        f"{agencies} agencies inside {RADIUS} miles of {city} on it right now.\n\n"
        "You can check your own area free before paying anything:\n"
        f"{SITE}/go/{row['slug']}\n\n"
        "Worth a look?\n\n"
        "Josh\n"
        "CurbCall Pro"
    )
    return subject, body


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=DAILY,
                    help=f"how many to draft (default {DAILY})")
    ap.add_argument("--slug", action="append", default=None,
                    help="draft only these prospects, ignoring status")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    if not os.path.exists(PROSPECTS):
        print(f"no prospect list at {PROSPECTS}\n"
              "It is deliberately not in the repo -- business contact data, "
              "public repo.\nSee this file's header for the columns.",
              file=sys.stderr)
        return 2
    with open(PROSPECTS, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if args.slug:
        want = set(args.slug)
        queue = [r for r in rows if r["slug"] in want]
    else:
        queue = [r for r in rows if r.get("status") == "ready"][:args.limit]

    drafts, held = [], []
    for row in queue:
        if not row.get("intro"):
            held.append((row, "no intro written"))
            continue
        data = coverage(row["city"], row["state"])
        if not data or not data.get("ok"):
            held.append((row, "coverage lookup failed"))
            continue
        n = int(data.get("agencies") or 0)
        if not _looks_like_the_right_town(data, row["city"]):
            near = ", ".join((data.get("nearest") or [])[:3])
            held.append((row, f"location may be misresolved (nearest: {near})"))
            continue
        if n < MIN_AGENCIES:
            held.append((row, f"only {n} agencies — too thin to lead with"))
            continue
        subject, body = compose(row, n)
        drafts.append({"slug": row["slug"], "company": row["company"],
                       "to": row["email"], "subject": subject, "body": body,
                       "agencies": n})

    if args.json:
        print(json.dumps({"drafts": drafts,
                          "held": [{"slug": r["slug"], "reason": why}
                                   for r, why in held]}, indent=2))
        return 0

    for d in drafts:
        print("=" * 72)
        print(f"TO:      {d['to']}")
        print(f"SUBJECT: {d['subject']}")
        print("-" * 72)
        print(d["body"])
        print()
    if held:
        print("=" * 72)
        print("HELD (not drafted):")
        for r, why in held:
            print(f"  {r['slug']:14} {r['company'][:28]:30} {why}")
    print(f"\n{len(drafts)} draft(s), {len(held)} held. Nothing was sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
