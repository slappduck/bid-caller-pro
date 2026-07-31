"""
map_view.py — "Show me the scan on a map" for Bid Caller Pro
══════════════════════════════════════════════════════════════
Builds a self-contained HTML file (Leaflet.js + free OpenStreetMap tiles,
no API key needed) that pins:
  - your search location (center)
  - every nearby town the last scan looked at, colored by outcome:
      green  = bids found there
      blue   = scanned successfully, no open bids right now
      gray   = couldn't auto-find that town's bid page

Opened in the user's default browser via webbrowser.open() — no new
pip dependency, so nothing changes in the PyInstaller build.
"""

import os
import json

MAP_FILE = "scan_map.html"


def _marker_color(name, town_status, bid_counts):
    if town_status.get(name) == "skipped":
        return "#9aa0a6"          # gray — no bid page found
    if bid_counts and bid_counts.get(name):
        return "#34a853"          # green — bids found
    return "#4a90e2"              # blue — scanned, nothing found


def build_map_html(center_lat, center_lon, center_label, towns,
                    bid_counts=None, out_dir="."):
    """
    towns: list of dicts like {"name": "Ozark", "state": "MO",
                                "lat": 37.02, "lon": -93.20, "status": "scanned"}
           (status is "scanned" or "skipped"; towns with no lat/lon are skipped
           on the map but still counted in the scan log)
    bid_counts: optional dict {city_name: number_of_bids_found}
    Returns the path to the written HTML file.
    """
    bid_counts = bid_counts or {}
    town_status = {t["name"]: t.get("status", "scanned") for t in towns}

    markers = [{
        "lat": center_lat,
        "lon": center_lon,
        "label": f"Search center: {center_label}".replace('"', "'"),
        "color": "#f5b400",
        "radius": 9,
    }]

    for t in towns:
        if t.get("lat") is None or t.get("lon") is None:
            continue
        name = t["name"]
        n_bids = bid_counts.get(name, 0)
        status = t.get("status", "scanned")
        if status == "skipped":
            label = f"{name}, {t.get('state','')} — no bid page auto-found"
        elif n_bids:
            label = f"{name}, {t.get('state','')} — {n_bids} bid(s) found"
        else:
            label = f"{name}, {t.get('state','')} — scanned, no open bids"
        markers.append({
            "lat": t["lat"],
            "lon": t["lon"],
            "label": label.replace('"', "'"),
            "color": _marker_color(name, town_status, bid_counts),
            "radius": 7,
        })

    markers_json = json.dumps(markers)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Bid Caller Pro — Scan Map</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body {{ margin:0; padding:0; height:100%; background:#0d0f18; font-family: Segoe UI, sans-serif; }}
  #map {{ height:100%; width:100%; }}
  .legend {{
    position:absolute; top:12px; right:12px; z-index:1000;
    background:#13162a; color:#f1f5f9; padding:10px 14px; border-radius:8px;
    font-size:13px; box-shadow:0 2px 8px rgba(0,0,0,.4); line-height:1.6;
  }}
  .dot {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; }}
</style>
</head>
<body>
<div id="map"></div>
<div class="legend">
  <div><span class="dot" style="background:#f5b400"></span>Search center</div>
  <div><span class="dot" style="background:#34a853"></span>Bids found</div>
  <div><span class="dot" style="background:#4a90e2"></span>Scanned, none found</div>
  <div><span class="dot" style="background:#9aa0a6"></span>No bid page found</div>
</div>
<script>
  const markers = {markers_json};
  const map = L.map('map').setView([{center_lat}, {center_lon}], 9);
  L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
    attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
    maxZoom: 20,
    subdomains: 'abcd',
  }}).addTo(map);

  const bounds = [];
  markers.forEach(m => {{
    L.circleMarker([m.lat, m.lon], {{
      radius: m.radius, color: m.color, fillColor: m.color, fillOpacity: 0.9, weight: 2,
    }}).addTo(map).bindPopup(m.label);
    bounds.push([m.lat, m.lon]);
  }});
  if (bounds.length > 1) {{ map.fitBounds(bounds, {{ padding: [40, 40] }}); }}
</script>
</body>
</html>
"""

    path = os.path.join(out_dir, MAP_FILE)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path
