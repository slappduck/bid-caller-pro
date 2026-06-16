"""
license_server.py — License validation + bid scanning for Bid Caller Pro
═══════════════════════════════════════════════════════════════════════════
WHAT'S NEW IN THIS VERSION
  1. CORS is enabled so the Netlify front-end can call this server from a browser.
  2. A real /scan endpoint that pulls live federal construction solicitations
     from the free SAM.gov Opportunities API, normalizes them into the shape
     the app expects, and caches results per-state per-day to protect the
     SAM.gov rate limit.

ENV VARS TO SET IN RENDER:
  LICENSE_SECRET   - long random string (license signing)
  ADMIN_TOKEN      - admin token for /issue and /revoke
  OPENAI_API_KEY   - (optional) for /extract
  SAM_API_KEY      - REQUIRED for /scan. Free key from sam.gov -> Account
                     Details -> Public API Key.

START COMMAND (unchanged):  gunicorn license_server:app
"""

import os
import re
import json
import hmac
import hashlib
import datetime
import urllib.request
import urllib.parse
import urllib.error

from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)

# ── CORS ──────────────────────────────────────────────────────────────────
# Allow the production Netlify site AND its deploy-preview subdomains
# (e.g. 3619e65d6a--bidcaller.netlify.app). No credentials/cookies are used,
# so a simple origin allowance is all that's needed.
CORS(app, resources={r"/*": {"origins": [
    re.compile(r"^https://([a-z0-9-]+--)?bidcaller\.netlify\.app$"),
]}})

# ── Secrets (set these as environment variables in production!) ──
LICENSE_SECRET = os.environ.get("LICENSE_SECRET", "CHANGE_THIS_LONG_RANDOM_SECRET")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "CHANGE_THIS_ADMIN_TOKEN")
TRIAL_DAYS = 7

# ── Simple JSON "database" (fine to start; swap for real DB later) ──
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
    """Sign using plan + YYYYMMDD date string (must match on both sides)."""
    payload = f"{plan}|{date_str}"
    return hmac.new(LICENSE_SECRET.encode(), payload.encode(),
                    hashlib.sha256).hexdigest()[:16].upper()


def make_key(plan="monthly", months=1):
    exp = datetime.datetime.now() + datetime.timedelta(days=30 * months)
    date_short = exp.strftime("%Y%m%d")
    sig = _sign(plan, date_short)
    return f"BCP-{plan[:3].upper()}-{date_short}-{sig}", exp.isoformat()


def verify_key(key):
    """Returns (valid, plan, expires_iso, reason)."""
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
    device = data.get("device_id", "")
    db = _db()
    # Revoked?
    if key.strip().upper() in db.get("revoked", []):
        return jsonify({"valid": False, "reason": "revoked"})
    valid, plan, exp, reason = verify_key(key)
    if valid:
        return jsonify({"valid": True, "plan": plan,
                        "expires": exp[:10], "reason": "ok"})
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
    # Start a new trial for this device
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
# LICENSE / TRIAL GATE (shared by /scan and /extract)
# ═══════════════════════════════════════════════════════════
def _license_is_active(key, device):
    """Only paying/trial users may use server resources."""
    key = (key or "").strip().upper()
    db = _db()
    if key and key not in db.get("revoked", []):
        valid, plan, exp, reason = verify_key(key)
        if valid:
            return True
    # allow active trials too
    trials = db.get("trials", {})
    if device in trials:
        started = datetime.datetime.fromisoformat(trials[device]["started"])
        if datetime.datetime.now() <= started + datetime.timedelta(days=TRIAL_DAYS):
            return True
    return False


# ═══════════════════════════════════════════════════════════
# /scan  —  LIVE FEDERAL CONSTRUCTION BIDS FROM SAM.gov
# ═══════════════════════════════════════════════════════════
# SAM.gov returns federal opportunities filtered by *state* (place of
# performance), not by ZIP radius. We resolve the user's location to a state,
# pull recent solicitations, keep the construction ones, and cache per state
# per day so a busy day of scans only costs one SAM.gov call per state.

SAM_API_KEY = os.environ.get("SAM_API_KEY", "")
SAM_SEARCH_URL = os.environ.get(
    "SAM_SEARCH_URL", "https://api.sam.gov/prod/opportunities/v2/search")
SCAN_WINDOW_DAYS = int(os.environ.get("SCAN_WINDOW_DAYS", "45"))

# Construction lives in NAICS sectors 236/237/238. We also keyword-match the
# title to catch opportunities that are mis-coded.
CONSTRUCTION_NAICS_PREFIXES = ("236", "237", "238")
CONSTRUCTION_KEYWORDS = (
    "construction", "roof", "paving", "pavement", "road", "highway", "bridge",
    "demolition", "hvac", "plumbing", "electrical", "concrete", "grading",
    "sidewalk", "sewer", "water main", "waterline", "renovation", "remodel",
    "build", "facility", "repair", "install",
)

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


def _zip_to_location(zip_code):
    """Resolve a US ZIP to (state_abbr, city) using the free Zippopotam API."""
    url = f"https://api.zippopotam.us/us/{zip_code}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BidCallerPro/1.1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        places = data.get("places", [])
        if not places:
            return None, None
        state = (places[0].get("state abbreviation") or "").upper()
        city = places[0].get("place name") or ""
        return (state or None), city
    except Exception:
        return None, None


def _resolve_location(location):
    """Turn whatever the user typed into (state_abbr, display_label)."""
    loc = (location or "").strip()
    if not loc:
        return None, None
    # 5-digit ZIP anywhere in the string
    m = re.search(r"\b(\d{5})\b", loc)
    if m:
        state, city = _zip_to_location(m.group(1))
        if state:
            return state, (f"{city}, {state}" if city else state)
    # "City, ST"
    m2 = re.search(r",\s*([A-Za-z]{2})\b", loc)
    if m2 and m2.group(1).upper() in STATE_ABBRS:
        return m2.group(1).upper(), loc
    # full state name anywhere in the text
    low = loc.lower()
    for name, ab in STATE_NAME_TO_ABBR.items():
        if name in low:
            return ab, loc
    # bare 2-letter state code
    if loc.upper() in STATE_ABBRS:
        return loc.upper(), loc.upper()
    return None, None


def _sam_fetch(state):
    """Pull recent opportunities for a state from SAM.gov. Returns (list, status)."""
    if not SAM_API_KEY:
        return None, "no_sam_key"
    today = datetime.datetime.now()
    params = {
        "api_key": SAM_API_KEY,
        "postedFrom": (today - datetime.timedelta(days=SCAN_WINDOW_DAYS)).strftime("%m/%d/%Y"),
        "postedTo": today.strftime("%m/%d/%Y"),
        "state": state,          # place-of-performance state
        "limit": "1000",
        "offset": "0",
    }
    url = SAM_SEARCH_URL + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("opportunitiesData") or []), "ok"
    except urllib.error.HTTPError as e:
        return None, f"sam_http_{e.code}"
    except Exception as e:
        return None, f"sam_error: {e}"


def _is_construction(opp):
    naics = opp.get("naicsCode") or ""
    if isinstance(naics, list):
        naics = naics[0] if naics else ""
    if str(naics)[:3] in CONSTRUCTION_NAICS_PREFIXES:
        return True
    title = (opp.get("title") or "").lower()
    return any(k in title for k in CONSTRUCTION_KEYWORDS)


def _normalize_opp(opp):
    """Map a SAM.gov opportunity into the bid shape the front-end renders."""
    poc_list = opp.get("pointOfContact") or []
    poc = poc_list[0] if poc_list else {}
    pop = opp.get("placeOfPerformance") or {}
    city = ((pop.get("city") or {}).get("name")) or ""

    deadline = (opp.get("responseDeadLine") or "")[:10]
    is_open = (opp.get("active") or "").strip().lower() == "yes"
    agency = opp.get("fullParentPathName") or opp.get("organizationName") or ""
    notice_type = opp.get("type") or ""
    scope = " · ".join([b for b in (notice_type, agency) if b])

    award = opp.get("award") or {}
    amount = award.get("amount") or ""
    value = ""
    if amount:
        try:
            value = "${:,.0f}".format(float(amount))
        except (ValueError, TypeError):
            value = str(amount)

    bid = {
        "title": opp.get("title") or "Untitled Opportunity",
        "scope": scope,
        "status": "Open" if is_open else "Closed",
        "deadline": deadline,
        "contact": poc.get("fullName") or "",
        "email": poc.get("email") or "",
        "phone": poc.get("phone") or "",
        "value": value,
        "url": opp.get("uiLink") or "",
    }
    return bid, (city or "Statewide")


@app.route("/scan", methods=["POST"])
def scan():
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key", "")
    device = data.get("device_id", "")
    location = (data.get("location") or "").strip()

    # 1. License/trial gate — front-end sends users to the Plan tab on 403.
    if not _license_is_active(key, device):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    if not location:
        return jsonify({"ok": False, "reason": "no_location"})
    if not SAM_API_KEY:
        return jsonify({"ok": False, "reason": "server_not_configured"})

    # 2. Resolve to a state (SAM.gov filters by state, not ZIP radius).
    state, label = _resolve_location(location)
    if not state:
        return jsonify({"ok": False, "reason": "location_not_found"})

    # 3. Serve from cache if we already pulled this state today.
    db = _db()
    cache = db.setdefault("scan_cache", {})
    today_key = datetime.datetime.now().strftime("%Y%m%d")
    cache_key = f"{state}|{today_key}"
    if cache_key in cache:
        c = cache[cache_key]
        return jsonify({"ok": True, "location": label,
                        "bids": c["bids"], "total_bids": c["total"], "cached": True})

    # 4. Live pull from SAM.gov.
    opps, status = _sam_fetch(state)
    if opps is None:
        code = 502 if status.startswith("sam_http") else 500
        return jsonify({"ok": False, "reason": status}), code

    # 5. Keep construction bids whose place of performance is IN the user's
    #    state, normalize, and group by city. (SAM's own state filter is
    #    unreliable, so we enforce it here too.)
    grouped = {}
    for opp in opps:
        if not _is_construction(opp):
            continue
        pop = opp.get("placeOfPerformance") or {}
        opp_state = ((pop.get("state") or {}).get("code") or "").upper()
        if opp_state != state:        # drop out-of-state / unspecified
            continue
        bid, city = _normalize_opp(opp)
        grouped.setdefault(city, []).append(bid)
    total = sum(len(v) for v in grouped.values())

    # 6. Cache (today only) and persist.
    cache[cache_key] = {"ts": datetime.datetime.now().isoformat(),
                        "bids": grouped, "total": total}
    db["scan_cache"] = {k: v for k, v in cache.items() if k.endswith(today_key)}
    _save_db(db)

    return jsonify({"ok": True, "location": label,
                    "bids": grouped, "total_bids": total})


# ═══════════════════════════════════════════════════════════
# /extract  —  optional AI extraction from scraped text (unchanged)
# ═══════════════════════════════════════════════════════════
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def _ai_extract(city, text):
    """Call OpenAI to pull structured bids from scraped text. Returns a list."""
    if not OPENAI_API_KEY:
        return None, "no_api_key"
    prompt = (
        f"You are a construction bid intelligence agent for {city}.\n\n"
        "Read the municipal website text and extract ANY construction, infrastructure, "
        "roofing, paving, road, utility, demolition, or HVAC/facility maintenance bids, "
        "RFPs, RFQs, or solicitations.\n\n"
        "Respond ONLY with a JSON array. Each item has keys: \"title\", \"scope\", "
        "\"status\" (\"Open\" or \"Closed\"), \"deadline\", \"contact\", \"email\", "
        "\"phone\", \"value\", \"url\". The \"value\" is the dollar amount ONLY if stated, "
        "else \"\". Use \"\" for any missing field. If no bids, return []. "
        "No markdown, no text outside the array.\n\n"
        f"WEBSITE TEXT:\n{text[:18000]}"
    )
    body = json.dumps({
        "model": OPENAI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = data["choices"][0]["message"]["content"].strip()
        if "```" in out:
            for p in out.split("```"):
                p = p.strip()
                if p.startswith("json"):
                    p = p[4:].strip()
                if p.startswith("[") and p.endswith("]"):
                    out = p
                    break
        s, e = out.find("["), out.rfind("]")
        if s != -1 and e != -1 and e > s:
            out = out[s:e + 1]
        bids = json.loads(out)
        return (bids if isinstance(bids, list) else []), "ok"
    except Exception as ex:
        return None, f"ai_error: {ex}"


@app.route("/extract", methods=["POST"])
def extract():
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key", "")
    device = data.get("device_id", "")
    city = data.get("city", "Unknown")
    text = data.get("text", "")
    if not _license_is_active(key, device):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    if not text.strip():
        return jsonify({"ok": True, "bids": []})
    bids, status = _ai_extract(city, text)
    if bids is None:
        return jsonify({"ok": False, "reason": status}), 500
    return jsonify({"ok": True, "bids": bids})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
