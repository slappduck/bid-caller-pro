"""
license_server.py — Tiny online license-validation server for Bid Caller Pro
═══════════════════════════════════════════════════════════════════════════

WHY THIS EXISTS:
The desktop app used to check licenses on the customer's own computer, which
means anyone could edit the files to bypass it. This server moves the secret
and the "is this key valid / has this trial been used" decision OFF the
customer's machine and onto a server only YOU control. That's what actually
protects revenue.

WHAT IT DOES:
  POST /validate   → app calls this on launch. Checks a key (or trial) and
                     returns whether access is allowed.
  POST /trial      → app calls this to start a trial, keyed to a device id,
                     so deleting local files can't reset it.
  POST /issue      → YOU call this (admin token required) after a Stripe
                     payment to mint a license key for a customer.
  POST /revoke     → YOU call this to kill a key (refund/chargeback).

HOW TO RUN LOCALLY (for testing):
  pip install flask
  python license_server.py
  # serves on http://127.0.0.1:5000

HOW TO DEPLOY FREE (always-on):
  - Render.com  → New Web Service → connect repo → start command:
        gunicorn license_server:app
  - Railway.app / Fly.io work the same way.
  - Set env vars ADMIN_TOKEN and LICENSE_SECRET in the host dashboard.
  Then put that public URL into subscription.py → SERVER_URL.

NEXT UPGRADE (recommended once you have customers):
  Replace the manual /issue step with a Stripe WEBHOOK so paid customers get
  a key automatically, and have /validate check the live Stripe subscription
  status so cancellations cut off access immediately.
"""

import os
import json
import hmac
import hashlib
import datetime
from flask import Flask, request, jsonify

app = Flask(__name__)

# ── Secrets (set these as environment variables in production!) ──
LICENSE_SECRET = os.environ.get("LICENSE_SECRET", "CHANGE_THIS_LONG_RANDOM_SECRET")
ADMIN_TOKEN    = os.environ.get("ADMIN_TOKEN", "CHANGE_THIS_ADMIN_TOKEN")
TRIAL_DAYS     = 7

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
#  AI BID EXTRACTION (server-side, so customers need no Ollama)
# ═══════════════════════════════════════════════════════════
import urllib.request

# Set this in Render env vars: OPENAI_API_KEY
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")  # cheap + capable

def _license_is_active(key, device):
    """Only paying/trial users may use our AI budget."""
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
        # Pull the JSON array out even if wrapped
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
    """
    Body: {key, device_id, city, text}
    Returns: {ok, bids:[...]} — runs the AI for licensed users only.
    """
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
