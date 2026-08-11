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
  - Baton Rouge, LA's "EBR Building Permits" has a dedicated
    permittype="Driveway Permit (R)" category, address, coordinates, and a
    contractor name (sometimes the property owner themselves, self-
    permitting a DIY/repair job -- treated as an open lead, not "taken",
    since a name alone with no trade match isn't a company). Verified live
    and current (latest permit issued within days of checking).
  - tools/discover_permit_sources.py's Socrata-catalog search surfaced
    several more candidates with plausible dedicated categories --
    Seattle's "Curb Cut" (data through 2021 only), Gainesville's "Driveway
    Apron" (through 2023), Dallas's "Paving (Sidewalk, Drive Approaches)"
    (2018 only), Somerville MA's "Curb Cut and Driveway" (through 2019),
    and Prince George's County MD's "RESIDENTIAL DRIVEWAY PERMIT" (portal
    reports a 2026 "latest" date, but only 3 records in the trailing 365
    days -- the feed has effectively stopped, that date is stale, not
    live). All five have a real dedicated category (the hard part a naive
    scrape usually gets wrong) but are dead or dying datasets -- checked by
    hand, not included. A field having a clean category value is necessary
    but not sufficient; recency has to be checked too, per-dataset, before
    anything goes live.

So: SOURCES is a per-city registry, each entry hand-verified against the
niche (not "any Socrata URL with a permits-shaped name") before being added
-- same discipline as bid_portals.py's seed list. Coverage starts narrow
and is meant to grow city by city, verified each time, not by guessing at
a schema and hoping it holds.
"""

import datetime
import json
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


def _guess_trade_from_name(name):
    """Baton Rouge's dataset has no dedicated trade field, only a contractor
    name -- this checks that name against the same keyword lists
    _classify_lead uses for an explicit trade field, so "ABC Concrete LLC"
    still correctly reads as taken rather than falling through to open just
    because there's no separate trade column to check. A guess, never a
    fact: an empty result here still falls through to _classify_lead's
    existing "named but no trade" handling."""
    low = (name or "").lower()
    for kw in TAKEN_TRADE_KEYWORDS:
        if kw in low:
            return kw
    for kw in BUILDER_TRADE_KEYWORDS:
        if kw in low:
            return kw
    return ""


def _baton_rouge_parser(row):
    contractor = (row.get("contractorname") or "").strip()
    owner = (row.get("ownername") or row.get("applicantname") or "").strip()
    # A permit where the homeowner lists themselves as their own contractor
    # (common for a DIY/self-permitted driveway repair) isn't "already has a
    # contractor" in the sense the card's contact chip implies -- showing
    # their own name back to them under a "Contractor" label reads as if
    # someone's already been hired. Leave it blank so it reads as the open
    # lead it actually is.
    if not contractor or contractor.upper() in ("N/A", "NONE") or contractor.lower() == owner.lower():
        contractor = ""
    return {
        "permit_id": row.get("permitid", ""),
        "address": row.get("streetaddress") or row.get("address", ""),
        "city": "Baton Rouge",
        "state": "LA",
        "zip": row.get("zip", ""),
        "permit_type": row.get("permittype", ""),
        "description": row.get("projectdescription", ""),
        "issued_date": (row.get("issueddate") or "")[:10],
        "status": "",
        "contractor_name": contractor,
        "contractor_trade": _guess_trade_from_name(contractor),
        "contractor_phone": "",
        "lat": _to_float(row.get("lat")),
        "lon": _to_float(row.get("long")),
        "url": "",
    }


PARSERS = {
    "austin": _austin_parser,
    "cambridge": _cambridge_parser,
    "baton_rouge": _baton_rouge_parser,
}

# ── Lead classification ──
# The single most important thing about one of these leads isn't the address,
# it's whether calling it is even useful. Real data check (Austin, live):
# of 100 recent Driveway/Sidewalks permits, 67% had contractor_trade=
# "General Contractor" and a homebuilder name (Highland Homes, Trophy
# Signature Homes, ...) -- new-subdivision construction where a GC already
# holds the job. That's NOT an open "nobody's hired anyone yet" lead, and the
# original "pitch as a subcontractor" label oversold it as one: a production
# homebuilder pouring dozens of driveways a year almost always already has a
# standing concrete subcontractor across the whole subdivision by the time a
# single lot's permit posts. Landing that work is a slow "get on their
# approved-vendor list" sales motion, not a live opportunity a cold call
# converts -- the label has to say that, not imply good odds on a call today.
BUILDER_TRADE_KEYWORDS = ("general contractor", "builder", "homebuilder", "home builder")
TAKEN_TRADE_KEYWORDS = ("concrete", "paving", "flatwork", "cement", "masonry")
LEAD_TYPE_LABELS = {
    "open": "Open Lead — no contractor listed",
    "builder": "Builder's Project — GC likely already has a concrete sub; "
               "a cold call is a long shot without an existing relationship",
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
# "center" is the city's own coordinates, stored rather than geocoded at
# request time. Callers need them to decide which sources are near enough to a
# search to be worth reading, and that decision shouldn't depend on a
# third-party geocoder being reachable — this list is short, static and
# hand-verified, so the coordinates belong with the rest of the entry.
SOURCES = {
    ("austin", "TX"): {
        "domain": "data.austintexas.gov",
        "dataset_id": "3syk-w9eu",
        "date_field": "issue_date",
        "where_extra": "permit_type_desc='Driveway / Sidewalks'",
        "parser": "austin",
        "days": 45,
        "center": (30.2672, -97.7431),
    },
    ("cambridge", "MA"): {
        "domain": "data.cambridgema.gov",
        "dataset_id": "q2hw-5t8j",
        "date_field": "applicant_submit_date",
        "where_extra": None,
        "parser": "cambridge",
        "days": 270,
        "center": (42.3736, -71.1097),
    },
    ("baton rouge", "LA"): {
        "domain": "data.brla.gov",
        "dataset_id": "7fq7-8j7r",
        "date_field": "issueddate",
        "where_extra": "permittype='Driveway Permit (R)'",
        "parser": "baton_rouge",
        # ~4/month recently -- a 45-day default would often come back with
        # 1-2 results or none; 90 days reliably returns several without
        # being so wide it surfaces work that's plausibly already finished.
        "days": 90,
        "center": (30.4515, -91.1871),
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
