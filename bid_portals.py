"""
bid_portals.py — persistent directory of known bid-platform URLs, per city/state
══════════════════════════════════════════════════════════════════════════════
Problem this solves: /scan's "local" search used to be pure live web search
(DuckDuckGo/Tavily) re-run from scratch on every single scan. That's slow,
costs an OpenAI call per page every time, and finds nothing that doesn't
happen to rank well in that day's search results.

This module is a small, growing database of "for city X, its bid page is at
URL Y" — scraped directly instead of re-discovered. It starts seeded with a
handful of known-good URLs and grows over time: whenever live search finds a
real per-agency bid page (not a generic aggregator listing), the caller can
record it here so the next scan of that city skips the search step entirely.

Storage mirrors license_server.py's license-db pattern: Upstash Redis via its
REST API when configured (survives Render restarts), else a local JSON file
(fine for dev, lost on redeploy without Upstash).

Callers load the directory once per request, pass the same dict through
get_portals/learn_portal/record_result, and save once at the end — same
pattern as license_server.py's `cdb = _cache()` / `_save_cache(cdb)`.
"""

import csv
import os
import datetime
import urllib.request
import urllib.parse

import kv_backend

UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
_DIR_KEY = "bidcaller:portal_directory"
_LOCAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portal_directory.json")

MAX_FAIL = 5  # consecutive no-content results before we stop trusting an entry

# Generic aggregator/search domains: URLs here are dynamic search-result pages,
# not a stable "this is city X's bid page" URL, so they're never learned as a
# portal entry even when they show up in scan results.
AGGREGATOR_DOMAINS = {
    "bidnetdirect.com", "demandstar.com", "publicpurchase.com", "planetbids.com",
    "questcdn.com", "opengov.com", "bonfirehub.com", "bidexpress.com",
    "bidsearch.com", "missouribuys.mo.gov", "sam.gov", "network.procore.com",
    "google.com", "bing.com", "duckduckgo.com",
}

# Known-good URLs already in production use (regional_printer.py's default
# scan targets) — safe to seed since they're verified real bid pages.
# platform matters now, not just as a label: "civicplus" entries are read by
# bid_sources' structured parser instead of being scraped and handed to the AI.
#
# AgendaCenter is the council-MEETINGS module, not the bids one. Two of these
# entries pointed there, so those cities were being scanned for bids on a page
# that never contains any. Bids.aspx is the right module.
SEED_PORTALS = {
    ("aurora", "MO"): [{"url": "https://www.aurora-cityhall.org/Bids.aspx", "platform": "civicplus"},
                       {"url": "https://www.aurora-cityhall.org/AgendaCenter", "platform": "custom"}],
    ("springfield", "MO"): [{"url": "https://www.springfieldmo.gov/Bids.aspx", "platform": "civicplus"}],
    ("joplin", "MO"): [{"url": "https://www.joplinmo.org/Bids.aspx", "platform": "civicplus"}],
    ("republic", "MO"): [{"url": "https://www.republicmo.com/Bids.aspx", "platform": "civicplus"}],
    ("ozark", "MO"): [{"url": "https://ozarkmissouri.com/Bids.aspx", "platform": "civicplus"}],
}

# tools/discover_bid_portals.py's national crawl -- thousands of verified bid
# pages, one row per hit, city and county alike. Same trust level as
# SEED_PORTALS (each was structurally verified, not just "the URL returned
# 200" -- see that script's _looks_like_a_bid_page), just far too many to
# write out by hand. Parsed once per process and cached: this file has
# thousands of rows, and _seed() below runs on every load_directory() call.
_NATIONAL_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "bid_portal_directory.csv")
_national_seeds_cache = None

# Governments that are NOT on a .gov domain, found via Wikidata's official-website
# property -- Lee's Summit is lees-summit.mo.us, Blue Springs is
# bluespringsgov.com. The national crawl is built from the CISA .gov registry, so
# it is structurally blind to every one of these.
#
# Kept in its own file rather than merged into bid_portal_directory.csv because
# tools/discover_bid_portals.py rewrites that file wholesale on every run and
# would silently delete them. Same reason bid_portal_coords.csv is separate.
#
# Each row passed two independent checks in tools/verify_wikidata_candidates.py:
# the domain is provably the named town's (name in the domain, or the town named
# on its homepage), and it serves a page that reads like a bid page. The
# ownership check matters -- Wikidata is crowd-sourced and 20 candidates pointed
# at a website belonging to some other town entirely.
_WIKIDATA_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "wikidata_portals.csv")
_wikidata_seeds_cache = None


def _rows_to_seeds(path):
    """(city, state) -> [{url, platform}] for every 'found' row in a portal CSV."""
    seeds = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("status") != "found" or not row.get("bid_url"):
                    continue
                key = ((row.get("city") or "").strip(), (row.get("state") or "").strip())
                if not key[0] or not key[1]:
                    continue
                seeds.setdefault(key, []).append({
                    "url": row["bid_url"], "platform": row.get("platform") or "custom",
                })
    except OSError:
        pass  # missing file degrades to "no seeds from here", never a crash
    return seeds


def _wikidata_seeds():
    global _wikidata_seeds_cache
    if _wikidata_seeds_cache is None:
        _wikidata_seeds_cache = _rows_to_seeds(_WIKIDATA_CSV)
    return _wikidata_seeds_cache


def _national_seeds():
    global _national_seeds_cache
    if _national_seeds_cache is not None:
        return _national_seeds_cache
    seeds = {}
    try:
        with open(_NATIONAL_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("status") != "found" or not row.get("bid_url"):
                    continue
                key = ((row.get("city") or "").strip(), (row.get("state") or "").strip())
                if not key[0] or not key[1]:
                    continue
                seeds.setdefault(key, []).append({
                    "url": row["bid_url"], "platform": row.get("platform") or "custom",
                })
    except OSError:
        pass  # missing file degrades to "no national seeds", never a crash
    _national_seeds_cache = seeds
    return seeds


# ── Coordinates, for radius-based lookup ──
# A wide-radius scan used to only ever search the exact town typed plus a
# handful of geographically-guessed "anchor" points (license_server.py's
# _nearby_anchor_towns), capped at 6 regardless of how large the radius
# actually was -- a 125mi scan covers ~49,000 sq mi, and 7 sample points is a
# real recall gap. This is the fix: tools/geocode_bid_portals.py pre-geocodes
# every known-portal town offline (so /scan does arithmetic against already-
# known coordinates instead of a live geocode call per candidate on every
# request), and towns_within_radius below answers "which of the towns I
# already have a real bid page for fall inside this radius" -- cheaply,
# reusing pages this codebase already found for free, not new search credits.
_COORDS_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "bid_portal_coords.csv")
_coords_cache = None


def _coords():
    global _coords_cache
    if _coords_cache is not None:
        return _coords_cache
    coords = {}
    seen = set()
    try:
        with open(_COORDS_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                city, state = (row.get("city") or "").strip(), (row.get("state") or "").strip().upper()
                try:
                    lat, lon = float(row["lat"]), float(row["lon"])
                except (KeyError, ValueError, TypeError):
                    continue
                if not (city and state):
                    continue
                # Keyed case-insensitively: portal lookups already normalise
                # case (_key), so "Springfield" and "springfield" are one
                # town -- letting both in would have the scanner fetch and
                # search it twice. First row wins, and the tool writes the
                # registry's own casing before any lowercase seed name.
                if (city.lower(), state) in seen:
                    continue
                seen.add((city.lower(), state))
                coords[(city, state)] = (lat, lon)
    except OSError:
        pass  # not geocoded yet -- towns_within_radius just finds nothing, not a crash
    _coords_cache = coords
    return coords


_towns_by_state_cache = None


def towns_by_state():
    """{STATE: {lowercased town name: canonical name}} for every geocoded town."""
    global _towns_by_state_cache
    if _towns_by_state_cache is None:
        out = {}
        for (city, state) in _coords():
            out.setdefault(state, {})[city.lower()] = city
        _towns_by_state_cache = out
    return _towns_by_state_cache


def snap_city_name(city, state):
    """Correct a near-miss town name against the towns we actually know.

    The extraction model reads city names off page text and occasionally
    drops a character -- a real scan filed a Missouri bid under "Ashlan".
    That town does not exist, so it cannot geocode, which means radius search
    never sees the bid at all, and it never groups with the rest of Ashland's
    work.

    Deliberately narrow. It only fires when the name is NOT already a town we
    know, and exactly one known town in that same state is a single edit away.
    Two candidates, or a name that is already valid, are left alone: silently
    relocating a bid to the wrong town would be far worse than failing to
    place one.
    """
    name = (city or "").strip()
    st = (state or "").strip().upper()
    if not name or not st:
        return city
    known = towns_by_state().get(st)
    if not known or name.lower() in known:
        return city  # already a town we know -- never second-guess it
    matches = [canon for low, canon in known.items()
               if _within_one_edit(name.lower(), low)]
    return matches[0] if len(matches) == 1 else city


def _within_one_edit(a, b):
    """True if a and b differ by at most one insertion, deletion or swap."""
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    short, long_ = (a, b) if la < lb else (b, a)
    i = j = 0
    skipped = False
    while i < len(short) and j < len(long_):
        if short[i] == long_[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            j += 1
    return True


_domain_town_cache = None


def _domain_town_index():
    """host -> (city, state) for every agency whose domain we know.

    Built from the directory files rather than the seed maps because those
    are keyed by town, and the question here runs the other way: a search
    engine handed us a URL, whose town is it?
    """
    global _domain_town_cache
    if _domain_town_cache is not None:
        return _domain_town_cache
    idx = {}
    for path in (_NATIONAL_CSV, _WIKIDATA_CSV):
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    host = (row.get("domain") or "").strip().lower()
                    city = (row.get("city") or "").strip()
                    state = (row.get("state") or "").strip().upper()
                    if not (host and city and state):
                        continue
                    if host.startswith("www."):
                        host = host[4:]
                    idx.setdefault(host, (city, state))
        except OSError:
            continue
    _domain_town_cache = idx
    return idx


def town_for_url(url):
    """(city, state) for a URL on an agency domain we know, else None.

    Lets a search result be placed before it is fetched: a Missouri scan that
    gets back a Colorado city's page can skip it instead of paying a fetch
    and an extraction to discover the distance afterwards.
    """
    try:
        host = (urllib.parse.urlparse(
            url if "//" in str(url) else "//" + str(url)).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    return _domain_town_index().get(host)


def coords_for_town(city, state):
    """Known coordinates for a town, or None."""
    if not city or not state:
        return None
    want = (str(city).strip().lower(), str(state).strip().upper())
    for (c, st), pt in _coords().items():
        if (c.lower(), st) == want:
            return pt
    return None


def towns_within_radius(directory, center_lat, center_lon, radius, exclude=()):
    """Every town in the known-portal directory (seed + national crawl +
    learned) with a real bid page AND known coordinates, within `radius`
    miles of (center_lat, center_lon). `exclude` is a set of (city_lower,
    state) tuples already covered elsewhere (the center town itself, the
    sampled anchor towns) so this only adds NEW coverage.

    Returns (city, state, lat, lon) tuples, same shape as
    license_server._nearby_anchor_towns, so the caller can treat them
    identically -- just read the known page directly, no search queries."""
    from math import asin, cos, radians, sin, sqrt
    R = 3958.8

    def miles(lat1, lon1, lat2, lon2):
        p1, p2 = radians(lat1), radians(lat2)
        dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
        a = sin(dlat / 2) ** 2 + cos(p1) * cos(p2) * sin(dlon / 2) ** 2
        return 2 * R * asin(sqrt(a))

    out = []
    for (city, state), (lat, lon) in _coords().items():
        if (city.lower(), state) in exclude:
            continue
        if not get_portals(directory, city, state):
            continue  # geocoded but no longer a trusted entry (aged out via MAX_FAIL)
        if miles(center_lat, center_lon, lat, lon) <= radius:
            out.append((city, state, lat, lon))
    return out


def load_directory():
    """Load the directory, seeded with known-good defaults for any key not
    already present. Storage lives in kv_backend — this is the data that makes
    the scanner improve scan over scan, so it has to outlive a restart."""
    directory = kv_backend.get(_DIR_KEY, None)
    if directory is None:
        directory = {}
    _seed(directory)
    return directory


def save_directory(directory):
    """Persist the directory. Fails soft: losing a write costs the learning
    from one scan, never the scan itself."""
    kv_backend.set(_DIR_KEY, directory)


def _key(city, state):
    return f"{(city or '').strip().lower()}|{(state or '').strip().upper()}"


def _seed(directory):
    today = datetime.date.today().isoformat()
    for (city, state), entries in SEED_PORTALS.items():
        k = _key(city, state)
        if k not in directory:
            directory[k] = [
                {**e, "source": "seed", "added": today, "last_ok": None,
                 "last_checked": None, "fail_count": 0}
                for e in entries
            ]
    # Hand-verified SEED_PORTALS entries above always win for the same city --
    # this only fills in cities the hardcoded list never covered.
    for (city, state), entries in _national_seeds().items():
        k = _key(city, state)
        if k not in directory:
            directory[k] = [
                {**e, "source": "national_crawl", "added": today, "last_ok": None,
                 "last_checked": None, "fail_count": 0}
                for e in entries
            ]
    # Last, so a .gov page already known for a town keeps precedence: these are
    # the towns neither list could ever have reached.
    for (city, state), entries in _wikidata_seeds().items():
        k = _key(city, state)
        if k not in directory:
            directory[k] = [
                {**e, "source": "wikidata", "added": today, "last_ok": None,
                 "last_checked": None, "fail_count": 0}
                for e in entries
            ]


def is_aggregator_url(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return True
    host = host[4:] if host.startswith("www.") else host
    return any(host == d or host.endswith("." + d) for d in AGGREGATOR_DOMAINS)


def get_portals(directory, city, state):
    """Return the list of still-trusted {url, platform} entries for a city."""
    k = _key(city, state)
    entries = directory.get(k, [])
    return [e for e in entries if e.get("fail_count", 0) < MAX_FAIL]


def is_city_scoped_portal_url(url):
    """True for a hosted-platform URL that belongs to ONE agency.

    is_aggregator_url exists to keep dynamic search-result pages out of the
    directory, and it rejects a whole domain to do it. That is too blunt for
    the case where a city has genuinely MOVED its bids onto one of these
    platforms: procurement.opengov.com/portal/farmvilleva is that city's own
    page and belongs in the directory exactly as much as farmvilleva.gov did.

    Two or more path segments and no query string. A query is how every one of
    these platforms expresses a search, which is the thing being excluded.
    """
    try:
        parts = urllib.parse.urlparse(url or "")
    except Exception:
        return False
    if parts.query:
        return False
    return len([s for s in parts.path.split("/") if s]) >= 2


def learn_portal(directory, city, state, url, platform="custom",
                 allow_hosted=False):
    """Record a newly-discovered per-agency bid page so future scans of this
    city can hit it directly instead of re-searching. No-ops for generic
    aggregator/search-result URLs — those aren't a stable per-city page.

    `allow_hosted` admits a city-scoped URL on a hosted procurement platform.
    Off by default: only a caller that found the link on the city's OWN site,
    and so knows whose page it is, may set it."""
    if not url:
        return
    if is_aggregator_url(url) and not (allow_hosted
                                       and is_city_scoped_portal_url(url)):
        return
    k = _key(city, state)
    entries = directory.setdefault(k, [])
    for e in entries:
        if e["url"] == url:
            return  # already known
    today = datetime.date.today().isoformat()
    entries.append({
        "url": url, "platform": platform, "source": "learned",
        "added": today, "last_ok": today, "last_checked": today, "fail_count": 0,
    })


def record_result(directory, city, state, url, ok):
    """Update health tracking for a known portal entry after fetching it.
    Entries that keep failing (site moved/redesigned) age out via MAX_FAIL
    in get_portals rather than being deleted outright, so a transient outage
    doesn't permanently drop a good source."""
    k = _key(city, state)
    entries = directory.get(k, [])
    today = datetime.date.today().isoformat()
    for e in entries:
        if e["url"] == url:
            e["last_checked"] = today
            if ok:
                e["last_ok"] = today
                e["fail_count"] = 0
            else:
                e["fail_count"] = e.get("fail_count", 0) + 1
            return
