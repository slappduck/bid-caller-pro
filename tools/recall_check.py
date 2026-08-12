#!/usr/bin/env python3
"""Recall check: does the real reader pipeline actually find real bids?

SEARCH_PLAN.md originally called for a fixed list of "real bids known to be
open right now" as a recall benchmark. That doesn't hold up in practice --
checked live against springfieldmo.gov/Bids.aspx while building this and the
two bids the plan had documented as ground truth had both already closed.
Public bids stay open 2-4 weeks; a fixed list goes stale within a month and
starts reporting 0% recall for reasons that have nothing to do with whether
the scanner works.

So this checks the thing that's actually stable: given whatever a real
CivicPlus listing page contains right now (or a saved fixture of one), does
bid_sources.parse_civicplus_html() find every posting, does looks_relevant()
correctly keep the on-trade ones and drop the rest, and (if OPENAI_API_KEY is
configured) does the real extraction prompt agree. That's the funnel
`_run_known_portals` runs in production, exposed standalone so it can be
pointed at any known source on demand -- including right after noticing a
real bid the app missed, to see exactly which stage dropped it.

data/recall_fixtures/springfield_civicplus.html is the permanent regression
case: real current Springfield postings plus the two originally-documented
ground-truth bids, reconstructed in the real page's exact template, so the
acceptance test survives those bids eventually closing. Run it with no
arguments to check that fixture; tests/test_recall_fixtures.py runs the same
check in the normal test suite.

Usage:
    python3 tools/recall_check.py                          # the Springfield fixture
    python3 tools/recall_check.py --fixture path/to.html
    python3 tools/recall_check.py --url https://city.gov/Bids.aspx
    python3 tools/recall_check.py --url ... --expect "sidewalk" "ADA ramp"
"""
import argparse
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_sources  # noqa: E402

_DEFAULT_FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "recall_fixtures", "springfield_civicplus.html")

UA = {"User-Agent": "BidCallerPro/1.0 (recall check; "
                     "contact via github.com/slappduck/bid-caller-pro)"}
FETCH_TIMEOUT = 15


def _fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.read(800_000).decode("utf-8", "ignore")


def _ai_stage(area, text):
    """None if OPENAI_API_KEY isn't set locally -- this stage is then just
    skipped and reported as such, rather than the whole tool refusing to
    run. It's the same function the live server calls, imported directly so
    this can't silently drift from what production actually does."""
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    import license_server as ls
    return ls._ai_extract(area, text)


def run(html, base_url, area="the area", expect=None, try_ai=False):
    rows = bid_sources.parse_civicplus_html(html, base_url=base_url)
    print(f"parsed:   {len(rows)} row(s)")
    for r in rows:
        print(f"  - {r['title']}  (closes {r['deadline'] or 'unknown'})")

    relevant = [r for r in rows
                if bid_sources.looks_relevant(r["title"], r.get("scope"))]
    print(f"relevant: {len(relevant)} of {len(rows)} passed looks_relevant()")
    for r in relevant:
        print(f"  + {r['title']}")
    dropped = [r for r in rows if r not in relevant]
    for r in dropped:
        print(f"  - {r['title']}  [{bid_sources.rejection_reason(r['title'], r.get('scope'))}]")

    if try_ai:
        text = " ".join(f"{r['title']} {r.get('scope', '')}" for r in relevant)
        ai_result = _ai_stage(area, text) if text else None
        if ai_result is None and not os.environ.get("OPENAI_API_KEY"):
            print("AI stage: skipped (OPENAI_API_KEY not set)")
        else:
            n = len(ai_result or [])
            print(f"AI stage: extraction confirmed {n} bid(s)")

    ok = True
    if expect:
        titles_blob = " ".join(r["title"] for r in relevant).lower()
        print(f"\nexpected {len(expect)} known bid(s):")
        for needle in expect:
            found = needle.lower() in titles_blob
            print(f"  {'FOUND    ' if found else 'MISSING  '} {needle!r}")
            ok = ok and found
        recall = sum(1 for n in expect if n.lower() in titles_blob) / len(expect)
        print(f"\nrecall: {recall:.0%} ({sum(1 for n in expect if n.lower() in titles_blob)}/{len(expect)})")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixture", default=None, help="local HTML file to check")
    ap.add_argument("--url", default=None, help="live URL to fetch and check instead")
    ap.add_argument("--area", default="Springfield, MO",
                     help="area name passed to the AI extraction prompt")
    ap.add_argument("--expect", nargs="*", default=None,
                     help="title substrings that must appear among the relevant rows")
    ap.add_argument("--ai", action="store_true",
                     help="also run the real extraction prompt (needs OPENAI_API_KEY)")
    args = ap.parse_args()

    if args.url:
        print(f"fetching {args.url}")
        try:
            html = _fetch(args.url)
        except (urllib.error.URLError, OSError) as ex:
            sys.exit(f"fetch failed: {ex}")
        base_url = args.url.rsplit("/", 1)[0]
        expect = args.expect
    else:
        path = args.fixture or _DEFAULT_FIXTURE
        print(f"reading {path}")
        with open(path, encoding="utf-8") as f:
            html = f.read()
        base_url = "https://www.springfieldmo.gov"
        # The bundled fixture's own acceptance criteria, used when no
        # --expect is given and no --fixture override either.
        expect = args.expect
        if expect is None and path == _DEFAULT_FIXTURE:
            expect = ["ADA IMPROVEMENT PROJECT", "MT. VERNON & MILLER SIDEWALKS"]

    ok = run(html, base_url, area=args.area, expect=expect, try_ai=args.ai)
    if expect and not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
