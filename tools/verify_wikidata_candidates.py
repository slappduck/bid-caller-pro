"""Probe data/wikidata_candidates.csv and confirm which are real bid pages.

Two checks, because Wikidata is crowd-sourced and does get websites wrong (a
Kentucky town in the first sample pointed at an Ohio city's site):

  1. does the domain actually belong to this town -- the town name has to
     appear either in the domain itself or in the homepage text;
  2. does it serve a bid page on one of CANDIDATE_BID_PATHS.

Only rows passing BOTH are safe to merge into the portal directory. Output
carries every row and its verdict so the failures stay auditable rather than
silently disappearing.

A national candidate set runs to several thousand domains and each one costs
up to a homepage fetch plus a walk of CANDIDATE_BID_PATHS, so the run is long
enough that losing it to an interruption matters. Results are therefore
written as they complete, and --resume skips domains already in the output:

  python3 tools/verify_wikidata_candidates.py [--limit N] [--workers N]
  python3 tools/verify_wikidata_candidates.py --resume    # continue a run
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


# "BID" is also a Business Improvement District, and those pages are dense
# with the word -- cityofselma.com/.../downtown_selma_bid.php sailed through
# the loose test below. It is never a solicitation page.
_NOT_A_BID_PAGE = ("business improvement district",)

# A guessed path like /Bids.aspx is itself evidence, so the body test there
# can stay loose. An arbitrary link followed off a homepage carries no such
# evidence, so it has to say something only a real solicitation page says --
# the bare token "bid" also matches "bidding", "forbidden" and the district
# sense above.
_STRONG_BID_MARKERS = (
    "invitation to bid", "invitation for bid", "request for proposal",
    "request for qualification", "sealed bid", "bid opportunit",
    "current bid", "open bid", "notice to bidder", "bid opening",
    "accepting bid", "bid document", "bid tabulation", "bids due",
    "request for bid", "bid packet", "solicitation", "procurement",
    "rfp", "rfq",
)


def _bid_page_at(url, strict=False):
    """{"bid_url", "relevant"} if this URL serves something that reads like a
    bid page, else None.

    Plenty of sites answer 200 for any URL, so a page coming back is not by
    itself evidence -- it has to read like one.
    """
    body = _get(url)
    if not body:
        return None
    low = body.lower()
    if any(w in low for w in _NOT_A_BID_PAGE):
        return None
    markers = _STRONG_BID_MARKERS if strict else (
        "bid", "rfp", "request for proposal", "solicitation", "procurement",
        "invitation to")
    if not any(w in low for w in markers):
        return None
    # relevant = concrete/sidewalk work open right now. Usually "no", and that
    # is fine: ~8% of live bid pages carry concrete work at any moment. The
    # page itself is what belongs in the directory.
    return {"bid_url": url,
            "relevant": "yes" if bid_sources.looks_relevant(body) else "no"}


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
        hit = _bid_page_at(base + path)
        if hit:
            return dict(row, status="found", owns=owns, **hit)

    # None of the guessed paths hit. Before giving up, follow the link a real
    # visitor would click: every platform puts its bid page somewhere
    # different, but nearly all of them link to it from the front page with
    # obvious wording. The homepage is already fetched, so this costs a couple
    # of requests and only on sites that would otherwise be recorded as a
    # miss.
    for url in bid_sources.extract_bid_link_candidates(home, base):
        hit = _bid_page_at(url, strict=True)
        if hit:
            return dict(row, status="found", owns=owns, **hit)

    return dict(row, status="no_bid_page", owns=owns, bid_url="", relevant="")


FIELDS = ["state", "place", "domain", "status", "owns", "bid_url", "relevant"]


def _already_done(path):
    """domain -> row for everything a previous run already probed."""
    done = {}
    if not os.path.exists(path):
        return done
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("domain"):
                done[row["domain"]] = row
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=14)
    ap.add_argument("--resume", action="store_true",
                    help="skip domains already in the output and append")
    ap.add_argument("--in", dest="src", default=IN)
    ap.add_argument("--out", dest="dst", default=OUT)
    args = ap.parse_args()

    with open(args.src, newline="") as fh:
        rows = list(csv.DictReader(fh))

    prior = _already_done(args.dst) if args.resume else {}
    if prior:
        rows = [r for r in rows if r["domain"] not in prior]
        print(f"{len(prior)} already probed, {len(rows)} left", flush=True)
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        print("nothing left to probe")
        return
    print(f"probing {len(rows)} candidates with {args.workers} workers",
          flush=True)

    # Append when resuming so an interrupted run keeps what it earned; the
    # header only goes in when the file is being created.
    mode = "a" if (args.resume and prior) else "w"
    tally = {}
    for r in prior.values():
        tally[r["status"]] = tally.get(r["status"], 0) + 1

    done = 0
    with open(args.dst, mode, newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if mode == "w":
            w.writeheader()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for res in ex.map(probe, rows):
                w.writerow({k: res.get(k, "") for k in FIELDS})
                tally[res["status"]] = tally.get(res["status"], 0) + 1
                done += 1
                if done % 100 == 0:
                    # Flush on the same cadence as the progress line: a kill
                    # then loses at most the last hundred probes.
                    fh.flush()
                    print(f"  {done}/{len(rows)} — {tally.get('found', 0)} "
                          f"found", flush=True)

    print(f"\n{args.dst}")
    for k, v in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {k:16s} {v}")


if __name__ == "__main__":
    main()
