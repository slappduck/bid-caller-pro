#!/usr/bin/env python3
"""Pull US school district websites out of Wikidata.

School districts are the largest untouched pool of bid pages this product
has. They let exactly our trade -- parking lots, walkways, play surfaces, ADA
ramps -- and the directory holds five of them.

The reason is not that discovery fails on them. It is that the registry the
directory is built from is the CISA .gov list, and districts are almost never
on .gov: they sit on .k12.xx.us, on .org, and on vanity domains. So they were
never in the input at all. 65 of roughly 13,000 in the country.

The obvious source, NCES, does not solve it: the Urban Institute's mirror of
the Common Core directory returns all 19,714 districts with coordinates,
enrollment and school counts -- and 69 fields, not one of them a website.
Wikidata does have websites, on P856, for 808 of them.

808 is not 13,000, but Wikidata's coverage skews to the large districts,
which is exactly the right bias here: a district with fifty schools lets far
more concrete than a two-school rural one.

CANDIDATES ONLY, same discipline as tools/wikidata_gov_sites.py -- Wikidata
is crowd-sourced and does carry wrong website values. Nothing here belongs in
the directory until discover_bid_portals.py has probed it and found a real
bid page.

  python3 tools/wikidata_school_districts.py
  python3 tools/wikidata_school_districts.py --limit 50
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
OUT_CSV = os.path.join(os.path.dirname(_HERE), "data",
                       "school_district_candidates.csv")
ENDPOINT = "https://query.wikidata.org/sparql"
UA = ("CurbCallBot/1.0 (+https://curbcallpro.com; concrete bid aggregator; "
      "contact support@curbcallpro.com)")
FIELDS = ["domain", "org", "city", "state", "type", "qid", "website"]

# Q15726209 is "school district in the United States" specifically -- the
# generic Q398141 pulls in Canadian and other national systems, and an
# earlier attempt at Q3742929 matched nothing at all.
QUERY = """
SELECT ?d ?dLabel ?site ?stateLabel WHERE {
  ?d wdt:P31/wdt:P279* wd:Q15726209 .
  ?d wdt:P856 ?site .
  OPTIONAL { ?d wdt:P131* ?state . ?state wdt:P31 wd:Q35657 . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

# Two-letter codes, because the rest of the pipeline keys on them.
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


def _sparql(query, tries=6):
    """One query, waiting out a throttle rather than failing on it.

    WDQS throttles hard and says so in the error text -- "Aggressively
    rate-limiting to 1 req / min" during an outage. This is a one-off
    registry build, not a scan, so waiting is free and giving up is not.
    """
    url = ENDPOINT + "?" + urllib.parse.urlencode({"query": query})
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/sparql-results+json"})
    delay = 65
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
            delay = min(delay * 2, 600)
        except Exception as e:
            if attempt == tries:
                raise
            print(f"[wikidata] {type(e).__name__}; retrying in {delay}s",
                  flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 600)
    return []


def _domain(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def main():
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None,
                    help="only keep the first N (for a quick look)")
    args = ap.parse_args()

    print("[wikidata] querying US school districts with a website...",
          flush=True)
    t0 = time.time()
    rows = _sparql(QUERY)
    print(f"[wikidata] {len(rows)} rows in {time.time()-t0:.1f}s", flush=True)

    out, seen = [], set()
    for r in rows:
        site = r.get("site", {}).get("value", "")
        dom = _domain(site)
        # A district with no resolvable host is not a candidate, and the same
        # host twice is one candidate -- some districts carry several
        # P856 values (a portal and a homepage).
        if not dom or dom in seen:
            continue
        seen.add(dom)
        state = STATE_ABBR.get(r.get("stateLabel", {}).get("value", ""), "")
        if not state:
            continue          # the pipeline keys on state; unusable without
        out.append({
            "domain": dom,
            "org": r.get("dLabel", {}).get("value", ""),
            # No city on these -- a district spans several. discovery keys on
            # (city, state) so the org name stands in, and placement happens
            # later from whatever the bid page itself says.
            "city": "",
            "state": state,
            "type": "School district",
            "qid": r.get("d", {}).get("value", "").rsplit("/", 1)[-1],
            "website": site,
        })
        if args.limit and len(out) >= args.limit:
            break

    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out)

    by_state = {}
    for r in out:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    print(f"[wikidata] wrote {len(out)} candidates -> {OUT_CSV}")
    print("[wikidata] top states: " + ", ".join(
        f"{s}={n}" for s, n in sorted(by_state.items(),
                                      key=lambda x: -x[1])[:10]))
    print("\nCandidates only. Probe them before anything reaches the "
          "directory:\n  python3 tools/discover_bid_portals.py --recheck-missing "
          "--type 'School district'")


if __name__ == "__main__":
    main()
