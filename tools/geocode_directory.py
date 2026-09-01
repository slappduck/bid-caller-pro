#!/usr/bin/env python3
"""Give the directory's towns coordinates, so the scanner can reach them.

A verified bid page is worth nothing on its own. /scan works outward from a
point, and bid_portals.py joins a directory row to a location through
bid_portal_coords.csv on (city, state). A row whose town is missing from that
file is not merely unranked -- it is invisible, and no radius will ever
return it.

That is exactly what happened to the municipal crawl: 1,796 verified pages
added, 1,574 of them for towns the coordinate file had never heard of,
because it was built from the .gov registry's towns and these places were
never in it. Measured across five metros, a 53% larger directory moved
reachable agencies by 7%.

The coordinates are already known. Every candidate row carries the Wikidata
QID it came from, and Wikidata holds P625 for essentially every populated
place, so this is a join rather than a geocode -- no third-party geocoding
service, no rate-limited address lookups, and the values agree with the
places the rows actually describe.

  python3 tools/geocode_directory.py
  python3 tools/geocode_directory.py --dry-run
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
DIRECTORY = os.path.join(_ROOT, "data", "bid_portal_directory.csv")
COORDS = os.path.join(_ROOT, "data", "bid_portal_coords.csv")
CANDIDATES = ["municipal_candidates.csv", "school_candidates.csv",
              "county_candidates.csv", "special_candidates.csv",
              "school_district_candidates.csv"]
ENDPOINT = "https://query.wikidata.org/sparql"
ENTITY_URL = "https://www.wikidata.org/wiki/Special:EntityData/%s.json"
UA = ("CurbCallBot/1.0 (+https://curbcallpro.com; concrete bid aggregator; "
      "contact support@curbcallpro.com)")
BATCH = 300
WORKERS = 16

# No label service and no property paths: just a coordinate per QID, which
# is the one query shape this endpoint has never refused.
COORD_QUERY = """
SELECT ?x ?coord WHERE {
  VALUES ?x { %s }
  ?x wdt:P625 ?coord .
}
"""


def _sparql(query, tries=2):
    url = ENDPOINT + "?" + urllib.parse.urlencode({"query": query})
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/sparql-results+json"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)["results"]["bindings"]
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(20)
    return []


def _parse_point(text):
    """"Point(-86.1581 39.7684)" -> (lat, lon). Longitude comes first in
    WKT, which is the opposite order to everything else here."""
    try:
        inner = text[text.index("(") + 1:text.index(")")]
        lon, lat = inner.split()
        return float(lat), float(lon)
    except Exception:
        return None


def _entity_point(qid):
    req = urllib.request.Request(ENTITY_URL % qid, headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                e = list(json.load(r)["entities"].values())[0]
            for c in (e.get("claims") or {}).get("P625", []):
                v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
                if isinstance(v, dict) and "latitude" in v:
                    return float(v["latitude"]), float(v["longitude"])
            return None
        except Exception:
            if attempt == 2:
                return None
            time.sleep(3 * (attempt + 1))
    return None


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what is missing and stop")
    args = ap.parse_args()

    have = set()
    with open(COORDS, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            have.add((r["city"], r["state"]))

    # domain -> qid, from whichever candidate files exist.
    qid_of = {}
    for name in CANDIDATES:
        path = os.path.join(_ROOT, "data", name)
        if not os.path.exists(path):
            continue
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("qid") and r.get("domain"):
                    qid_of[r["domain"].strip().lower()] = r["qid"]

    # One place per (city, state): the coordinate file is keyed that way, and
    # several agencies commonly share a town.
    wanted = {}
    with open(DIRECTORY, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["status"] != "found":
                continue
            key = (r["city"], r["state"])
            if not r["city"] or key in have or key in wanted:
                continue
            qid = qid_of.get(r["domain"].strip().lower())
            if qid:
                wanted[key] = qid

    missing_total = sum(
        1 for r in csv.DictReader(open(DIRECTORY, newline="", encoding="utf-8"))
        if r["status"] == "found" and (r["city"], r["state"]) not in have)
    print(f"[geocode] {missing_total} found rows have no coordinates")
    print(f"[geocode] {len(wanted)} distinct towns carry a QID to resolve")
    if args.dry_run:
        return 0

    keys = list(wanted)
    points = {}

    # SPARQL first: 300 places per request beats 300 requests. It is also the
    # endpoint that has been refusing service all day, so failure here is
    # expected rather than exceptional and simply falls through.
    for i in range(0, len(keys), BATCH):
        chunk = keys[i:i + BATCH]
        values = " ".join("wd:" + wanted[k] for k in chunk)
        try:
            rows = _sparql(COORD_QUERY % values)
        except Exception as e:
            print(f"[geocode] SPARQL batch {i//BATCH} unavailable "
                  f"({type(e).__name__}); REST will cover it", flush=True)
            continue
        by_qid = {}
        for r in rows:
            q = r.get("x", {}).get("value", "").rsplit("/", 1)[-1]
            pt = _parse_point(r.get("coord", {}).get("value", ""))
            if q and pt:
                by_qid.setdefault(q, pt)
        for k in chunk:
            if wanted[k] in by_qid:
                points[k] = by_qid[wanted[k]]
        print(f"[geocode] sparql {len(points)}/{len(keys)}", flush=True)

    todo = [k for k in keys if k not in points]
    if todo:
        print(f"[geocode] {len(todo)} left; resolving over REST", flush=True)
        with ThreadPoolExecutor(WORKERS) as ex:
            for k, pt in zip(todo, ex.map(lambda k: _entity_point(wanted[k]), todo)):
                if pt:
                    points[k] = pt

    # A coordinate outside the continental bounding box means the QID was
    # matched to the wrong thing; better to drop it than to place a bid
    # somewhere the customer will never look.
    good = {k: v for k, v in points.items()
            if -180 <= v[1] <= -60 and 15 <= v[0] <= 72}
    dropped = len(points) - len(good)

    with open(COORDS, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for (city, state), (lat, lon) in sorted(good.items()):
            w.writerow([city, state, lat, lon])

    print(f"\n[geocode] added {len(good)} towns to {COORDS}")
    if dropped:
        print(f"[geocode] dropped {dropped} outside plausible US bounds")
    unresolved = len(keys) - len(points)
    if unresolved:
        print(f"[geocode] {unresolved} towns had no P625 and stay unreachable")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
