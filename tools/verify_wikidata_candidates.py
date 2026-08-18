"""Probe data/wikidata_candidates.csv and confirm which are real bid pages.

Two checks, because Wikidata is crowd-sourced and does get websites wrong (a
Kentucky town in the first sample pointed at an Ohio city's site):

  1. does the domain actually belong to this town -- the town name has to
     appear either in the domain itself or in the homepage text;
  2. does it serve a bid page on one of CANDIDATE_BID_PATHS.

Only rows passing BOTH are safe to merge into the portal directory. Output
carries every row and its verdict so the failures stay auditable rather than
silently disappearing.

  python3 tools/verify_wikidata_candidates.py [--limit N] [--workers N]
"""
import argparse, csv, os, re, sys, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_sources

UA = "BidCallerPro/1.0 (+https://curbcallpro.netlify.app)"
IN = "data/wikidata_candidates.csv"
OUT = "data/wikidata_verified.csv"


def _get(url, limit=80000, timeout=12):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype and "text" not in ctype:
                return None
            return resp.read(limit).decode("utf-8", "ignore")
    except Exception:
        return None


def _slug(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _owns(place, domain, home):
    """Is this domain plausibly this town's own site?

    A name match in the domain is the strong signal, but plenty of real ones
    don't have it (Appleton City runs acmogov.com), so the homepage text is
    the fallback. Both missing means we cannot say it belongs to the town.
    """
    slug = _slug(place)
    if slug and slug in _slug(domain):
        return "domain"
    if home and place and place.lower() in home.lower():
        return "homepage"
    return ""


def probe(row):
    state, place, domain = row["state"], row["place"], row["domain"]
    base = "https://" + domain
    home = _get(base)
    if home is None:
        base = "http://" + domain
        home = _get(base)
    if home is None:
        return dict(row, status="unreachable", owns="", bid_url="", relevant="")

    owns = _owns(place, domain, home)
    if not owns:
        # Reachable but we cannot tie it to this town. Never promote these.
        return dict(row, status="not_this_town", owns="", bid_url="", relevant="")

    for path in bid_sources.CANDIDATE_BID_PATHS:
        body = _get(base + path)
        if not body:
            continue
        # Plenty of sites answer 200 for any URL, so a page that came back is
        # not yet evidence of a bid page -- it has to read like one.
        low = body.lower()
        if any(w in low for w in ("bid", "rfp", "request for proposal",
                                  "solicitation", "procurement",
                                  "invitation to")):
            # relevant = concrete/sidewalk work open right now. Usually "no",
            # and that is fine: ~8% of live bid pages carry concrete work at
            # any moment. The page itself is what belongs in the directory.
            rel = "yes" if bid_sources.looks_relevant(body) else "no"
            return dict(row, status="found", owns=owns,
                        bid_url=base + path, relevant=rel)
    return dict(row, status="no_bid_page", owns=owns, bid_url="", relevant="")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=14)
    args = ap.parse_args()

    rows = list(csv.DictReader(open(IN)))
    if args.limit:
        rows = rows[:args.limit]
    print(f"probing {len(rows)} candidates with {args.workers} workers",
          flush=True)

    out, done = [], 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for res in ex.map(probe, rows):
            out.append(res)
            done += 1
            if done % 100 == 0:
                found = sum(1 for r in out if r["status"] == "found")
                print(f"  {done}/{len(rows)} — {found} found", flush=True)

    with open(OUT, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["state", "place", "domain",
                                           "status", "owns", "bid_url",
                                           "relevant"])
        w.writeheader()
        w.writerows(out)

    tally = {}
    for r in out:
        tally[r["status"]] = tally.get(r["status"], 0) + 1
    print(f"\n{OUT}")
    for k, v in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {k:16s} {v}")


if __name__ == "__main__":
    main()
