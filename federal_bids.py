#!/usr/bin/env python3
"""Federal contract opportunities from SAM.gov.

Why this exists: the portal directory is 4,428 entries and 95% of them are
plain cities. The federal government is the single largest buyer of concrete
work in the country and the app could not see one job of it. A survey of the
active notices found roughly 1,200 open solicitations nationwide in the
concrete trades at any moment -- Gateway Arch NP sidewalk leveling, VA
hospital parking garages, ADA ramp packages on military posts -- none of
which appear on any city's Bids.aspx.

What makes this a better source than a municipal page, not just another one:

  * It is structured. Place of performance arrives as city/state/zip/street,
    the contact as name/email/phone, and the trade as a NAICS code. Nothing
    has to be guessed out of prose, so no extraction call is spent and the
    contact gap -- the app's second-biggest quality problem -- does not exist
    here at all.
  * It is national and uniform. One reader covers every state, unlike the
    50 DOT pages of which 3 turned out usable.
  * NAICS decides relevance far better than words do. 238110 IS "poured
    concrete contractor". No amount of title parsing is that certain.

This module is deliberately pure: it builds URLs and parses payloads. The
fetching belongs to the caller, same discipline as bid_sources.py, so every
function here is testable without a network.

ACCESS
------
Two transports, same data:

  official  api.data.gov/sam/opportunities/v2 -- documented, supported,
            needs a free API key (api.data.gov/signup, about two minutes).
            Used automatically whenever SAM_API_KEY is set. This is the one
            to prefer: it is a published contract, so it will not change
            shape without notice.

  public    sam.gov's own unauthenticated search endpoint, which serves the
            public search page at sam.gov/search. Used when no key is set,
            so the feature works on day one.

Both serve the same public-domain data. Federal solicitations are required
to be publicly posted; there is no login, no paywall and no robots rule
against either path (sam.gov/robots.txt disallows /search/, which is the
human page, not /api/). Requests identify themselves honestly and a scan
makes a handful of them.
"""
import json
import re
import urllib.parse

# ── What counts as our trade ────────────────────────────────────────────────
# NAICS beats keywords here and it is worth being precise about why: a title
# like "Repair Building 174" says nothing, but its NAICS says 238110 and that
# is a poured-concrete contract. Codes, with the count of active notices
# carrying each when this was measured:
#
#   237310  Highway, Street, and Bridge Construction          342
#   238910  Site Preparation Contractors                       83
#   238190  Other Foundation/Structure/Building Exterior       32
#   238110  Poured Concrete Foundation and Structure           24
#   238140  Masonry Contractors                                21
#
# 237990 (Other Heavy and Civil Engineering) is deliberately NOT here. It
# carries 727 active notices, but the class is a grab bag -- dredging,
# pipelines, marine construction -- so it would roughly double the volume
# while diluting it. The title filter would then be doing the real work,
# which is exactly the arrangement NAICS is supposed to replace.
CONCRETE_NAICS = ("237310", "238910", "238190", "238110", "238140")

# 237990's exclusion was checked rather than assumed. Across six states it
# carried 17 active notices; four passed a title filter and all four were
# wrong -- a boat ramp, two stormwater spill gates and a drainage district.
# The remainder were dams, levees, powerhouses and cemetery expansions.

# Product Service Codes catch what NAICS misses: a notice can be filed under
# a building code while the work is paving. Only Z2PZ is used -- "repair or
# alteration of highways, roads, streets, bridges" -- because it is the only
# one that paid for itself. Across the same six states it found two notices
# NAICS had not, one of them "FY25 Concrete Project - National Animal Disease
# Center, Ames" which is squarely our trade.
#
# Y1PZ and Z2AZ were measured and rejected: 8 and 7 notices respectively, not
# one of them concrete work. Gravesite expansions, a boat ramp, switchgear,
# restroom renovations, 120v outlets.
#
# PSC is broader than NAICS, so unlike a NAICS hit a PSC hit is NOT taken on
# trust -- the caller runs the title past the normal relevance filter.
CONCRETE_PSC = ("Z2PZ",)

OFFICIAL_BASE = "https://api.data.gov/sam/opportunities/v2/search"
PUBLIC_SEARCH = "https://sam.gov/api/prod/sgs/v1/search/"
PUBLIC_DETAIL = "https://sam.gov/api/prod/opps/v2/opportunities/"

# The human-readable posting. Every bid links back here rather than to an
# API URL -- the contractor needs the page with the attachments on it.
VIEW_URL = "https://sam.gov/opp/%s/view"


def search_url(naics=None, state=None, api_key=None, size=25, page=0,
               psc=None):
    """URL for one page of active opportunities in one NAICS code.

    `state` is the place of performance, not the contracting office. That
    distinction matters: a contract let by an office in Washington DC for
    work at a fort in Missouri is a Missouri job, and filtering on the office
    would have hidden it.
    """
    if api_key:
        q = {"api_key": api_key, "limit": size, "offset": page * size}
        if naics:
            q["ncode"] = naics
        if psc:
            q["ccode"] = psc
        if state:
            q["state"] = state
        return OFFICIAL_BASE + "?" + urllib.parse.urlencode(q)
    q = {"index": "opp", "is_active": "true",
         "size": size, "page": page, "sort": "-modifiedDate"}
    if naics:
        q["naics"] = naics
    if psc:
        q["psc"] = psc
    # pop_state, not state. "state" is a recognised parameter that matches
    # nothing useful, and an unknown parameter is ignored silently rather
    # than refused -- so `zip=` and `placeOfPerformanceState=` both returned
    # the full unfiltered count while looking like they had worked.
    if state:
        q["pop_state"] = state
    return PUBLIC_SEARCH + "?" + urllib.parse.urlencode(q)


def detail_url(opp_id):
    """URL for the full record behind a search hit.

    The search index carries no place of performance and no contact -- both
    live only here, and both are the reason this source is worth having. One
    extra fetch per candidate is the price.
    """
    return PUBLIC_DETAIL + str(opp_id or "").strip()


def _text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_search(payload):
    """Rows out of either transport's search response.

    The two shapes differ (`_embedded.results` vs `opportunitiesData`), so
    both are read here rather than making the caller care which is in use.
    """
    data = payload if isinstance(payload, dict) else _loads(payload)
    if not isinstance(data, dict):
        return []
    rows = []
    for r in (data.get("_embedded") or {}).get("results") or []:
        if not isinstance(r, dict):
            continue
        rows.append({
            "id": r.get("_id"),
            "title": _text(r.get("title")),
            "solicitation": _text(r.get("solicitationNumber")),
            "deadline": _text(r.get("responseDateActual")
                              or r.get("responseDate")),
            "active": bool(r.get("isActive")) and not r.get("isCanceled"),
        })
    for r in data.get("opportunitiesData") or []:      # official transport
        if not isinstance(r, dict):
            continue
        rows.append({
            "id": r.get("noticeId"),
            "title": _text(r.get("title")),
            "solicitation": _text(r.get("solicitationNumber")),
            "deadline": _text(r.get("responseDeadLine")),
            "active": str(r.get("active", "Yes")).strip().lower() != "no",
            # The official payload already carries these, so a detail fetch
            # can be skipped entirely when it is the transport in use.
            "_inline": r,
        })
    return rows


def _loads(raw):
    try:
        return json.loads(raw)
    except Exception:
        return None


def _first_contact(entries):
    """The primary point of contact, falling back to any at all.

    A name on its own is not a way to reach anybody -- the same rule the
    municipal enricher applies -- so an entry with neither email nor phone
    is skipped rather than reported as a contact.
    """
    best = None
    for c in entries or []:
        if not isinstance(c, dict):
            continue
        if not (c.get("email") or c.get("phone")):
            continue
        if str(c.get("type") or "").lower() == "primary":
            return c
        best = best or c
    return best


def parse_detail(payload):
    """Place of performance, contact and trade out of a full record."""
    data = payload if isinstance(payload, dict) else _loads(payload)
    if not isinstance(data, dict):
        return {}
    d = data.get("data2") if isinstance(data.get("data2"), dict) else data
    pop = d.get("placeOfPerformance") or {}
    if not isinstance(pop, dict):
        pop = {}
    city = ((pop.get("city") or {}) if isinstance(pop.get("city"), dict)
            else {}).get("name") or pop.get("city")
    state = ((pop.get("state") or {}) if isinstance(pop.get("state"), dict)
             else {}).get("code") or pop.get("state")
    contact = _first_contact(d.get("pointOfContact")) or {}
    naics = ""
    for n in d.get("naics") or []:
        if isinstance(n, dict) and n.get("code"):
            code = n["code"]
            naics = str(code[0] if isinstance(code, list) and code else code)
            break
    sol = d.get("solicitation") or {}
    deadlines = sol.get("deadlines") if isinstance(sol, dict) else None
    return {
        "city": _text(city),
        "state": _text(state).upper()[:2],
        "zip": _text(pop.get("zip")),
        "street": _text(pop.get("streetAddress")),
        "contact": _text(contact.get("fullName")),
        "email": _text(contact.get("email")),
        "phone": _text(contact.get("phone")),
        "naics": naics,
        "set_aside": _text(sol.get("setAside") if isinstance(sol, dict) else ""),
        "deadline": _text((deadlines or {}).get("response")
                          if isinstance(deadlines, dict) else ""),
    }


# A contracting officer's name is not "Frank, Matthew" to a contractor
# picking up the phone.
def _human_name(name):
    n = _text(name)
    if "," in n and len(n.split(",")) == 2:
        last, first = (p.strip() for p in n.split(","))
        if last and first:
            return "%s %s" % (first, last)
    return n


def to_bid(row, detail, source="SAM.gov"):
    """One normalised bid, or None if the record cannot be placed.

    Returns the same shape the rest of the scan speaks, so nothing
    downstream needs to know a bid came from here -- except `source`, which
    the card shows so a contractor can tell a federal job from a city one.
    Those are bid very differently and conflating them would be unkind.
    """
    detail = detail or {}
    if not row or not row.get("id"):
        return None
    if not detail.get("city") or not detail.get("state"):
        return None            # unplaceable; a federal job with no location
    bid = {
        "title": row.get("title") or "",
        "url": VIEW_URL % row["id"],
        "deadline": detail.get("deadline") or row.get("deadline") or "",
        "source": source,
        "city": detail["city"],
        "state": detail["state"],
    }
    if detail.get("contact"):
        bid["contact"] = _human_name(detail["contact"])
    for k in ("email", "phone"):
        if detail.get(k):
            bid[k] = detail[k]
    scope = []
    if row.get("solicitation"):
        scope.append("Solicitation %s" % row["solicitation"])
    if detail.get("set_aside"):
        # Worth surfacing: a set-aside is the difference between a job a
        # small contractor can win and one they cannot.
        scope.append("%s set-aside" % detail["set_aside"])
    if detail.get("street"):
        scope.append(detail["street"])
    if scope:
        bid["scope"] = " · ".join(scope)
    return bid
