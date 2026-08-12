#!/usr/bin/env python3
"""Find city/town/village official websites Wikidata knows about that
data/gov_domains.csv can't -- the .gov registry only covers .gov, and plenty
of small towns run their real site on .org/.com/.us instead (Aurora, MO's
actual site is aurora-cityhall.org; its .gov entry, auroramo.gov, doesn't
even resolve).

Wikidata's "official website" property (P856) on municipality entities is a
free, CC0, genuinely-populated source for this -- verified against Missouri
before building this: 234 of 716 municipality entities had P856 set,
including places as small as Rocheport (pop. 201), and more than half of
those don't have a .gov entry at all.

Method: one SPARQL query gets every US state's QID once, then one query per
state (P131+ under that state, P31 in {city,town,village,municipality} that
have P856 set) -- a full nationwide traversal in one query times out on
query.wikidata.org, so this has to be chunked by state regardless of
--state/--limit.

Every candidate domain is live-checked with a real GET before being written
-- Wikidata's data is good but not infallible (dead links, a webmaster who
moved on), and this should ship a directory of pages that actually load, the
same discipline tools/discover_bid_portals.py applies to the .gov crawl.

Output is its own file, NOT merged into data/gov_domains.csv -- that file is
fully rewritten from the CISA source by refresh_gov_domains.py, which would
silently wipe any Wikidata rows appended into it. gov_directory.py reads both
files and merges them at lookup time instead.

Usage:
    python3 tools/discover_wikidata_domains.py --state MO      # one state
    python3 tools/discover_wikidata_domains.py                 # every state
    python3 tools/discover_wikidata_domains.py --resume        # skip states
                                                                  already done
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
from concurrent.futures import ThreadPoolExecutor, as_completed

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_CSV = os.path.join(os.path.dirname(_HERE), "data", "wikidata_domains.csv")
FIELDS = ["domain", "type", "org", "city", "state", "source", "checked_date"]

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
UA = {"User-Agent": "BidCallerPro/1.0 (municipal website discovery; "
                     "contact via github.com/slappduck/bid-caller-pro)"}
SPARQL_TIMEOUT = 60
FETCH_TIMEOUT = 8

# City (Q515), town in the US (Q1093829), village (Q532), municipality of the
# United States (Q3957) -- the four classes that actually turned up small
# incorporated places in the Missouri pilot. Broader classes (Q486972,
# "human settlement") were tried and pulled in unincorporated places with no
# government of their own, so were deliberately left out.
_CLASSES = "wd:Q515 wd:Q1093829 wd:Q3957 wd:Q532"

_STATES_QUERY = """
SELECT ?state ?stateLabel ?iso WHERE {
  ?state wdt:P31 wd:Q35657 .
  OPTIONAL { ?state wdt:P300 ?iso . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

_CITY_QUERY_TMPL = """
SELECT ?item ?itemLabel ?website WHERE {{
  ?item wdt:P131+ wd:{qid} .
  ?item wdt:P31 ?class .
  VALUES ?class {{ {classes} }}
  ?item wdt:P856 ?website .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""


def _sparql(query, timeout=SPARQL_TIMEOUT, retries=4):
    data = urllib.parse.urlencode({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        f"{SPARQL_ENDPOINT}?{data.decode('utf-8')}",
        headers={**UA, "Accept": "application/sparql-results+json"},
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))["results"]["bindings"]
        except urllib.error.HTTPError as ex:
            if ex.code != 429 or attempt == retries - 1:
                raise
            wait = int(ex.headers.get("Retry-After", "60")) if ex.headers else 60
            print(f"[wikidata] rate-limited, waiting {wait}s (attempt {attempt + 1}/{retries})",
                  flush=True)
            time.sleep(wait)
    return []


def _states():
    """{2-letter state code: wikidata QID}, fetched once."""
    rows = _sparql(_STATES_QUERY)
    out = {}
    for r in rows:
        iso = (r.get("iso") or {}).get("value", "")
        # ISO 3166-2 codes come back as "US-MO" -- want just "MO".
        if not iso.startswith("US-") or len(iso) != 5:
            continue
        qid = r["state"]["value"].rsplit("/", 1)[-1]
        out[iso[3:]] = qid
    return out


def _domain_from_url(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except ValueError:
        return ""
    host = host.split("@")[-1].split(":")[0]  # strip any userinfo/port
    if host.startswith("www."):
        host = host[4:]
    return host


def _cities_for_state(qid):
    query = _CITY_QUERY_TMPL.format(qid=qid, classes=_CLASSES)
    try:
        rows = _sparql(query)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as ex:
        print(f"[wikidata] query failed for {qid}: {ex}", flush=True)
        return []
    out = []
    for r in rows:
        city = (r.get("itemLabel") or {}).get("value", "").strip()
        url = (r.get("website") or {}).get("value", "").strip()
        domain = _domain_from_url(url)
        if not city or not domain or domain.endswith(".gov"):
            continue  # .gov is already the authoritative registry's job
        out.append({"city": city, "domain": domain})
    return out


def _is_live(domain):
    """A real GET, not just a HEAD -- some small-town WAFs 403 HEAD but allow
    GET, and a plain-HTTP/no-UA request gets blocked by others (both seen in
    the Missouri pilot), so this always tries https with a browser-shaped UA
    and falls back to http on a connection failure."""
    for scheme in ("https", "http"):
        try:
            req = urllib.request.Request(f"{scheme}://{domain}/", headers=UA)
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
                return 200 <= resp.status < 400
        except urllib.error.HTTPError as ex:
            # A real server answered, even if unhappy with this request --
            # still evidence the domain is alive, unlike a connection error.
            if ex.code < 500:
                return True
        except Exception:
            continue
    return False


def _process_state(state, qid, checked_date, workers):
    candidates = _cities_for_state(qid)
    if not candidates:
        return []
    live = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_is_live, c["domain"]): c for c in candidates}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                ok = fut.result()
            except Exception:
                ok = False
            if ok:
                live.append({
                    "domain": c["domain"], "type": "City", "org": c["city"],
                    "city": c["city"], "state": state, "source": "wikidata",
                    "checked_date": checked_date,
                })
    return live


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default=None, help="only this state's 2-letter code")
    ap.add_argument("--workers", type=int, default=20,
                     help="concurrent domain liveness checks in flight (default 20 -- "
                          "spread across many different small-town hosts, not aggressive "
                          "against any one of them)")
    ap.add_argument("--resume", action="store_true",
                     help="skip states whose domains are already in the output CSV")
    args = ap.parse_args()

    print("[wikidata] fetching state list", flush=True)
    states = _states()
    if args.state:
        st = args.state.upper()
        if st not in states:
            sys.exit(f"unknown state code {st!r}")
        states = {st: states[st]}
    print(f"[wikidata] {len(states)} state(s) to process", flush=True)

    already_states = set()
    file_exists = os.path.exists(OUT_CSV)
    if args.resume and file_exists:
        with open(OUT_CSV, newline="", encoding="utf-8") as f:
            already_states = {row["state"] for row in csv.DictReader(f)}
        states = {k: v for k, v in states.items() if k not in already_states}
        print(f"[wikidata] resuming, {len(already_states)} state(s) already done",
              flush=True)

    out_f = open(OUT_CSV, "a" if (args.resume and file_exists) else "w",
                 newline="", encoding="utf-8")
    writer = csv.DictWriter(out_f, fieldnames=FIELDS)
    if not (args.resume and file_exists):
        writer.writeheader()
        out_f.flush()

    checked_date = time.strftime("%Y-%m-%d")
    total_found = 0
    for state, qid in sorted(states.items()):
        t0 = time.time()
        rows = _process_state(state, qid, checked_date, args.workers)
        for row in rows:
            writer.writerow(row)
        out_f.flush()
        total_found += len(rows)
        print(f"[wikidata] {state}: {len(rows)} live non-.gov domain(s) "
              f"({time.time() - t0:.0f}s)", flush=True)

    out_f.close()
    print(f"[wikidata] done. {total_found} live domain(s) written to {OUT_CSV}", flush=True)


if __name__ == "__main__":
    main()
