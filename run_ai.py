import os
import re
import json
from applog import debug

try:
    import requests
except ImportError:
    requests = None

import subscription  # for the server URL + license key + device id

MASTER_FILE = "all_regional_intel.txt"
PACKET_RE = re.compile(r"=== PACKET \| CITY: (.*?) \| PART: .*? ===")


def _extract_via_server(city, text):
    """Send scraped text to OUR server, which runs the AI. Returns (bids, status)."""
    if requests is None:
        return [], "no_requests"
    url = subscription.SERVER_URL.rstrip("/") + "/extract"
    payload = {
        "key": subscription._load_cache().get("key", ""),
        "device_id": subscription._device_id(),
        "city": city,
        "text": text,
    }
    try:
        r = requests.post(url, json=payload, timeout=130)
        if r.status_code == 200:
            data = r.json()
            return (data.get("bids", []) if data.get("ok") else []), "ok"
        if r.status_code == 403:
            return [], "not_licensed"
        debug(f"/extract HTTP {r.status_code} for {city}")
        return [], "server_error"
    except Exception as e:
        debug(f"/extract error for {city}: {e}")
        return [], "unreachable"


def run_analysis(master_file=MASTER_FILE):
    """
    Reads the scraped master file and asks OUR server to extract bids with AI.
    Customers need NO Ollama installed — the AI runs server-side.
    Returns (results_dict, status_code).
    """
    if not os.path.exists(master_file):
        return {}, "no_data"
    with open(master_file, "r", encoding="utf-8") as f:
        full = f.read()
    if not full.strip():
        return {}, "no_data"

    # Split into per-city buckets
    buckets = {}
    current = None
    for line in full.splitlines():
        m = PACKET_RE.match(line.strip())
        if m:
            current = m.group(1).strip()
            buckets.setdefault(current, [])
        elif current is not None:
            buckets[current].append(line)
    if not buckets:
        return {}, "no_data"

    debug(f"Analyzing {len(buckets)} town(s) via server: {', '.join(buckets.keys())}")
    results = {}
    any_unreachable = False
    any_unlicensed = False

    for city, lines in buckets.items():
        data = "\n".join(lines).strip()
        if not data:
            results[city] = []
            continue
        bids, status = _extract_via_server(city, data)
        if status == "unreachable":
            any_unreachable = True
        elif status == "not_licensed":
            any_unlicensed = True
        results[city] = bids
        debug(f"[{city}] -> {len(bids)} bid(s) ({status})")

    total = sum(len(v) for v in results.values())
    debug(f"Finished. {total} bid(s) across {len(results)} town(s).")

    if any_unlicensed:
        return results, "not_licensed"
    if any_unreachable and total == 0:
        return results, "server_down"
    if total == 0:
        return results, "no_bids"
    return results, "ok"
