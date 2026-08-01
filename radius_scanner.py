"""
radius_scanner.py — map/location helpers for Bid Caller Pro
═══════════════════════════════════════════════════════════

Geocoding and nearby-town lookup used by the desktop app's map UI (auto-locate,
typed-location preview, town markers, map-click reverse geocoding). The actual
bid search is server-side (license_server.py's /scan, via bid_portals.py's
known-portal directory + live search) — this module no longer guesses at bid
page URLs itself; that approach was replaced by the server-side directory.

Free services used (no API key needed):
  - Nominatim (geocoding a typed location)
  - Overpass  (nearby towns)
  - ipapi.co  (optional auto-locate by IP)
"""

import requests

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


def reverse_geocode(lat, lon):
    """Turn (lat, lon) into a short 'City, State' label. Returns None on failure.
    Tries Nominatim first, then falls back to BigDataCloud (same free,
    keyless provider the server already uses) since Nominatim alone was
    leaving map clicks stuck showing raw coordinates when it rate-limited
    or failed to resolve."""
    try:
        r = requests.get("https://nominatim.openstreetmap.org/reverse",
                         params={"lat": lat, "lon": lon, "format": "json", "zoom": 10},
                         headers=UA, timeout=10)
        if r.status_code == 200 and r.json():
            addr = r.json().get("address", {})
            city = (addr.get("city") or addr.get("town") or addr.get("village")
                    or addr.get("hamlet") or addr.get("county") or "")
            state = addr.get("ISO3166-2-lvl4", "").split("-")[-1] or addr.get("state", "")
            if city and state:
                return f"{city}, {state}"
            if city or state:
                return city or state
    except Exception:
        pass
    try:
        r = requests.get("https://api.bigdatacloud.net/data/reverse-geocode-client",
                         params={"latitude": lat, "longitude": lon, "localityLanguage": "en"},
                         headers=UA, timeout=10)
        if r.status_code == 200:
            d = r.json()
            city = d.get("city") or d.get("locality") or ""
            state = (d.get("principalSubdivisionCode") or "").split("-")[-1]
            if city and state:
                return f"{city}, {state}"
            if city or state:
                return city or state
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
