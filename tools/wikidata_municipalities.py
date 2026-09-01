#!/usr/bin/env python3
"""Pull US municipal websites out of Wikidata.

The national crawl reads the CISA .gov registry, and that registry holds
12,711 bare second-level domains and not one subdomain. Every one of them has
already been probed, so the .gov well is dry -- but a large share of American
municipalities is not on a bare .gov at all. They sit on state subdomains
(greenwood.in.gov, noblesville.in.gov), on .us (ci.town.st.us), and on plain
.org and .com. None of those can ever appear in the input the crawl reads.

That is why Indiana looked so thin: the state routes its cities through
*.in.gov, so the registry sees almost none of them. Hand-probing ten
Indianapolis-metro hostnames found three live bid boards.

Wikidata knows 13,806 US municipalities with an official website on P856. An
earlier pull captured 5,332 of them; this one is written to reach the rest.

CANDIDATES ONLY, the same discipline as the other pullers here. Wikidata is
crowd-sourced and does carry stale and wrong website values. Nothing written
by this script belongs in the directory until discover_bid_portals.py has
probed it and found a real bid page:

  python3 tools/wikidata_municipalities.py
  python3 tools/discover_bid_portals.py --registry data/municipal_candidates.csv

Queried one state at a time rather than as a single national query. The
P131* join that resolves a place to its state is the expensive half, and
over 13,806 places it reliably exceeds the endpoint's 60s budget; per state
it is comfortable, and a state that fails can be retried on its own.
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
OUT_CSV = os.path.join(_ROOT, "data", "municipal_candidates.csv")
ENDPOINT = "https://query.wikidata.org/sparql"
UA = ("CurbCallBot/1.0 (+https://curbcallpro.com; concrete bid aggregator; "
      "contact support@curbcallpro.com)")
FIELDS = ["domain", "org", "city", "state", "type", "qid", "website"]

# Files whose domains are already known, so a candidate that duplicates one
# is not worth a second probe. gov_domains.csv is the crawl's input rather
# than its output: a domain in there has been probed whatever the result.
KNOWN = [
    ("data/bid_portal_directory.csv", "domain"),
    ("data/wikidata_portals.csv", "domain"),
    ("data/gov_domains.csv", "domain"),
    ("data/school_district_candidates.csv", "domain"),
]

# Q35657 is "U.S. state". DC is not one and has to be named; the territories
# are left out because the scanner's coordinate set does not cover them.
STATES_QUERY = """
SELECT ?s ?sLabel WHERE {
  { ?s wdt:P31 wd:Q35657 } UNION { VALUES ?s { wd:Q61 } }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

# Q15284 is "municipality of the United States". Measured against the
# alternatives before settling on it: Q1093829 ("city in the United States")
# returns 6,866 and is entirely contained in this tree, so the union of the
# two is still 13,806. One root class is enough.
PLACES_QUERY = """
SELECT ?x ?xLabel ?site WHERE {
  ?x wdt:P31/wdt:P279* wd:Q15284 ;
     wdt:P17 wd:Q30 ;
     wdt:P131* wd:%s ;
     wdt:P856 ?site .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

STATE_ABBR = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT",
    "Delaware": "DE", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME",
    "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI",
    "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO",
    "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND",
    "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC",
}


def _sparql(query, tries=5):
    """One query, waiting out a throttle rather than failing on it.

    WDQS throttles hard and says so in the error text. This is a one-off
    registry build, not a scan, so waiting is free and giving up is not.
    """
    url = ENDPOINT + "?" + urllib.parse.urlencode({"query": query})
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/sparql-results+json"})
    delay = 30
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)["results"]["bindings"]
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503) or attempt == tries:
                raise
            wait = int(e.headers.get("Retry-After") or delay)
            print(f"[wikidata] HTTP {e.code}; waiting {wait}s "
                  f"(attempt {attempt}/{tries})", flush=True)
            time.sleep(wait)
            delay = min(delay * 2, 300)
        except Exception:
            if attempt == tries:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 300)
    return []


def _domain(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _known_domains():
    """Every domain the pipeline has already seen, from whichever of the
    known files exist. A missing file is not an error -- this has to run on a
    checkout that has not built every registry yet."""
    seen = set()
    for rel, col in KNOWN:
        path = os.path.join(_ROOT, rel)
        if not os.path.exists(path):
            continue
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    d = (row.get(col) or "").strip().lower()
                    if d:
                        seen.add(d)
        except Exception as e:
            print(f"[wikidata] could not read {rel}: {e}", flush=True)
    return seen


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", action="append", default=None,
                    help="only these two-letter codes (repeatable). Default: all.")
    ap.add_argument("--all", action="store_true",
                    help="write every candidate, not just domains the pipeline "
                         "has never probed")
    ap.add_argument("--out", default=OUT_CSV, help="output CSV path")
    args = ap.parse_args()

    want = {s.upper() for s in args.state} if args.state else None

    print("[wikidata] resolving state QIDs...", flush=True)
    states = []
    for r in _sparql(STATES_QUERY):
        label = r.get("sLabel", {}).get("value", "")
        abbr = STATE_ABBR.get(label)
        qid = r.get("s", {}).get("value", "").rsplit("/", 1)[-1]
        if abbr and qid and (want is None or abbr in want):
            states.append((abbr, label, qid))
    states.sort()
    print(f"[wikidata] {len(states)} state(s) to query", flush=True)

    known = set() if args.all else _known_domains()
    if known:
        print(f"[wikidata] {len(known)} domains already probed; "
              f"they will be skipped", flush=True)

    out, seen, failed = [], set(), []
    for i, (abbr, label, qid) in enumerate(states, 1):
        t0 = time.time()
        try:
            rows = _sparql(PLACES_QUERY % qid)
        except Exception as e:
            failed.append(abbr)
            print(f"[wikidata] {abbr} FAILED ({type(e).__name__})", flush=True)
            continue
        added = 0
        for r in rows:
            site = r.get("site", {}).get("value", "")
            dom = _domain(site)
            # Same host twice is one candidate: places carry several P856
            # values (a homepage and a portal), and neighbouring towns
            # sometimes share one county-run site.
            if not dom or dom in seen or dom in known:
                continue
            seen.add(dom)
            name = r.get("xLabel", {}).get("value", "")
            out.append({
                "domain": dom,
                "org": name,
                "city": name,
                "state": abbr,
                "type": "City",
                "qid": r.get("x", {}).get("value", "").rsplit("/", 1)[-1],
                "website": site,
            })
            added += 1
        print(f"[wikidata] {i}/{len(states)} {abbr}: {len(rows)} places, "
              f"{added} new ({time.time()-t0:.0f}s)", flush=True)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out)

    print(f"\n[wikidata] wrote {len(out)} new candidates -> {args.out}")
    if failed:
        print(f"[wikidata] {len(failed)} state(s) failed and are worth a "
              f"retry: {', '.join(failed)}")
    print("\nCandidates only. Probe them before anything reaches the "
          "directory:\n"
          f"  python3 tools/discover_bid_portals.py --registry {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
