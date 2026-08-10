"""
gov_directory.py — every US local-government web domain, as shipped data
════════════════════════════════════════════════════════════════════════

The scanner's weakest link was never parsing; it was not knowing *where to
look*. Finding a town's bid page meant running search queries and hoping the
right page ranked — per query cost, per scan, forever, for an answer that never
changes.

It does not have to be discovered at all. CISA publishes the authoritative
registry of every .gov domain, including which organisation owns it and where
that organisation is. `data/gov_domains.csv` is that registry, filtered to
local government: cities, counties, special districts and school districts —
about 12,700 of them, covering every state and territory.

So instead of "search for Nixa's bid page", the scanner can now ask "what is
Nixa, Missouri's official domain?", get `nixamo.gov`, and try the handful of
URL shapes a bid page actually takes. A hit gets recorded in the portal
directory and is free from then on.

Counties matter here specifically. They let a lot of curb, road and drainage
work, and they were entirely absent from the scanner's world — nothing in the
seed list, and a county name doesn't geocode, so any bid naming one was
discarded. The registry lists 2,623 of them.

Source: https://github.com/cisagov/dotgov-data (public domain, US government).
Refresh by re-running tools/refresh_gov_domains.py.
"""

import csv
import os
import re
import threading

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "data", "gov_domains.csv")

_lock = threading.Lock()
_by_place = None   # (city_lower, STATE) -> [entry, ...]
_all = None

# The registry records a mailing city, which for a county is usually its seat.
# Recognising the county-shaped names lets a caller prefer or skip them.
_COUNTY_RE = re.compile(r"\bcount(?:y|ies)\b|\bparish\b|\bborough\b", re.I)


def _load():
    global _by_place, _all
    with _lock:
        if _by_place is not None:
            return
        by_place, everything = {}, []
        try:
            with open(_DATA, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    entry = {
                        "domain": (row.get("domain") or "").strip().lower(),
                        "type": (row.get("type") or "").strip(),
                        "org": (row.get("org") or "").strip(),
                        "city": (row.get("city") or "").strip(),
                        "state": (row.get("state") or "").strip().upper(),
                    }
                    if not entry["domain"] or not entry["state"]:
                        continue
                    everything.append(entry)
                    by_place.setdefault(
                        (entry["city"].lower(), entry["state"]), []).append(entry)
        except OSError:
            # Missing data file degrades this to "no help", never to a crash.
            by_place, everything = {}, []
        _by_place, _all = by_place, everything


def loaded_count():
    _load()
    return len(_all)


# Ordered so the most likely owner of a bid page comes first. A city's own site
# is the best guess for work inside that city; the county is the next most
# likely to be letting road and curb work nearby.
_TYPE_ORDER = {"City": 0, "County": 1, "Special district": 2, "School district": 3}


def _squash(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def lookup(city, state):
    """Every local-government domain registered to this place, best first.

    Within a type, an entry that actually names the town wins. The registry
    records a mailing city, so a small neighbour can be registered under a
    bigger town's name — Fremont Hills' domain lists Nixa as its city — and
    without this the town you asked about could rank below its neighbour.
    """
    _load()
    key = (str(city or "").strip().lower(), str(state or "").strip().upper())
    if not key[0] or not key[1]:
        return []
    wanted = _squash(key[0])

    def rank(entry):
        names = _squash(entry["org"]) + _squash(entry["domain"])
        return (_TYPE_ORDER.get(entry["type"], 9),
                0 if wanted and wanted in names else 1,
                entry["domain"])

    return sorted(_by_place.get(key, []), key=rank)


def domains_for(city, state, include_county=True):
    """Just the hostnames for a place, best first."""
    out = []
    for entry in lookup(city, state):
        if not include_county and entry["type"] == "County":
            continue
        out.append(entry["domain"])
    return out


def is_county(entry):
    return entry.get("type") == "County" or bool(_COUNTY_RE.search(entry.get("org", "")))
