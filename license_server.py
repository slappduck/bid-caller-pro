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

  Real automated saved-search email alerts (/run-saved-search-alerts) --
  OFF until BOTH of these are set; safe to leave unset indefinitely:
  SUPABASE_SERVICE_ROLE_KEY  Supabase -> Settings -> API -> service_role
                             key. HIGH PRIVILEGE (bypasses row-level
                             security for the whole project) -- Render env
                             var ONLY, never send this to a client.
  CRON_SECRET                a random string you make up; put the SAME
                             value in this Render env var AND in the
                             GitHub repo's Actions secrets (see
                             .github/workflows/saved-search-alerts.yml).
                             This is what lets that scheduled workflow
                             (and only it) trigger the alert run.

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
import bid_sources
import gov_directory
import residential_permits

app = Flask(__name__)

# ── CORS: production Netlify site + deploy previews ──
CORS(app, resources={r"/*": {"origins": [
    re.compile(r"^https://([a-z0-9-]+--)?curbcallpro\.netlify\.app$"),
]}})

# ── Secrets ──
LICENSE_SECRET = os.environ.get("LICENSE_SECRET", "CHANGE_THIS_LONG_RANDOM_SECRET")
_ADMIN_TOKEN_PLACEHOLDER = "CHANGE_THIS_ADMIN_TOKEN"
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", _ADMIN_TOKEN_PLACEHOLDER)


def _admin_configured():
    """False when ADMIN_TOKEN was never set to a real value.

    The fallback above is a literal published in this public repo, so treating
    it as a valid password would let anyone who reads the source mint
    themselves unlimited licence keys through /issue. An unset or still-default
    token disables the admin endpoints entirely rather than leaving them open.
    """
    return bool(ADMIN_TOKEN) and ADMIN_TOKEN != _ADMIN_TOKEN_PLACEHOLDER


def _admin_ok(supplied):
    """Constant-time admin token check. Compare with hmac, never ==, so the
    response time can't be used to guess the token a character at a time."""
    return _admin_configured() and hmac.compare_digest(supplied or "", ADMIN_TOKEN)
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
    if not _admin_configured():
        return jsonify({"ok": False, "reason": "admin_not_configured"}), 503
    if not _admin_ok(data.get("admin_token")):
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
    if not _admin_configured():
        return jsonify({"ok": False, "reason": "admin_not_configured"}), 503
    if not _admin_ok(data.get("admin_token")):
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


@app.route("/health", methods=["GET"])
def health_detail():
    """Which backends are actually wired up, and is local search still working.

    Exists because the failure that matters most here is silent: if a search
    backend is unset or has started getting blocked, /scan still returns 200
    with a smaller, worse set of bids and nothing anywhere says why. This
    answers "is the search engine actually at full strength right now" from a
    browser, without running a scan or spending an API call.

    Reports only whether each secret is present, never any part of its value,
    so it is safe to leave unauthenticated the way the plain / probe is.
    """
    # Never call out to a provider here: this endpoint has to stay instant and
    # free, and a hung upstream must not make the health check itself look down.
    backends = {
        "openai": bool(OPENAI_API_KEY),          # AI bid extraction — /scan is inert without it
        "tavily": bool(TAVILY_API_KEY),          # primary local search
        "sam_gov": bool(SAM_API_KEY),            # federal bids
        "supabase": bool(SUPABASE_URL and SUPABASE_ANON_KEY),
        "upstash_redis": bool(UPSTASH_URL and UPSTASH_TOKEN),  # else state is lost on redeploy
        "resend_email": bool(RESEND_API_KEY),
        "saved_search_alerts": bool(SUPABASE_SERVICE_ROLE_KEY and CRON_SECRET and RESEND_API_KEY),
    }
    tav = _tavily_health()
    with _ddg_lock:
        ddg_streak = _ddg_fail_streak
    # DuckDuckGo is scraped, so it can be blocked outright. That only threatens
    # local search when Tavily isn't configured to take over.
    ddg = {
        "consecutive_empty_searches": ddg_streak,
        "degraded": ddg_streak >= DDG_TRIP_THRESHOLD,
        "is_sole_local_search": not bool(TAVILY_API_KEY),
    }
    problems = []
    # A configured-but-rejected key is worse than an absent one: everything
    # keeps returning 200 and scans just quietly come back nearly empty.
    if backends["tavily"] and tav["quota_or_auth_failure"]:
        problems.append(
            f"Tavily is rejecting searches (HTTP {tav['last_status']}) — most likely the "
            "monthly credit allowance is spent. Local bid search has fallen back to "
            "scraping DuckDuckGo and scans will look almost empty until this clears.")
    elif backends["tavily"] and tav["failing"]:
        problems.append("Every Tavily search this run has failed — local bid search is degraded.")
    if not backends["openai"]:
        problems.append("OPENAI_API_KEY unset — /scan and /upcoming return no local bids at all")
    if not backends["tavily"] and ddg["degraded"]:
        problems.append("DuckDuckGo appears blocked and no TAVILY_API_KEY is set — "
                        "local bid search is effectively down")
    elif not backends["tavily"]:
        problems.append("TAVILY_API_KEY unset — local search depends solely on scraping DuckDuckGo")
    if not backends["sam_gov"]:
        problems.append("SAM_API_KEY unset — no federal bids in results")
    if not backends["upstash_redis"]:
        problems.append("Upstash unset — licenses, portal directory and geo cache "
                        "are lost on every redeploy")
    return jsonify({
        "service": "Bid Caller Pro License Server",
        "status": "ok" if not problems else "degraded",
        "backends": backends,
        "local_search": ddg,
        "tavily": {k: tav[k] for k in
                   ("ok", "failed", "last_status", "last_error",
                    "quota_or_auth_failure", "failing")},
        "search_depth": TAVILY_DEPTH,
        # The recall knobs, so what's actually running is visible without
        # reading Render's env-var screen. All are env-tunable; raising them
        # trades scan time and OpenAI spend for coverage.
        "scan_config": {
            "max_pages_per_town": MAX_PAGES,          # SCAN_MAX_PAGES
            "page_workers": PAGE_WORKERS,             # SCAN_PAGE_WORKERS
            "max_pages_per_domain": MAX_PAGES_PER_DOMAIN,  # SCAN_MAX_PAGES_PER_DOMAIN
            "max_anchor_towns": MAX_ANCHOR_TOWNS,     # SCAN_MAX_ANCHORS
            "federal_window_days": SCAN_WINDOW_DAYS,  # SCAN_WINDOW_DAYS
            "geo_miss_retry_hours": GEO_MISS_RETRY_HOURS,
            "model": OPENAI_MODEL,                    # OPENAI_MODEL
        },
        "problems": problems,
    })


# ═══════════════════════════════════════════════════════════
# PAYMENTS: Stripe webhook -> auto-issue keys (survives restarts)
# ═══════════════════════════════════════════════════════════
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "Bid Caller Pro <onboarding@resend.dev>")
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "Yumiwave1@gmail.com")

# Saved-search email alerts (/run-saved-search-alerts, see below). Both are
# OPTIONAL and the feature is inert (returns "not_configured") until both are
# set -- nothing changes for existing users until you deliberately turn this
# on. SUPABASE_SERVICE_ROLE_KEY is a HIGH-PRIVILEGE secret (bypasses every
# row-level-security policy in the project) -- only ever set it as a Render
# env var, never ship it to a client. CRON_SECRET is a password only your
# scheduler (e.g. a GitHub Actions workflow) knows, so this endpoint can't be
# triggered by randoms hammering the URL.
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")


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


# ── Saved-search alerts: Supabase admin access + new-bid emails ──
# Uses the service-role key to read across ALL users' saved_searches (bypasses
# the row-level-security policies the anon key is normally scoped by) and to
# look up a user's email via the Auth admin API. See /run-saved-search-alerts.
def _supabase_admin_request(path):
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return None
    req = urllib.request.Request(
        f"{SUPABASE_URL}{path}",
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as ex:
        print(f"[alerts] supabase admin request failed ({path}): {ex}", flush=True)
        return None


def _fetch_all_saved_searches():
    data = _supabase_admin_request("/rest/v1/saved_searches?select=user_id,location,radius")
    return data if isinstance(data, list) else []


def _get_user_email(user_id):
    data = _supabase_admin_request(f"/auth/v1/admin/users/{user_id}")
    if isinstance(data, dict):
        return data.get("email") or (data.get("user") or {}).get("email")
    return None


def _bid_sig(city, bid):
    """Stable id for 'have we already told this user about this bid' —
    based on content, not a server-assigned id (there isn't one), so the
    same real-world bid gets the same signature scan over scan."""
    raw = f"{city}|{bid.get('title', '')}|{bid.get('deadline', '')}|{bid.get('url', '')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _send_alert_email(email, location, radius, new_bids):
    lines = [f'New bids matching your saved search "{location}" ({int(radius)} mi):', ""]
    for city, b in new_bids[:20]:
        line = f"- {b.get('title') or 'Untitled'} — {city}"
        if b.get("deadline"):
            line += f" (due {b['deadline']})"
        lines.append(line)
        if b.get("url"):
            lines.append(f"  {b['url']}")
    if len(new_bids) > 20:
        lines.append(f"...and {len(new_bids) - 20} more.")
    lines.append("")
    lines.append("Open Bid Caller Pro to see full details or save any of these to your pipeline.")
    body = json.dumps({
        "from": FROM_EMAIL,
        "to": [email],
        "subject": f"{len(new_bids)} new bid(s) near {location}",
        "text": "\n".join(lines),
    }).encode("utf-8")
    req = urllib.request.Request("https://api.resend.com/emails", data=body,
        method="POST", headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                                "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
        print(f"[alerts] sent {len(new_bids)} new-bid email to {email}", flush=True)
    except Exception as ex:
        print(f"[alerts] email failed for {email}: {ex}", flush=True)


def _run_saved_search_alerts():
    """Runs every saved search once, diffs against what that search already
    notified about last time (persisted in the same Upstash-backed cache as
    scan_cache/geo_cache), and emails the user only the NEW open bids. The
    first run for a brand-new saved search has nothing to diff against, so
    it emails everything currently open -- an immediate "yes, this is
    working" confirmation rather than a bug.

    Sequential, not parallelized across searches: reusing _perform_scan
    already fans out per-town internally, and running many users' searches
    concurrently on top of that risks hammering DuckDuckGo/OpenAI far harder
    than a single interactive /scan does. Fine at today's volume; if the
    saved-search count grows large enough that a daily run runs long, that's
    a sign to add pagination/batching here, not to parallelize blindly."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return {"ok": False, "reason": "supabase_not_configured"}
    if not RESEND_API_KEY:
        return {"ok": False, "reason": "email_not_configured"}

    searches = _fetch_all_saved_searches()
    cdb = _cache()
    seen_store = cdb.setdefault("alert_seen", {})
    email_cache = {}
    emails_sent = 0
    users_checked = set()
    errors = []

    for s in searches:
        user_id = s.get("user_id")
        location = (s.get("location") or "").strip()
        try:
            radius = float(s.get("radius") or 25)
        except (TypeError, ValueError):
            radius = 25.0
        if not (user_id and location):
            continue
        users_checked.add(user_id)

        try:
            outcome = _perform_scan(location, radius)
        except Exception as ex:
            errors.append(f"{user_id}/{location}: {ex}")
            print(f"[alerts] scan failed for {location!r}: {ex}", flush=True)
            continue
        if not outcome:
            continue

        seen_key = f"{user_id}|{location.lower()}|{int(radius)}"
        seen = set(seen_store.get(seen_key, []))
        all_sigs, new_bids = [], []
        for city, bids in (outcome.get("bids") or {}).items():
            for b in bids:
                if (b.get("status") or "").lower() == "closed":
                    continue
                sig = _bid_sig(city, b)
                all_sigs.append(sig)
                if sig not in seen:
                    new_bids.append((city, b))
        seen_store[seen_key] = all_sigs[-300:]  # cap so this can't grow forever

        if new_bids:
            if user_id not in email_cache:
                email_cache[user_id] = _get_user_email(user_id) or ""
            email = email_cache[user_id]
            if email:
                _send_alert_email(email, outcome.get("location", location), radius, new_bids)
                emails_sent += 1

    cdb["alert_seen"] = seen_store
    _save_cache(cdb)
    return {"ok": True, "searches_checked": len(searches),
            "users_checked": len(users_checked),
            "emails_sent": emails_sent, "errors": errors}


@app.route("/run-saved-search-alerts", methods=["POST"])
def run_saved_search_alerts():
    """Triggered by an external scheduler (see .github/workflows) once a day
    -- Render's web dyno alone has no way to wake itself up on a schedule.
    Gated by CRON_SECRET, a shared secret only the scheduler knows, so this
    can't be used by anyone who just finds the URL."""
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("token") or request.headers.get("X-Cron-Secret", "")
    if not CRON_SECRET or not hmac.compare_digest(token, CRON_SECRET):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    result = _run_saved_search_alerts()
    return jsonify(result), (200 if result.get("ok") else 500)


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
    """App calls this to auto-unlock after a purchase.

    Resolves by device id first, then by the signed-in account's email. The
    email path matters because a checkout started from the marketing site
    carries no device id at all -- those Stripe links can't know one -- so the
    webhook recorded no device mapping and this endpoint always answered
    "no_key". The buyer's Account tab then showed "Trial expired, subscribe
    below" to someone who had just paid, which invites a second subscription.
    It also covers paying on a laptop and then signing in on a phone.
    """
    data = request.get_json(force=True, silent=True) or {}
    device = (data.get("device_id") or "").strip()
    db = _db()
    key = db.get("devices", {}).get(device) if device else None
    matched_by_email = False
    if not key:
        email = _verify_supabase_token(data.get("supabase_token", ""))
        if email:
            key = db.get("emails", {}).get(email.lower())
            matched_by_email = bool(key)
    if not key:
        return jsonify({"ok": False, "reason": "no_key"})
    valid, plan, exp, _ = verify_key(key)
    if not valid or key in db.get("revoked", []):
        return jsonify({"ok": False, "reason": "inactive"})
    if matched_by_email and device:
        # Remember the device so later calls take the fast path.
        db.setdefault("devices", {})[device] = key
        _save_db(db)
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
    """Restore a purchase on a new device, for the signed-in account.

    The email is taken from a verified Supabase token, never from the request
    body. It used to be read straight out of the body, which meant anyone who
    knew (or guessed) a customer's email address could POST it here and be
    handed that customer's working licence key, bound to their own device --
    an email address is not a secret and nothing else was being checked.
    """
    data = request.get_json(force=True, silent=True) or {}
    email = (_verify_supabase_token(data.get("supabase_token", "")) or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "reason": "signin_required"}), 403
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
    # Was a plain != comparison: timing-unsafe, and the fix applied to
    # /issue and /revoke never reached here.
    if not _admin_configured():
        return jsonify({"ok": False, "reason": "admin_not_configured"}), 503
    if not _admin_ok(data.get("admin_token")):
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


# Public bodies rarely call themselves by a bare city name. The AI is told to
# copy the location "exactly as written in the text", so it faithfully returns
# things like "City of O'Fallon", "Greene County", or "Aurora R-VIII School
# District" — none of which zippopotam can resolve, so every one of those bids
# used to be thrown away. Counties, townships and school districts are core
# buyers for sidewalk and ADA work, so that was a large hole in coverage.
# \b + \s* rather than \s+ so a truncated "City of" reduces to nothing at all,
# instead of leaving "City of" to be geocoded as if it were a place.
_PLACE_PREFIX_RE = re.compile(
    r"^(?:the\s+)?(?:city|town|village|township|borough|county|municipality)\s+of\b\s*", re.I)
_PLACE_SUFFIX_RE = re.compile(
    r"\s+(?:r-[ivxlc]+\s+)?(?:school\s+district|public\s+schools|community\s+schools|"
    r"unified\s+school\s+district|isd|usd|county\s+schools|"
    r"housing\s+authority|water\s+district|utility\s+district|"
    r"public\s+works|road\s+district|fire\s+district|park\s+district)\b.*$", re.I)
_PLACE_TRAILING_RE = re.compile(r"[\s,;:.\-]+$")


def _normalize_place(name):
    """Reduce an authority's name to the place it is named after.

    "City of O'Fallon" -> "O'Fallon", "Aurora R-VIII School District" ->
    "Aurora". Returns "" if nothing usable is left.
    """
    s = " ".join(str(name or "").split())
    if not s:
        return ""
    s = _PLACE_PREFIX_RE.sub("", s)
    s = _PLACE_SUFFIX_RE.sub("", s)
    s = _PLACE_TRAILING_RE.sub("", s)
    return s.strip()


def _zippopotam_city(city, state):
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
    return (sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts))


# Nominatim asks for a descriptive User-Agent and no more than one request a
# second. Results are cached in the geo db, so real traffic here is low.
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_UA = "BidCallerPro/2.0 (bid search for concrete contractors)"
_nominatim_lock = threading.Lock()
_nominatim_last = [0.0]


def _nominatim_place(place, state):
    """Geocode anything zippopotam can't: counties, townships, unincorporated
    communities, and other named administrative areas."""
    with _nominatim_lock:
        wait = 1.0 - (time.time() - _nominatim_last[0])
        if wait > 0:
            time.sleep(wait)
        _nominatim_last[0] = time.time()
    qs = urllib.parse.urlencode({
        "q": f"{place}, {state}, USA", "format": "json", "limit": "1",
        "countrycodes": "us", "addressdetails": "0",
    })
    data = _get_json(f"{_NOMINATIM_URL}?{qs}",
                     headers={"User-Agent": _NOMINATIM_UA, "Accept": "application/json"})
    if not isinstance(data, list) or not data:
        return None
    try:
        return float(data[0]["lat"]), float(data[0]["lon"])
    except (KeyError, ValueError, TypeError):
        return None


def _geo_from_city(city, state):
    """Resolve a place name to coordinates, trying progressively looser forms.

    zippopotam only knows names that appear in ZIP-code data, which excludes
    most counties and townships, so it is tried first (fast, generous rate
    limit) and Nominatim picks up whatever it misses.
    """
    state = (state or "").upper()
    raw = " ".join(str(city or "").split())
    if not raw or not state:
        return None
    normalized = _normalize_place(raw)
    attempts = [a for a in dict.fromkeys([raw, normalized]) if a]
    for attempt in attempts:
        hit = _zippopotam_city(attempt, state)
        if hit:
            return {"lat": hit[0], "lon": hit[1], "city": attempt, "state": state}
    for attempt in attempts:
        hit = _nominatim_place(attempt, state)
        if hit:
            return {"lat": hit[0], "lon": hit[1], "city": attempt, "state": state}
    return None


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
    n = max(2, min(MAX_ANCHOR_TOWNS, round(radius / 20)))
    # One ring of towns at a single distance leaves the ground between it and
    # the centre unsearched, which on a 125mi scan is most of the area. Wide
    # radii get two rings, with the outer ring's bearings offset so the towns
    # interleave rather than lining up along the same spokes.
    rings = [0.7] if radius < 80 else [0.5, 0.85]
    per_ring = max(1, n // len(rings))
    seen = {(center["city"].lower(), center["state"])}
    towns = []
    for ring_i, frac in enumerate(rings):
        dist = radius * frac
        for i in range(per_ring):
            bearing = (i * (360.0 / per_ring)) + (ring_i * (180.0 / per_ring))
            lat, lon = _destination_point(center["lat"], center["lon"], bearing, dist)
            found = _reverse_geocode_city(lat, lon)
            if not found:
                continue
            city, state = found
            key = (city.lower(), state)
            if key in seen:
                continue
            seen.add(key)
            # Coordinates come back too: a bid found by searching this town but
            # naming a place we can't geocode (a county, a school district) can
            # be anchored here instead of discarded. See _place_bid.
            towns.append((city, state, lat, lon))
    return towns


_DEADLINE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y",
    "%B %d %Y", "%b %d %Y", "%m/%d/%y",
)


# Date shapes to pull out of surrounding prose. Ordered longest-first within a
# family so "12/01/2026" isn't mistaken for a 2-digit year.
_MONTH_WORDS = ("January|February|March|April|May|June|July|August|September|"
                "October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec")
_DATE_PATTERNS = (
    re.compile(r"\d{4}-\d{2}-\d{2}"),
    re.compile(r"\d{1,2}/\d{1,2}/\d{4}"),
    re.compile(r"\d{1,2}-\d{1,2}-\d{4}"),
    re.compile(rf"(?:{_MONTH_WORDS})\.?\s+\d{{1,2}},?\s+\d{{4}}", re.I),
    re.compile(r"\d{1,2}/\d{1,2}/\d{2}(?!\d)"),
)


def _parse_deadline(text):
    """Best-effort parse of a free-text deadline into a date. None if unparseable.

    Deadlines on real bid pages are almost never a bare date -- they read
    "Due by 12/01/2026 at 2:00 PM", "Bids due December 1, 2026 at 2:00 p.m.",
    "Thursday, December 1, 2026". Parsing only the whole string missed every
    one of those, and the miss was expensive in two directions: the bid lost
    its entire deadline-urgency score (a job due in 3 days ranked like one with
    no deadline at all), and _apply_deadline_status could not tell that an
    expired listing had expired, so it kept showing as open. So find the date
    inside the text first, then parse that.
    """
    if not text:
        return None
    t = " ".join(str(text).split())
    candidates = []
    for pat in _DATE_PATTERNS:
        m = pat.search(t)
        if m:
            candidates.append(m.group(0))
    candidates.append(t)  # whole string, for anything the patterns don't cover
    for cand in candidates:
        # "Sept." -> "Sep" so %b matches; drop trailing punctuation.
        cand = re.sub(r"(?i)\bsept\b", "Sep", cand).replace(".", "").strip().strip(",")
        for fmt in _DEADLINE_FORMATS:
            try:
                return datetime.datetime.strptime(cand, fmt).date()
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

    A full date is checked first. If the deadline text doesn't match any
    known date format (e.g. "FY2024", a stale notice reused from a prior
    year, or other free-text the AI didn't clean up despite instructions),
    fall back to a bare 4-digit year: a deadline field naming a year strictly
    before the current one means this is almost certainly a stale/expired
    listing, not a genuinely open bid, and got missed by the earlier version
    of this check (its own docstring used to say unparseable deadlines were
    "left as-is" -- which is exactly how old bids were slipping through as
    apparently active)."""
    deadline_text = bid.get("deadline")
    d = _parse_deadline(deadline_text)
    if d:
        if d < datetime.datetime.now().date():
            bid["status"] = "Closed"
        return bid
    m = re.search(r"(?<!\d)(20\d{2})(?!\d)", str(deadline_text or ""))
    if m and int(m.group(1)) < datetime.datetime.now().year:
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


GEO_MISS_RETRY_HOURS = 24


def _cached_point(cache, key, fetch):
    """Look up a [lat, lon] in `cache`, calling `fetch()` on a miss.

    Failures are remembered only briefly, and a legacy bare None counts as an
    already-expired miss so previously poisoned caches heal by themselves.
    Caching a miss forever is how one transient geocoder outage used to change
    results permanently — for a city, bids there were dropped from every later
    scan; for a ZIP, leads there stopped being distance-checked at all. One
    helper for both so a third copy can't drift off on its own.
    """
    hit = cache.get(key)
    if isinstance(hit, list):
        return hit
    if isinstance(hit, dict):
        try:
            missed_at = datetime.datetime.fromisoformat(hit.get("missed_at", ""))
        except (TypeError, ValueError):
            missed_at = None
        if missed_at and (datetime.datetime.now() - missed_at).total_seconds() \
                < GEO_MISS_RETRY_HOURS * 3600:
            return None
    point = fetch()
    if not point:
        cache[key] = {"missed_at": datetime.datetime.now().isoformat()}
        return None
    cache[key] = [point[0], point[1]]
    return cache[key]


def _city_coords(city, state, db):
    """Geocode a (city, state) to [lat, lon], cached in the JSON db."""
    if not city or not state:
        return None

    def _fetch():
        g = _geo_from_city(city, state)
        return (g["lat"], g["lon"]) if g else None

    return _cached_point(db.setdefault("geo_cache", {}),
                         f"{city.lower()}|{state.upper()}", _fetch)


# ═══════════════════════════════════════════════════════════
# LOCAL SEARCH  (Tavily — AI search API, free 1k/mo, no card)
# ═══════════════════════════════════════════════════════════
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
TAVILY_URL = "https://api.tavily.com/search"

# Recall knobs. All three trade scan time and OpenAI spend for coverage, and
# the right values depend on how the live backend actually performs, so they
# are env-tunable — watch the "funnel" figures in /scan's debug and the wall
# clock, and dial from there. Fetch+extract is entirely I/O-bound, so raising
# PAGE_WORKERS buys pages without proportionally more wall clock.
MAX_PAGES = int(os.environ.get("SCAN_MAX_PAGES", "24"))
# Kept well under the OpenAI burst limits: several towns run at once, each
# with its own pool, so concurrent extractions is this times the town workers.
PAGE_WORKERS = int(os.environ.get("SCAN_PAGE_WORKERS", "6"))
# At most this many pages from any one domain, so a single aggregator can't
# consume the whole per-town budget.
MAX_PAGES_PER_DOMAIN = int(os.environ.get("SCAN_MAX_PAGES_PER_DOMAIN", "4"))
# Towns searched around the radius, on top of the centre. More towns means
# more of a wide radius is actually looked at, at the cost of scan time.
MAX_ANCHOR_TOWNS = int(os.environ.get("SCAN_MAX_ANCHORS", "6"))

# Primary sources: the agency actually letting the work, rather than a site
# re-listing it. Preferred when the budget is tight.
_GOV_DOMAIN_RE = re.compile(r"(?:^|\.)(?:gov|mil)$|(?:^|\.)[a-z]{2}\.us$", re.I)


def _page_domain(url):
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _prioritize_pages(items):
    """Order candidate pages so a fixed budget buys the widest, best coverage.

    BidNet entries keep their existing precedence (they arrive first and carry
    no content), then government domains, then everything else — with a per
    domain cap applied across the whole list so one site can't dominate.
    """
    def rank(pair):
        idx, it = pair
        dom = _page_domain(it.get("url", ""))
        bidnet = 0 if "bidnetdirect.com" in dom else 1
        gov = 0 if _GOV_DOMAIN_RE.search(dom) else 1
        return (bidnet, gov, idx)

    ordered = [it for _, it in sorted(enumerate(items), key=rank)]
    per_domain, head, tail = {}, [], []
    for it in ordered:
        dom = _page_domain(it.get("url", ""))
        per_domain[dom] = per_domain.get(dom, 0) + 1
        # Over-quota pages aren't discarded, just moved behind everything else,
        # so they're still used if the budget outlasts the diverse candidates.
        (head if per_domain[dom] <= MAX_PAGES_PER_DOMAIN else tail).append(it)
    return head + tail

_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


# Tavily bills per search and charges DOUBLE for "advanced" depth. A 50mi scan
# issues ~39 searches, so at advanced depth one scan cost ~78 credits and the
# free 1,000/month allowance was gone in about a dozen scans — after which
# every search silently returns nothing and the scan quietly falls back to
# scraping DuckDuckGo. Depth only affects how hard Tavily works at ranking;
# we use the results as a URL list and fetch the pages ourselves, so basic is
# the right trade and halves the bill.
TAVILY_DEPTH = os.environ.get("TAVILY_SEARCH_DEPTH", "basic")

# A quota or auth failure here is the single most damaging silent failure in
# the product: /scan still returns 200, just with almost nothing in it. Track
# it the same way the DuckDuckGo breaker does so /health can say so out loud.
_tavily_lock = threading.Lock()
_tavily_state = {"ok": 0, "failed": 0, "last_error": "", "last_status": 0}


def _tavily_note(ok, status=0, detail=""):
    with _tavily_lock:
        if ok:
            _tavily_state["ok"] += 1
        else:
            _tavily_state["failed"] += 1
            _tavily_state["last_status"] = status
            _tavily_state["last_error"] = (detail or "")[:200]


def _tavily_health():
    with _tavily_lock:
        st = dict(_tavily_state)
    total = st["ok"] + st["failed"]
    # 402/429/432 are the shapes a spent allowance arrives in.
    st["quota_or_auth_failure"] = st["last_status"] in (401, 402, 429, 432)
    st["failing"] = total > 0 and st["ok"] == 0 and st["failed"] > 0
    return st


def _tavily_search(query, max_results=5):
    """Search via Tavily; returns [{url, content}]."""
    if not TAVILY_API_KEY:
        print("[scan] no TAVILY_API_KEY set", flush=True)
        return []
    body = json.dumps({
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": TAVILY_DEPTH,
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
        _tavily_note(False, e.code, detail)
        if e.code in (401, 402, 429, 432):
            _alert_admin(
                f"Tavily returning HTTP {e.code} — local bid search is degraded",
                "Tavily rejected a search. 402/429/432 normally means the monthly "
                "credit allowance is spent; 401 means the key is wrong. While this "
                "lasts, /scan falls back to scraping DuckDuckGo, which from a shared "
                "Render IP often returns nothing — so scans will look like the area "
                f"simply has no bids.\n\nResponse: {detail}",
            )
        return []
    except Exception as ex:
        print(f"[scan] Tavily error: {ex}", flush=True)
        _tavily_note(False, 0, str(ex))
        return []
    _tavily_note(True)
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
# Pause between consecutive scraped searches, to stay unobtrusive.
DDG_QUERY_PAUSE = float(os.environ.get("SCAN_DDG_PAUSE", "0.5"))


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


# A URL we know is real is worth waiting for. A guessed one is not: most
# speculative probes are 404s or dead hosts, and at the full timeout a handful
# of them will spend the entire request budget before the search path even
# starts. That is not a hypothetical — it is what made every scan return zero.
FETCH_TIMEOUT = int(os.environ.get("SCAN_FETCH_TIMEOUT", "18"))
PROBE_TIMEOUT = int(os.environ.get("SCAN_PROBE_TIMEOUT", "6"))
PORTAL_WORKERS = int(os.environ.get("SCAN_PORTAL_WORKERS", "6"))


def _fetch_raw(url, timeout=None):
    """Page source, untouched. _fetch_text strips tags, which is right for
    feeding prose to the AI and useless for a parser that needs the markup."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 BidCallerPro"})
        with urllib.request.urlopen(req, timeout=timeout or FETCH_TIMEOUT) as resp:
            return resp.read(800000).decode("utf-8", "ignore")
    except Exception:
        return ""


def _fetch_text(url, timeout=None):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 BidCallerPro"})
        with urllib.request.urlopen(req, timeout=timeout or FETCH_TIMEOUT) as resp:
            raw = resp.read(800000).decode("utf-8", "ignore")
    except Exception:
        return ""
    raw = _SCRIPT_RE.sub(" ", raw)
    raw = _TAG_RE.sub(" ", raw)
    raw = re.sub(r"&[a-z#0-9]+;", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


# ── BidNet Direct: a real public source, queried directly (no key) ──
# Unlike `site:bidnetdirect.com` search-engine queries (which only surface
# whatever DDG/Tavily happened to index), this hits BidNet Direct's own
# public "Open Solicitations" search directly by state + keyword. Verified
# by hand: no login wall, no JS rendering required -- a plain scripted GET
# with realistic browser headers gets a normal 200 with real server-rendered
# results (the earlier 403 seen from a bare fetch was header-fingerprint
# bot-blocking, the same class of thing DDG scraping already works around
# below, not a real access restriction). `location` is BidNet's own numeric
# state code, scraped once from their filter dropdown.
BIDNET_LOCATION_CODES = {
    "AL": 19, "AK": 25, "AZ": 31, "AR": 37, "CA": 43, "CO": 49, "CT": 55,
    "DE": 61, "DC": 67, "FL": 73, "GA": 79, "HI": 85, "ID": 91, "IL": 97,
    "IN": 103, "IA": 109, "KS": 115, "KY": 121, "LA": 127, "ME": 133,
    "MD": 139, "MA": 145, "MI": 151, "MN": 157, "MS": 163, "MO": 169,
    "MT": 175, "NE": 181, "NV": 187, "NH": 193, "NJ": 199, "NM": 205,
    "NY": 211, "NC": 217, "ND": 223, "OH": 229, "OK": 235, "OR": 241,
    "PA": 247, "RI": 253, "SC": 259, "SD": 265, "TN": 271, "TX": 277,
    "UT": 283, "VT": 289, "VA": 295, "WA": 301, "WV": 307, "WI": 313,
    "WY": 319,
}
BIDNET_KEYWORDS = ("sidewalk", "curb ramp")
_BIDNET_HREF_RE = re.compile(r'href="(/[a-z0-9][a-z0-9-]*/solicitations/open-bids/[^"]+)"')


def _bidnet_direct_urls(keywords, state_abbr, max_results=5):
    """Return up to max_results detail-page URLs from BidNet Direct's public
    search for this state + keyword. Returned in the same {"url","content"}
    shape _ddg_search/_tavily_search use, so callers can merge them straight
    into the existing fetch+AI-extract+place pipeline instead of needing a
    separate code path."""
    code = BIDNET_LOCATION_CODES.get(state_abbr)
    if not code:
        return []
    try:
        qs = urllib.parse.urlencode({
            "keywords": keywords, "location": code,
            "solSearchStatus": "openSolicitationsTab",
        })
        req = urllib.request.Request(
            f"https://www.bidnetdirect.com/public/solicitations/open?{qs}",
            headers={
                "User-Agent": random.choice(_DDG_UAS),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
        with urllib.request.urlopen(req, timeout=20) as resp:
            html_text = resp.read().decode("utf-8", "ignore")
    except Exception as ex:
        print(f"[scan] BidNet Direct search error ({state_abbr}/{keywords!r}): {ex}", flush=True)
        return []
    out, seen = [], set()
    for m in _BIDNET_HREF_RE.finditer(html_text):
        href = m.group(1).replace("&amp;", "&")
        url = "https://www.bidnetdirect.com" + href
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "content": ""})
        if len(out) >= max_results:
            break
    return out


# ═══════════════════════════════════════════════════════════
# FEDERAL SEARCH  (SAM.gov)
# ═══════════════════════════════════════════════════════════
SAM_API_KEY = os.environ.get("SAM_API_KEY", "")
SAM_SEARCH_URL = os.environ.get(
    "SAM_SEARCH_URL", "https://api.sam.gov/prod/opportunities/v2/search")
SCAN_WINDOW_DAYS = int(os.environ.get("SCAN_WINDOW_DAYS", "60"))

# Deliberately narrow to the product's actual niche. An earlier version OR'd
# this keyword check with "NAICS starts with 236/237/238" -- but that top-level
# bucket covers every construction trade there is (electricians, HVAC, roofers,
# painters...), so on a state with a lot of federal opportunity volume it let
# in a flood of bids with nothing to do with concrete. Title-keyword match
# only, against niche terms -- lower recall, but recall on the wrong bids
# isn't useful to a contractor scanning for sidewalk/curb/concrete work.
CONSTRUCTION_KEYWORDS = (
    "sidewalk", "ada ramp", "curb ramp", "curb and gutter", "curb & gutter",
    "concrete", "flatwork", "pedestrian ramp",
)


def _is_construction(opp):
    title = (opp.get("title") or "").lower()
    return any(k in title for k in CONSTRUCTION_KEYWORDS)


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


def _normalize_opp(opp):
    poc_list = opp.get("pointOfContact") or []
    poc = poc_list[0] if poc_list else {}
    pop = opp.get("placeOfPerformance") or {}
    city = ((pop.get("city") or {}).get("name")) or ""
    # SAM states the performance state outright; pass it along so the bid is
    # geocoded against that rather than assumed to be in the centre's state.
    perf_state = (((pop.get("state") or {}).get("code")) or "").upper()
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
    return bid, city, perf_state


# ═══════════════════════════════════════════════════════════
# AI EXTRACTION (now also returns a "city" so we can radius-filter)
# ═══════════════════════════════════════════════════════════
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def _ai_extract(area, text):
    if not OPENAI_API_KEY:
        return None
    prompt = (
        f"You extract SIDEWALK, ADA RAMP, CURB & GUTTER, and CONCRETE FLATWORK/"
        f"PAVING bid leads for a niche concrete contractor near {area}.\n\n"
        "From the website text below (which may be a full bid listing page "
        "covering many unrelated trades), extract ONLY bids, RFPs, RFQs, or "
        "solicitations where sidewalk construction/repair/replacement, ADA curb "
        "ramps, curb-and-gutter work, or concrete flatwork/paving is clearly "
        "part of the stated scope. A bid with mixed scope items still counts if "
        "concrete/sidewalk/curb work is explicitly one of them.\n\n"
        "Do NOT extract bids that are only about roofing, HVAC, plumbing, "
        "electrical, general building construction, demolition, painting, "
        "landscaping, or other unrelated trades -- even if they appear on the "
        "same page as real matches, and even though they're all technically "
        "\"construction.\" When in doubt, leave it out.\n\n"
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
                        city_coords, lock, pdb, default_city="", town_coords=None,
                        stats=None):
    """Fetch URLs already known (from a prior scan or the seed list) to be
    this city's real bid page, directly — no search engine involved. This is
    the fast, deterministic path that /scan tries before falling back to
    live search: no per-query search-API cost, no dependency on that day's
    search rankings, and it can't be blocked the way scraping DuckDuckGo can.
    Entries age out (via bid_portals.MAX_FAIL) if they stop returning real
    content, so a site redesign doesn't silently keep failing forever."""
    with lock:
        portals = list(bid_portals.get_portals(pdb, city, state))[:6]

    # Nothing learned for this town yet? Its official domain is already known —
    # CISA publishes the registry of every .gov, so there is no need to search
    # for it. Probe the handful of paths a municipal bid page actually takes;
    # a hit is recorded below and costs nothing on every later scan.
    #
    # This also finally reaches counties. They let a great deal of curb, road
    # and drainage work and were entirely absent before: none were seeded, and
    # a county name doesn't geocode, so any bid naming one was thrown away.
    if not portals:
        probed = []
        for entry in gov_directory.lookup(city, state)[:2]:
            for candidate in bid_sources.candidate_bid_urls(entry["domain"], limit=2):
                probed.append({"url": candidate, "probe": True,
                               "platform": "civicplus"
                               if candidate.lower().endswith("bids.aspx") else "custom"})
        portals = probed[:4]
    # Read every portal at once. This loop used to be sequential, which was
    # survivable when it only ever touched one or two known-good URLs. Adding
    # speculative .gov probes on top of it was not: a handful of dead guesses
    # at a full fetch timeout each consumed the entire request budget before
    # the search path ran, the app aborted the request, and every scan came
    # back empty. Fetching is pure I/O, so width costs nothing here.
    raw = [0]

    def _read_portal(entry):
        url = entry["url"]
        timeout = PROBE_TIMEOUT if entry.get("probe") else None

        # Structured reading is an OPTIMISATION over the AI path, never a
        # replacement. If the parser matches nothing we fall through to the AI
        # below rather than losing the portal entirely, and a live page counts
        # as a success even when our regex didn't understand it — a parser gap
        # is our problem, not a dead site.
        if bid_sources.identify_platform(url) == "civicplus":
            rows = bid_sources.parse_civicplus_html(
                _fetch_raw(url, timeout=timeout), base_url=url)
            if rows:
                with lock:
                    bid_portals.record_result(pdb, city, state, url, True)
                    for row in rows:
                        if not bid_sources.looks_relevant(row["title"], row.get("scope")):
                            if stats is not None:
                                stats["filtered_not_niche"] = stats.get("filtered_not_niche", 0) + 1
                            continue
                        raw[0] += 1
                        _place_bid(grouped, {
                            "title": row["title"], "scope": row.get("scope", ""),
                            "status": "Open", "deadline": row.get("deadline", ""),
                            "contact": "", "email": "", "phone": "", "value": "",
                            "url": row["url"], "city": default_city or city,
                        }, center, radius, cdb, default_city=default_city or city,
                            city_coords=city_coords, default_state=state,
                            fallback_coords=town_coords, stats=stats)
                return
            if stats is not None:
                with lock:
                    stats["civicplus_parse_miss"] = stats.get("civicplus_parse_miss", 0) + 1

        text = _fetch_text(url, timeout=timeout)
        ok = len(text) >= 200
        with lock:
            bid_portals.record_result(pdb, city, state, url, ok)
        if not ok:
            return
        bids = _ai_extract(ai_label, text)
        if not bids:
            return
        with lock:
            raw[0] += len(bids)
            for b in bids:
                if isinstance(b, dict):
                    b.setdefault("url", url)
                    _place_bid(grouped, b, center, radius, cdb, default_city=default_city,
                              city_coords=city_coords, default_state=state,
                              fallback_coords=town_coords, stats=stats)

    if portals:
        with ThreadPoolExecutor(max_workers=min(PORTAL_WORKERS, len(portals))) as ex:
            list(ex.map(_read_portal, portals))
    return raw[0]


def _run_local_queries(queries, ai_label, max_pages, grouped, center, radius, cdb,
                        city_coords, seen_urls, lock, pdb, default_city="", state="",
                        town_coords=None, stats=None):
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
    resetting every time.

    BidNet Direct results (a real public source, queried directly by state --
    see _bidnet_direct_urls) are put FIRST in the item list, so they win the
    max_pages budget over speculative search-engine hits: a guaranteed real
    government solicitation is worth more than an uncertain DDG/Tavily
    result, and this doesn't raise the per-scan AI-extraction cost since the
    slice below is still capped at the same max_pages either way."""
    items = []
    for kw in BIDNET_KEYWORDS:
        for r in _bidnet_direct_urls(kw, state):
            with lock:
                if r["url"] in seen_urls:
                    continue
                seen_urls.add(r["url"])
            items.append(r)

    for q in queries:
        results = _tavily_search(q, max_results=6) if TAVILY_API_KEY else []
        used_ddg = not results
        if used_ddg:
            results = _ddg_search(q)
        for r in results:
            with lock:
                if r["url"] in seen_urls:
                    continue
                seen_urls.add(r["url"])
            items.append(r)
        # The pause exists to avoid hammering DuckDuckGo, which is scraped and
        # will start blocking. It used to run after every query regardless, so
        # with Tavily configured — where DDG is never even called — it was
        # several seconds of pure dead time per town, on an endpoint already
        # pushing against the client's timeout.
        if used_ddg:
            time.sleep(DDG_QUERY_PAUSE)

    # How the page budget gets spent decides recall as much as its size does.
    # The queries deliberately include several site: searches, so left in
    # discovery order one aggregator can swallow the whole allowance while the
    # agency's own posting further down the list is never opened. Ordering
    # keeps BidNet's verified solicitations first, then favours government
    # domains (a .gov page is the primary source, not a re-listing), and caps
    # how many pages any single domain may take.
    items = _prioritize_pages(items)

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
                              city_coords=city_coords, default_state=state,
                              fallback_coords=town_coords, stats=stats)
                    if bid_city and state:
                        bid_portals.learn_portal(pdb, bid_city, state, it["url"])

    with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as ex:
        list(ex.map(_process, items[:max_pages]))

    return raw[0]


def _is_open_bid(bid):
    """A bid the client will actually display. Mirrors isOpen() in app.html."""
    return str((bid or {}).get("status") or "Open").strip().lower() == "open"


def _bid_dupe_key(bid):
    """Identity of a solicitation for de-duplication: title + deadline."""
    title = re.sub(r"\s+", " ", str((bid or {}).get("title") or "")).strip().lower()
    return title, str((bid or {}).get("deadline") or "").strip().lower()


def _split_city_state(raw):
    """'Bentonville, AR' -> ('Bentonville', 'AR'). Bare city -> ('Bentonville', '')."""
    parts = [p.strip() for p in str(raw or "").split(",")]
    city = parts[0]
    state = ""
    if len(parts) > 1 and parts[1]:
        cand = parts[1]
        state = cand.upper() if cand.upper() in STATE_ABBRS \
            else STATE_NAME_TO_ABBR.get(cand.lower(), "")
    return city, state


def _place_bid(grouped, bid, center, radius, db, default_city="", city_coords=None,
               default_state="", fallback_coords=None, stats=None):
    """Keep a bid ONLY if its real city geocodes within the radius.

    The city is resolved against the most specific state we have: the one the
    AI actually stated, else the town whose search produced this bid, else the
    search centre's. Previously every bid was geocoded against the CENTRE
    state alone, and the state the AI returned was thrown away by splitting on
    the comma. Any radius wide enough to cross a state line — which is most of
    the 75mi and 125mi range, and the whole point of those options — then
    looked up a real "Bentonville, AR" bid as "Bentonville, MO", found
    nothing, and dropped it. Out-of-state bids were invisible on exactly the
    wide scans meant to surface them.
    """
    def _count(reason):
        if stats is not None:
            stats[reason] = stats.get(reason, 0) + 1

    if not isinstance(bid, dict):
        _count("malformed")
        return
    city, stated_state = _split_city_state(bid.get("city") or default_city or "")
    if not city:
        _count("no_location")
        return  # no stated location -> can't verify it's local -> drop
    # A stated state is taken as final: if the text says Springfield, IL, that
    # is the Illinois one, and failing to geocode it must drop the bid rather
    # than fall through to a same-named city in the centre's state. Guessing
    # there would quietly relocate a bid hundreds of miles and present it as
    # local, which is worse than missing it. The fallback chain only applies
    # when no state was stated at all.
    if stated_state:
        candidates = [stated_state]
    else:
        candidates = [s for s in (default_state, center["state"]) if s]
    coords, used_state = None, ""
    tried = []
    for st in candidates:
        st = st.upper()
        if st in tried:
            continue
        tried.append(st)
        coords = _city_coords(city, st, db)
        if coords:
            used_state = st
            break
    if not coords:
        # Last resort: anchor the bid to the town whose search turned it up.
        # Plenty of real buyers name themselves in ways no gazetteer resolves
        # ("Greene County", a road district, a regional authority), and this
        # page was found by searching a specific town, so the work is around
        # there. Only safe when the text didn't name a different state — a bid
        # explicitly in another state must not be pinned to this one.
        search_state = (default_state or center["state"]).upper()
        if fallback_coords and (not stated_state or stated_state == search_state):
            coords, used_state = fallback_coords, search_state
            _count("placed_by_search_town")
        else:
            _count("unresolvable_place")
            return
    if _miles_between(center["lat"], center["lon"], coords[0], coords[1]) > radius:
        _count("out_of_radius")
        return  # outside the chosen radius
    _apply_deadline_status(bid)
    bid.pop("city", None)
    # Out-of-state towns keep their state in the label, both to disambiguate
    # same-named cities and because "this one is across the line" is something
    # a contractor wants to see before driving to look at it.
    label = city if used_state == center["state"] else f"{city}, {used_state}"
    # The same solicitation routinely turns up on two different pages -- an
    # aggregator and the agency's own site -- and both used to be kept. The
    # client derives a bid's id from its city + title + scope, so the copies
    # came out as duplicate cards sharing one id: starring one appeared to
    # star the other. Same title and deadline in the same town is the same job.
    bucket = grouped.setdefault(label, [])
    key = _bid_dupe_key(bid)
    if key[0] and any(_bid_dupe_key(existing) == key for existing in bucket):
        _count("duplicate")
        return
    bucket.append(bid)
    _count("kept")
    if city_coords is not None:
        city_coords[label] = {"lat": coords[0], "lon": coords[1]}


def _perform_scan(location, radius, force=False):
    """Core of /scan: resolve a location, search local + federal sources, rank
    and cache the result. Extracted out of the /scan route so the saved-search
    alert job can run the exact same pipeline (portal directory, DDG failover,
    SAM.gov, fit ranking, same-day cache) instead of duplicating it.
    Returns a dict of response fields, or None if the location can't be resolved."""
    center = _resolve_center(location)
    if not center:
        return None

    cdb = _cache()
    today = datetime.datetime.now().strftime("%Y%m%d")
    cache = cdb.setdefault("scan_cache", {})
    ckey = f"{center['state']}|{center['city'].lower()}|{int(radius)}|{today}"
    # The cache is per calendar day, so the FIRST scan of an area sets what
    # everyone sees until midnight. If that scan ran while a search backend was
    # down — or against a cold server mid-deploy — its thin result was locked in
    # and every retry returned the same disappointment with no way to force a
    # real re-run. `force` is that way.
    if ckey in cache and not force:
        c = cache[ckey]
        return {"location": f"{center['city']}, {center['state']}",
                "bids": c["bids"], "total_bids": c["total"],
                "city_coords": c.get("city_coords", {}),
                "center": c.get("center", {"lat": center["lat"], "lon": center["lon"],
                                           "label": f"{center['city']}, {center['state']}"}),
                "cached": True}

    pdb = bid_portals.load_directory()
    grouped = {}
    city_coords = {}
    local_raw = 0
    # Why extracted bids did or did not make it into the result. Recall is the
    # thing that matters most here and it fails silently, so the funnel is
    # reported in the response rather than left to be guessed at.
    drop_stats = {}

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
        center_coords = (center["lat"], center["lon"])

        def _run_center():
            # default_city=c, not "": a known portal IS this city's own bid
            # page, so a bid on it that doesn't restate the city is still that
            # city's. Defaulting to blank made _place_bid drop those outright,
            # losing bids from the single most reliable source in the pipeline.
            got = _run_known_portals(c, s, f"{c}, {s}", grouped, center, radius,
                                      cdb, city_coords, lock, pdb, default_city=c,
                                      town_coords=center_coords, stats=drop_stats)
            got += _run_local_queries(center_queries, f"{c}, {s}", MAX_PAGES,
                                      grouped, center, radius, cdb, city_coords,
                                      seen_urls, lock, pdb, default_city="", state=s,
                                      town_coords=center_coords, stats=drop_stats)
            print(f"[scan] {got} raw bids from {c}, {s} (center)", flush=True)
            return got

        def _run_anchor(anchor):
            ac, ast, alat, alon = anchor
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
                                      cdb, city_coords, lock, pdb, default_city=ac,
                                      town_coords=(alat, alon), stats=drop_stats)
            got += _run_local_queries(anchor_queries, f"{ac}, {ast}", 5,
                                      grouped, center, radius, cdb, city_coords,
                                      seen_urls, lock, pdb, default_city=ac, state=ast,
                                      town_coords=(alat, alon), stats=drop_stats)
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
            bid, city, perf_state = _normalize_opp(opp)
            _place_bid(grouped, bid, center, radius, cdb, default_city=city,
                      city_coords=city_coords, default_state=perf_state,
                      stats=drop_stats)

    for city_bids in grouped.values():
        city_bids.sort(key=_score_bid, reverse=True)

    # Open bids only. Closed ones are still returned (ranked last) but counting
    # them made the reported total disagree with what the app shows: a scan
    # that turned up nothing but expired listings announced "12 bids" and then
    # dropped the user on an empty feed.
    total = sum(1 for v in grouped.values() for b in v if _is_open_bid(b))
    funnel = ", ".join(f"{k}={v}" for k, v in sorted(drop_stats.items())) or "none"
    print(f"[scan] {int(radius)} mi from {center['city']},{center['state']} "
          f"-> {total} bids kept (local_raw={local_raw}; {funnel})", flush=True)

    result = {"bids": grouped, "total": total, "city_coords": city_coords,
              "center": {"lat": center["lat"], "lon": center["lon"],
                        "label": f"{center['city']}, {center['state']}"}}

    # cache (today only) + persist geo cache
    cache[ckey] = {"ts": datetime.datetime.now().isoformat(), **result}
    cdb["scan_cache"] = {k: v for k, v in cache.items() if k.endswith(today)}
    _save_cache(cdb)
    bid_portals.save_directory(pdb)

    return {"location": f"{center['city']}, {center['state']}",
            "bids": grouped, "total_bids": total, "city_coords": city_coords,
            "center": result["center"],
            "debug": {"raw_local": local_raw, "kept": total, "funnel": drop_stats}}


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

    outcome = _perform_scan(location, radius, force=bool(data.get("force")))
    if outcome is None:
        return jsonify({"ok": False, "reason": "location_not_found"})
    return jsonify({"ok": True, **outcome})


# ═══════════════════════════════════════════════════════════
# /residential-leads — new driveway/sidewalk permits from city open-data
# (see residential_permits.py). No AI involved at all: this is clean
# structured data straight from each city's own permit system, not text an
# LLM has to interpret -- more reliable than the bid-scan path, not less.
# Coverage is narrow and hand-verified city by city (see that module's
# docstring for why some candidate cities were rejected), so the response
# always says whether the area is covered yet rather than a bare empty list
# that could just look like a bug.
# ═══════════════════════════════════════════════════════════
def _lead_within_radius(lead, center, radius, cdb):
    """True if we can confirm the lead is within radius; also true (kept,
    not dropped) when we genuinely can't determine distance at all -- an
    address with no coordinates and no zip isn't grounds to hide a real
    lead, just to not be able to sort it by distance."""
    lat, lon = lead.get("lat"), lead.get("lon")
    if lat is None or lon is None:
        z = (lead.get("zip") or "").strip()
        if not z:
            return True

        def _fetch():
            g = _geo_from_zip(z)
            return (g["lat"], g["lon"]) if g else None

        coords = _cached_point(cdb.setdefault("zip_geo_cache", {}), z, _fetch)
        if not coords:
            return True
        lat, lon = coords
    return _miles_between(center["lat"], center["lon"], lat, lon) <= radius


# A source city's permits sit inside that city, but its centroid can be just
# outside the search radius while its near edge is well inside. Reaching a bit
# further when picking sources costs one extra fetch and nothing else, because
# _lead_within_radius still checks every individual lead exactly.
LEAD_SOURCE_MARGIN_MI = 25.0


def _nearby_lead_sources(center, radius, cdb):
    """Every configured permit source close enough to hold leads in range.

    Coverage used to be an exact match on the city the user typed, so someone
    in a suburb twenty miles from Austin was told residential leads "aren't
    set up for your area yet" while Austin's permit data covered addresses
    comfortably inside their chosen radius. The radius was only ever used to
    filter leads after the fact, never to work out which sources to read.
    """
    reach = radius + LEAD_SOURCE_MARGIN_MI
    found = []
    for (city, state), src in residential_permits.SOURCES.items():
        # Coordinates ship with the registry, so choosing sources never depends
        # on a geocoder being up. Anything without them falls back to a lookup.
        point = src.get("center") or _city_coords(city, state, cdb)
        if not point:
            continue
        if _miles_between(center["lat"], center["lon"], point[0], point[1]) <= reach:
            found.append((city, state))
    return found


@app.route("/residential-leads", methods=["POST"])
def residential_leads():
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
    sources = _nearby_lead_sources(center, radius, cdb)
    covered = bool(sources)

    today = datetime.datetime.now().strftime("%Y%m%d")
    cache = cdb.setdefault("leads_cache", {})
    ckey = f"{center['state']}|{center['city'].lower()}|{int(radius)}|{today}"
    if ckey in cache:
        c = cache[ckey]
        return jsonify({"ok": True, "location": f"{center['city']}, {center['state']}",
                        "leads": c["leads"], "total": len(c["leads"]),
                        "covered": covered, "cached": True})

    leads = []
    for scity, sstate in sources:
        leads.extend(residential_permits.fetch_leads(scity, sstate))
    kept = [l for l in leads if _lead_within_radius(l, center, radius, cdb)]

    cache[ckey] = {"ts": datetime.datetime.now().isoformat(), "leads": kept}
    cdb["leads_cache"] = {k: v for k, v in cache.items() if k.endswith(today)}
    _save_cache(cdb)

    return jsonify({"ok": True, "location": f"{center['city']}, {center['state']}",
                    "leads": kept, "total": len(kept), "covered": covered})


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
        used_ddg = not results
        if used_ddg:
            results = _ddg_search(q)
        for r in results:
            if r["url"] not in seen:
                seen.add(r["url"])
                pages.append(r)
        if used_ddg:  # only the scraped backend needs pacing — see _run_local_queries
            time.sleep(DDG_QUERY_PAUSE)

    grouped, city_coords, drop_stats = {}, {}, {}
    lock = threading.Lock()
    center_coords = (center["lat"], center["lon"])

    # Pages were processed one after another here, unlike /scan. Each one is a
    # page fetch plus an OpenAI call, so a full budget ran well past the app's
    # own request timeout and the user just saw "that took too long". Same
    # prioritisation and fan-out as /scan, and the same recall handling: a
    # planned project naming a county or an authority is anchored to the town
    # we searched rather than dropped.
    def _process(it):
        text = it["content"] or _fetch_text(it["url"])
        if len(text) < 200:
            return
        items = _ai_extract_upcoming(f"{c}, {s}", text)
        if not items:
            return
        with lock:
            for b in items:
                if isinstance(b, dict):
                    b.setdefault("url", it["url"])
                    b["status"] = "Planned"
                    _place_bid(grouped, b, center, radius, cdb, default_city="",
                              city_coords=city_coords, default_state=s,
                              fallback_coords=center_coords, stats=drop_stats)

    with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as ex:
        list(ex.map(_process, _prioritize_pages(pages)[:MAX_PAGES]))

    total = sum(len(v) for v in grouped.values())
    funnel = ", ".join(f"{k}={v}" for k, v in sorted(drop_stats.items())) or "none"
    print(f"[upcoming] {int(radius)} mi from {c},{s} -> {total} planned "
          f"({funnel})", flush=True)
    cache[ckey] = {"ts": datetime.datetime.now().isoformat(), "items": grouped,
                  "total": total, "city_coords": city_coords}
    cdb["upcoming_cache"] = {k: v for k, v in cache.items() if k.endswith(today)}
    _save_cache(cdb)

    return jsonify({"ok": True, "location": f"{center['city']}, {center['state']}",
                    "items": grouped, "total": total, "city_coords": city_coords,
                    "debug": {"pages": len(pages), "kept": total, "funnel": drop_stats}})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
