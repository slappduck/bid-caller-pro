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
    for (city, state), entries in SEED_PORTALS.items():
        k = _key(city, state)
        if k not in directory:
            today = datetime.date.today().isoformat()
            directory[k] = [
                {**e, "source": "seed", "added": today, "last_ok": None,
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


def learn_portal(directory, city, state, url, platform="custom"):
    """Record a newly-discovered per-agency bid page so future scans of this
    city can hit it directly instead of re-searching. No-ops for generic
    aggregator/search-result URLs — those aren't a stable per-city page."""
    if not url or is_aggregator_url(url):
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
