#!/usr/bin/env python3
"""Discovery pass for new residential_permits.py SOURCES entries: search
Socrata's public catalog (which indexes open-data portals across hundreds of
US cities/counties) for building-permit datasets, then check each candidate's
ACTUAL FIELD VALUES for a genuine dedicated driveway/sidewalk/curb category --
not just a promising-sounding field name.

Why values, not just names: residential_permits.py's own docstring already
documents the failure mode this exists to avoid. Fort Worth's permit dataset
has no dedicated category, so text-matching "driveway" against a free-text
description field mostly pulled in unrelated plumbing repairs that happened
to mention "driveway" as a location. A field literally named "permit_type"
tells you nothing about whether it's safe to filter on -- only its distinct
values do. This script only recommends a city when a SoQL GROUP BY on a
candidate field turns up a value that IS a driveway/sidewalk/curb category on
its own (e.g. Austin's "Driveway / Sidewalks"), with real volume behind it,
not a single stray record.

This is a SHORTLIST tool, not an auto-add. It never touches
residential_permits.py -- it prints candidates worth a human (or a follow-up
verification pass, same as Austin/Cambridge got) looking at, because a wrong
category match here doesn't just cost a missed bid, it costs a contractor a
real phone call about the wrong job.

Usage:
    python3 tools/discover_permit_sources.py
    python3 tools/discover_permit_sources.py --queries "building permit" "residential permit"
"""
import argparse
import json
import urllib.error
import urllib.parse
import urllib.request

CATALOG_URL = "https://api.us.socrata.com/api/catalog/v1"
DEFAULT_QUERIES = ("building permits", "residential permits", "construction permits")
UA = {"User-Agent": "BidCallerPro/1.0 (permit-dataset discovery; "
                     "contact via github.com/slappduck/bid-caller-pro)"}
TIMEOUT = 20

# A field is worth checking for a dedicated category only if its name hints
# at one -- avoids wasting a GROUP BY call on obviously irrelevant fields
# (contractor_name, zip, latitude, ...).
CATEGORY_FIELD_HINTS = ("type", "category", "class", "desc", "work", "subtype")
CATEGORY_FIELD_EXCLUDE = ("state", "city", "status", "date", "county")

# A distinct value counts as a genuine dedicated category only if it's
# ABOUT driveway/sidewalk/curb work specifically, as the whole value (or
# close to it) -- not a generic "Residential" or "Building" bucket that
# would need free-text matching downstream (the Fort Worth trap).
CATEGORY_VALUE_TERMS = ("driveway", "sidewalk", "curb cut", "curb ramp", "curb & gutter")
MIN_CATEGORY_COUNT = 3  # below this, could easily be one mislabeled stray row


def _fetch_json(url):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as ex:
        print(f"[permit-discover] fetch failed ({url[:100]}...): {ex}", flush=True)
        return None


def _search_catalog(query, limit=40):
    params = urllib.parse.urlencode({"q": query, "only": "dataset", "limit": limit})
    data = _fetch_json(f"{CATALOG_URL}?{params}")
    return (data or {}).get("results", [])


def _candidate_fields(columns):
    out = []
    for f in columns or []:
        low = f.lower()
        if any(h in low for h in CATEGORY_FIELD_HINTS) and not any(x in low for x in CATEGORY_FIELD_EXCLUDE):
            out.append(f)
    return out


def _has_location_fields(columns):
    low = [c.lower() for c in (columns or [])]
    has_latlon = any("latitude" in c for c in low) and any("longitude" in c for c in low)
    has_address = any("address" in c for c in low)
    return has_latlon or has_address


def _distinct_values(domain, dataset_id, field, limit=50):
    """SoQL GROUP BY -- cheap (aggregated server-side), not a full data pull."""
    params = urllib.parse.urlencode({
        "$select": f"{field}, count(*) AS n",
        "$group": field,
        "$order": "n DESC",
        "$limit": str(limit),
    })
    url = f"https://{domain}/resource/{dataset_id}.json?{params}"
    data = _fetch_json(url)
    if not isinstance(data, list):
        return []
    out = []
    for row in data:
        val = row.get(field)
        try:
            n = int(row.get("n", 0))
        except (TypeError, ValueError):
            n = 0
        if val:
            out.append((str(val), n))
    return out


def _check_dataset(domain, dataset_id, name, columns):
    if not _has_location_fields(columns):
        return None
    fields = _candidate_fields(columns)
    for field in fields:
        values = _distinct_values(domain, dataset_id, field)
        for val, count in values:
            low = val.lower()
            if count >= MIN_CATEGORY_COUNT and any(term in low for term in CATEGORY_VALUE_TERMS):
                return {"domain": domain, "dataset_id": dataset_id, "name": name,
                        "field": field, "value": val, "count": count,
                        "columns": columns}
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queries", nargs="+", default=list(DEFAULT_QUERIES))
    ap.add_argument("--limit-per-query", type=int, default=40)
    args = ap.parse_args()

    seen_datasets = set()
    hits = []
    checked = 0

    for q in args.queries:
        print(f"[permit-discover] searching catalog: {q!r}", flush=True)
        results = _search_catalog(q, limit=args.limit_per_query)
        print(f"[permit-discover]   {len(results)} candidate datasets", flush=True)
        for r in results:
            res = r.get("resource", {})
            domain = (r.get("metadata") or {}).get("domain", "")
            dataset_id = res.get("id", "")
            if not domain or not dataset_id or (domain, dataset_id) in seen_datasets:
                continue
            seen_datasets.add((domain, dataset_id))
            columns = res.get("columns_field_name") or []
            if not columns:
                continue
            checked += 1
            hit = _check_dataset(domain, dataset_id, res.get("name", ""), columns)
            if hit:
                hits.append(hit)
                print(f"[permit-discover] HIT: {hit['domain']} ({hit['name']}) -- "
                      f"field {hit['field']!r} = {hit['value']!r} ({hit['count']} rows)", flush=True)

    print(f"\n[permit-discover] checked {checked} datasets, {len(hits)} with a genuine "
          f"dedicated driveway/sidewalk/curb category", flush=True)
    print("\n=== SHORTLIST (needs manual verification before adding to SOURCES) ===")
    for h in hits:
        print(f"\n{h['domain']}  ({h['name']})")
        print(f"  dataset_id: {h['dataset_id']}")
        print(f"  category field: {h['field']} = {h['value']!r} ({h['count']} rows in sample)")
        print(f"  full column list: {h['columns']}")


if __name__ == "__main__":
    main()
