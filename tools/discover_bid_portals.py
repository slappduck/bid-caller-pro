#!/usr/bin/env python3
"""Thorough, re-runnable national crawl to build data/bid_portal_directory.csv:
for every city/county/district in data/gov_domains.csv, find and verify its
real bid-listing page.

Why this exists: gov_domains.csv answers "what is this place's official
domain" -- it says nothing about where bids live on that domain. Until now
that page was discovered live, one probe at a time, only when /scan actually
needed it for a specific place (see gov_directory.py + bid_sources's
candidate_bid_urls). This script runs that same probe up front, for every
entry in the registry, once -- so /scan never has to guess again for a place
this has already checked, and the portal directory that used to be five
hand-seeded Missouri cities becomes a real national asset.

Verification is deliberately not just "did the URL return 200". Plenty of
municipal CMSes 200 on any path via a generic template or search page. A hit
only counts if the page structurally looks like a bid listing: either the
CivicPlus parser recognizes real bid rows on it, or the fetched text carries
at least two distinct procurement terms (bid/RFP/solicitation/purchasing/
notice to bidders/etc.) and is long enough to be a real listing, not a stub
or error page dressed up as 200.

Output is a checked-in CSV, same pattern as data/gov_domains.csv -- reviewable
and versioned, not a silent write into the runtime KV store (which this
sandbox isn't even connected to the production instance of).

Usage:
    python3 tools/discover_bid_portals.py --limit 50            # pilot batch
    python3 tools/discover_bid_portals.py --state MO            # one state
    python3 tools/discover_bid_portals.py                       # full national run
    python3 tools/discover_bid_portals.py --resume               # skip domains
                                                                   already checked

Resumable by design: every domain's outcome (found or not) is appended to the
output CSV as soon as it's known, not batched to the end, so an interrupted
run keeps everything found so far and --resume never re-probes a domain that
was already checked.
"""
import argparse
import csv
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_sources  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
GOV_DOMAINS_CSV = os.path.join(os.path.dirname(_HERE), "data", "gov_domains.csv")
OUT_CSV = os.path.join(os.path.dirname(_HERE), "data", "bid_portal_directory.csv")
FIELDS = ["domain", "city", "state", "type", "org", "status", "bid_url", "platform", "checked_date"]

UA = {"User-Agent": "BidCallerPro/1.0 (municipal bid-page discovery; "
                     "contact via github.com/slappduck/bid-caller-pro)"}
FETCH_TIMEOUT = 8

# Broader than bid_sources.NICHE_TERMS (which is concrete/sidewalk-specific --
# right for filtering an individual LISTING against the trade). This is a
# general "is this page procurement-related at all" check, since the
# directory should hold a city's bid page regardless of what trade a future
# scan filters for.
GENERIC_BID_TERMS = (
    "bid", "rfp", "rfq", "solicitation", "procurement", "purchasing",
    "notice to bidders", "invitation to bid", "request for proposal",
    "request for quote", "vendor registration", "open bids",
)


def _fetch(url, timeout=FETCH_TIMEOUT):
    """Raw page text, or "" on any failure. No network call inside a parser
    downstream of this -- same discipline as bid_sources.py."""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(500_000).decode("utf-8", "ignore")
    except Exception:
        return ""


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _plain_text(html):
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html)).strip()


def _looks_like_a_bid_page(html):
    """True if a fetched page is worth recording as this place's bid page.

    Two independent signals, either is enough: the CivicPlus parser finds
    real bid rows (strong, structural), or the plain text carries at least
    two distinct generic procurement terms and is substantial (loose, but
    catches every platform bid_sources doesn't have a dedicated reader for
    yet -- which is most of them)."""
    if not html or len(html) < 200:
        return False, ""
    rows = bid_sources.parse_civicplus_html(html)
    if rows:
        return True, "civicplus"
    text = _plain_text(html).lower()
    if len(text) < 300:
        return False, ""
    hits = sum(1 for term in GENERIC_BID_TERMS if term in text)
    return hits >= 2, ""


_LINK_RE = re.compile(r'<a[^>]+href="([^"#][^"]*)"[^>]*>(.*?)</a>', re.I | re.S)
_HOMEPAGE_LINK_HINTS = ("bid", "rfp", "rfq", "solicitation", "procurement",
                        "purchasing", "vendor")
# Cheap noise a bid-shaped word sometimes false-positives on -- a "bid" match
# inside a cookie-consent or unrelated nav link isn't worth a fetch.
_HOMEPAGE_LINK_NOISE = ("facebook.com", "twitter.com", "x.com", "instagram.com",
                        "youtube.com", "linkedin.com", "mailto:", "tel:", "javascript:")


def _homepage_bid_link_candidates(domain, max_candidates=3):
    """A handful of links off the homepage whose href or label suggest a bid
    page -- the fallback for sites where none of the guessed common paths
    hit. Bounded to a few candidates so a domain with no real bid page
    doesn't cost more than one extra fetch beyond the homepage itself."""
    base = f"https://{domain}"
    html = _fetch(base)
    if not html:
        return []
    seen, scored = set(), []
    for m in _LINK_RE.finditer(html):
        href, label = m.group(1), _plain_text(m.group(2))
        blob = (href + " " + label).lower()
        if any(n in blob for n in _HOMEPAGE_LINK_NOISE):
            continue
        hits = sum(1 for term in _HOMEPAGE_LINK_HINTS if term in blob)
        if not hits:
            continue
        if href.startswith("/"):
            href = base + href
        elif not href.lower().startswith("http"):
            href = base + "/" + href.lstrip("/")
        if href in seen:
            continue
        seen.add(href)
        scored.append((hits, href))
    scored.sort(key=lambda t: -t[0])
    return [href for _, href in scored[:max_candidates]]


def _check_domain(entry, homepage_fallback=True):
    """Probe one domain's candidate bid-page paths, falling back to
    following an actual bid-shaped link off the homepage when none of the
    guessed common paths hit. Returns a result row."""
    domain = entry["domain"]
    checked = time.strftime("%Y-%m-%d")

    def _verify(url):
        html = _fetch(url)
        ok, structural_platform = _looks_like_a_bid_page(html)
        if not ok:
            return None
        platform = structural_platform or bid_sources.identify_platform(url) or "custom"
        return {**entry, "status": "found", "bid_url": url,
                "platform": platform, "checked_date": checked}

    for url in bid_sources.candidate_bid_urls(domain):
        hit = _verify(url)
        if hit:
            return hit

    if homepage_fallback:
        for url in _homepage_bid_link_candidates(domain):
            hit = _verify(url)
            if hit:
                return hit

    return {**entry, "status": "not_found", "bid_url": "", "platform": "",
            "checked_date": checked}


def _load_registry(state_filter=None, limit=None):
    rows = []
    with open(GOV_DOMAINS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            domain = (row.get("domain") or "").strip().lower()
            state = (row.get("state") or "").strip().upper()
            if not domain or not state:
                continue
            if state_filter and state != state_filter.upper():
                continue
            rows.append({
                "domain": domain, "city": (row.get("city") or "").strip(),
                "state": state, "type": (row.get("type") or "").strip(),
                "org": (row.get("org") or "").strip(),
            })
    # Cities first (most likely to let this trade's work, and the primary
    # thing /scan looks up), then counties, then the rest -- so a --limit
    # pilot or an interrupted full run has already covered the highest-value
    # entries before it runs out of time.
    order = {"City": 0, "County": 1, "Special district": 2, "School district": 3}
    rows.sort(key=lambda r: order.get(r["type"], 9))
    if limit:
        rows = rows[:limit]
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None,
                     help="only process the first N entries (cities first) -- for a pilot run")
    ap.add_argument("--state", default=None, help="only this state's 2-letter code")
    ap.add_argument("--workers", type=int, default=40,
                     help="concurrent domains in flight (default 40 -- spread across many "
                          "different hosts, so this is polite per-server, not aggressive)")
    ap.add_argument("--resume", action="store_true",
                     help="skip domains already present in the output CSV")
    args = ap.parse_args()

    registry = _load_registry(state_filter=args.state, limit=args.limit)

    already = set()
    file_exists = os.path.exists(OUT_CSV)
    if args.resume and file_exists:
        with open(OUT_CSV, newline="", encoding="utf-8") as f:
            already = {row["domain"] for row in csv.DictReader(f)}
        registry = [r for r in registry if r["domain"] not in already]

    print(f"[discover] {len(registry)} domains to check "
          f"({'resuming, ' + str(len(already)) + ' already done' if args.resume else 'fresh run'})",
          flush=True)

    write_lock = threading.Lock()
    out_f = open(OUT_CSV, "a" if (args.resume and file_exists) else "w",
                 newline="", encoding="utf-8")
    writer = csv.DictWriter(out_f, fieldnames=FIELDS)
    if not (args.resume and file_exists):
        writer.writeheader()
        out_f.flush()

    found, checked = 0, 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_check_domain, entry): entry for entry in registry}
        for fut in as_completed(futures):
            try:
                result = fut.result()
            except Exception as ex_:
                entry = futures[fut]
                result = {**entry, "status": "error", "bid_url": "", "platform": "",
                          "checked_date": time.strftime("%Y-%m-%d")}
                print(f"[discover] error on {entry['domain']}: {ex_}", flush=True)
            checked += 1
            if result["status"] == "found":
                found += 1
            with write_lock:
                writer.writerow(result)
                out_f.flush()
            if checked % 100 == 0 or checked == len(registry):
                elapsed = time.time() - t0
                rate = checked / elapsed if elapsed else 0
                print(f"[discover] {checked}/{len(registry)} checked, {found} found "
                      f"({rate:.1f}/s, {elapsed:.0f}s elapsed)", flush=True)

    out_f.close()
    print(f"[discover] done. {found}/{len(registry)} domains yielded a verified bid page.",
          flush=True)


if __name__ == "__main__":
    main()
