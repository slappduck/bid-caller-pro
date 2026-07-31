"""
radius_scanner.py — "Find bids near me" for Bid Caller Pro
═══════════════════════════════════════════════════════════

Given a location (typed ZIP/city, or auto-detected) and a radius, this finds
nearby towns and tries to locate each town's bids page, then scrapes them.

⚠ HONEST LIMITATION:
There is no universal directory of "every town's bid page." Municipal sites
are wildly inconsistent. This module:
  1. Finds nearby towns (OpenStreetMap Overpass — free, no key).
  2. GUESSES each town's bids URL using common patterns (CivicPlus, the same
     platform your 5 default cities use). Many MO towns use CivicPlus, so this
     catches a real share — but NOT all of them.
Towns it can't auto-find won't appear. For those, the manual "Search City"
tab (paste the exact URL) is the reliable fallback.

Free services used (no API key needed):
  - Nominatim (geocoding a typed location)
  - Overpass  (nearby towns)
  - ipapi.co  (optional auto-locate by IP)
"""

import time
import requests
import regional_printer as rp

UA = {"User-Agent": "BidCallerPro/1.0 (construction bid finder)"}


# ── Geolocation ───────────────────────────────────────────
def auto_locate():
    """Approximate location from IP. Returns (lat, lon, label) or None."""
    try:
        r = requests.get("https://ipapi.co/json/", headers=UA, timeout=8)
        if r.status_code == 200:
            d = r.json()
            return float(d["latitude"]), float(d["longitude"]), \
                   f"{d.get('city','')}, {d.get('region_code','')}"
    except Exception:
        pass
    return None


def geocode(location_text):
    """Turn a typed ZIP/city into (lat, lon, label). Returns None on failure."""
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": location_text, "format": "json",
                                 "countrycodes": "us", "limit": 1},
                         headers=UA, timeout=10)
        if r.status_code == 200 and r.json():
            d = r.json()[0]
            return float(d["lat"]), float(d["lon"]), d.get("display_name", location_text)
    except Exception:
        pass
    return None


# ── Nearby towns ──────────────────────────────────────────
def find_nearby_towns(lat, lon, radius_miles, max_towns=20):
    """Return list of dicts {name, state, lat, lon} within radius using Overpass."""
    radius_m = int(radius_miles * 1609.34)
    query = f"""
    [out:json][timeout:25];
    (
      node["place"~"city|town"](around:{radius_m},{lat},{lon});
    );
    out body {max_towns * 3};
    """
    try:
        r = requests.post("https://overpass-api.de/api/interpreter",
                          data={"data": query}, headers=UA, timeout=40)
        if r.status_code != 200:
            return []
        seen, towns = set(), []
        for el in r.json().get("elements", []):
            tags = el.get("tags", {})
            name = tags.get("name")
            state = tags.get("addr:state", "")
            if name and name.lower() not in seen:
                seen.add(name.lower())
                towns.append({
                    "name": name,
                    "state": state,
                    "lat": el.get("lat"),
                    "lon": el.get("lon"),
                })
            if len(towns) >= max_towns:
                break
        return towns
    except Exception:
        return []


# ── Guess a town's bids URL ───────────────────────────────
def _slug(name):
    return "".join(c for c in name.lower() if c.isalnum())


def discover_bid_url(city, state_abbr=""):
    """Probe common municipal URL patterns. Returns a working URL or None."""
    s = _slug(city)
    st = state_abbr.lower() if state_abbr else "mo"
    candidates = [
        f"https://www.{s}.gov/Bids.aspx",
        f"https://www.{s}{st}.gov/Bids.aspx",
        f"https://www.{s}{st}.com/Bids.aspx",
        f"https://{s}.gov/Bids.aspx",
        f"https://www.{s}.org/Bids.aspx",
        f"https://www.cityof{s}.org/Bids.aspx",
        f"https://www.cityof{s}.com/Bids.aspx",
        f"https://www.{s}.gov/bids.aspx",
        f"https://www.{s}.gov/AgendaCenter",
    ]
    for url in candidates:
        try:
            r = requests.get(url, headers=rp.DISGUISE, timeout=8)
            if r.status_code == 200 and ("bid" in r.text.lower()
                                         or "agenda" in r.text.lower()
                                         or "rfp" in r.text.lower()):
                return url
        except Exception:
            continue
    return None


# ── Orchestrator ──────────────────────────────────────────
def run_radius_scan(lat, lon, radius_miles, progress=None, master_file=rp.MASTER_FILE):
    """
    Finds nearby towns, discovers bid pages, scrapes them into master_file.
    `progress(msg)` is an optional callback for live status.
    Returns a dict: {"scanned": [...], "skipped": [...], "found_towns": N}
    """
    import os
    def say(m):
        print(m)
        if progress:
            progress(m)

    if os.path.exists(master_file):
        os.remove(master_file)

    say(f"📍 Looking for towns within {radius_miles} miles...")
    towns = find_nearby_towns(lat, lon, radius_miles)
    if not towns:
        say("No towns found nearby (Overpass may be busy — try again).")
        return {"scanned": [], "skipped": [], "found_towns": 0,
                "town_coords": [], "center": {"lat": lat, "lon": lon}}

    say(f"Found {len(towns)} nearby town(s). Locating their bid pages...")
    scanned, skipped = [], []
    town_coords = []

    for t in towns:
        name, state = t["name"], t["state"]
        url = discover_bid_url(name, state or "MO")
        if url:
            say(f"   ✅ {name}: {url}")
            ok = rp.scrape_city_to_file(name, url, master_file)
            (scanned if ok else skipped).append(name)
            status = "scanned" if ok else "skipped"
        else:
            skipped.append(name)
            status = "skipped"
            say(f"   – {name}: no bid page auto-found")
        town_coords.append({
            "name": name, "state": state,
            "lat": t.get("lat"), "lon": t.get("lon"),
            "status": status,
        })
        time.sleep(0.4)

    say(f"\nScraped {len(scanned)} town(s). {len(skipped)} couldn't be auto-found.")
    return {
        "scanned": scanned,
        "skipped": skipped,
        "found_towns": len(towns),
        "town_coords": town_coords,
        "center": {"lat": lat, "lon": lon},
    }
