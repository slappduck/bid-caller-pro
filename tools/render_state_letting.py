#!/usr/bin/env python3
"""Read a state DOT letting page that only exists after JavaScript runs.

Three states feed the scanner today -- Alabama, Florida, Missouri -- and the
other 47 return nothing. The reason is not that their pages are hostile or
that the parser is weak. It is that AL, FL and MO serve old-fashioned
server-rendered HTML tables, and most other states have moved their lettings
into JavaScript applications. North Carolina's page parses cleanly and yields
twenty rows that read:

    'show files 09-15-2026 Central Letting Status Advertised'
    'Let Date Sep 15, 2026 Type Central'

That is an index of letting dates. The projects appear only once the page's
scripts have run. Minnesota's advertisements page returns no table at all for
the same reason, and PennDOT's real source is ECMS, a session-based app.

So this opens the page in a real browser and hands the resulting HTML to the
SAME parser the live scan uses -- bid_sources.parse_state_letting with
counties.counties_named. Nothing about relevance or county placement is
re-implemented or relaxed here; a row still has to pass looks_relevant() and
still has to name a county we can put on a map. The only thing that changes
is that the HTML is complete.

Rendering is not a way around anything. robots.txt is checked before the
browser is pointed at a URL, exactly as state_fetch does for plain requests,
and the browser identifies itself with the same honest User-Agent. A state
that has declined non-browser agents outright is still recorded as blocked
and left alone.

WHERE TO RUN IT: not in a sandbox whose egress relay cannot carry a browser's
TLS. Chromium there fails every navigation with ERR_CONNECTION_RESET, on
example.com as surely as on any DOT site, while curl through the same proxy
returns 200. Run it on a machine with ordinary network access:

  pip install playwright && playwright install chromium
  python3 tools/render_state_letting.py --retry-empty
  python3 tools/render_state_letting.py --retry-empty --update

The first form reports what each state would yield and changes nothing. The
second writes the winners into data/state_bid_sources.csv. Commit that CSV --
it is the whole point of the exercise, and the product reads it, not this
script. Nothing here runs during a scan.
"""
import argparse
import csv
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)

import bid_sources          # noqa: E402
import counties             # noqa: E402
import state_fetch          # noqa: E402

SOURCES_CSV = os.path.join(_ROOT, "data", "state_bid_sources.csv")

# Chromium ships with the sandbox image at a versioned path; a local install
# puts it wherever Playwright decided. Prefer whatever exists.
CHROME_HINTS = [
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/opt/pw-browsers/chromium/chrome-linux/chrome",
]

# Long enough for a slow state portal to finish its XHRs, short enough that a
# dead one does not hold up a 47-state run.
NAV_TIMEOUT_MS = 45000
SETTLE_MS = 3500


def _executable():
    for p in CHROME_HINTS:
        if os.path.exists(p):
            return p
    return None          # let Playwright use its own download


def render(page, url):
    """Fully-rendered HTML for one URL, or "" if it will not load."""
    page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
    except Exception:
        # A page that never goes idle (polling widget, open socket) is still
        # worth reading -- it has usually painted its table by now.
        pass
    page.wait_for_timeout(SETTLE_MS)
    return page.content()


def measure(page, state, url):
    if not url:
        return dict(fetch="no_url", rows=0, usable=0, samples=[])
    if not state_fetch.robots_allows(url):
        return dict(fetch="robots_disallow", rows=0, usable=0, samples=[])
    try:
        html = render(page, url)
    except Exception as e:
        return dict(fetch="render_" + type(e).__name__, rows=0, usable=0,
                    samples=[])
    if not html:
        return dict(fetch="empty", rows=0, usable=0, samples=[])
    rows = bid_sources.letting_rows(html)
    hits = bid_sources.parse_state_letting(
        html, state, url, counties.counties_named)
    return dict(fetch="ok", rows=len(rows), usable=len(hits),
                samples=["[%s] %s" % (h["county"], h["title"][:90])
                         for h in hits[:3]])


def _load_sources():
    with open(SOURCES_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f)), f


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", action="append", default=None,
                    help="only these two-letter codes (repeatable)")
    ap.add_argument("--url", default=None,
                    help="try one URL against --state instead of the CSV")
    ap.add_argument("--retry-empty", action="store_true",
                    help="re-render every state currently yielding no rows")
    ap.add_argument("--update", action="store_true",
                    help="write improvements back into state_bid_sources.csv")
    ap.add_argument("--headed", action="store_true",
                    help="show the browser, for watching what a page does")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed.\n"
              "  pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 2

    with open(SOURCES_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        fields = list(rows[0].keys()) if rows else []

    want = {s.upper() for s in args.state} if args.state else None
    if args.url:
        if not want or len(want) != 1:
            print("--url needs exactly one --state", file=sys.stderr)
            return 2
        targets = [{"state": list(want)[0], "url": args.url, "kind": "listing"}]
    else:
        targets = [r for r in rows
                   if (want is None or r["state"].upper() in want)
                   and (not args.retry_empty or r.get("usable") in ("0", "", None))]

    print(f"[render] {len(targets)} state(s) to render", flush=True)
    exe = _executable()
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not args.headed,
            executable_path=exe,
            args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=state_fetch.UA,
                                  ignore_https_errors=True)
        page = ctx.new_page()
        for t in targets:
            st, url = t["state"], t.get("url", "")
            r = measure(page, st, url)
            mark = "  <<<" if r["usable"] else ""
            print(f"[render] {st}  {r['fetch']:22} rows={r['rows']:<4} "
                  f"usable={r['usable']:<4} {url[:56]}{mark}", flush=True)
            for s in r["samples"]:
                print(f"           {s}", flush=True)
            results[st] = r
        browser.close()

    gained = {st: r for st, r in results.items() if r["usable"]}
    print(f"\n[render] {len(gained)} state(s) now yield placeable rows: "
          + (", ".join(f"{s}={r['usable']}" for s, r in sorted(gained.items()))
             or "none"))

    if not args.update:
        if gained:
            print("[render] re-run with --update to record these")
        return 0

    changed = 0
    for row in rows:
        r = results.get(row["state"])
        # Only ever move a state forward. A render that found less than the
        # plain fetch already records means the page was having a bad day,
        # not that the recorded source got worse.
        if not r or r["usable"] <= int(row.get("usable") or 0):
            continue
        row["status"] = "ok"
        row["rows"] = str(r["rows"])
        row["usable"] = str(r["usable"])
        row["note"] = "rendered"
        changed += 1
    with open(SOURCES_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"[render] updated {changed} row(s) in {SOURCES_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
