#!/usr/bin/env python3
"""Rebuild data/gov_domains.csv from CISA's .gov registry.

The registry is the authoritative list of every .gov domain and who owns it.
We keep only local government — the bodies that actually let sidewalk, curb and
road work — which is roughly 12,700 of the ~16,500 entries.

    python3 tools/refresh_gov_domains.py

Worth re-running every few months; new towns register domains and a few change
hands. It rewrites the CSV in place, so `git diff` shows exactly what moved.
"""
import csv
import io
import os
import sys
import urllib.request

SOURCE = "https://raw.githubusercontent.com/cisagov/dotgov-data/main/current-full.csv"
# Federal, state, tribal and election domains don't let municipal concrete work.
KEEP_TYPES = {"City", "County", "Special district", "School district"}
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "gov_domains.csv")


def main():
    print(f"fetching {SOURCE}")
    with urllib.request.urlopen(SOURCE, timeout=120) as resp:
        text = resp.read().decode("utf-8", "ignore")

    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        if (row.get("Domain type") or "").strip() not in KEEP_TYPES:
            continue
        domain = (row.get("Domain name") or "").strip().lower()
        state = (row.get("State") or "").strip().upper()
        if not domain or not state:
            continue
        rows.append({
            "domain": domain,
            "type": (row.get("Domain type") or "").strip(),
            "org": (row.get("Organization name") or "").strip(),
            "city": (row.get("City") or "").strip(),
            "state": state,
        })

    if len(rows) < 5000:
        # A truncated download would otherwise silently shrink national coverage.
        sys.exit(f"refusing to write: only {len(rows)} rows, expected >5000")

    rows.sort(key=lambda r: (r["state"], r["city"].lower(), r["domain"]))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["domain", "type", "org", "city", "state"])
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} local-government domains to {OUT}")


if __name__ == "__main__":
    main()
