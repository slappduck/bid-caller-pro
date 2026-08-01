"""
license_server.py — License validation + bid scanning for Bid Caller Pro
═══════════════════════════════════════════════════════════════════════════
/scan now returns LOCAL + FEDERAL leads, filtered to a mile radius:
  • LOCAL   — DuckDuckGo (with browser-like headers, no key) finds local bid
              pages; pages are scraped and OpenAI extracts structured bids.
              If DDG ever returns nothing and TAVILY_API_KEY is set, Tavily is
              used as an automatic fallback.
  • FEDERAL — SAM.gov solicitations for the user's state.
  • Both are distance-filtered against the user's radius and grouped by city,
    then cached per area per day.

ENV VARS (set in Render → your service → Environment):
  LICENSE_SECRET           license signing secret
  ADMIN_TOKEN              admin token for /issue and /revoke
  OPENAI_API_KEY           REQUIRED for local extraction
  SAM_API_KEY              REQUIRED for federal bids (free key: sam.gov)
  TAVILY_API_KEY           OPTIONAL fallback search (free 1k/mo: tavily.com)
  UPSTASH_REDIS_REST_URL   persistent storage (free: upstash.com) -- needed so
  UPSTASH_REDIS_REST_TOKEN   trials/keys survive restarts
  SUPABASE_URL             your Supabase project URL (https://xxx.supabase.co)
  SUPABASE_ANON_KEY        your Supabase publishable/anon key (safe, public)
  STRIPE_WEBHOOK_SECRET    from Stripe -> Developers -> Webhooks (whsec_...)
  RESEND_API_KEY           OPTIONAL, emails the key to buyers (resend.com)
  FROM_EMAIL               OPTIONAL sender, e.g. "Bids <keys@yourdomain.com>"
  (BRAVE_API_KEY is no longer used — you can delete it.)

START COMMAND (raise the timeout — scans do real work, and wide-radius scans
now search multiple towns around the area, not just the center one, so they
take longer):
  gunicorn license_server:app --timeout 240 --workers 1 --threads 4

IMPORTANT: this MUST match Render's actual "Start Command" in the service's
Settings tab, or this comment is just decoration. --workers 1 alone means
ONE request at a time server-wide -- two customers scanning simultaneously
queue behind each other. --threads 4 lets that single worker process handle
several requests concurrently (cheap: same dyno/plan, no extra cost) since
scans are I/O-bound (network calls), not CPU-bound, so threads help here.
Going further (more workers, an always-on paid plan to avoid free-tier
cold starts) is a real cost tradeoff -- worth it once there's paying
customer volume, not required to ship.
"""

import os
import re
import json
import math
import hmac
import hashlib
import datetime
import time
import random
import threading
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

import bid_portals

app = Flask(__name__)

# ── CORS: production Netlify site + deploy previews ──
CORS(app, resources={r"/*": {"origins": [
    re.compile(r"^https://([a-z0-9-]+--)?curbcallpro\.netlify\.app$"),
]}})

# ── Secrets ──
LICENSE_SECRET = os.environ.get("LICENSE_SECRET", "CHANGE_THIS_LONG_RANDOM_SECRET")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "CHANGE_THIS_ADMIN_TOKEN")
TRIAL_DAYS = 7

# ── Persistence ──────────────────────────────────────────────────────────
# License data (trials/keys/customers) is stored in Upstash Redis via its REST
# API so it SURVIVES Render restarts/redeploys. If Upstash isn't configured it
# falls back to a local file (ephemeral) so the app still runs in dev.
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
_LIC_KEY = "bidcaller:license_db"
_LOCAL_LIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "license_db.json")
_LOCAL_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_cache.json")


def _empty_lic():
    return {"revoked": [], "trials": {}, "issued": {},
            "customers": {}, "emails": {}, "devices": {}}


def _upstash(*cmd):
    """Run one Redis command via Upstash REST. Returns (result, ok)."""
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        return None, False
    body = json.dumps(list(cmd)).encode("utf-8")
    req = urllib.request.Request(UPSTASH_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {UPSTASH_TOKEN}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8")).get("result"), True
    except Exception as ex:
        print(f"[upstash] error: {ex}", flush=True)
        return None, False


def _db():
    """Load persistent license data (Upstash if configured, else local file)."""
    result, ok = _upstash("GET", _LIC_KEY)
    if ok:
        if result:
            try:
                return json.loads(result)
            except Exception:
                pass
        return _empty_lic()
    try:
        with open(_LOCAL_LIC) as f:
            return json.load(f)
    except Exception:
        return _empty_lic()


def _save_db(db):
    _, ok = _upstash("SET", _LIC_KEY, json.dumps(db))
    if ok:
        return
    try:
        with open(_LOCAL_LIC, "w") as f:
            json.dump(db, f, indent=2)
    except Exception:
        pass


_CACHE_KEY = "bidcaller:scan_cache"


def _cache():
    """Scan/geo cache. Upstash-backed (like the license db) so the geocode
    cache and same-day scan cache survive Render restarts/redeploys instead
    of resetting every time — previously this was local-file-only, which on
    Render's ephemeral disk meant every deploy silently threw away the geo
    cache and forced re-geocoding. Falls back to a local file when Upstash
    isn't configured (dev)."""
    result, ok = _upstash("GET", _CACHE_KEY)
    if ok:
        if result:
            try:
                return json.loads(result)
            except Exception:
                pass
        return {"scan_cache": {}, "geo_cache": {}}
    try:
        with open(_LOCAL_CACHE) as f:
            return json.load(f)
    except Exception:
        return {"scan_cache": {}, "geo_cache": {}}


def _save_cache(c):
    _, ok = _upstash("SET", _CACHE_KEY, json.dumps(c))
    if ok:
        return
    try:
        with open(_LOCAL_CACHE, "w") as f:
            json.dump(c, f)
    except Exception:
        pass


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
    """Trials are keyed by verified account email, not the client-supplied
    device_id — a local device_id file can be deleted to get infinite fresh
    trials, but an email requires a real (and confirmed) Supabase account.
    device_id is still checked so trials started before this change (or by
    someone not signed in yet) keep working until they naturally expire."""
    data = request.get_json(force=True, silent=True) or {}
    device = (data.get("device_id") or "").strip()
    email = _verify_supabase_token(data.get("supabase_token", ""))
    db = _db()
    trials = db.setdefault("trials", {})

    def _status(rec):
        started = datetime.datetime.fromisoformat(rec["started"])
        end = started + datetime.timedelta(days=TRIAL_DAYS)
        if datetime.datetime.now() <= end:
            left = (end - datetime.datetime.now()).days + 1
            # expires_at is a precise timestamp (unlike the legacy
            # date-only "expires") so clients can show a live days/hours
            # countdown instead of a static day count that only ticks over
            # once every 24 hours.
            return jsonify({"ok": True, "active": True, "days_left": max(1, left),
                            "expires": end.isoformat()[:10], "expires_at": end.isoformat()})
        return jsonify({"ok": False, "active": False, "reason": "trial_expired"})

    trial_key = f"email:{email}" if email else None
    if trial_key and trial_key in trials:
        return _status(trials[trial_key])
    if device and device in trials:
        # Legacy/anonymous trial already running — honor it either way.
        return _status(trials[device])

    if not email:
        if not device:
            return jsonify({"ok": False, "reason": "no_device"})
        return jsonify({"ok": False, "reason": "signin_required"})

    started = datetime.datetime.now()
    trials[trial_key] = {"started": started.isoformat(), "email": email}
    _save_db(db)
    end = started + datetime.timedelta(days=TRIAL_DAYS)
    return jsonify({"ok": True, "active": True, "days_left": TRIAL_DAYS,
                    "expires": end.isoformat()[:10], "expires_at": end.isoformat(), "new": True})


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
# PAYMENTS: Stripe webhook -> auto-issue keys (survives restarts)
# ═══════════════════════════════════════════════════════════
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "Bid Caller Pro <onboarding@resend.dev>")
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "Yumiwave1@gmail.com")


def _verify_supabase_token(token):
    """Ask Supabase if this access token belongs to a real signed-in user.
    Returns the user's email on success, None on failure.
    No external packages needed — just a plain HTTPS call."""
    if not (SUPABASE_URL and SUPABASE_ANON_KEY and token):
        return None
    try:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": SUPABASE_ANON_KEY,
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            user = json.loads(resp.read().decode("utf-8"))
            return (user.get("email") or "").lower() or None
    except Exception:
        return None


def _stripe_verify(payload, sig_header):
    """Verify a Stripe webhook signature without the stripe library."""
    if not STRIPE_WEBHOOK_SECRET or not sig_header:
        return False
    try:
        parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
        signed = f"{parts.get('t')}.{payload.decode('utf-8')}".encode("utf-8")
        expected = hmac.new(STRIPE_WEBHOOK_SECRET.encode(), signed,
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, parts.get("v1", ""))
    except Exception:
        return False


def _send_key_email(email, key):
    """Email the license key to the buyer (only if Resend is configured)."""
    if not (RESEND_API_KEY and email):
        return
    body = json.dumps({
        "from": FROM_EMAIL,
        "to": [email],
        "subject": "Your Bid Caller Pro license key",
        "text": ("Thanks for subscribing to Bid Caller Pro!\n\n"
                 f"Your license key:\n\n    {key}\n\n"
                 "If the app didn't unlock automatically, open it, go to the Plan "
                 "tab, paste the key under 'Have a license key?', and tap Activate."),
    }).encode("utf-8")
    req = urllib.request.Request("https://api.resend.com/emails", data=body,
        method="POST", headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                                "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
        print(f"[email] sent key to {email}", flush=True)
    except Exception as ex:
        print(f"[email] failed: {ex}", flush=True)


# ── Admin error alerts: know about a crash before a customer reports it ──
_alert_lock = threading.Lock()
_alert_last_sent = {}
ALERT_COOLDOWN_SEC = 1800  # don't re-alert the same error more than every 30 min


def _alert_admin(subject, detail):
    """Email SUPPORT_EMAIL on server errors (best-effort, never raises).
    Rate-limited per distinct subject so a flapping error doesn't spam."""
    if not (RESEND_API_KEY and SUPPORT_EMAIL):
        return
    now = time.time()
    with _alert_lock:
        last = _alert_last_sent.get(subject, 0)
        if now - last < ALERT_COOLDOWN_SEC:
            return
        _alert_last_sent[subject] = now
    body = json.dumps({
        "from": FROM_EMAIL,
        "to": [SUPPORT_EMAIL],
        "subject": f"[CurbCall Pro] {subject}",
        "text": detail[:4000],
    }).encode("utf-8")
    req = urllib.request.Request("https://api.resend.com/emails", data=body,
        method="POST", headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                                "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as ex:
        print(f"[alert] failed to send alert email: {ex}", flush=True)


@app.errorhandler(Exception)
def _handle_unexpected_error(err):
    """Safety net: any unhandled exception in any route lands here instead
    of a bare 500 the customer can't do anything about, and we get an email
    about it instead of finding out when a customer complains. Routine HTTP
    errors (404 on a bad path, 405, etc.) are left to Flask's normal
    handling -- only genuine unexpected exceptions get alerted."""
    if isinstance(err, HTTPException):
        return err
    import traceback
    tb = traceback.format_exc()
    print(f"[error] unhandled exception on {request.path}: {err}\n{tb}", flush=True)
    _alert_admin(
        f"Unhandled error on {request.path}",
        f"{request.method} {request.path}\n\n{tb}",
    )
    return jsonify({"ok": False, "reason": "server_error"}), 500


def _issue_for(db, email, device, plan):
    """Create a key and index it by both email and device for later lookup."""
    months = 12 if plan == "annual" else 1
    key, exp = make_key(plan, months)
    db.setdefault("issued", {})[key] = {
        "plan": plan, "expires": exp[:10], "email": email, "device": device,
        "updated": datetime.datetime.now().isoformat()[:10],
    }
    if email:
        db.setdefault("emails", {})[email.lower()] = key
    if device:
        db.setdefault("devices", {})[device] = key
    return key


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    if not _stripe_verify(payload, request.headers.get("Stripe-Signature", "")):
        return jsonify({"ok": False, "reason": "bad_signature"}), 400
    try:
        event = json.loads(payload.decode("utf-8"))
    except Exception:
        return jsonify({"ok": False}), 400

    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    db = _db()

    if etype == "checkout.session.completed":
        email = ((obj.get("customer_details") or {}).get("email")
                 or obj.get("customer_email") or "")
        device = obj.get("client_reference_id") or ""
        cust = obj.get("customer") or ""
        amount = obj.get("amount_total") or 0
        plan = "annual" if amount and amount >= 10000 else "monthly"
        key = _issue_for(db, email, device, plan)
        if cust:
            db.setdefault("customers", {})[cust] = {
                "email": email, "device": device, "plan": plan}
        _save_db(db)
        _send_key_email(email, key)
        print(f"[stripe] issued {plan} key for {email or device}", flush=True)

    elif etype == "invoice.paid":
        cust = obj.get("customer") or ""
        info = db.get("customers", {}).get(cust)
        if info:
            _issue_for(db, info.get("email", ""), info.get("device", ""),
                       info.get("plan", "monthly"))
            _save_db(db)
            print(f"[stripe] renewed for {cust}", flush=True)

    elif etype in ("customer.subscription.deleted", "customer.subscription.paused"):
        cust = obj.get("customer") or ""
        info = db.get("customers", {}).get(cust)
        if info:
            email = (info.get("email") or "").lower()
            key = db.get("emails", {}).get(email)
            if key and key not in db.setdefault("revoked", []):
                db["revoked"].append(key)
            _save_db(db)
            print(f"[stripe] revoked for {cust}", flush=True)

    return jsonify({"ok": True})


@app.route("/mykey", methods=["POST"])
def mykey():
    """App calls this with its device id to auto-unlock after a purchase."""
    data = request.get_json(force=True, silent=True) or {}
    device = (data.get("device_id") or "").strip()
    db = _db()
    key = db.get("devices", {}).get(device)
    if not key:
        return jsonify({"ok": False, "reason": "no_key"})
    valid, plan, exp, _ = verify_key(key)
    if not valid or key in db.get("revoked", []):
        return jsonify({"ok": False, "reason": "inactive"})
    return jsonify({"ok": True, "key": key, "plan": plan, "expires": exp[:10]})


@app.route("/support", methods=["POST"])
def support():
    """Emails a customer's in-app support message to SUPPORT_EMAIL via
    Resend — reuses the same email setup as license-key delivery, no new
    service or secret needed."""
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip()
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"ok": False, "reason": "no_message"})
    if not RESEND_API_KEY:
        return jsonify({"ok": False, "reason": "email_unavailable"})
    payload = {
        "from": FROM_EMAIL,
        "to": [SUPPORT_EMAIL],
        "subject": f"Bid Caller Pro support request{f' from {email}' if email else ''}",
        "text": message,
    }
    if email:
        payload["reply_to"] = email
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=json.dumps(payload).encode("utf-8"),
        method="POST", headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                                "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
        return jsonify({"ok": True})
    except Exception as ex:
        print(f"[support] email failed: {ex}", flush=True)
        return jsonify({"ok": False, "reason": "send_failed"}), 500


@app.route("/claim", methods=["POST"])
def claim():
    """Restore a purchase on a new device using the buyer's email."""
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    device = (data.get("device_id") or "").strip()
    db = _db()
    key = db.get("emails", {}).get(email)
    if not key:
        return jsonify({"ok": False, "reason": "no_purchase"})
    valid, plan, exp, _ = verify_key(key)
    if not valid or key in db.get("revoked", []):
        return jsonify({"ok": False, "reason": "inactive"})
    if device:
        db.setdefault("devices", {})[device] = key
        _save_db(db)
    return jsonify({"ok": True, "key": key, "plan": plan, "expires": exp[:10]})


@app.route("/admin/list", methods=["POST"])
def admin_list():
    """Admin dashboard data: trials, issued keys, counts."""
    data = request.get_json(force=True, silent=True) or {}
    if data.get("admin_token") != ADMIN_TOKEN:
        return jsonify({"ok": False, "reason": "unauthorized"}), 401
    db = _db()
    now = datetime.datetime.now()
    revoked = set(db.get("revoked") or [])

    trials = []
    for dev, t in (db.get("trials") or {}).items():
        try:
            started = datetime.datetime.fromisoformat(t["started"])
        except Exception:
            continue
        end = started + datetime.timedelta(days=TRIAL_DAYS)
        active = now <= end
        trials.append({"device": dev, "started": started.isoformat()[:10],
                       "expires": end.isoformat()[:10], "active": active,
                       "days_left": (end - now).days + 1 if active else 0})

    issued = []
    for key, info in (db.get("issued") or {}).items():
        issued.append({"key": key, "email": info.get("email", ""),
                       "plan": info.get("plan", ""), "expires": info.get("expires", ""),
                       "device": info.get("device", ""), "revoked": key in revoked})

    return jsonify({"ok": True,
                    "trials": sorted(trials, key=lambda x: x["expires"], reverse=True),
                    "issued": sorted(issued, key=lambda x: x["expires"], reverse=True),
                    "counts": {
                        "active_trials": sum(1 for t in trials if t["active"]),
                        "total_trials": len(trials),
                        "active_subs": sum(1 for i in issued if not i["revoked"]),
                        "issued": len(issued),
                        "revoked": len(revoked),
                    }})


# ═══════════════════════════════════════════════════════════
# LICENSE / TRIAL GATE
# ═══════════════════════════════════════════════════════════
def _license_is_active(key, device, supabase_token=None):
    """Return True if this request has a valid license, active trial,
    OR a signed-in Supabase account (email-based trial counts too)."""
    key = (key or "").strip().upper()
    db = _db()

    # 1. Valid license key
    if key and key not in db.get("revoked", []):
        valid, _, _, _ = verify_key(key)
        if valid:
            return True

    # 2. Supabase account — check if their email has a key, or give them a trial
    if supabase_token:
        email = _verify_supabase_token(supabase_token)
        if email:
            # email has an active issued key?
            ekey = db.get("emails", {}).get(email)
            if ekey and ekey not in db.get("revoked", []):
                ev, _, _, _ = verify_key(ekey)
                if ev:
                    return True
            # email-based trial
            trials = db.setdefault("trials", {})
            trial_key = f"email:{email}"
            if trial_key in trials:
                started = datetime.datetime.fromisoformat(trials[trial_key]["started"])
                if datetime.datetime.now() <= started + datetime.timedelta(days=TRIAL_DAYS):
                    return True
            else:
                # First time this account scans — start their trial
                trials[trial_key] = {"started": datetime.datetime.now().isoformat(),
                                     "email": email}
                _save_db(db)
                return True

    # 3. Anonymous device-based trial (legacy / no account)
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


def _destination_point(lat, lon, bearing_deg, distance_mi):
    """Point `distance_mi` miles from (lat,lon) along compass bearing `bearing_deg`."""
    R = 3958.8
    br = math.radians(bearing_deg)
    lat1, lon1 = math.radians(lat), math.radians(lon)
    d_r = distance_mi / R
    lat2 = math.asin(math.sin(lat1) * math.cos(d_r) + math.cos(lat1) * math.sin(d_r) * math.cos(br))
    lon2 = lon1 + math.atan2(
        math.sin(br) * math.sin(d_r) * math.cos(lat1),
        math.cos(d_r) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def _reverse_geocode_city(lat, lon):
    """Free, keyless reverse geocode (same provider the app uses client-side
    for auto-fill) -- turns a lat/lon into a {city, state} so we can search
    towns scattered across a wide radius, not just the one the user typed."""
    url = (f"https://api.bigdatacloud.net/data/reverse-geocode-client"
           f"?latitude={lat}&longitude={lon}&localityLanguage=en")
    data = _get_json(url)
    if not data:
        return None
    city = data.get("city") or data.get("locality") or ""
    sub = (data.get("principalSubdivisionCode") or "").split("-")[-1].upper()
    country = (data.get("countryCode") or "").upper()
    if not city or sub not in STATE_ABBRS or country != "US":
        return None
    return city, sub


def _nearby_anchor_towns(center, radius):
    """Pick a handful of towns scattered around the search radius (not just
    the center city) so a wide-radius scan actually looks in more places
    instead of only searching near the one city the user typed. Skipped for
    tight radii where the center-only search already covers the area well."""
    if radius < 40:
        return []
    n = max(2, min(6, round(radius / 25) - 1))
    dist = radius * 0.7
    seen = {(center["city"].lower(), center["state"])}
    towns = []
    for i in range(n):
        bearing = i * (360.0 / n)
        lat, lon = _destination_point(center["lat"], center["lon"], bearing, dist)
        found = _reverse_geocode_city(lat, lon)
        if not found:
            continue
        city, state = found
        key = (city.lower(), state)
        if key in seen:
            continue
        seen.add(key)
        towns.append((city, state))
    return towns


_DEADLINE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y",
    "%B %d %Y", "%b %d %Y", "%m/%d/%y",
)


def _parse_deadline(text):
    """Best-effort parse of a free-text deadline into a date. None if unparseable."""
    if not text:
        return None
    t = str(text).strip()
    m = re.search(r"\d{4}-\d{2}-\d{2}", t)
    if m:
        t = m.group(0)
    for fmt in _DEADLINE_FORMATS:
        try:
            return datetime.datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


_NICHE_KEYWORDS = ("sidewalk", "ada ramp", "ada", "curb", "gutter", "concrete", "flatwork")


def _score_bid(bid):
    """Fit score used to sort each city's bids best-first, instead of
    leaving them in whatever order sources happened to return them. Combines
    deadline urgency, how strongly the text matches our niche (vs. a bid that
    only qualified through the broad construction NAICS codes), and whether
    there's enough info to actually act on it. Higher is better."""
    score = 0.0
    if bid.get("status") == "Closed":
        score -= 100
    else:
        d = _parse_deadline(bid.get("deadline"))
        if d:
            days_left = (d - datetime.datetime.now().date()).days
            score -= 50 if days_left < 0 else 0
            score += max(0, 30 - min(days_left, 30)) if days_left >= 0 else 0
    text = f"{bid.get('title', '')} {bid.get('scope', '')}".lower()
    score += sum(2 for k in _NICHE_KEYWORDS if k in text)
    if bid.get("email") or bid.get("phone"):
        score += 3
    if bid.get("value"):
        score += 1
    return score


def _apply_deadline_status(bid):
    """Force status to Closed if the stated deadline has already passed.
    Bids with no parseable deadline are left as-is (can't verify either way)."""
    d = _parse_deadline(bid.get("deadline"))
    if d and d < datetime.datetime.now().date():
        bid["status"] = "Closed"
    return bid


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


_COORD_RE = re.compile(r"^(-?\d{1,3}(?:\.\d+)?),\s*(-?\d{1,3}(?:\.\d+)?)$")


def _resolve_center(location):
    """Return {lat, lon, city, state} for a ZIP, 'City, ST' / 'City, State',
    or a raw 'lat, lon' pair (the map-click auto-fill falls back to this
    format when reverse geocoding hasn't resolved a city name yet, or when
    it fails outright — resolving it here instead of failing outright means
    a map click always produces a usable location)."""
    loc = (location or "").strip()
    if not loc:
        return None
    mc = _COORD_RE.match(loc)
    if mc:
        lat, lon = float(mc.group(1)), float(mc.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            got = _reverse_geocode_city(lat, lon)
            city, state = got if got else ("", "")
            return {"lat": lat, "lon": lon, "city": city, "state": state}
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
MAX_PAGES = int(os.environ.get("SCAN_MAX_PAGES", "16"))

_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _tavily_search(query, max_results=5):
    """Search via Tavily; returns [{url, content}]."""
    if not TAVILY_API_KEY:
        print("[scan] no TAVILY_API_KEY set", flush=True)
        return []
    body = json.dumps({
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": "advanced",
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
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        print(f"[scan] Tavily HTTP {e.code}: {detail}", flush=True)
        return []
    except Exception as ex:
        print(f"[scan] Tavily error: {ex}", flush=True)
        return []
    results = data.get("results") or []
    print(f"[scan] Tavily: {len(results)} results for {query!r}", flush=True)
    out = []
    for r in results:
        url = r.get("url") or ""
        if url:
            out.append({"url": url,
                        "content": r.get("raw_content") or r.get("content") or ""})
    return out


# ── DuckDuckGo with a browser "disguise" — primary local search (no key) ──
_DDG_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def _ddg_headers(ua):
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://duckduckgo.com/",
        "Origin": "https://duckduckgo.com",
        "Content-Type": "application/x-www-form-urlencoded",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Ch-Ua": '"Chromium";v="125", "Not.A/Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    }


def _parse_ddg(html):
    """Pull real result URLs out of a DuckDuckGo HTML results page."""
    out, seen = [], set()
    for m in re.finditer(r'uddg=([^&"\']+)', html):
        u = urllib.parse.unquote(m.group(1))
        if u.startswith("http") and u not in seen:
            seen.add(u)
            out.append(u)
    if out:
        return out
    for m in re.finditer(r'href="(https?://[^"]+)"', html):
        u = m.group(1)
        if "duckduckgo.com" in u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


# Circuit breaker: DuckDuckGo scraping is the one search path with no API
# key, so when TAVILY_API_KEY isn't set it's the *only* local search source.
# If it starts getting blocked (layout change, IP block on Render's shared
# dyno IP) every scan would silently return zero local bids with nothing in
# the logs pointing at why. This counter + the check in /scan turns that into
# a proactive admin email instead of a customer complaint.
_ddg_lock = threading.Lock()
_ddg_fail_streak = 0
DDG_TRIP_THRESHOLD = 8


def _ddg_note_result(found):
    global _ddg_fail_streak
    with _ddg_lock:
        _ddg_fail_streak = 0 if found else _ddg_fail_streak + 1


def _ddg_is_degraded():
    with _ddg_lock:
        return _ddg_fail_streak >= DDG_TRIP_THRESHOLD


def _ddg_search(query, count=6):
    """Scrape DuckDuckGo with rotating, browser-like headers. Returns [{url, content}]."""
    ua = random.choice(_DDG_UAS)
    for endpoint in ("https://html.duckduckgo.com/html/",
                     "https://lite.duckduckgo.com/lite/"):
        try:
            body = urllib.parse.urlencode({"q": query, "kl": "us-en"}).encode()
            req = urllib.request.Request(endpoint, data=body, headers=_ddg_headers(ua))
            with urllib.request.urlopen(req, timeout=20) as resp:
                html = resp.read().decode("utf-8", "ignore")
            found = _parse_ddg(html)
            if found:
                _ddg_note_result(True)
                return [{"url": u, "content": ""} for u in found[:count]]
            print(f"[scan] DDG no links from {endpoint} for {query!r}", flush=True)
        except Exception as ex:
            print(f"[scan] DDG error ({endpoint}): {ex}", flush=True)
    _ddg_note_result(False)
    return []


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
    _apply_deadline_status(bid)
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
# AI-DRAFTED BID PROPOSALS
# Turns a bid the user is looking at into a ready-to-edit cover-letter-style
# proposal draft, personalized with the contractor's own company info (sent
# from the client — nothing is stored server-side). Same license gate and
# same OpenAI key as /scan and /extract, no new secrets needed.
# ═══════════════════════════════════════════════════════════
def _ai_draft_proposal(bid, company):
    if not OPENAI_API_KEY:
        return None
    co_name = (company.get("name") or "").strip() or "[Your Company Name]"
    co_contact = (company.get("contact") or "").strip() or "[Your Name]"
    co_phone = (company.get("phone") or "").strip() or "[Your Phone]"
    co_email = (company.get("email") or "").strip() or "[Your Email]"
    co_specialty = (company.get("specialty") or "").strip() or \
        "sidewalk, ADA ramp, and curb & gutter concrete work"

    prompt = (
        "You are an experienced construction estimator writing a bid proposal "
        "cover letter for a small concrete contracting company responding to a "
        "public bid or RFP. Write a professional, concise, ready-to-send "
        "proposal letter body (no subject line, no markdown) that:\n"
        "- Opens by referencing the specific project by name\n"
        "- States the company's interest and relevant experience in "
        f"{co_specialty}\n"
        "- Briefly addresses the stated scope of work\n"
        "- Notes willingness to meet the stated deadline (if one is given)\n"
        "- Requests any plan documents, addenda, or walkthrough details needed "
        "to submit a formal quote\n"
        "- Closes with the contact information provided\n"
        "Keep it under 300 words. Do NOT invent specific dollar amounts, "
        "license numbers, bond amounts, or past project names — omit those "
        "details rather than making them up.\n\n"
        f"PROJECT TITLE: {bid.get('title', '')}\n"
        f"SCOPE: {bid.get('scope', '')}\n"
        f"DEADLINE: {bid.get('deadline', '')}\n"
        f"LOCATION: {bid.get('city', '')}\n\n"
        f"COMPANY NAME: {co_name}\n"
        f"CONTACT PERSON: {co_contact}\n"
        f"PHONE: {co_phone}\n"
        f"EMAIL: {co_email}\n"
    )
    body = json.dumps({
        "model": OPENAI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except Exception as ex:
        print(f"[draft-proposal] error: {ex}", flush=True)
        return None


@app.route("/draft-proposal", methods=["POST"])
def draft_proposal():
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key", "")
    device = data.get("device_id", "")
    supabase_token = data.get("supabase_token", "")
    if not _license_is_active(key, device, supabase_token):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "reason": "ai_unavailable"})

    bid = data.get("bid") or {}
    company = data.get("company") or {}
    if not (bid.get("title") or "").strip():
        return jsonify({"ok": False, "reason": "no_bid"})

    draft = _ai_draft_proposal(bid, company)
    if draft is None:
        return jsonify({"ok": False, "reason": "ai_error"}), 500
    return jsonify({"ok": True, "draft": draft})


# ═══════════════════════════════════════════════════════════
# /scan  —  LOCAL (Brave + AI) + FEDERAL (SAM), radius-filtered
# ═══════════════════════════════════════════════════════════
def _run_known_portals(city, state, ai_label, grouped, center, radius, cdb,
                        city_coords, lock, pdb, default_city=""):
    """Fetch URLs already known (from a prior scan or the seed list) to be
    this city's real bid page, directly — no search engine involved. This is
    the fast, deterministic path that /scan tries before falling back to
    live search: no per-query search-API cost, no dependency on that day's
    search rankings, and it can't be blocked the way scraping DuckDuckGo can.
    Entries age out (via bid_portals.MAX_FAIL) if they stop returning real
    content, so a site redesign doesn't silently keep failing forever."""
    with lock:
        portals = list(bid_portals.get_portals(pdb, city, state))[:6]
    raw = 0
    for entry in portals:
        url = entry["url"]
        text = _fetch_text(url)
        ok = len(text) >= 200
        with lock:
            bid_portals.record_result(pdb, city, state, url, ok)
        if not ok:
            continue
        bids = _ai_extract(ai_label, text)
        if not bids:
            continue
        with lock:
            raw += len(bids)
            for b in bids:
                if isinstance(b, dict):
                    b.setdefault("url", url)
                    _place_bid(grouped, b, center, radius, cdb, default_city=default_city,
                              city_coords=city_coords)
    return raw


def _run_local_queries(queries, ai_label, max_pages, grouped, center, radius, cdb,
                        city_coords, seen_urls, lock, pdb, default_city="", state=""):
    """Run one town's worth of search queries and extract bids from the top
    pages. Each town gets its own max_pages slice so a big scan with several
    anchor towns doesn't let the first town's results crowd out the rest.

    `lock` guards every read/write to the structures shared across towns
    (seen_urls, grouped, city_coords, cdb's geo_cache, pdb the portal
    directory) since towns are now run concurrently from the /scan route.
    The searches themselves stay sequential per-town (with the existing
    throttle) to avoid hammering DuckDuckGo; only the independent per-page
    fetch+AI-extract step below is fanned out.

    Any page that turns out to be a real per-agency bid page (not a generic
    aggregator listing) gets recorded into the portal directory, so future
    scans of this city can skip straight to it via _run_known_portals
    instead of re-searching — coverage improves scan over scan instead of
    resetting every time."""
    items = []
    for q in queries:
        results = _tavily_search(q, max_results=6) if TAVILY_API_KEY else []
        if not results:
            results = _ddg_search(q)
        for r in results:
            with lock:
                if r["url"] in seen_urls:
                    continue
                seen_urls.add(r["url"])
            items.append(r)
        time.sleep(0.4)

    raw = [0]

    def _process(it):
        text = it["content"] or _fetch_text(it["url"])
        if len(text) < 200:
            return
        bids = _ai_extract(ai_label, text)
        if not bids:
            return
        with lock:
            raw[0] += len(bids)
            for b in bids:
                if isinstance(b, dict):
                    b.setdefault("url", it["url"])
                    bid_city = (b.get("city") or default_city or "").split(",")[0].strip()
                    _place_bid(grouped, b, center, radius, cdb, default_city=default_city,
                              city_coords=city_coords)
                    if bid_city and state:
                        bid_portals.learn_portal(pdb, bid_city, state, it["url"])

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(_process, items[:max_pages]))

    return raw[0]


def _place_bid(grouped, bid, center, radius, db, default_city="", city_coords=None):
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
    _apply_deadline_status(bid)
    bid.pop("city", None)
    grouped.setdefault(city, []).append(bid)
    if city_coords is not None:
        city_coords[city] = {"lat": coords[0], "lon": coords[1]}


@app.route("/scan", methods=["POST"])
def scan():
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key", "")
    device = data.get("device_id", "")
    supabase_token = data.get("supabase_token", "")
    location = (data.get("location") or "").strip()
    try:
        radius = float(data.get("radius") or 25)
    except (TypeError, ValueError):
        radius = 25.0

    if not _license_is_active(key, device, supabase_token):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    if not location:
        return jsonify({"ok": False, "reason": "no_location"})

    center = _resolve_center(location)
    if not center:
        return jsonify({"ok": False, "reason": "location_not_found"})

    cdb = _cache()
    today = datetime.datetime.now().strftime("%Y%m%d")
    cache = cdb.setdefault("scan_cache", {})
    ckey = f"{center['state']}|{center['city'].lower()}|{int(radius)}|{today}"
    if ckey in cache:
        c = cache[ckey]
        return jsonify({"ok": True, "location": f"{center['city']}, {center['state']}",
                        "bids": c["bids"], "total_bids": c["total"],
                        "city_coords": c.get("city_coords", {}),
                        "center": c.get("center", {"lat": center["lat"], "lon": center["lon"],
                                                   "label": f"{center['city']}, {center['state']}"}),
                        "cached": True})

    pdb = bid_portals.load_directory()
    grouped = {}
    city_coords = {}
    local_raw = 0

    # ---- LOCAL: disguised DuckDuckGo first, Tavily fallback if it's empty ----
    # A wide radius is only useful if we actually search more than the one
    # town the user typed -- otherwise a 125mi scan just returns whatever the
    # engines happen to surface near the center point. So for radius >= 40mi
    # we also pick a handful of towns scattered around the radius (via free
    # reverse geocoding) and run a lighter query set against each of them.
    if OPENAI_API_KEY:
        c, s = center["city"], center["state"]
        seen_urls = set()
        lock = threading.Lock()
        center_queries = [
            f"{c} {s} sidewalk replacement concrete construction bid invitation",
            f"{c} {s} ADA ramp curb gutter concrete bid opportunities",
            f"{c} {s} concrete flatwork sidewalk public works solicitation",
            f"{c} {s} city county sidewalk curb concrete RFP",
            f"{s} concrete sidewalk ADA curb bids near {c}",
            f"{c} {s} school district sidewalk ADA concrete project bid",
            f"{c} {s} sidewalk ADA curb bid site:bidnetdirect.com OR site:demandstar.com",
            f"{c} {s} sidewalk ADA curb bid site:planetbids.com OR site:publicpurchase.com",
            f"{c} {s} sidewalk ADA curb bid site:questcdn.com OR site:opengov.com",
            f"{c} {s} sidewalk ADA curb bid site:bonfirehub.com",
            f"{c} {s} sidewalk ADA curb bid site:civicplus.com OR site:municode.com",
            f"{c} {s} sidewalk ADA curb bid site:bidexpress.com",
            f"{c} {s} invitation to bid concrete sidewalk 2026",
            f"{c} {s} county road department concrete curb bid notice",
            f"{c} {s} Safe Routes to School OR ADA transition plan sidewalk bid",
            f"{c} {s} CDBG sidewalk curb ramp bid notice to contractors",
            f"{c} {s} sidewalk ADA curb bid site:bidsearch.com",
        ]
        if center["state"] == "MO":
            center_queries.append(f"{c} {s} sidewalk ADA curb bid site:missouribuys.mo.gov")

        anchors = _nearby_anchor_towns(center, radius)

        # Each "town job" (center + every anchor) is fully independent work,
        # so they run concurrently instead of one after another — this is
        # the biggest lever on wall-clock scan time. Capped at 4 workers so
        # we don't fire too many simultaneous search-engine requests at once
        # (DuckDuckGo in particular will start blocking if hammered).
        def _run_center():
            got = _run_known_portals(c, s, f"{c}, {s}", grouped, center, radius,
                                      cdb, city_coords, lock, pdb, default_city="")
            got += _run_local_queries(center_queries, f"{c}, {s}", MAX_PAGES,
                                      grouped, center, radius, cdb, city_coords,
                                      seen_urls, lock, pdb, default_city="", state=s)
            print(f"[scan] {got} raw bids from {c}, {s} (center)", flush=True)
            return got

        def _run_anchor(anchor):
            ac, ast = anchor
            anchor_queries = [
                f"{ac} {ast} sidewalk ADA curb concrete bid invitation",
                f"{ac} {ast} sidewalk ADA curb bid site:bidnetdirect.com OR site:demandstar.com",
                f"{ac} {ast} sidewalk ADA curb bid site:planetbids.com OR site:publicpurchase.com",
                f"{ac} {ast} concrete curb gutter bid Bonfire OpenGov CivicPlus procurement",
                f"{ac} {ast} invitation to bid concrete sidewalk ADA ramp 2026",
                f"{ac} {ast} sidewalk ADA curb bid site:bidsearch.com",
            ]
            if ast == "MO":
                anchor_queries.append(f"{ac} {ast} sidewalk ADA curb bid site:missouribuys.mo.gov")
            got = _run_known_portals(ac, ast, f"{ac}, {ast}", grouped, center, radius,
                                      cdb, city_coords, lock, pdb, default_city=ac)
            got += _run_local_queries(anchor_queries, f"{ac}, {ast}", 5,
                                      grouped, center, radius, cdb, city_coords,
                                      seen_urls, lock, pdb, default_city=ac, state=ast)
            print(f"[scan] {got} raw bids from {ac}, {ast} (anchor)", flush=True)
            return got

        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = [ex.submit(_run_center)] + [ex.submit(_run_anchor, a) for a in anchors]
            for f in as_completed(futures):
                local_raw += f.result()

        print(f"[scan] {local_raw} raw local bids extracted total "
              f"({len(anchors)} anchor town(s) searched)", flush=True)

        if not TAVILY_API_KEY and _ddg_is_degraded():
            _alert_admin(
                "DuckDuckGo search appears blocked",
                "DuckDuckGo has returned empty results on 8+ consecutive "
                "searches and no TAVILY_API_KEY is configured, so local bid "
                "search may be completely down (only SAM.gov federal bids "
                "would still work). Add a TAVILY_API_KEY (tavily.com, free "
                "tier) as a fallback search backend, or investigate whether "
                "Render's outbound IP has been blocked by DuckDuckGo.",
            )

    # ---- FEDERAL: SAM.gov for the state, radius-filtered ----
    if SAM_API_KEY:
        for opp in (_sam_fetch(center["state"]) or []):
            if not _is_construction(opp):
                continue
            bid, city = _normalize_opp(opp)
            _place_bid(grouped, bid, center, radius, cdb, default_city=city,
                      city_coords=city_coords)

    for city_bids in grouped.values():
        city_bids.sort(key=_score_bid, reverse=True)

    total = sum(len(v) for v in grouped.values())
    print(f"[scan] {int(radius)} mi from {center['city']},{center['state']} "
          f"-> {total} bids kept (local_raw={local_raw})", flush=True)

    result = {"bids": grouped, "total": total, "city_coords": city_coords,
              "center": {"lat": center["lat"], "lon": center["lon"],
                        "label": f"{center['city']}, {center['state']}"}}

    # cache (today only) + persist geo cache
    cache[ckey] = {"ts": datetime.datetime.now().isoformat(), **result}
    cdb["scan_cache"] = {k: v for k, v in cache.items() if k.endswith(today)}
    _save_cache(cdb)
    bid_portals.save_directory(pdb)

    return jsonify({"ok": True, "location": f"{center['city']}, {center['state']}",
                    "bids": grouped, "total_bids": total, "city_coords": city_coords,
                    "center": result["center"],
                    "debug": {"raw_local": local_raw, "kept": total}})


# ═══════════════════════════════════════════════════════════
# /upcoming — planned work spotted in council agendas, budgets & CIPs,
# BEFORE it becomes a formal bid. Same license gate + radius filter as /scan.
# ═══════════════════════════════════════════════════════════
def _ai_extract_upcoming(area, text):
    if not OPENAI_API_KEY:
        return None
    prompt = (
        f"You scout FUTURE concrete work for contractors near {area}, before it "
        "becomes a formal bid.\n\n"
        "From the website text below (council agendas, budgets, capital improvement "
        "plans / CIPs, engineering department pages, planning documents), find ANY "
        "mention of PLANNED or PROPOSED sidewalk, ADA ramp, curb & gutter, or related "
        "concrete/flatwork projects that are not yet an open bid.\n\n"
        "Respond ONLY with a JSON array. Each item has keys: \"title\", \"scope\", "
        "\"timeframe\" (e.g. a fiscal year, quarter, or phrase like \"FY2027\" or "
        "\"pending council approval\" — use \"\" if unclear), \"contact\", \"email\", "
        "\"phone\", \"url\", \"city\". \"city\" is the US city where the work is "
        "planned, exactly as written; if unclear use \"\" and do NOT guess. "
        "Use \"\" for any missing field. If nothing planned is mentioned, return []. "
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
        items = json.loads(out)
        return items if isinstance(items, list) else []
    except Exception:
        return None


@app.route("/upcoming", methods=["POST"])
def upcoming():
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key", "")
    device = data.get("device_id", "")
    supabase_token = data.get("supabase_token", "")
    location = (data.get("location") or "").strip()
    try:
        radius = float(data.get("radius") or 25)
    except (TypeError, ValueError):
        radius = 25.0

    if not _license_is_active(key, device, supabase_token):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    if not location:
        return jsonify({"ok": False, "reason": "no_location"})

    center = _resolve_center(location)
    if not center:
        return jsonify({"ok": False, "reason": "location_not_found"})
    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "reason": "ai_unavailable"})

    cdb = _cache()
    today = datetime.datetime.now().strftime("%Y%m%d")
    cache = cdb.setdefault("upcoming_cache", {})
    ckey = f"{center['state']}|{center['city'].lower()}|{int(radius)}|{today}"
    if ckey in cache:
        c = cache[ckey]
        return jsonify({"ok": True, "location": f"{center['city']}, {center['state']}",
                        "items": c["items"], "total": c["total"],
                        "city_coords": c.get("city_coords", {}), "cached": True})

    c, s = center["city"], center["state"]
    queries = [
        f"{c} {s} capital improvement plan sidewalk ADA curb",
        f"{c} {s} council agenda sidewalk program budget",
        f"{c} {s} engineering department sidewalk ADA transition plan",
        f"{c} {s} public works budget sidewalk curb gutter fiscal year",
        f"{c} {s} CIP concrete infrastructure plan upcoming",
    ]
    seen, pages = set(), []
    for q in queries:
        results = _tavily_search(q, max_results=6) if TAVILY_API_KEY else []
        if not results:
            results = _ddg_search(q)
        for r in results:
            if r["url"] not in seen:
                seen.add(r["url"])
                pages.append(r)
        time.sleep(0.6)

    grouped, city_coords = {}, {}
    for it in pages[:MAX_PAGES]:
        text = it["content"] or _fetch_text(it["url"])
        if len(text) < 200:
            continue
        items = _ai_extract_upcoming(f"{c}, {s}", text)
        if not items:
            continue
        for b in items:
            if isinstance(b, dict):
                b.setdefault("url", it["url"])
                b["status"] = "Planned"
                _place_bid(grouped, b, center, radius, cdb, default_city="",
                          city_coords=city_coords)

    total = sum(len(v) for v in grouped.values())
    cache[ckey] = {"ts": datetime.datetime.now().isoformat(), "items": grouped,
                  "total": total, "city_coords": city_coords}
    cdb["upcoming_cache"] = {k: v for k, v in cache.items() if k.endswith(today)}
    _save_cache(cdb)

    return jsonify({"ok": True, "location": f"{center['city']}, {center['state']}",
                    "items": grouped, "total": total, "city_coords": city_coords})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
