"""Pull municipal and county websites for the sales region out of Wikidata.

The bid-portal directory is built from the CISA .gov registry, so it can only
ever see governments on a .gov domain. A large share of US cities are not:
Lee's Summit is lees-summit.mo.us, Blue Springs is bluespringsgov.com,
Republic is republicmo.com. Wikidata knows those, via P856 (official website)
on entities that are instances of a US municipality or county class.

Output is CANDIDATES ONLY -- data/wikidata_candidates.csv. Wikidata is
crowd-sourced and does contain wrong website values (a Kentucky town pointing
at an Ohio city's site turned up in the first sample), so nothing here belongs
in the directory until it has been probed AND the domain confirmed to match
the town. Run tools/discover_bid_portals.py against the output to do that.

  python3 tools/wikidata_gov_sites.py            # the 9-state sales region
  python3 tools/wikidata_gov_sites.py MO KS      # named states only
  python3 tools/wikidata_gov_sites.py --all      # all 50 states plus DC

Two queries per state, direct P131 and one hop through the county, rather than
a transitive P131+ -- the transitive form times out server-side at 60s.
"""
import argparse, csv, json, os, subprocess, sys, time
from urllib.parse import urlparse

UA = "BidCallerPro/1.0 (+https://curbcallpro.netlify.app)"
OUT = "data/wikidata_candidates.csv"

# Missouri and the eight states around it: the market being sold into. This
# stays the default so a bare run keeps doing what it always did.
SALES_REGION = ("MO", "IL", "IA", "KS", "NE", "OK", "AR", "TN", "KY")

# Every state plus DC. Each QID was checked against its Wikidata label before
# being written down -- a wrong QID here silently harvests the wrong state's
# towns, which is the kind of error that only shows up much later as junk in
# the directory.
STATES = {
    "AL": "Q173",   "AK": "Q797",   "AZ": "Q816",   "AR": "Q1612",
    "CA": "Q99",    "CO": "Q1261",  "CT": "Q779",   "DE": "Q1393",
    "FL": "Q812",   "GA": "Q1428",  "HI": "Q782",   "ID": "Q1221",
    "IL": "Q1204",  "IN": "Q1415",  "IA": "Q1546",  "KS": "Q1558",
    "KY": "Q1603",  "LA": "Q1588",  "ME": "Q724",   "MD": "Q1391",
    "MA": "Q771",   "MI": "Q1166",  "MN": "Q1527",  "MS": "Q1494",
    "MO": "Q1581",  "MT": "Q1212",  "NE": "Q1553",  "NV": "Q1227",
    "NH": "Q759",   "NJ": "Q1408",  "NM": "Q1522",  "NY": "Q1384",
    "NC": "Q1454",  "ND": "Q1207",  "OH": "Q1397",  "OK": "Q1649",
    "OR": "Q824",   "PA": "Q1400",  "RI": "Q1387",  "SC": "Q1456",
    "SD": "Q1211",  "TN": "Q1509",  "TX": "Q1439",  "UT": "Q829",
    "VT": "Q16551", "VA": "Q1370",  "WA": "Q1223",  "WV": "Q1371",
    "WI": "Q1537",  "WY": "Q1214",  "DC": "Q61",
}

# city, town, village, city-in-the-US, US county, civil township,
# US administrative territorial entity, unincorporated community, borough
CLASSES = ("wd:Q515 wd:Q3957 wd:Q532 wd:Q1093829 wd:Q47168 wd:Q1115575 "
           "wd:Q852446 wd:Q17343829 wd:Q3301053 wd:Q62049")

QUERY = ('SELECT ?placeLabel ?website WHERE {{ VALUES ?cls {{ %s }} '
         '{{ ?place wdt:P131 wd:{qid} . }} UNION '
         '{{ ?place wdt:P131/wdt:P131 wd:{qid} . }} '
         '?place wdt:P31 ?cls ; wdt:P856 ?website . '
         'SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }} }}'
         ) % CLASSES

# The UNION above is one query for speed, but in the biggest states (CA, TX,
# NY have an order of magnitude more places than Missouri) it can exceed
# Wikidata's 60s server-side limit. Splitting the two halves into separate
# queries costs a round trip and gets those states back rather than silently
# harvesting nothing from them. Built by concatenation rather than str.format
# because SPARQL is mostly braces and escaping them twice is a bug farm.
HALF_PATHS = ("P131", "P131/wdt:P131")


def _half_query(path, qid):
    return ("SELECT ?placeLabel ?website WHERE { VALUES ?cls { " + CLASSES +
            " } ?place wdt:" + path + " wd:" + qid +
            " ; wdt:P31 ?cls ; wdt:P856 ?website . "
            'SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } }')

JUNK = ("facebook.", "wikipedia.", "instagram.", "twitter.", "youtube.",
        "tripadvisor.")


def _sparql(query, tries=3):
    for attempt in range(tries):
        proc = subprocess.run(
            ["curl", "-sS", "--max-time", "180", "-G",
             "https://query.wikidata.org/sparql",
             "--data-urlencode", "query=" + query,
             "-H", "Accept: application/sparql-results+json",
             "-H", "User-Agent: " + UA],
            capture_output=True, text=True)
        try:
            return json.loads(proc.stdout)["results"]["bindings"]
        except Exception:
            # 502/504 from Wikidata under load is routine; back off and retry.
            if attempt == tries - 1:
                print(f"  query failed: {proc.stdout[:120]}", file=sys.stderr)
                return []
            time.sleep(5)


def _known_domains(*paths):
    """Every domain already in the directory, from all its source files.

    Both the .gov directory and the promoted Wikidata portals count: a re-run
    that re-emitted the ~300 already-promoted domains would send the verifier
    off to probe several thousand pages it has already seen.
    """
    paths = paths or ("data/bid_portal_directory.csv",
                      "data/wikidata_portals.csv")
    known = set()
    for path in paths:
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            for row in csv.DictReader(fh):
                d = (row.get("domain") or "").lower()
                if d:
                    known.add(d[4:] if d.startswith("www.") else d)
    return known


def _harvest(state, qid):
    """All (domain -> place) pairs Wikidata knows for one state."""
    rows = _sparql(QUERY.format(qid=qid))
    if not rows:
        # Combined query timed out or errored; try the two halves separately.
        print(f"  {state}: combined query empty, retrying in halves",
              file=sys.stderr, flush=True)
        rows = []
        for path in HALF_PATHS:
            rows.extend(_sparql(_half_query(path, qid)))
    found = {}
    for r in rows:
        host = (urlparse(r["website"]["value"]).hostname or "").lower()
        if not host or any(j in host for j in JUNK):
            continue
        if host.startswith("www."):
            host = host[4:]
        found[host] = r["placeLabel"]["value"]
    return found


def _existing(path, skip_states):
    """Rows already in the candidates file for states this run won't touch.

    Without this, `wikidata_gov_sites.py MO` would truncate a national
    harvest down to Missouri.
    """
    if not os.path.exists(path):
        return []
    keep = []
    with open(path) as fh:
        for row in csv.DictReader(fh):
            if row["state"] not in skip_states:
                keep.append((row["state"], row["place"], row["domain"]))
    return keep


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("states", nargs="*", help="state codes; default is the "
                    "nine-state sales region")
    ap.add_argument("--all", action="store_true",
                    help="every state plus DC, not just the sales region")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)

    if args.all:
        wanted = sorted(STATES)
    else:
        wanted = [s.upper() for s in args.states] or list(SALES_REGION)

    known = _known_domains()
    print(f"{len(known)} domains already in the directory\n", flush=True)

    rows, seen = [], set()
    for state in wanted:
        qid = STATES.get(state)
        if not qid:
            print(f"{state}: not a known state, skipping", file=sys.stderr)
            continue
        found = _harvest(state, qid)
        fresh = 0
        for host, place in sorted(found.items(), key=lambda kv: kv[1]):
            # A domain can be reached from two states via the county hop
            # (border towns, and counties that span a state line); keep the
            # first so the verifier never probes the same host twice.
            if host in known or host in seen:
                continue
            seen.add(host)
            rows.append((state, place, host))
            fresh += 1
        print(f"{state}: {len(found)} gov sites, {fresh} new", flush=True)

    rows.extend(_existing(args.out, set(wanted)))
    rows.sort(key=lambda r: (r[0], r[1]))

    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["state", "place", "domain"])
        w.writerows(rows)
    print(f"\n{len(rows)} candidate domains -> {args.out}")
    print("These are UNVERIFIED. Probe them before trusting any of them.")


if __name__ == "__main__":
    main()
