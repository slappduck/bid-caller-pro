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

Two queries per state, direct P131 and one hop through the county, rather than
a transitive P131+ -- the transitive form times out server-side at 60s.
"""
import csv, json, subprocess, sys, time
from urllib.parse import urlparse

UA = "BidCallerPro/1.0 (+https://curbcallpro.netlify.app)"
OUT = "data/wikidata_candidates.csv"

# Missouri and the eight states around it: the market being sold into.
STATES = {"MO": "Q1581", "IL": "Q1204", "IA": "Q1546", "KS": "Q1558",
          "NE": "Q1553", "OK": "Q1649", "AR": "Q1612", "TN": "Q1509",
          "KY": "Q1603"}

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


def _known_domains(path="data/bid_portal_directory.csv"):
    known = set()
    with open(path) as fh:
        for row in csv.DictReader(fh):
            d = row["domain"].lower()
            known.add(d[4:] if d.startswith("www.") else d)
    return known


def main(argv):
    wanted = [s.upper() for s in argv[1:]] or list(STATES)
    known = _known_domains()
    rows = []
    for state in wanted:
        qid = STATES.get(state)
        if not qid:
            print(f"{state}: not in the sales region, skipping", file=sys.stderr)
            continue
        found, fresh = {}, 0
        for r in _sparql(QUERY.format(qid=qid)):
            host = (urlparse(r["website"]["value"]).hostname or "").lower()
            if not host or any(j in host for j in JUNK):
                continue
            if host.startswith("www."):
                host = host[4:]
            found[host] = r["placeLabel"]["value"]
        for host, place in sorted(found.items(), key=lambda kv: kv[1]):
            if host not in known:
                rows.append((state, place, host))
                fresh += 1
        print(f"{state}: {len(found)} gov sites, {fresh} not already known",
              flush=True)

    with open(OUT, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["state", "place", "domain"])
        w.writerows(rows)
    print(f"\n{len(rows)} candidate domains -> {OUT}")
    print("These are UNVERIFIED. Probe them before trusting any of them.")


if __name__ == "__main__":
    main(sys.argv)
