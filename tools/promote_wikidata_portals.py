#!/usr/bin/env python3
"""Promote verified Wikidata finds into the portal directory bid_portals.py reads.

tools/verify_wikidata_candidates.py writes every probe result, verdicts and
all, to data/wikidata_verified.csv so failures stay auditable. Only the rows
that came back `found` belong in the live directory, and they need reshaping
into the column set bid_portals._rows_to_seeds expects.

This step used to be done by hand, which meant a national re-harvest had no
repeatable way to reach the directory. Running it is idempotent: rows already
promoted keep their original checked_date so a re-run doesn't churn the diff.

  python3 tools/promote_wikidata_portals.py            # merge into the directory
  python3 tools/promote_wikidata_portals.py --dry-run  # report, write nothing
"""
import argparse
import csv
import datetime
import os
import re
import sys

IN = "data/wikidata_verified.csv"
OUT = "data/wikidata_portals.csv"
COLUMNS = ["domain", "city", "state", "type", "org", "status", "bid_url",
           "platform", "checked_date", "source", "verified_by"]

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


def clean_city(place, state):
    """Wikidata labels are inconsistent: 'Springfield' but also
    'Springfield, Missouri' and 'Cairo, Illinois'. bid_portals keys seeds on
    (city, state), so a label carrying its own state suffix would never match
    a scan looking for plain 'Springfield'."""
    city = (place or "").strip()
    suffix = ", " + STATE_NAMES.get(state, "\0")
    if city.lower().endswith(suffix.lower()):
        city = city[:-len(suffix)].strip()
    # A trailing state abbreviation shows up too ("Aurora, MO").
    city = re.sub(r",\s*[A-Z]{2}$", "", city).strip()
    return city


def entity_type(city):
    low = city.lower()
    if low.endswith(" county") or low.startswith("county of "):
        return "County"
    if low.endswith(" township") or low.endswith(" borough"):
        return "Township"
    return "City"


def platform_of(bid_url):
    # CivicPlus is the only platform the path itself identifies; everything
    # else is the agency's own site, which is what "agency" has always meant
    # in this file.
    return "civicplus" if bid_url.lower().endswith("/bids.aspx") else "agency"


def existing_dates(path):
    """domain -> checked_date already recorded, so re-runs stay stable."""
    dates = {}
    if not os.path.exists(path):
        return dates
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("domain"):
                dates[row["domain"]] = row.get("checked_date") or ""
    return dates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", default=IN)
    ap.add_argument("--out", dest="dst", default=OUT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.src):
        sys.exit(f"{args.src} not found — run verify_wikidata_candidates.py first")

    today = datetime.date.today().isoformat()
    prior = existing_dates(args.dst)

    rows, skipped, seen = [], 0, set()
    with open(args.src, newline="") as fh:
        src_rows = list(csv.DictReader(fh))
    for r in src_rows:
        if r.get("status") != "found" or not r.get("bid_url"):
            skipped += 1
            continue
        domain = (r.get("domain") or "").strip().lower()
        state = (r.get("state") or "").strip().upper()
        city = clean_city(r.get("place"), state)
        if not (domain and city and state) or domain in seen:
            skipped += 1
            continue
        seen.add(domain)
        rows.append({
            "domain": domain,
            "city": city,
            "state": state,
            "type": entity_type(city),
            "org": city,
            "status": "found",
            "bid_url": r["bid_url"],
            "platform": platform_of(r["bid_url"]),
            "checked_date": prior.get(domain) or today,
            "source": "wikidata",
            "verified_by": r.get("owns") or "",
        })

    rows.sort(key=lambda r: (r["state"], r["city"], r["domain"]))
    new = sum(1 for r in rows if r["domain"] not in prior)
    print(f"{len(rows)} portals ({new} new, {len(prior)} already present), "
          f"{skipped} rows skipped as not found")

    if args.dry_run:
        print("--dry-run: nothing written")
        return

    with open(args.dst, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {args.dst}")


if __name__ == "__main__":
    main()
