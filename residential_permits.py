"""
residential_permits.py — free, structured residential lead data via city
open-data (Socrata) APIs
══════════════════════════════════════════════════════════════════════════
Different animal from bid_portals.py's bid-portal directory: there's no
universal "give me sidewalk/driveway permits" API, and unlike government bid
solicitations, permit data has no consistent nationwide schema. Many cities
publish permits as open datasets on Socrata (the SODA API), but field names
AND permit-type taxonomy differ city to city -- confirmed by hand before
writing any of this:

  - Austin's "Issued Construction Permits" dataset has a clean, dedicated
    permit_type_desc value of "Driveway / Sidewalks" (permit_class_mapped=
    "Residential"), plus address, lat/lon, and a named contractor + phone.
    Low noise, high signal -- a great fit, verified live.
  - Fort Worth's dataset (same Socrata platform, "BLDS" data standard) has
    NO equivalent category. Text-matching "sidewalk"/"driveway" against its
    free-text description field mostly surfaced unrelated plumbing/sewer
    repairs that just happen to mention "driveway" as a location (e.g.
    "S.S. Leak under driveway") -- the exact same false-positive problem
    just fixed on the bid side (see _ai_extract's tightened prompt). Not
    included for that reason.
  - Kansas City's public dataset (data.kcmo.org, 6h9j-mu65) has no address,
    no coordinates, no permit-type field at all -- just a permit number and
    a free-text comment. Not usable for a location-based, actionable lead.

So: SOURCES is a per-city registry, each entry hand-verified against the
niche (not "any Socrata URL with a permits-shaped name") before being added
-- same discipline as bid_portals.py's seed list. Coverage starts narrow
(Austin only) and is meant to grow city by city, verified each time, not by
guessing at a schema and hoping it holds.
"""

import datetime
import json
import re
import urllib.parse
import urllib.request

UA = {"User-Agent": "BidCallerPro/1.0 (residential concrete lead finder)"}


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _austin_parser(row):
    link = row.get("link") or {}
    loc = row.get("location") or {}
    return {
        "permit_id": row.get("permit_number", ""),
        "address": row.get("permit_location") or row.get("original_address1", ""),
        "city": "Austin",
        "state": "TX",
        # Some permits (new subdivisions especially) aren't geocoded by the
        # source yet -- zip is a fallback the caller can use for a coarser
        # radius check when lat/lon is missing, rather than dropping the
        # lead or showing it unfiltered.
        "zip": row.get("original_zip", ""),
        "permit_type": row.get("permit_type_desc", ""),
        "description": row.get("description", ""),
        "issued_date": (row.get("issue_date") or "")[:10],
        "status": row.get("status_current", ""),
        "contractor_name": row.get("contractor_company_name") or row.get("contractor_full_name") or "",
        "contractor_trade": row.get("contractor_trade", ""),
        "contractor_phone": row.get("contractor_phone", ""),
        "lat": _to_float(row.get("latitude") or loc.get("latitude")),
        "lon": _to_float(row.get("longitude") or loc.get("longitude")),
        "url": link.get("url", ""),
    }


PARSERS = {
    "austin": _austin_parser,
}

# Each entry hand-verified: real address/contact data, a permit-type value
# specific enough that filtering on it (not a free-text description search)
# won't pull in unrelated trades. Key is (city.lower(), state.upper()).
SOURCES = {
    ("austin", "TX"): {
        "domain": "data.austintexas.gov",
        "dataset_id": "3syk-w9eu",
        "date_field": "issue_date",
        "where_extra": "permit_type_desc='Driveway / Sidewalks'",
        "parser": "austin",
    },
}


def has_source(city, state):
    return (
        (city or "").strip().lower(),
        (state or "").strip().upper(),
    ) in SOURCES


def fetch_leads(city, state, days=45, limit=100):
    """Return recent residential driveway/sidewalk permit leads for a city,
    or [] if no source is configured there yet (check has_source() to tell
    that apart from "configured but genuinely found nothing recent")."""
    src = SOURCES.get(((city or "").strip().lower(), (state or "").strip().upper()))
    if not src:
        return []
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    where = f"{src['date_field']} >= '{since}T00:00:00.000'"
    if src.get("where_extra"):
        where += f" AND {src['where_extra']}"
    params = {
        "$where": where,
        "$limit": str(limit),
        "$order": f"{src['date_field']} DESC",
    }
    url = f"https://{src['domain']}/resource/{src['dataset_id']}.json?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={**UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
    except Exception as ex:
        print(f"[residential_permits] fetch error ({city}, {state}): {ex}", flush=True)
        return []
    if not isinstance(rows, list):
        return []
    parser = PARSERS.get(src["parser"])
    if not parser:
        return []
    return [parser(r) for r in rows if isinstance(r, dict)]
