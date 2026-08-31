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


# A single document is not a bid page.
#
# Discovery recorded twelve of these: a 2023 PDF on a township site, a Google
# Doc, an uploaded RFP under /wp-content/uploads. Each passes the text test
# -- an RFP naturally contains procurement words -- but a document never
# changes, so the scan re-reads one frozen solicitation on every run forever,
# and the town looks covered while its real bid page goes unread.
_NOT_A_LISTING_RE = re.compile(
    r"\.(?:pdf|docx?|xlsx?|rtf)(?:$|[?#])"
    r"|docs\.google\.com|drive\.google\.com|dropbox\.com"
    r"|/wp-content/uploads/|/sites/default/files/|/uploads/dm/",
    re.I)


def is_listing_url(url):
    """False for a URL that can only ever be one document."""
    return not _NOT_A_LISTING_RE.search(str(url or ""))


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


def _homepage_bid_link_candidates(domain, max_candidates=3):
    """A handful of links off the homepage whose href or label suggest a bid
    page -- the fallback for sites where none of the guessed common paths
    hit. Bounded to a few candidates so a domain with no real bid page
    doesn't cost more than one extra fetch beyond the homepage itself.
    Extraction logic lives in bid_sources.extract_bid_link_candidates so the
    live /scan path (license_server.py) and this offline crawl can't drift
    apart on what counts as a bid-shaped link."""
    base = f"https://{domain}"
    html = _fetch(base)
    return bid_sources.extract_bid_link_candidates(html, base, max_candidates=max_candidates)


def _check_domain(entry, homepage_fallback=True):
    """Probe one domain's candidate bid-page paths, falling back to
    following an actual bid-shaped link off the homepage when none of the
    guessed common paths hit. Returns a result row."""
    domain = entry["domain"]
    checked = time.strftime("%Y-%m-%d")

    def _verify(url):
        if not is_listing_url(url):
            return None
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


# Offices that sit inside a county but never let a construction contract.
# The registry lists them as County-type entries, so a crawl spends its whole
# path list on a sheriff's office or a probate court and records another
# not_found. 643 of 2,862 county-matched domains are one of these; skipping
# them raised the hit rate in a 120-domain sample from 11% to 20%.
#
# Deliberately matched against org/city/domain rather than just the org: some
# rows carry the giveaway only in the hostname (halecoso.gov, madco911al.gov).
# Two patterns, because org text and hostnames need opposite treatment.
#
# Org text has spaces, so it is word-bounded: an unanchored "treasur" skipped
# the City of Treasure Island, and a bare "court" would take out any
# Courtland.
#
# A hostname has no spaces -- cubaassessoril.gov, halecoso.gov, madco911al.gov
# -- so boundaries would match nothing there and it is scanned for whole
# tokens instead. That list is deliberately more conservative: only strings
# that cannot turn up inside a place name. "court" is absent for exactly the
# Courtland reason; the compound forms are safe.
_OFFICE_IN_ORG_RE = re.compile(
    r"sheriff|\bclerk\b|\bcourts?\b|courthouse|judicial|"
    r"\battorney\b|prosecut|\bassessor\b|\brecorder\b|\btreasurer\b|"
    r"\bcoroner\b|medical examiner|\belections?\b|\bregistrar\b|"
    r"\bsurveyor\b|\bjail\b|detention|probation|public defender|"
    r"\b911\b|dispatch|emergency (?:comm|service)|\besd\b|"
    r"\blibrar(?:y|ies)\b|health depart", re.I)

_OFFICE_IN_DOMAIN_RE = re.compile(
    r"sheriff|assessor|coroner|probate|judicial|prosecut|"
    r"circuitclerk|countyclerk|cityclerk|clerkof|"
    r"municipalcourt|circuitcourt|districtcourt|probatecourt|countycourt|"
    r"districtattorney|publicdefender|treasurer|recorder|"
    r"librar|911|dispatch", re.I)


def is_procurement_entity(row):
    """False for an office that has nothing to put out to bid.

    Used to skip, never to delete: a row that matches is simply not probed,
    so nothing already discovered is lost if this pattern is ever wrong.

    Skipping a county's sheriff does not cost that county its bid page --
    the commission is a separate row. Of the 155 places where every row gets
    skipped, all are counties whose only registry entry is a sheriff, clerk
    or assessor, none of which has ever let a concrete contract.
    """
    org = " ".join(str(row.get(k) or "") for k in ("org", "city"))
    if _OFFICE_IN_ORG_RE.search(org):
        return False
    return not _OFFICE_IN_DOMAIN_RE.search(str(row.get("domain") or ""))


def _load_registry(state_filter=None, limit=None, skip_non_procurement=True,
                   registry_path=None):
    """Rows to probe. Defaults to the CISA .gov registry.

    registry_path exists because that registry can only ever see governments
    on a .gov domain, and whole classes of buyer are not: school districts
    sit on .k12.xx.us, .org and vanity domains, which is why 65 of roughly
    13,000 were in it. Any CSV with domain/city/state/type/org columns works,
    so a new source of buyers does not need a new crawler.
    """
    rows = []
    with open(registry_path or GOV_DOMAINS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            domain = (row.get("domain") or "").strip().lower()
            state = (row.get("state") or "").strip().upper()
            if not domain or not state:
                continue
            if state_filter and state != state_filter.upper():
                continue
            entry = {
                "domain": domain, "city": (row.get("city") or "").strip(),
                "state": state, "type": (row.get("type") or "").strip(),
                "org": (row.get("org") or "").strip(),
            }
            if skip_non_procurement and not is_procurement_entity(entry):
                continue
            rows.append(entry)
    # Cities first (most likely to let this trade's work, and the primary
    # thing /scan looks up), then counties, then the rest -- so a --limit
    # pilot or an interrupted full run has already covered the highest-value
    # entries before it runs out of time.
    order = {"City": 0, "County": 1, "Special district": 2, "School district": 3}
    rows.sort(key=lambda r: order.get(r["type"], 9))
    if limit:
        rows = rows[:limit]
    return rows


def _recheck_missing(args):
    """Re-probe the domains a previous run recorded as not_found, and rewrite
    their rows in place.

    Separate from --resume, which skips anything already in the file --
    exactly the wrong behaviour after widening CANDIDATE_BID_PATHS, when the
    rows worth revisiting are the ones already recorded as misses. Rewrites
    rather than appends so a domain never ends up with two rows.
    """
    with open(OUT_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    def _targeted(row):
        if row.get("status") == "found":
            return False
        # A re-probe is exactly where the sheriff's-office rows hurt most:
        # they are all sitting in this file as not_found and every run pays
        # the full path list for them again.
        if not is_procurement_entity(row):
            return False
        if args.type and (row.get("type") or "").lower() != args.type.lower():
            return False
        return not args.state or (row.get("state") or "").upper() == args.state.upper()

    targets = [r for r in rows if _targeted(r)]
    if args.limit:
        targets = targets[:args.limit]
    print(f"[recheck] {len(targets)} previously-missed domain(s) to re-probe "
          f"({len(bid_sources.CANDIDATE_BID_PATHS)} paths each)", flush=True)

    by_domain, found, checked, t0 = {}, 0, 0, time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_check_domain, {
            "domain": t["domain"], "city": t.get("city", ""), "state": t.get("state", ""),
            "type": t.get("type", ""), "org": t.get("org", ""),
        }): t["domain"] for t in targets}
        for fut in as_completed(futures):
            checked += 1
            try:
                res = fut.result()
            except Exception:
                continue
            if res["status"] == "found":
                found += 1
                by_domain[res["domain"]] = res
            if checked % 100 == 0 or checked == len(targets):
                print(f"[recheck] {checked}/{len(targets)}, {found} newly found "
                      f"({time.time() - t0:.0f}s)", flush=True)

    for row in rows:
        hit = by_domain.get(row["domain"])
        if hit:
            row.update(hit)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    total = sum(1 for r in rows if r.get("status") == "found")
    print(f"[recheck] done. {found} newly found; {total} verified bid pages total.",
          flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None,
                     help="only process the first N entries (cities first) -- for a pilot run")
    ap.add_argument("--state", default=None, help="only this state's 2-letter code")
    ap.add_argument("--overwrite", action="store_true",
                     help="rebuild the directory from scratch, discarding "
                          "every row already in it. Without this, and without "
                          "--resume, a run that would truncate the file is "
                          "refused.")
    ap.add_argument("--registry", default=None,
                     help="probe a different CSV of candidates instead of the "
                          ".gov registry (domain/city/state/type/org columns)")
    ap.add_argument("--type", default=None,
                     help="only this registry type (City, County, "
                          "'Special district', 'School district')")
    ap.add_argument("--workers", type=int, default=40,
                     help="concurrent domains in flight (default 40 -- spread across many "
                          "different hosts, so this is polite per-server, not aggressive)")
    ap.add_argument("--resume", action="store_true",
                     help="skip domains already present in the output CSV")
    ap.add_argument("--recheck-missing", action="store_true",
                     help="re-probe ONLY the domains already recorded as not_found/error, "
                          "and rewrite their rows in place. For after CANDIDATE_BID_PATHS "
                          "is widened -- --resume would skip exactly these, since they are "
                          "already in the file.")
    args = ap.parse_args()

    if args.recheck_missing:
        return _recheck_missing(args)

    registry = _load_registry(state_filter=args.state, limit=args.limit,
                              registry_path=args.registry)

    already = set()
    file_exists = os.path.exists(OUT_CSV)
    if args.resume and file_exists:
        with open(OUT_CSV, newline="", encoding="utf-8") as f:
            already = {row["domain"] for row in csv.DictReader(f)}
        registry = [r for r in registry if r["domain"] not in already]

    print(f"[discover] {len(registry)} domains to check "
          f"({'resuming, ' + str(len(already)) + ' already done' if args.resume else 'fresh run'})",
          flush=True)

    # Refuse to silently destroy the directory.
    #
    # Without --resume this opened OUT_CSV in "w" mode, so any run that
    # forgot the flag truncated the whole thing. It happened: a three-domain
    # smoke test of --registry replaced 12,711 rows with 3, and only a git
    # checkout got them back. A tool whose default action is "delete
    # everything we know" is a trap regardless of how it is documented, and
    # the cost of the guard is one flag on the one run a year that wants a
    # rebuild.
    if (not args.resume) and file_exists and not args.overwrite:
        existing = sum(1 for _ in open(OUT_CSV, encoding="utf-8")) - 1
        if existing > 0:
            print(f"[discover] refusing to overwrite {OUT_CSV} "
                  f"({existing} rows).\n"
                  f"           --resume    add to it, skipping domains "
                  f"already recorded  <- probably what you want\n"
                  f"           --overwrite rebuild it from scratch, "
                  f"discarding those {existing} rows", file=sys.stderr)
            return 1

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
    # Propagate main()'s status: a refusal to overwrite must fail loudly
    # enough that a script wrapping this stops rather than continuing as if
    # the crawl had run.
    sys.exit(main() or 0)
