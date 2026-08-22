#!/usr/bin/env python3
"""Run the known-portal engine end to end and report what a customer sees.

Not the audit. The audit asks "is the feed stale" over sampled portals. This
runs the actual scan path -- portal discovery, parsing, the niche gate,
detail-page enrichment, placement and the radius check -- for a list of real
locations, and reports the funnel plus the quality of what survives.

No API keys needed: the search and AI paths sit idle without them, which is
the point. What is measured here is the deterministic half, the half that
answers a scan when search is down.

  python3 tools/engine_benchmark.py                 # the default location set
  python3 tools/engine_benchmark.py --towns 8       # towns per location
  python3 tools/engine_benchmark.py --only Aurora
"""
import argparse
import json
import os
import sys
import threading
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bid_portals
import license_server as ls

# Deliberately spans metro and rural: the difference between them is the
# product's most important property and any average that hides it is a lie.
LOCATIONS = [
    ("Aurora", "MO", 36.9709, -93.7180, 50),
    ("Springfield", "MO", 37.2090, -93.2923, 50),
    ("Jefferson City", "MO", 38.5767, -92.1735, 50),
    ("Kansas City", "MO", 39.0997, -94.5786, 50),
    ("Topeka", "KS", 39.0473, -95.6752, 50),
    ("Tulsa", "OK", 36.1540, -95.9928, 50),
    ("Little Rock", "AR", 34.7465, -92.2896, 50),
    ("Des Moines", "IA", 41.5868, -93.6250, 50),
]


def run_location(city, state, lat, lon, radius, max_towns):
    center = {"city": city, "state": state, "lat": lat, "lon": lon}
    pdb = bid_portals.load_directory()
    grouped, stats, coords = {}, {}, {}
    lock = threading.Lock()
    t0 = time.time()

    towns = bid_portals.towns_within_radius(pdb, lat, lon, radius)
    towns.sort(key=lambda t: ls._miles_between(lat, lon, t[2], t[3]))
    todo = [(city, state, lat, lon)] + towns[:max_towns]

    for tc, ts, tlat, tlon in todo:
        try:
            ls._run_known_portals(tc, ts, f"{tc}, {ts}", grouped, center,
                                  radius, {}, coords, lock, pdb,
                                  default_city=tc, town_coords=(tlat, tlon),
                                  stats=stats)
        except Exception as e:      # one town must never sink the run
            stats["town_error"] = stats.get("town_error", 0) + 1
            stats.setdefault("_errors", []).append(f"{tc}: {type(e).__name__}")

    bids = [b for v in grouped.values() for b in v]
    shown = [b for b in bids if ls._is_open_bid(b)]
    return {
        "location": f"{city}, {state}",
        "towns_read": len(todo),
        "seconds": round(time.time() - t0, 1),
        "placed": len(bids),
        "shown": len(shown),
        "with_deadline": sum(1 for b in shown if b.get("deadline")),
        "with_contact": sum(1 for b in shown if b.get("email") or b.get("phone")),
        "with_docs": sum(1 for b in shown if b.get("documents")),
        "with_value": sum(1 for b in shown if b.get("value")),
        "prebid_flagged": sum(1 for b in shown if b.get("prebid")),
        "funnel": {k: v for k, v in sorted(stats.items())
                   if not k.startswith("_")},
        "titles": [b.get("title", "")[:60] for b in shown[:4]],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--towns", type=int, default=6,
                    help="known towns to read per location (plus the centre)")
    ap.add_argument("--only", default="", help="substring filter on city")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    picks = [l for l in LOCATIONS if args.only.lower() in l[0].lower()]
    results, totals = [], Counter()
    for loc in picks:
        r = run_location(*loc, max_towns=args.towns)
        results.append(r)
        for k in ("placed", "shown", "with_deadline", "with_contact",
                  "with_docs", "with_value", "prebid_flagged", "towns_read"):
            totals[k] += r[k]
        for k, v in r["funnel"].items():
            totals["f_" + k] += v
        if not args.json:
            print(f"{r['location']:22s} {r['seconds']:5.1f}s  "
                  f"towns={r['towns_read']:2d}  placed={r['placed']:3d}  "
                  f"shown={r['shown']:3d}  "
                  f"deadline={r['with_deadline']:2d} contact={r['with_contact']:2d} "
                  f"docs={r['with_docs']:2d}")
            for t in r["titles"]:
                print(f"      · {t}")

    if args.json:
        print(json.dumps({"results": results, "totals": dict(totals)}, indent=2))
        return
    shown = max(totals["shown"], 1)
    print(f"\n{'-'*66}\nTOTALS over {len(results)} locations, "
          f"{totals['towns_read']} towns read")
    print(f"  placed {totals['placed']}   shown {totals['shown']}")
    print(f"  of shown: deadline {100*totals['with_deadline']//shown}%  "
          f"contact {100*totals['with_contact']//shown}%  "
          f"docs {100*totals['with_docs']//shown}%  "
          f"value {100*totals['with_value']//shown}%  "
          f"pre-bid {totals['prebid_flagged']}")
    print("\n  funnel:")
    for k, v in sorted(totals.items()):
        if k.startswith("f_"):
            print(f"    {k[2:]:28s} {v}")


if __name__ == "__main__":
    main()
