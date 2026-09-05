#!/usr/bin/env python3
"""Brief the next few outreach emails: the verified facts, not the prose.

This used to emit finished emails from a template. They read as machine-
written, and they were -- five bodies off one skeleton, the same three-item
list of trades in each, an em dash in every other sentence. Two of the five
went to contractors in the same city, so a template was one forwarded email
away from being obvious.

A merge field cannot fix that, because being a merge field is the problem.
So this hands over what a person cannot look up in thirty seconds -- the
live agency count for that contractor's town, checked and sanity-tested,
plus their tracking link -- and the note itself gets typed. At five a day
that is a couple of minutes each, and it is the difference between mail
that gets read and mail that gets binned.

The number is the part worth automating: it is a fact about their town, not
an adjective about the product, and it is the one thing in the email the
recipient can verify in half a minute.

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

# Match the app. This quoted 50 while app.html defaults the Find radius to
# 125, so every email undersold the product against the very screen the
# recipient would open: Indianapolis reads 15 agencies at 50 miles and 93 at
# 125, and two of the five emails already sent quoted the 15. The 125 default
# is not arbitrary either -- it was benchmarked because at 25 miles seven of
# eight metros returned an empty board, and a contractor will drive 100 miles
# for a curb job.
RADIUS = 125

# Below this, the honest number argues against us. Re-set with RADIUS, since
# a floor calibrated at 50 miles waves through anything at 125. Measured
# across fifty metros: the thin end is Boise at 13 and Little Rock at 25,
# while an ordinary market like Springfield MO is 51 and a strong one like
# Milwaukee is 221. Thirty is the line below which the number stops making
# the argument for us.
MIN_AGENCIES = 30

DAILY = 5

# Statuses that mean "never again", enforced below even against --slug.
# A.C. Moate replied "Please remove me from your email list" the morning after
# the first send. Honouring that is not a preference, and a list is only as
# trustworthy as the one row somebody has to remember not to touch -- so the
# code remembers instead.
DO_NOT_CONTACT = {"unsubscribed", "bounced", "do-not-contact"}


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


# The nearest agency has to be close enough that the prospect would recognise
# it as their own patch. Thirty miles is inside the distance these contractors
# already drive for a curb job.
NEAR_ENOUGH_MI = 30.0


def _looks_like_the_right_town(data, city):
    """Did the location resolve to somewhere near this contractor?

    Frankfort is what this is for: /coverage answered 8 for Frankfort, IL
    because three Illinois places share the name and the geocoder averaged
    them into a field 150 miles away.

    This used to test whether the prospect's own town appeared among the
    three nearest agencies, and that proxy failed in both directions. It held
    Morici Bros because Milwaukee's three closest entries are Whitefish Bay,
    South Milwaukee and New Berlin -- suburbs, all within fifteen miles. It
    held Clauss Brothers because Skippack has no bid page of its own, though
    Lansdale is eight miles down the road. Both locations had resolved
    perfectly, and both were the best-fitting prospects on the list.

    The real question was never "is this town in the list" but "how far away
    is the nearest work", so /coverage returns that distance and this reads
    it. Falls back to the old name test when talking to a server that has not
    been deployed yet.
    """
    near_mi = data.get("nearest_mi")
    if isinstance(near_mi, (int, float)):
        return near_mi <= NEAR_ENOUGH_MI
    nearest = data.get("nearest") or []
    if not nearest:
        return False
    want = city.strip().lower()
    return any(want == n.split(",")[0].strip().lower() for n in nearest[:3])


def brief(row, agencies, nearest, nearest_mi=None):
    """The facts for one prospect. The email gets written by a person."""
    return {
        "slug": row["slug"],
        "company": row["company"],
        "to": row["email"],
        "greeting": row["greeting"],
        "agencies": agencies,
        "city": f"{row['city']}, {row['state']}",
        "link": f"{SITE}/go/{row['slug']}",
        "angle": row["intro"],
        "nearest": nearest,
        "nearest_mi": nearest_mi,
    }


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

    # Someone who asked to be left alone is never drafted again, and unlike
    # every other rule here that is not a judgement call the operator gets to
    # override. --slug deliberately ignores status so a held row can be forced
    # through; this is the one status it must not be able to force.
    blocked = [r for r in rows if r.get("status") in DO_NOT_CONTACT]
    rows = [r for r in rows if r.get("status") not in DO_NOT_CONTACT]

    if args.slug:
        want = set(args.slug)
        queue = [r for r in rows if r["slug"] in want]
        refused = sorted(want & {r["slug"] for r in blocked})
        if refused:
            print("refusing, these asked to be removed: " + ", ".join(refused),
                  file=sys.stderr)
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
        drafts.append(brief(row, n, (data.get("nearest") or [])[:4],
                            data.get("nearest_mi")))

    if args.json:
        print(json.dumps({"drafts": drafts,
                          "held": [{"slug": r["slug"], "reason": why}
                                   for r, why in held]}, indent=2))
        return 0

    for d in drafts:
        print("=" * 72)
        print(f"{d['company']}   ({d['city']})")
        print(f"  to        {d['to']}")
        print(f"  open with {d['greeting']},")
        print(f"  the fact  {d['agencies']} agencies within {RADIUS} miles")
        print(f"  link      {d['link']}")
        print(f"  angle     {d['angle']}")
        if d.get("nearest_mi") is not None:
            print(f"  closest   {d['nearest_mi']} miles away")
        if d["nearest"]:
            print(f"  nearby    {', '.join(d['nearest'])}")
        print()
    if held:
        print("=" * 72)
        print("HELD (not drafted):")
        for r, why in held:
            print(f"  {r['slug']:14} {r['company'][:28]:30} {why}")
    print(f"\n{len(drafts)} brief(s), {len(held)} held. Write the notes yourself:\n"
          "keep them short, vary them, and do not reuse a sentence between two\n"
          "contractors in the same city.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
