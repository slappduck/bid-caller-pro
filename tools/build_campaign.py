#!/usr/bin/env python3
"""Turn a prospect CSV into a personalised campaign payload -- measured.

Every recipient's numbers come from an actual scan of their own market, run
here before anything is drafted. That matters twice over: the email can say
something specific and checkable, and a prospect whose market comes back
empty is DROPPED rather than promised a feed that does not exist. The worst
outcome for a cold campaign is a contractor signing up, scanning, and seeing
nothing.

Reads the CSV, scans each distinct city once, writes the JSON body for
POST /campaign/send. Sends nothing itself and needs no admin token -- the
two-step draft/approve flow in license_server.py is the only way to mail
anybody.

  python3 tools/build_campaign.py prospects.csv --priority 1 -o payload.json
  python3 tools/build_campaign.py prospects.csv --min-bids 3 --radius 125
"""
import argparse
import csv
import json
import re
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_portals
import license_server as ls

# Deliberately not derived from scan data or permit records: the list is
# whatever the operator supplies, same rule the sender itself follows.
SUBJECT = "{{bids}} open concrete jobs near {{city}} right now"

BODY = """{{greeting}},

I built a tool that watches every city bid page around {{city}} and pulls out the concrete work -- curb, sidewalk, ADA ramps, flatwork, paving.

Right now it's showing {{bids}} open jobs within {{radius}} miles of you. The nearest is "{{nearest_title}}", {{nearest_miles}} miles out, closing {{nearest_due}}.

Every one comes with the closing date and the buyer's phone number, so you can call and ask for the plans the same day.

Free to try, no card: {{link}}

-- Josh
CurbCall Pro"""


def scan_city(city, state, lat, lon, radius, max_towns=120):
    """Open bids in this market, nearest first. Deterministic path only, so
    the number is a floor a real scan can only beat."""
    pdb = bid_portals.load_directory()
    grouped, stats, coords = {}, {}, {}
    lock = threading.Lock()
    center = {"city": city, "state": state, "lat": lat, "lon": lon}
    towns = bid_portals.towns_within_radius(pdb, lat, lon, radius)
    towns.sort(key=lambda t: ls._miles_between(lat, lon, t[2], t[3]))

    def one(t):
        tc, ts, tla, tlo = t
        try:
            ls._run_known_portals(tc, ts, f"{tc}, {ts}", grouped, center, radius,
                                  {}, coords, lock, pdb, default_city=tc,
                                  town_coords=(tla, tlo), stats=stats)
        except Exception:
            pass

    todo = [(city, state, lat, lon)] + towns[:max_towns]
    with ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(one, todo))
    bids = [b for v in grouped.values() for b in v if ls._is_open_bid(b)]
    bids.sort(key=lambda b: (b.get("miles") if b.get("miles") is not None else 9999))
    return bids


# A row sourced from a web search carries the Google Business listing title,
# not a company name -- "Decorative, Stamped Concrete Driveway & Floor
# Contractor Chesterfield, Barnhart & St. Louis MO - Hoffman Concrete LLC".
# Opening an email with that is worse than opening with nothing.
_REAL_NAME_RE = re.compile(
    r"([A-Z][\w.&'-]*(?:\s+[A-Z][\w.&'-]*){0,3}\s+"
    r"(?:LLC|L\.L\.C\.|Inc\.?|Incorporated|Co\.?|Company|Corp\.?|"
    r"Construction|Concrete|Contractors?|Paving))\b")
GREETING_MAX = 40
# Words that describe the trade rather than name the business. A candidate
# made only of these is still keyword soup -- "Commercial Concrete
# Contractors" names nobody -- so it loses to a plain "Hi there".
_GENERIC_WORDS = {
    "residential", "commercial", "concrete", "contractor", "contractors",
    "construction", "paving", "asphalt", "services", "service", "company",
    "co", "inc", "incorporated", "llc", "corp", "and", "of", "the", "all",
    "serving", "areas", "surrounding", "flatwork", "cement", "masonry",
    "driveway", "driveways", "sidewalk", "curb", "general", "quality",
    "professional", "local", "best", "affordable", "in", "at", "near",
    "for", "your", "top", "rated", "expert", "experts", "pro", "pros",
}


_DOUBLED_RE = re.compile(r"^(.{6,}?)\1$")


def _dedoubled(text):
    """Scrapes sometimes concatenate a name with itself -- "E. Meier
    ContractingE. Meier Contracting"."""
    m = _DOUBLED_RE.match(text.strip())
    return m.group(1).strip() if m else text


def _names_somebody(candidate, place_words=()):
    """True if this reads as a business name rather than a listing title.

    The test is the FIRST word. Across the whole file every directory listing
    title opens with the trade -- "Concrete Contractor Gladstone, MO",
    "Asphalt Services in Kansas City MO", "Residential & Commercial Concrete
    Contractors serving..." -- and every real business name opens with
    something of its own: Musselman, Vanguard, Hoffman, Dormark, Daedalus,
    Kain's, SCC. Greeting a contractor by a listing title announces that
    nobody looked, which is worse than not using their name at all.

    A gazetteer check was the obvious alternative and does not work: the town
    index only knows places that own a bid portal, so Gladstone is absent.
    """
    stop = _GENERIC_WORDS | set(place_words)
    words = [w.strip(".,&'-").lower() for w in candidate.split()]
    words = [w for w in words if w]
    if not words:
        return False
    return words[0] not in stop and any(w not in stop for w in words)


def company_name(row):
    """The company's actual name, or "" if the row only has a listing title."""
    raw = _dedoubled(" ".join((row.get("company") or "").split()))
    if not raw:
        return ""
    # The prospect's own town and state are not distinguishing information --
    # every listing title in the file contains them.
    place = {w.strip(".,").lower()
             for w in f"{row.get('city') or ''} {row.get('state') or ''}".split()}
    place |= {"kansas", "missouri", "iowa", "nebraska", "oklahoma", "city",
              "st", "saint"}

    def ok(text):
        return bool(text) and len(text) <= GREETING_MAX \
            and _names_somebody(text, place)

    if ok(raw):
        return raw
    # "<keyword soup> - Hoffman Concrete LLC" -- the real name is usually the
    # last dash- or pipe-separated part.
    for sep in (" - ", " | ", " – "):
        if sep in raw:
            tail = _dedoubled(raw.rsplit(sep, 1)[-1].strip())
            if ok(tail):
                return tail
    for hit in sorted(_REAL_NAME_RE.findall(raw), key=len):
        if ok(_dedoubled(hit)):
            return _dedoubled(hit)
    return ""


def greeting(row):
    """Never a wall of scraped keywords. "Hi there" beats getting it wrong."""
    name = (row.get("contact") or "").strip()
    if name and name.lower() not in ("?", "n/a", "unknown", "-"):
        first = name.split()[0]
        if first.isalpha() and len(first) <= 20:
            return f"Hi {first}"
    company = company_name(row)
    return f"Hi {company}" if company else "Hi there"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--priority", default="",
                    help="only rows whose priority column starts with this")
    ap.add_argument("--segment", default="concrete contractor")
    ap.add_argument("--radius", type=int, default=125)
    ap.add_argument("--min-bids", type=int, default=1,
                    help="drop a prospect whose market has fewer open jobs "
                         "than this. Never promise a feed that is empty.")
    ap.add_argument("--link", default="https://curbcallpro.com")
    ap.add_argument("-o", "--out", default="")
    args = ap.parse_args()

    with open(args.csv_path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    picked = [r for r in rows
              if (not args.priority
                  or (r.get("priority") or "").strip().startswith(args.priority))
              and (not args.segment
                   or args.segment.lower() in (r.get("segment") or "").lower())
              and (r.get("email") or "").strip()]
    print(f"{len(picked)} of {len(rows)} rows match", file=sys.stderr)

    markets = {}
    for r in picked:
        markets.setdefault(((r.get("city") or "").strip(),
                            (r.get("state") or "").strip().upper()), []).append(r)

    recipients, dropped = [], []
    for (city, state), members in sorted(markets.items()):
        coords = ls._city_coords(city, state, {})
        if not coords:
            dropped.append((city, state, "could not geocode", len(members)))
            continue
        bids = scan_city(city, state, coords[0], coords[1], args.radius)
        print(f"  {city + ', ' + state:24s} {len(bids):3d} open  "
              f"({len(members)} prospect(s))", file=sys.stderr)
        if len(bids) < args.min_bids:
            dropped.append((city, state, f"only {len(bids)} open", len(members)))
            continue
        near = bids[0]
        for r in members:
            recipients.append({"email": r["email"].strip().lower(), "vars": {
                "greeting": greeting(r),
                "company": company_name(r) or (r.get("company") or "").strip(),
                "city": city,
                "bids": str(len(bids)),
                "radius": str(args.radius),
                "nearest_title": near.get("title", "")[:80],
                "nearest_miles": str(near.get("miles", "")),
                "nearest_due": near.get("deadline", "") or "no stated date",
                "link": args.link,
            }})

    payload = {"subject": SUBJECT, "body": BODY, "recipients": recipients}
    text = json.dumps(payload, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"\nwrote {args.out}: {len(recipients)} recipient(s)", file=sys.stderr)
    else:
        print(text)
    if dropped:
        print("\ndropped, and why:", file=sys.stderr)
        for city, state, why, n in dropped:
            print(f"  {city + ', ' + state:24s} {why:20s} ({n} prospect(s))",
                  file=sys.stderr)
    print("\nNothing has been sent. POST this to /campaign/send with your "
          "admin_token to get a draft and a preview.", file=sys.stderr)


if __name__ == "__main__":
    main()
