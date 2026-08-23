#!/usr/bin/env python3
"""Judge a discovered state page on what it actually yields, not how it looks.

discover_state_sources.py scores a page on dated, repeated rows. That finds
listings, and it also finds South Dakota's fuel price index (294 dated rows of
diesel prices) and Nebraska's "Policies and Forms". The only honest test is to
run the real pipeline over the page and count what survives: rows that name a
county we can place on a map AND pass the same looks_relevant() filter every
city bid goes through.

Writes data/state_bid_sources.csv back with the measured columns filled in, so
the checked-in file records yield rather than a heuristic score.
"""
import argparse
import csv
import html as htmllib
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_sources          # noqa: E402
import counties             # noqa: E402
from tools import state_fetch   # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(HERE, "data", "state_bid_sources.csv")
_TAGS = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")


def measure(state, url, kind="listing"):
    """Run the REAL production parser over the page and count what survives.

    Uses bid_sources.parse_state_letting + counties.counties_named -- the same
    code path a live scan will use -- so this file records what the product
    would actually show a contractor, not what a heuristic hoped for. Earlier
    versions of this tool used a loose row extractor and reported Washington
    as 13 usable rows; every one was a search-facet chip ("Public Works
    Awarded Pierce County"), and Louisiana's 4 were street names that happen
    to match parish names ("St. Mary Street"). Measuring with the loose
    extractor is how you ship those.
    """
    if not url:
        return dict(rows=0, usable=0, fetch="no_url", samples=[])
    status, html = state_fetch.fetch(url)
    if status != 200 or not html:
        return dict(rows=0, usable=0, fetch=str(status), samples=[])
    if kind == "index":
        import datetime
        link = bid_sources.newest_letting_link(
            html, url, today=datetime.date.today().timetuple()[:3])
        if link:
            st2, body = state_fetch.fetch(link)
            if st2 == 200 and body:
                url, html = link, body
    records = bid_sources.letting_rows(html)
    hits = bid_sources.parse_state_letting(
        html, state, url, counties.counties_named)
    return dict(rows=len(records), usable=len(hits), fetch="ok",
                samples=["[%s] %s" % (h["county"], h["title"][:100])
                         for h in hits[:3]])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", action="append")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--show", action="store_true", help="print sample rows")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(CSV_PATH)))
    todo = rows
    if args.state:
        want = {s.upper() for s in args.state}
        todo = [r for r in rows if r["state"] in want]

    results = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for r, m in zip(todo, ex.map(
                lambda r: measure(r["state"], r["url"],
                                  (r.get("kind") or "listing")), todo)):
            results[r["state"]] = m
            flag = "USABLE" if m["usable"] >= 2 else ("thin  " if m["usable"] else "no    ")
            print("%s %s  record_rows=%-4d usable=%-4d %s"
                  % (flag, r["state"], m["rows"], m["usable"], m["fetch"]),
                    flush=True)
            if args.show:
                for s in m.get("samples", ()):
                    print("        * " + s, flush=True)

    fields = ["state", "url", "kind", "status", "score", "note", "rows", "usable"]
    for r in rows:
        m = results.get(r["state"])
        if m:
            for k in ("rows", "usable"):
                r[k] = m[k]
        for k in fields:
            r.setdefault(k, "")
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    good = sum(1 for m in results.values() if m["usable"] >= 2)
    print("\n%d/%d states yield 2+ placeable concrete-relevant rows"
          % (good, len(results)))


if __name__ == "__main__":
    main()
