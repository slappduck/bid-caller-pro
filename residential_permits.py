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
    A second KC dataset (ntw8-aacc, same BLDS standard as Fort Worth) DOES
    have address/coordinates, but its permit-type categories top out at
    generic buckets like "Site Improvement" -- no dedicated driveway/
    sidewalk/curb category, so same false-positive risk as Fort Worth.
  - St. Louis's open-data portal only offers interactive dashboards for
    building permits -- no CSV/JSON/API export at all. Not automatable.
  - Springfield, MO has no public open-data portal for permits at all.
  - Cambridge, MA's "Curb Cut Permits" dataset is single-purpose -- every
    row IS a curb-cut/driveway-approach permit, so there's no false-positive
    risk from filtering at all. Verified live: real addresses, coordinates,
    applicant names. Much lower volume than Austin (a small city, roughly
    1-2 permits/month) -- needs a longer lookback window (see "days" below)
    or a 45-day default would come back empty most of the time.

So: SOURCES is a per-city registry, each entry hand-verified against the
niche (not "any Socrata URL with a permits-shaped name") before being added
-- same discipline as bid_portals.py's seed list. Coverage starts narrow
and is meant to grow city by city, verified each time, not by guessing at
a schema and hoping it holds.
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


def _cambridge_parser(row):
    width = row.get("driveway_width")
    desc = f"Curb cut / driveway approach, {width} ft wide" if width else "Curb cut permit"
    return {
        "permit_id": row.get("id", ""),
        "address": row.get("full_address", ""),
        "city": "Cambridge",
        "state": "MA",
        "zip": "",
        "permit_type": row.get("permit_type", "Curb Cut"),
        "description": desc,
        "issued_date": (row.get("applicant_submit_date") or "")[:10],
        "status": row.get("status", ""),
        # No phone/company field in this dataset -- applicant_name (often
        # the property owner, not always a contractor) is the only contact.
        "contractor_name": row.get("applicant_name") or "",
        "contractor_trade": "",
        "contractor_phone": "",
        "lat": _to_float(row.get("latitude")),
        "lon": _to_float(row.get("longitude")),
        "url": "",
    }


PARSERS = {
    "austin": _austin_parser,
    "cambridge": _cambridge_parser,
}

# ── Lead classification ──
# The single most important thing about one of these leads isn't the address,
# it's whether calling it is even useful. Real data check (Austin): every
# sampled permit had contractor_trade="General Contractor" and a homebuilder
# name (Highland Homes, Trophy Signature Homes, ...) -- these are new-
# subdivision construction permits where a GC already holds the job. That's
# NOT an open "nobody's hired anyone yet" lead; a GC almost never pours their
# own concrete, so the real play is pitching to become THEIR subcontractor,
# not cold-calling for this one driveway. Without labeling that distinction,
# a user could easily burn calls on already-assigned jobs thinking they're
# fresh leads.
BUILDER_TRADE_KEYWORDS = ("general contractor", "builder", "homebuilder", "home builder")
TAKEN_TRADE_KEYWORDS = ("concrete", "paving", "flatwork", "cement", "masonry")
LEAD_TYPE_LABELS = {
    "open": "Open Lead — no contractor listed",
    "builder": "Builder Lead — pitch as a subcontractor",
    "taken": "Concrete Sub Already Listed",
    "unknown": "Contact Listed",
}
_LEAD_TYPE_PRIORITY = {"open": 0, "builder": 1, "unknown": 2, "taken": 3}


def _classify_lead(contractor_trade, contractor_name):
    trade = (contractor_trade or "").strip().lower()
    name = (contractor_name or "").strip()
    if not trade and not name:
        return "open"
    if any(k in trade for k in TAKEN_TRADE_KEYWORDS):
        return "taken"
    if any(k in trade for k in BUILDER_TRADE_KEYWORDS):
        return "builder"
    if not trade and name:
        # A named applicant with no trade at all reads as an individual/
        # owner permit rather than a company -- likely still open.
        return "open"
    return "unknown"

# Each entry hand-verified: real address/contact data, a permit-type value
# specific enough that filtering on it (not a free-text description search)
# won't pull in unrelated trades. Key is (city.lower(), state.upper()).
# "days": how far back to look by default -- a low-volume source (a small
# city with a couple of permits a month) needs a much longer window than a
# high-volume one to reliably return anything at all.
SOURCES = {
    ("austin", "TX"): {
        "domain": "data.austintexas.gov",
        "dataset_id": "3syk-w9eu",
        "date_field": "issue_date",
        "where_extra": "permit_type_desc='Driveway / Sidewalks'",
        "parser": "austin",
        "days": 45,
    },
    ("cambridge", "MA"): {
        "domain": "data.cambridgema.gov",
        "dataset_id": "q2hw-5t8j",
        "date_field": "applicant_submit_date",
        "where_extra": None,
        "parser": "cambridge",
        "days": 270,
    },
}


def has_source(city, state):
    return (
        (city or "").strip().lower(),
        (state or "").strip().upper(),
    ) in SOURCES


def fetch_leads(city, state, days=None, limit=100):
    """Return recent residential driveway/sidewalk permit leads for a city,
    or [] if no source is configured there yet (check has_source() to tell
    that apart from "configured but genuinely found nothing recent").
    days=None uses the source's own configured lookback window -- pass an
    explicit value only to override it."""
    src = SOURCES.get(((city or "").strip().lower(), (state or "").strip().upper()))
    if not src:
        return []
    if days is None:
        days = src.get("days", 45)
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
    leads = [parser(r) for r in rows if isinstance(r, dict)]
    for lead in leads:
        lead["lead_type"] = _classify_lead(lead.get("contractor_trade"), lead.get("contractor_name"))
        lead["lead_type_label"] = LEAD_TYPE_LABELS[lead["lead_type"]]
    # Stable sort: most-actionable type first, freshest-within-type second
    # (the query already sorted by date DESC, and Python's sort is stable).
    leads.sort(key=lambda l: _LEAD_TYPE_PRIORITY.get(l["lead_type"], 2))
    return leads
