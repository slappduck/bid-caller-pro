#!/usr/bin/env python3
"""Phase 0 recall benchmark — SEARCH_PLAN.md.

"is it better?" stops being an opinion the moment there is a number attached
to it. This script is that number: it reads a fixed list of real, verified
bid portals directly (no search engine, no AI — the same `bid_sources` path
`/scan` uses) and reports what fraction of a known-real bid list it finds and
correctly parses.

    python3 tools/recall_benchmark.py

Ground truth lives in GROUND_TRUTH below, not in a separate data file — a
benchmark whose answer key nobody can see next to the code that checks it is
easy to quietly drift out of sync with reality. Every entry must be something
a human actually verified on the live site; see the comment on each one for
when and how. Add to it the moment a real, currently-open, trade-relevant bid
turns up in the 50mi ring — that entry is what turns this from a parser
smoke test into an actual recall percentage.

Current limitation: as of 2026-08-11 this sandbox's network policy allows
only springfieldmo.gov — every other host tried (Aurora, Joplin, Republic,
Ozark, even other .gov county sites) was rejected at the proxy with a policy
403. GROUND_TRUTH is Springfield-only until that widens or this is run
somewhere unrestricted.
"""
import random
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(
    __import__("os").path.abspath(__file__))))
import bid_sources as bs

# Same rotation license_server.py's _page_headers() uses — municipal sites
# sit behind bot filters that reject on header fingerprint, so a fetch here
# has to look like the one /scan actually makes.
_UAS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
)


def fetch(url, timeout=20):
    headers = {
        "User-Agent": random.choice(_UAS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(800000).decode("utf-8", "ignore"), "ok"
    except urllib.error.HTTPError as e:
        return "", f"http_{e.code}"
    except Exception as ex:
        name = type(ex).__name__.lower()
        return "", "timeout" if "timeout" in name else str(ex)


# ── Ground truth ─────────────────────────────────────────────────────────
# Each portal entry is a URL that should be readable right now, plus the
# postings a human actually confirmed are on it. `expect` entries are matched
# by substring against parsed row titles, case-insensitive.
#
# `currently_open`: bids the benchmark expects among rows with no
# showAllBids param — the live, load-bearing recall check.
# `historical`: real bids confirmed to exist in the closed/awarded archive
# (showAllBids=on). These can't test live recall — they're closed — but they
# are genuine trade-relevant CivicPlus postings, which is otherwise
# impossible to check today: Springfield has zero open concrete/sidewalk
# bids as of this writing (see SEARCH_PLAN.md's Ground Truth section), so
# this is the only way to confirm looks_relevant() and the field parsers
# handle a real one correctly end to end.
GROUND_TRUTH = [
    {
        "city": "Springfield", "state": "MO",
        "listing_url": "https://www.springfieldmo.gov/Bids.aspx",
        "archive_url": "https://www.springfieldmo.gov/Bids.aspx?showAllBids=on",
        # Verified live, 2026-08-11: these are the only 3 open postings on
        # the whole site. None are trade-relevant — that's a real fact about
        # Springfield right now, not a scanner gap.
        "currently_open": [
            "RENTAL OF ICE MACHINES WITH STORAGE BINS AND MAINTENANCE",
            "REPLACEMENT OF PUBLIC ADDRESS AND TERMINAL PAGING SYSTEM",
            "SKATE AND PRO SHOP CONCESSIONAIRE",
        ],
        # Verified live in the closed/awarded archive, 2026-08-11.
        "historical": [
            {"title": "FY25 SIDEWALK IMPROVEMENTS (2025PW0001)",
             "status": "Awarded", "deadline": "7/16/2025"},
            {"title": "FY25 SIDEWALK IMPROVEMENTS ZONE 4 (2025PW0048)",
             "status": "Awarded", "deadline": "11/4/2025"},
        ],
    },
    # Aurora, MO belongs here next (SEARCH_PLAN.md names it as the second
    # Phase 0 town) but aurora-cityhall.org is currently blocked by this
    # sandbox's network policy — add it once that's fixed.
]


def _title_matches(rows, wanted):
    wanted_low = wanted.lower()
    return next((r for r in rows if wanted_low in r["title"].lower()), None)


def run():
    total_expected, total_found = 0, 0
    any_fetch_failed = False

    for portal in GROUND_TRUTH:
        label = f"{portal['city']}, {portal['state']}"
        print(f"\n=== {label} ===")

        html, outcome = fetch(portal["listing_url"])
        if outcome != "ok":
            print(f"  FETCH FAILED ({outcome}): {portal['listing_url']}")
            any_fetch_failed = True
            total_expected += len(portal["currently_open"])
            continue
        rows = bs.parse_civicplus_html(html, base_url=portal["listing_url"])
        relevant = [r for r in rows if bs.looks_relevant(r["title"], r.get("scope"))]
        print(f"  {len(rows)} open posting(s) read, {len(relevant)} pass looks_relevant()")

        for wanted in portal["currently_open"]:
            total_expected += 1
            hit = _title_matches(rows, wanted)
            if hit:
                total_found += 1
                print(f"  [x] {wanted}  (status={hit['status'] or '?'}, "
                      f"deadline={hit['deadline'] or '?'})")
            else:
                print(f"  [ ] MISSING: {wanted}")

        if portal.get("historical"):
            archive_html, aoutcome = fetch(portal["archive_url"])
            if aoutcome != "ok":
                print(f"  ARCHIVE FETCH FAILED ({aoutcome}): {portal['archive_url']}")
                any_fetch_failed = True
                continue
            arows = bs.parse_civicplus_html(archive_html, base_url=portal["archive_url"])
            print(f"  -- trade-relevance sanity check ({len(arows)} archived posting(s)) --")
            for case in portal["historical"]:
                hit = _title_matches(arows, case["title"])
                ok = bool(hit)
                relevant_ok = ok and bs.looks_relevant(hit["title"], hit.get("scope"))
                status_ok = ok and hit["status"] == case["status"]
                deadline_ok = ok and hit["deadline"] == case["deadline"]
                mark = "x" if (ok and relevant_ok and status_ok and deadline_ok) else " "
                print(f"  [{mark}] {case['title']}")
                if ok:
                    if not relevant_ok:
                        print("      looks_relevant() should have been True")
                    if not status_ok:
                        print(f"      status: got {hit['status']!r}, want {case['status']!r}")
                    if not deadline_ok:
                        print(f"      deadline: got {hit['deadline']!r}, want {case['deadline']!r}")
                else:
                    print("      not found on the archive page")

        time.sleep(0.5)  # be a polite, single-threaded caller against a real site

    print(f"\n=== Recall: {total_found}/{total_expected} currently-open ground-truth bids found ===")
    if any_fetch_failed:
        print("(one or more portals could not be fetched — recall is understated)")
    return 0 if total_found == total_expected and not any_fetch_failed else 1


if __name__ == "__main__":
    sys.exit(run())
