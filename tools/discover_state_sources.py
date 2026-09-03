#!/usr/bin/env python3
"""Find each state DOT's actual bid-letting LISTING page.

Why this is its own tool. The city crawl (discover_bid_portals.py) works
because on a municipal site the first bid-shaped link off the homepage IS the
listing. On a state site it is a menu. Running the city crawl's
extract_bid_link_candidates against 18 state DOT homepages found a landing
page every single time and a listing never -- every hit came back with zero
dates on it. modot.org/bidding is found easily; the actual data lives at
modotweb.modot.mo.gov/BidLettingPlansRoom/Letting, a different subdomain one
hop further on.

So this crawls two hops instead of one, and judges each candidate by running
the production parser over it and counting the rows that survive -- concrete-
relevant and placeable on a map.

It used to judge them on a heuristic instead: dated, repeated, project-shaped
rows. That is a good filter for "is this a listing of something" and no filter
at all for "is this a listing of WORK". South Dakota's fuel price index (295
dated rows of diesel prices) beat the real letting page on that score, and
Nebraska's "Policies and Forms" beat its own. Twelve states were pointed at
pages that could never yield a bid. The heuristic is still computed -- it is
cheap and it breaks ties -- but yield is what decides.

Output: data/state_bid_sources.csv, checked in and reviewable, same pattern
as data/bid_portal_directory.csv.

Usage:
    python3 tools/discover_state_sources.py --state MO
    python3 tools/discover_state_sources.py --limit 10
    python3 tools/discover_state_sources.py            # all fifty
"""
import argparse
import csv
import html as htmllib
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_sources  # noqa: E402
import counties  # noqa: E402
from tools import state_fetch  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "data", "state_bid_sources.csv")

DOT_ROOTS = {
    "AL": "https://www.dot.state.al.us/", "AK": "https://dot.alaska.gov/",
    "AZ": "https://azdot.gov/", "AR": "https://www.ardot.gov/",
    "CA": "https://dot.ca.gov/", "CO": "https://www.codot.gov/",
    "CT": "https://portal.ct.gov/dot", "DE": "https://deldot.gov/",
    "FL": "https://www.fdot.gov/", "GA": "https://www.dot.ga.gov/",
    "HI": "https://hidot.hawaii.gov/", "ID": "https://itd.idaho.gov/",
    "IL": "https://idot.illinois.gov/", "IN": "https://www.in.gov/indot/",
    "IA": "https://iowadot.gov/", "KS": "https://www.ksdot.gov/",
    "KY": "https://transportation.ky.gov/", "LA": "https://www.dotd.la.gov/",
    "ME": "https://www.maine.gov/mdot/", "MD": "https://www.roads.maryland.gov/",
    "MA": "https://www.mass.gov/orgs/massachusetts-department-of-transportation",
    "MI": "https://www.michigan.gov/mdot", "MN": "https://www.dot.state.mn.us/",
    "MS": "https://mdot.ms.gov/", "MO": "https://www.modot.org/",
    "MT": "https://www.mdt.mt.gov/", "NE": "https://dot.nebraska.gov/",
    "NV": "https://www.dot.nv.gov/", "NH": "https://www.dot.nh.gov/",
    "NJ": "https://www.nj.gov/transportation/", "NM": "https://www.dot.nm.gov/",
    "NY": "https://www.dot.ny.gov/", "NC": "https://www.ncdot.gov/",
    "ND": "https://www.dot.nd.gov/", "OH": "https://www.transportation.ohio.gov/",
    "OK": "https://oklahoma.gov/odot.html", "OR": "https://www.oregon.gov/odot/",
    "PA": "https://www.penndot.pa.gov/", "RI": "https://www.dot.ri.gov/",
    "SC": "https://www.scdot.org/", "SD": "https://dot.sd.gov/",
    "TN": "https://www.tn.gov/tdot.html", "TX": "https://www.txdot.gov/",
    "UT": "https://www.udot.utah.gov/", "VT": "https://vtrans.vermont.gov/",
    "VA": "https://www.vdot.virginia.gov/", "WA": "https://wsdot.wa.gov/",
    "WV": "https://transportation.wv.gov/", "WI": "https://wisconsindot.gov/",
    "WY": "https://www.dot.state.wy.us/",
}

# Words that mark a link as worth following on a state site. Wider than the
# city list: states say "letting" and "advertisement" where a town says "bids".
_FOLLOW_RE = re.compile(
    r"bid|letting|solicitation|advertis|proposal|contract|procure|"
    r"plans?\s*room|construction\s+program|notice\s+to\s+(?:bidders|contractors)",
    re.I)
# ...and words that mark it as definitely not the listing.
_SKIP_RE = re.compile(
    r"prequalif|registration|register|vendor\s+guide|training|archive|"
    r"result|award|tabulat|histor|previous|prior|past\b|complet(?:ed)?|"
    r"policy|manual|specification|faq|help|"
    r"login|sign\s*in|disadvantaged|dbe\b|civil\s+rights|wage", re.I)

_DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|"
                      r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
                      r"[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b")
_TAGS = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")


def page_text(html):
    body = _TAGS.sub(" ", html or "")
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", body)))


def listing_score(html):
    """How much this page looks like a real list of open solicitations.

    Deliberately not "does it contain the word bid" -- every landing page and
    every site-wide nav does. What separates a listing from a menu is dated,
    repeated, project-shaped rows. The MoDOT listing carries 306 dates and 21
    table rows of three or more cells; its landing page carries zero dates.
    """
    text = page_text(html)
    dates = len(_DATE_RE.findall(text))
    rows = [r for r in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", html or "")
            if len(re.findall(r"(?is)<t[dh]", r)) >= 3]
    # Some states render the list as repeated cards/list-items, not a table.
    items = len(re.findall(r"(?is)<li[^>]*>.{40,600}?</li>", html or ""))
    niche = len(re.findall(
        r"sidewalk|curb|gutter|\bADA\b|ramp|concrete|pavement|resurfac|"
        r"overlay|culvert|seal\s*coat|grading|bridge", text, re.I))
    score = 0
    if dates >= 3:
        score += 2
    if dates >= 20:
        score += 1
    if len(rows) >= 3:
        score += 2
    elif items >= 6:
        score += 1
    if niche >= 3:
        score += 1
    return score, {"dates": dates, "rows": len(rows), "items": items,
                   "niche": niche, "chars": len(text)}


def measured_yield(state, url, html):
    """Placeable, concrete-relevant rows the REAL parser gets from this page.

    listing_score() cannot tell a list of jobs from a list of numbers, and
    that is not a tuning problem. South Dakota's fuel price index carries 295
    dated rows of diesel prices and outscored the actual letting page;
    Nebraska's "Policies and Forms" won its state the same way. Both look
    exactly like a listing to a heuristic that counts dates and table rows.

    The only thing that settles it is running the production parser and
    counting what survives placement -- which is what verify_state_sources.py
    already did, one URL at a time, after discovery had already picked wrong.
    Doing it here means the choice is made on yield in the first place.
    """
    try:
        return len(bid_sources.parse_state_letting(
            html, state, url, counties.counties_named))
    except Exception:
        # A malformed page must not take the whole crawl down; scoring it
        # zero simply means some other candidate wins.
        return 0


def candidates(html, base_url, limit=6):
    """Bid-shaped links off a page, best first, de-duplicated."""
    out, seen = [], set()
    for m in re.finditer(r'(?is)<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                         html or ""):
        label = re.sub(r"\s+", " ", htmllib.unescape(
            re.sub(r"<[^>]+>", " ", m.group(2)))).strip()
        href = htmllib.unescape(m.group(1))
        if not label or len(label) > 90:
            continue
        blob = label + " " + href
        if not _FOLLOW_RE.search(blob) or _SKIP_RE.search(blob):
            continue
        try:
            url = __import__("urllib.parse", fromlist=["urljoin"]).urljoin(
                base_url, href)
        except ValueError:
            continue
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        # A link that says "letting" outranks one that merely says "contract".
        rank = 0
        if re.search(r"letting|plans?\s*room|advertis|solicitation", blob, re.I):
            rank -= 2
        if re.search(r"current|open|upcoming|active|schedule", blob, re.I):
            rank -= 1
        out.append((rank, url, label))
    out.sort(key=lambda t: t[0])
    return out[:limit]


def _as_index(state, url, html, today=None):
    """(listing_url, yield) when this page is a DATE INDEX over lettings.

    Alabama's real listing lives at a new address every letting
    (.../NTC_August_28_2026.html) and the old one 404s, so the durable source
    is the index and the listing is resolved from it per scan. A page like
    that yields nothing when parsed directly -- its rows are dates -- and the
    old discovery scored it as a failure and moved on.
    """
    try:
        link = bid_sources.newest_letting_link(html, url, today=today)
    except Exception:
        return "", 0
    if not link:
        return "", 0
    st, body = state_fetch.fetch(link)
    if st != 200 or not body:
        return "", 0
    return link, measured_yield(state, link, body)


def discover(state, root, hops=2, today=None):
    """Best listing URL for one state, or a reason there isn't one."""
    status, html = state_fetch.fetch(root)
    if status != 200:
        return {"state": state, "url": "", "status": str(status), "kind": "listing",
                "score": 0, "usable": 0, "note": "root %s" % status}

    best = {"state": state, "url": "", "status": "no_listing", "kind": "listing",
            "score": 0, "usable": 0, "note": ""}
    frontier = [(root, html)]
    seen_urls = {root}
    for depth in range(hops):
        next_frontier = []
        for page_url, page_html in frontier:
            for _rank, url, label in candidates(page_html, page_url):
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                st, body = state_fetch.fetch(url)
                if st != 200 or not body:
                    continue
                score, detail = listing_score(body)
                kind = "listing"
                usable = measured_yield(state, url, body)
                if not usable:
                    # Nothing placeable here, but it may be the index ABOVE
                    # the listing rather than a dud.
                    _link, idx_usable = _as_index(state, url, body, today)
                    if idx_usable:
                        kind, usable = "index", idx_usable
                # Yield decides. listing_score only breaks ties between pages
                # that produce the same number of real rows, because a page
                # yielding nothing is not a listing however much it resembles
                # one.
                if (usable, score) > (best["usable"], best["score"]):
                    # A page that yields nothing is kept, because a human
                    # reviewing the CSV wants to see what the crawl landed on
                    # -- but it is not called "ok". That word meant "we found
                    # the listing" while pointing at a fuel price index.
                    # Production gates on the usable count, so a no_yield row
                    # is never fetched at scan time either way.
                    best = {"state": state, "url": url,
                            "status": ("ok" if usable else "no_yield"),
                            "kind": kind, "score": score, "usable": usable,
                            "note": "d%d %s %sdates=%d rows=%d niche=%d"
                                    % (depth, label[:34],
                                       ("index " if kind == "index" else ""),
                                       detail["dates"], detail["rows"],
                                       detail["niche"])}
                if depth + 1 < hops:
                    next_frontier.append((url, body))
        frontier = next_frontier
        # Stop paying for hops once a page has produced real, placeable work.
        # This was "score >= 5", which stopped the crawl on the strength of a
        # heuristic -- South Dakota's fuel price index scores 6.
        if best["usable"] >= 3:
            break
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", action="append", help="limit to these states")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--hops", type=int, default=2)
    args = ap.parse_args()

    todo = sorted(DOT_ROOTS.items())
    if args.state:
        want = {s.upper() for s in args.state}
        todo = [t for t in todo if t[0] in want]
    if args.limit:
        todo = todo[:args.limit]

    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for res in ex.map(lambda kv: discover(kv[0], kv[1], args.hops), todo):
            rows.append(res)
            flag = "OK " if res["usable"] >= 2 else ("~  " if res["usable"] else "-- ")
            print("%s %s  usable=%-3d score=%d  %s\n      %s"
                  % (flag, res["state"], res["usable"], res["score"],
                     res["note"], res["url"] or "(none)"), flush=True)

    # MERGE, never replace. A --state run used to write only the states it
    # touched, so re-crawling the 48 unresolved ones silently deleted the two
    # verified rows that were the whole point of the exercise.
    merged = {}
    if os.path.exists(OUT):
        with open(OUT, newline="") as f:
            for prev in csv.DictReader(f):
                merged[prev["state"]] = prev
    for r in rows:
        prior = merged.get(r["state"], {})
        same = prior.get("url") == r["url"]
        # The crawl now measures yield itself, so "usable" is fresh either
        # way. "rows" is the raw record count, which only verify computes, so
        # it survives only when the URL did not move.
        merged[r["state"]] = dict(
            r, rows=(prior.get("rows", "") if same else ""))
    # "kind" was missing from this list while the CSV carried it, so every
    # run of this tool silently blanked the column -- and kind=index is the
    # only thing that keeps Alabama and Kentucky resolvable, since their
    # listing URL changes with each letting. Discovery now sets it, and it
    # has to be written out.
    fields = ["state", "url", "kind", "status", "score", "note", "rows",
              "usable"]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for st in sorted(merged):
            row = merged[st]
            row.setdefault("kind", "listing")
            for k in fields:
                row.setdefault(k, "")
            w.writerow(row)
    good = sum(1 for r in rows if r["usable"] >= 2)
    blocked = sum(1 for r in rows if r["status"] == "root blocked")
    print("\n%d/%d states yielding 2+ placeable rows; %d blocked us; wrote %s"
          % (good, len(rows), blocked, OUT))


if __name__ == "__main__":
    main()
