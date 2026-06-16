"""
license_server.py — License validation + bid scanning for Bid Caller Pro
═══════════════════════════════════════════════════════════════════════════
/scan now returns LOCAL + FEDERAL leads, filtered to a mile radius:
  • LOCAL   — Tavily (AI search API) finds city/county/school bid pages near
              the user AND returns their content, which OpenAI turns into
              structured bids. Works reliably from a server.
  • FEDERAL — SAM.gov solicitations for the user's state.
  • Both are distance-filtered against the user's radius and grouped by city,
    then cached per area per day.

ENV VARS (set in Render → your service → Environment):
  LICENSE_SECRET   license signing secret
  ADMIN_TOKEN      admin token for /issue and /revoke
  TAVILY_API_KEY   REQUIRED for local search (free 1k/mo, no card: tavily.com)
  OPENAI_API_KEY   REQUIRED for local extraction
  SAM_API_KEY      REQUIRED for federal bids (free key: sam.gov)
  (BRAVE_API_KEY is no longer used — you can delete it.)

START COMMAND (raise the timeout — scans do real work):
  gunicorn license_server:app --timeout 120 --workers 1
"""

import os
import re
import json
import math
import hmac
import hashlib
import datetime
import time
import urllib.request
import urllib.parse
import urllib.error

from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)

# ── CORS: production Netlify site + deploy previews ──
CORS(app, resources={r"/*": {"origins": [
    re.compile(r"^https://([a-z0-9-]+--)?bidcaller\.netlify\.app$"),
]}})

# ── Secrets ──
LICENSE_SECRET = os.environ.get("LICENSE_SECRET", "CHANGE_THIS_LONG_RANDOM_SECRET")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "CHANGE_THIS_ADMIN_TOKEN")
TRIAL_DAYS = 7

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "license_db.json")


def _db():
    try:
        with open(DB_PATH) as f:
            return json.load(f)
    except Exception:
        return {"revoked": [], "trials": {}, "issued": {}}


def _save_db(db):
    with open(DB_PATH, "w") as f:
        json.dump(db, f, indent=2)


# ── Key signing / verification ──
def _sign(plan, date_str):
    payload = f"{plan}|{date_str}"
    return hmac.new(LICENSE_SECRET.encode(), payload.encode(),
                    hashlib.sha256).hexdigest()[:16].upper()


def make_key(plan="monthly", months=1):
    exp = datetime.datetime.now() + datetime.timedelta(days=30 * months)
    date_short = exp.strftime("%Y%m%d")
    sig = _sign(plan, date_short)
    return f"BCP-{plan[:3].upper()}-{date_short}-{sig}", exp.isoformat()


def verify_key(key):
    key = (key or "").strip().upper()
    if not key.startswith("BCP-"):
        return False, None, None, "bad_format"
    parts = key.split("-")
    if len(parts) != 4:
        return False, None, None, "bad_format"
    _, plan_short, date_str, sig = parts
    plan = {"MON": "monthly", "ANN": "annual"}.get(plan_short, "monthly")
    try:
        exp_dt = datetime.datetime.strptime(date_str, "%Y%m%d")
    except ValueError:
        return False, None, None, "bad_date"
    expected = _sign(plan, date_str)
    if not hmac.compare_digest(sig, expected):
        return False, None, None, "bad_signature"
    if datetime.datetime.now() > exp_dt:
        return False, plan, exp_dt.isoformat(), "expired"
    return True, plan, exp_dt.isoformat(), "ok"


@app.route("/validate", methods=["POST"])
def validate():
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key", "")
    db = _db()
    if key.strip().upper() in db.get("revoked", []):
        return jsonify({"valid": False, "reason": "revoked"})
    valid, plan, exp, reason = verify_key(key)
    if valid:
        return jsonify({"valid": True, "plan": plan, "expires": exp[:10], "reason": "ok"})
    return jsonify({"valid": False, "reason": reason})


@app.route("/trial", methods=["POST"])
def trial():
    data = request.get_json(force=True, silent=True) or {}
    device = (data.get("device_id") or "").strip()
    if not device:
        return jsonify({"ok": False, "reason": "no_device"})
    db = _db()
    trials = db.setdefault("trials", {})
    if device in trials:
        started = datetime.datetime.fromisoformat(trials[device]["started"])
        end = started + datetime.timedelta(days=TRIAL_DAYS)
        if datetime.datetime.now() <= end:
            left = (end - datetime.datetime.now()).days + 1
            return jsonify({"ok": True, "active": True, "days_left": max(1, left),
                            "expires": end.isoformat()[:10]})
        return jsonify({"ok": False, "active": False, "reason": "trial_expired"})
    started = datetime.datetime.now()
    trials[device] = {"started": started.isoformat()}
    _save_db(db)
    end = started + datetime.timedelta(days=TRIAL_DAYS)
    return jsonify({"ok": True, "active": True, "days_left": TRIAL_DAYS,
                    "expires": end.isoformat()[:10], "new": True})


@app.route("/issue", methods=["POST"])
def issue():
    data = request.get_json(force=True, silent=True) or {}
    if data.get("admin_token") != ADMIN_TOKEN:
        return jsonify({"ok": False, "reason": "unauthorized"}), 401
    plan = data.get("plan", "monthly")
    months = 12 if plan == "annual" else int(data.get("months", 1))
    key, exp = make_key(plan, months)
    db = _db()
    db.setdefault("issued", {})[key] = {
        "plan": plan, "expires": exp[:10], "email": data.get("email", ""),
        "issued": datetime.datetime.now().isoformat()[:10],
    }
    _save_db(db)
    return jsonify({"ok": True, "key": key, "plan": plan, "expires": exp[:10]})


@app.route("/revoke", methods=["POST"])
def revoke():
    data = request.get_json(force=True, silent=True) or {}
    if data.get("admin_token") != ADMIN_TOKEN:
        return jsonify({"ok": False, "reason": "unauthorized"}), 401
    key = (data.get("key") or "").strip().upper()
    db = _db()
    if key not in db.setdefault("revoked", []):
        db["revoked"].append(key)
    _save_db(db)
    return jsonify({"ok": True, "revoked": key})


@app.route("/", methods=["GET"])
def health():
    return jsonify({"service": "Bid Caller Pro License Server", "status": "ok"})


# ═══════════════════════════════════════════════════════════
# LICENSE / TRIAL GATE
# ═══════════════════════════════════════════════════════════
def _license_is_active(key, device):
    key = (key or "").strip().upper()
    db = _db()
    if key and key not in db.get("revoked", []):
        valid, _, _, _ = verify_key(key)
        if valid:
            return True
    trials = db.get("trials", {})
    if device in trials:
        started = datetime.datetime.fromisoformat(trials[device]["started"])
        if datetime.datetime.now() <= started + datetime.timedelta(days=TRIAL_DAYS):
            return True
    return False


# ═══════════════════════════════════════════════════════════
# SHARED HELPERS (HTTP, geocoding, distance)
# ═══════════════════════════════════════════════════════════
def _get_json(url, headers=None, timeout=20):
    try:
        req = urllib.request.Request(url, headers=headers or {"User-Agent": "BidCallerPro/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _geo_from_zip(zip_code):
    data = _get_json(f"https://api.zippopotam.us/us/{zip_code}")
    p = (data or {}).get("places") or []
    if not p:
        return None
    try:
        return {"lat": float(p[0]["latitude"]), "lon": float(p[0]["longitude"]),
                "city": p[0].get("place name", ""),
                "state": (p[0].get("state abbreviation") or "").upper()}
    except (KeyError, ValueError, TypeError):
        return None


def _geo_from_city(city, state):
    url = f"https://api.zippopotam.us/us/{state.upper()}/{urllib.parse.quote(city)}"
    data = _get_json(url)
    places = (data or {}).get("places") or []
    pts = []
    for p in places:
        try:
            pts.append((float(p["latitude"]), float(p["longitude"])))
        except (KeyError, ValueError, TypeError):
            continue
    if not pts:
        return None
    lat = sum(x for x, _ in pts) / len(pts)
    lon = sum(y for _, y in pts) / len(pts)
    return {"lat": lat, "lon": lon, "city": city, "state": state.upper()}


def _miles_between(lat1, lon1, lat2, lon2):
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


STATE_NAME_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
STATE_ABBRS = set(STATE_NAME_TO_ABBR.values())


def _resolve_center(location):
    """Return {lat, lon, city, state} for a ZIP or 'City, ST' / 'City, State'."""
    loc = (location or "").strip()
    if not loc:
        return None
    m = re.search(r"\b(\d{5})\b", loc)
    if m:
        g = _geo_from_zip(m.group(1))
        if g:
            return g
    m2 = re.search(r"^(.*?),\s*([A-Za-z]{2})\b", loc)
    if m2 and m2.group(2).upper() in STATE_ABBRS:
        g = _geo_from_city(m2.group(1).strip(), m2.group(2).upper())
        if g:
            return g
    m3 = re.search(r"^(.*?),\s*([A-Za-z][A-Za-z ]+)$", loc)
    if m3:
        st = STATE_NAME_TO_ABBR.get(m3.group(2).strip().lower())
        if st:
            g = _geo_from_city(m3.group(1).strip(), st)
            if g:
                return g
    return None


def _city_coords(city, state, db):
    """Geocode a (city, state) to [lat, lon], cached in the JSON db."""
    if not city or not state:
        return None
    gc = db.setdefault("geo_cache", {})
    k = f"{city.lower()}|{state.upper()}"
    if k in gc:
        return gc[k]
    g = _geo_from_city(city, state)
    coords = [g["lat"], g["lon"]] if g else None
    gc[k] = coords
    return coords


# ═══════════════════════════════════════════════════════════
# LOCAL SEARCH  (Tavily — AI search API, free 1k/mo, no card)
# ═══════════════════════════════════════════════════════════
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
TAVILY_URL = "https://api.tavily.com/search"
MAX_PAGES = int(os.environ.get("SCAN_MAX_PAGES", "6"))

_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _tavily_search(query, max_results=5):
    """Search via Tavily; returns [{url, content}]. Page content comes back
    with the results, so no separate scrape is needed for most pages."""
    if not TAVILY_API_KEY:
        return []
    body = json.dumps({
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": "basic",
        "max_results": max_results,
        "include_raw_content": True,
    }).encode("utf-8")
    req = urllib.request.Request(TAVILY_URL, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TAVILY_API_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as ex:
        app.logger.warning("Tavily error: %s", ex)
        return []
    out = []
    for r in (data.get("results") or []):
        url = r.get("url") or ""
        if url:
            out.append({"url": url,
                        "content": r.get("raw_content") or r.get("content") or ""})
    return out


def _fetch_text(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 BidCallerPro"})
        with urllib.request.urlopen(req, timeout=18) as resp:
            raw = resp.read(800000).decode("utf-8", "ignore")
    except Exception:
        return ""
    raw = _SCRIPT_RE.sub(" ", raw)
    raw = _TAG_RE.sub(" ", raw)
    raw = re.sub(r"&[a-z#0-9]+;", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


# ═══════════════════════════════════════════════════════════
# FEDERAL SEARCH  (SAM.gov)
# ═══════════════════════════════════════════════════════════
SAM_API_KEY = os.environ.get("SAM_API_KEY", "")
SAM_SEARCH_URL = os.environ.get(
    "SAM_SEARCH_URL", "https://api.sam.gov/prod/opportunities/v2/search")
SCAN_WINDOW_DAYS = int(os.environ.get("SCAN_WINDOW_DAYS", "60"))

CONSTRUCTION_NAICS_PREFIXES = ("236", "237", "238")
CONSTRUCTION_KEYWORDS = (
    "construction", "roof", "paving", "pavement", "road", "highway", "bridge",
    "demolition", "hvac", "plumbing", "electrical", "concrete", "grading",
    "sidewalk", "sewer", "water main", "waterline", "renovation", "remodel",
    "build", "facility", "repair", "install",
)


def _sam_fetch(state):
    if not SAM_API_KEY:
        return None
    today = datetime.datetime.now()
    params = {
        "api_key": SAM_API_KEY,
        "postedFrom": (today - datetime.timedelta(days=SCAN_WINDOW_DAYS)).strftime("%m/%d/%Y"),
        "postedTo": today.strftime("%m/%d/%Y"),
        "state": state,
        "limit": "1000",
        "offset": "0",
    }
    data = _get_json(SAM_SEARCH_URL + "?" + urllib.parse.urlencode(params),
                     headers={"Accept": "application/json"}, timeout=60)
    return (data or {}).get("opportunitiesData") or []


def _is_construction(opp):
    naics = opp.get("naicsCode") or ""
    if isinstance(naics, list):
        naics = naics[0] if naics else ""
    if str(naics)[:3] in CONSTRUCTION_NAICS_PREFIXES:
        return True
    title = (opp.get("title") or "").lower()
    return any(k in title for k in CONSTRUCTION_KEYWORDS)


def _normalize_opp(opp):
    poc_list = opp.get("pointOfContact") or []
    poc = poc_list[0] if poc_list else {}
    pop = opp.get("placeOfPerformance") or {}
    city = ((pop.get("city") or {}).get("name")) or ""
    deadline = (opp.get("responseDeadLine") or "")[:10]
    is_open = (opp.get("active") or "").strip().lower() == "yes"
    agency = opp.get("fullParentPathName") or opp.get("organizationName") or ""
    scope = " · ".join([b for b in ("Federal", opp.get("type") or "", agency) if b])
    bid = {
        "title": opp.get("title") or "Untitled Opportunity",
        "scope": scope,
        "status": "Open" if is_open else "Closed",
        "deadline": deadline,
        "contact": poc.get("fullName") or "",
        "email": poc.get("email") or "",
        "phone": poc.get("phone") or "",
        "value": "",
        "url": opp.get("uiLink") or "",
    }
    return bid, city


# ═══════════════════════════════════════════════════════════
# AI EXTRACTION (now also returns a "city" so we can radius-filter)
# ═══════════════════════════════════════════════════════════
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def _ai_extract(area, text):
    if not OPENAI_API_KEY:
        return None
    prompt = (
        f"You extract construction bid leads for contractors near {area}.\n\n"
        "From the website text below, extract ANY construction, infrastructure, "
        "roofing, paving, road, utility, demolition, HVAC, or facility bids, RFPs, "
        "RFQs, or solicitations.\n\n"
        "Respond ONLY with a JSON array. Each item has keys: \"title\", \"scope\", "
        "\"status\" (\"Open\" or \"Closed\"), \"deadline\", \"contact\", \"email\", "
        "\"phone\", \"value\", \"url\", \"city\". \"city\" is the US city where the work "
        "will be performed, exactly as written in the text; if the location is not clearly "
        "stated, use \"\" and do NOT guess. \"value\" is a dollar amount only if stated. "
        "Use \"\" for any missing field. If no real bids, return []. "
        "No markdown, no text outside the array.\n\n"
        f"WEBSITE TEXT:\n{text[:16000]}"
    )
    body = json.dumps({
        "model": OPENAI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = data["choices"][0]["message"]["content"].strip()
        s, e = out.find("["), out.rfind("]")
        if s != -1 and e != -1 and e > s:
            out = out[s:e + 1]
        bids = json.loads(out)
        return bids if isinstance(bids, list) else []
    except Exception:
        return None


@app.route("/extract", methods=["POST"])
def extract():
    data = request.get_json(force=True, silent=True) or {}
    if not _license_is_active(data.get("key", ""), data.get("device_id", "")):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    text = data.get("text", "")
    if not text.strip():
        return jsonify({"ok": True, "bids": []})
    bids = _ai_extract(data.get("city", "Unknown"), text)
    if bids is None:
        return jsonify({"ok": False, "reason": "ai_error"}), 500
    return jsonify({"ok": True, "bids": bids})


# ═══════════════════════════════════════════════════════════
# /scan  —  LOCAL (Brave + AI) + FEDERAL (SAM), radius-filtered
# ═══════════════════════════════════════════════════════════
def _place_bid(grouped, bid, center, radius, db, default_city=""):
    """Keep a bid ONLY if its real city geocodes within the radius."""
    if not isinstance(bid, dict):
        return
    city = (bid.get("city") or default_city or "").split(",")[0].strip()
    if not city:
        return  # no stated location -> can't verify it's local -> drop
    coords = _city_coords(city, center["state"], db)
    if not coords:
        return  # city not found in this state -> can't verify -> drop
    if _miles_between(center["lat"], center["lon"], coords[0], coords[1]) > radius:
        return  # outside the chosen radius
    bid.pop("city", None)
    grouped.setdefault(city, []).append(bid)


@app.route("/scan", methods=["POST"])
def scan():
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key", "")
    device = data.get("device_id", "")
    location = (data.get("location") or "").strip()
    try:
        radius = float(data.get("radius") or 25)
    except (TypeError, ValueError):
        radius = 25.0

    if not _license_is_active(key, device):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    if not location:
        return jsonify({"ok": False, "reason": "no_location"})

    center = _resolve_center(location)
    if not center:
        return jsonify({"ok": False, "reason": "location_not_found"})

    db = _db()
    today = datetime.datetime.now().strftime("%Y%m%d")
    cache = db.setdefault("scan_cache", {})
    ckey = f"{center['state']}|{center['city'].lower()}|{int(radius)}|{today}"
    if ckey in cache:
        c = cache[ckey]
        return jsonify({"ok": True, "location": f"{center['city']}, {center['state']}",
                        "bids": c["bids"], "total_bids": c["total"], "cached": True})

    grouped = {}
    local_raw = 0

    # ---- LOCAL: Tavily finds pages + returns their content -> AI extract ----
    if OPENAI_API_KEY and TAVILY_API_KEY:
        c, s = center["city"], center["state"]
        queries = [
            f"{c} {s} construction bid RFP invitation to bid",
            f"{c} {s} city county procurement construction solicitation",
            f"{c} {s} public works school district construction bids",
        ]
        seen, items = set(), []
        for q in queries:
            for r in _tavily_search(q, max_results=5):
                if r["url"] not in seen:
                    seen.add(r["url"])
                    items.append(r)
            time.sleep(0.7)  # stay under the free-tier rate limit
        app.logger.info("scan: %d candidate pages near %s, %s", len(items), c, s)
        for it in items[:MAX_PAGES]:
            text = it["content"] or _fetch_text(it["url"])
            if len(text) < 200:
                continue
            bids = _ai_extract(f"{c}, {s}", text)
            if not bids:
                continue
            local_raw += len(bids)
            for b in bids:
                if isinstance(b, dict):
                    b.setdefault("url", it["url"])
                    _place_bid(grouped, b, center, radius, db, default_city="")
        app.logger.info("scan: %d raw local bids extracted", local_raw)

    # ---- FEDERAL: SAM.gov for the state, radius-filtered ----
    if SAM_API_KEY:
        for opp in (_sam_fetch(center["state"]) or []):
            if not _is_construction(opp):
                continue
            bid, city = _normalize_opp(opp)
            _place_bid(grouped, bid, center, radius, db, default_city=city)

    total = sum(len(v) for v in grouped.values())
    app.logger.info("scan: %s mi from %s,%s -> %d bids kept",
                    int(radius), center["city"], center["state"], total)

    # cache (today only) + persist geo cache
    cache[ckey] = {"ts": datetime.datetime.now().isoformat(), "bids": grouped, "total": total}
    db["scan_cache"] = {k: v for k, v in cache.items() if k.endswith(today)}
    _save_db(db)

    return jsonify({"ok": True, "location": f"{center['city']}, {center['state']}",
                    "bids": grouped, "total_bids": total})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
