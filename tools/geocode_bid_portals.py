#!/usr/bin/env python3
"""Add coordinates to the known-portal directory so a scan can find every
known bid page within its radius, not just the handful of towns it happens
to sample.

Why this exists: a wide-radius scan only ever searched the exact town typed
plus a small number of geographically-guessed "anchor" towns (see
_nearby_anchor_towns in license_server.py) -- capped at 6 regardless of how
large the radius actually is. A 125-mile radius covers roughly 49,000 square
miles; sampling ~7 points across that area is a real recall gap, and it's
one we shouldn't need search credits to fix -- tools/discover_bid_portals.py
already found 3,151 real bid pages nationally. The problem was never "we
don't know where the bids are", it's that /scan never asked "which of the
pages I already know about fall inside this radius" -- it only asked "does
this one exact town + a few sampled points have something".

This script is what makes that question answerable cheaply: geocode every
"found" row in data/bid_portal_directory.csv once, offline, so /scan can
just do arithmetic (haversine against already-known coordinates) instead of
a live geocode call per candidate town on every request.

Output is its own file, data/bid_portal_coords.csv (city,state,lat,lon) --
NOT merged into bid_portal_directory.csv itself, which
tools/discover_bid_portals.py fully rewrites on every run and would
silently drop any extra columns appended into it. bid_portals.py joins the
two files by (city, state) at load time.

Zippopotam (fast, generous rate limit) resolves the large majority of
these -- they're all real incorporated places, exactly what ZIP-code data
covers. Nominatim only picks up what zippopotam misses, rate-limited to
1req/sec same as everywhere else this codebase calls it.

Usage:
    python3 tools/geocode_bid_portals.py --limit 100   # pilot batch
    python3 tools/geocode_bid_portals.py --state MO    # one state
    python3 tools/geocode_bid_portals.py                # full national run
    python3 tools/geocode_bid_portals.py --resume        # skip towns already geocoded
"""
import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_CSV = os.path.join(os.path.dirname(_HERE), "data", "bid_portal_directory.csv")
OUT_CSV = os.path.join(os.path.dirname(_HERE), "data", "bid_portal_coords.csv")
FIELDS = ["city", "state", "lat", "lon"]


def _load_towns(state_filter=None, limit=None):
    seen, towns = set(), []
    with open(SOURCE_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("status") != "found":
                continue
            city, state = (row.get("city") or "").strip(), (row.get("state") or "").strip().upper()
            if not city or not state:
                continue
            if state_filter and state != state_filter.upper():
                continue
            key = (city, state)
            if key in seen:
                continue
            seen.add(key)
            towns.append(key)
    if limit:
        towns = towns[:limit]
    return towns


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None, help="only the first N towns")
    ap.add_argument("--state", default=None, help="only this state's 2-letter code")
    ap.add_argument("--resume", action="store_true",
                     help="skip towns already present in the output CSV")
    args = ap.parse_args()

    towns = _load_towns(state_filter=args.state, limit=args.limit)

    already = set()
    file_exists = os.path.exists(OUT_CSV)
    if args.resume and file_exists:
        with open(OUT_CSV, newline="", encoding="utf-8") as f:
            already = {(row["city"], row["state"]) for row in csv.DictReader(f)}
        towns = [t for t in towns if t not in already]

    print(f"[geocode] {len(towns)} town(s) to geocode "
          f"({'resuming, ' + str(len(already)) + ' already done' if args.resume else 'fresh run'})",
          flush=True)

    out_f = open(OUT_CSV, "a" if (args.resume and file_exists) else "w",
                 newline="", encoding="utf-8")
    writer = csv.DictWriter(out_f, fieldnames=FIELDS)
    if not (args.resume and file_exists):
        writer.writeheader()
        out_f.flush()

    found, checked, t0 = 0, 0, time.time()
    for city, state in towns:
        g = ls._geo_from_city(city, state)
        checked += 1
        if g:
            writer.writerow({"city": city, "state": state, "lat": g["lat"], "lon": g["lon"]})
            out_f.flush()
            found += 1
        if checked % 50 == 0 or checked == len(towns):
            elapsed = time.time() - t0
            print(f"[geocode] {checked}/{len(towns)} checked, {found} resolved "
                  f"({elapsed:.0f}s elapsed)", flush=True)

    out_f.close()
    print(f"[geocode] done. {found}/{len(towns)} towns geocoded into {OUT_CSV}", flush=True)


if __name__ == "__main__":
    main()
