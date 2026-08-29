"""
license_server.py — License validation + bid scanning for Bid Caller Pro
═══════════════════════════════════════════════════════════════════════════
/scan now returns LOCAL + FEDERAL leads, filtered to a mile radius:
  • LOCAL   — 2,995 known city/county bid pages are read directly (no search
              cost at all), plus web search for everything not in that
              directory: Google Programmable Search first (free, 100/day),
              Tavily if configured and Google came back empty, then scraping
              DuckDuckGo as the last resort. Pages are fetched and OpenAI
              extracts structured bids.
  • FEDERAL — SAM.gov solicitations for the user's state.
  • Both are distance-filtered against the user's radius and grouped by city,
    then cached per area per day.

ENV VARS (set in Render → your service → Environment):
  LICENSE_SECRET           license signing secret
  ADMIN_TOKEN              admin token for /issue and /revoke
  OPENAI_API_KEY           REQUIRED for local extraction
  SAM_API_KEY              optional; federal bids work without it via
                           sam.gov's public search. With a key they use the
                           documented API instead (free: api.data.gov/signup)
  UPSTASH_REDIS_REST_URL   persistent storage (free: upstash.com) -- needed so
  UPSTASH_REDIS_REST_TOKEN   trials/keys survive restarts
  SUPABASE_URL             your Supabase project URL (https://xxx.supabase.co)
  SUPABASE_ANON_KEY        your Supabase publishable/anon key (safe, public)
  STRIPE_WEBHOOK_SECRET    from Stripe -> Developers -> Webhooks (whsec_...)
  RESEND_API_KEY           OPTIONAL, emails the key to buyers (resend.com)
  FROM_EMAIL               OPTIONAL sender, e.g. "Bids <keys@yourdomain.com>"
  BRAVE_API_KEY            local bid search. Free tier ~1,000 queries/mo.
  BRAVE_MIN_INTERVAL       seconds between Brave calls (default 1.1; the
                           with it. BOTH are required or Google is skipped.
  TAVILY_API_KEY           OPTIONAL paid fallback, tried only when Google
                           returns nothing. Scans work without it.

  Real automated email alerts -- daily open bids (/run-saved-search-alerts)
  and weekly planned work (/run-upcoming-alerts), both driven by the same two
  variables. OFF until BOTH are set; safe to leave unset indefinitely:
  SUPABASE_SERVICE_ROLE_KEY  Supabase -> Settings -> API -> service_role
                             key. HIGH PRIVILEGE (bypasses row-level
                             security for the whole project) -- Render env
                             var ONLY, never send this to a client.
  CRON_SECRET                a random string you make up; put the SAME
                             value in this Render env var AND in the
                             GitHub repo's Actions secrets (see
                             .github/workflows/saved-search-alerts.yml and
                             weekly-upcoming-alerts.yml). This is what lets
                             those scheduled workflows (and only them)
                             trigger an alert run.

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
import csv
import json
import math
import base64
import hmac
import hashlib
import datetime
import time
import random
import secrets
import threading
import urllib.request
import urllib.parse
import urllib.error
import urllib.robotparser
from concurrent.futures import (ThreadPoolExecutor, as_completed,
                                TimeoutError as FuturesTimeout)

from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

import bid_portals
import bid_sources
import counties
import federal_bids
import kv_backend
import gov_directory
import residential_permits

app = Flask(__name__)

# ── CORS: the Netlify site, its deploy previews, and any custom domain ──
# This list is the whole reason the browser is allowed to talk to this server,
# so putting the site on a new hostname WITHOUT adding it here loads the page
# fine and then fails every single API call -- login, scan, save. It looks
# like the server is down when it is really the browser refusing to send.
#
# SITE_ORIGINS is how a custom domain gets added without a deploy: a
# comma-separated list of full origins, e.g.
#   SITE_ORIGINS=https://curbcallpro.com,https://www.curbcallpro.com
# Set it in Render the same day DNS is pointed, not after.
def _site_origins():
    origins = [
        re.compile(r"^https://([a-z0-9-]+--)?curbcallpro\.netlify\.app$"),
    ]
    for raw in os.environ.get("SITE_ORIGINS", "").split(","):
        raw = raw.strip().rstrip("/")
        if raw:
            origins.append(raw)
    return origins


CORS(app, resources={r"/*": {"origins": _site_origins()}})

# ── Secrets ──

def _env_secret(name, default):
    """A secret from the environment, trimmed.

    Values pasted into a hosting dashboard routinely pick up a trailing
    newline or space. A token that differs from what the operator typed by an
    invisible character fails every comparison and reports plain
    "unauthorized", which is indistinguishable from having the wrong token --
    a genuinely nasty afternoon. The client already trims what the user types,
    so trimming here makes the two ends agree.
    """
    # Strip first, then fall back: a variable holding only whitespace is a
    # variable someone meant to set and didn't, and it must not become a
    # usable secret.
    return (os.environ.get(name) or "").strip() or default


LICENSE_SECRET = _env_secret("LICENSE_SECRET", "CHANGE_THIS_LONG_RANDOM_SECRET")
_ADMIN_TOKEN_PLACEHOLDER = "CHANGE_THIS_ADMIN_TOKEN"
ADMIN_TOKEN = _env_secret("ADMIN_TOKEN", _ADMIN_TOKEN_PLACEHOLDER)


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


def _db():
    """Load persistent licence data. See kv_backend for where it actually lives."""
    return kv_backend.get(_LIC_KEY, None) or _empty_lic()


def _save_db(db):
    kv_backend.set(_LIC_KEY, db)


_CACHE_KEY = "bidcaller:scan_cache"


def _cache():
    """Scan and geocode cache. Durable via kv_backend, so the geocode cache and
    the same-day scan cache survive a restart instead of resetting every time."""
    return kv_backend.get(_CACHE_KEY, None) or {"scan_cache": {}, "geo_cache": {}}


def _save_cache(c):
    kv_backend.set(_CACHE_KEY, c)


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


def _make_key_with_expiry(plan, exp_dt):
    """Same signing scheme as make_key, but for an explicit expiry date
    rather than a relative month count -- used to stack bonus days onto
    whatever a user's plan already is, instead of overwriting it."""
    date_short = exp_dt.strftime("%Y%m%d")
    sig = _sign(plan, date_short)
    return f"BCP-{plan[:3].upper()}-{date_short}-{sig}", exp_dt.isoformat()


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


def _recent_scans():
    """The scan history, newest first. Never raises — /health must answer even
    when the storage backend is the thing that is broken."""
    try:
        history = kv_backend.get(SCAN_HISTORY_KEY, None) or []
        return list(reversed(history))[:SCAN_HISTORY_MAX] \
            if isinstance(history, list) else []
    except Exception:
        return []


@app.route("/coverage", methods=["POST"])
def coverage():
    """How many verified bid pages we hold within a radius of somewhere.

    Public and unauthenticated on purpose: this is what lets someone check
    their own area BEFORE paying. Coverage is genuinely uneven -- a 50mi
    radius around Boston reaches ~149 verified agencies and one around
    Springfield, MO reaches ~9 -- and a contractor who finds that out after
    subscribing is a refund and a bad review. Better they see it first.

    Cheap by construction: a geocode (cached) plus arithmetic against
    coordinates already on disk. No search credits, no AI call, nothing that
    scales with cost -- so leaving it open costs essentially nothing.
    """
    data = request.get_json(force=True, silent=True) or {}
    location = (data.get("location") or "").strip()
    try:
        radius = float(data.get("radius") or 50)
    except (TypeError, ValueError):
        radius = 50.0
    radius = max(5.0, min(radius, 250.0))
    if not location:
        return jsonify({"ok": False, "reason": "no_location"}), 400
    center = _resolve_center(location)
    if not center:
        return jsonify({"ok": False, "reason": "unresolved_location"}), 404
    try:
        towns = bid_portals.towns_within_radius(
            bid_portals.load_directory(), center["lat"], center["lon"], radius)
    except Exception as ex:
        print(f"[coverage] lookup failed: {ex}", flush=True)
        return jsonify({"ok": False, "reason": "lookup_failed"}), 500
    towns.sort(key=lambda t: _miles_between(center["lat"], center["lon"], t[2], t[3]))
    return jsonify({
        "ok": True,
        "location": f"{center['city']}, {center['state']}".strip(", "),
        "radius": int(radius),
        # Direct-read coverage only. The scan ALSO searches for county,
        # school-district and state-portal work that has no entry here, so
        # this is a floor on what a scan reaches, not a ceiling -- said
        # plainly in the UI rather than quietly inflating the number.
        "agencies": len(towns),
        "nearest": [f"{c}, {s}" for c, s, _, _ in towns[:8]],
    })


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
        "brave_search": bool(BRAVE_API_KEY),     # primary local search
        "tavily": bool(TAVILY_API_KEY),          # optional paid fallback
        "sam_gov": bool(SAM_API_KEY),            # federal bids
        "supabase": bool(SUPABASE_URL and SUPABASE_ANON_KEY),
        "upstash_redis": bool(UPSTASH_URL and UPSTASH_TOKEN),
        "durable_storage": kv_backend.is_durable(),
        "resend_email": bool(RESEND_API_KEY),
        "saved_search_alerts": bool(SUPABASE_SERVICE_ROLE_KEY and CRON_SECRET and RESEND_API_KEY),
        # Weekly planned-work alerts need everything the daily job needs, plus
        # an extraction key -- /upcoming has no non-AI path.
        "upcoming_alerts": bool(SUPABASE_SERVICE_ROLE_KEY and CRON_SECRET
                                and RESEND_API_KEY and OPENAI_API_KEY),
        # The campaign sender refuses outright without a postal address, so
        # "did MAILING_ADDRESS actually take effect" is answerable from the
        # Diagnostics screen instead of by attempting a real send.
        "campaign_sender": bool(RESEND_API_KEY and MAILING_ADDRESS.strip()
                                and _admin_configured()),
        # Without this, bounces and spam complaints are invisible and the
        # list never cleans itself -- which is how a sending domain ends up
        # in everyone's junk folder, taking the trial keys and bid alerts
        # real customers are waiting on with it.
        "resend_webhook": bool(RESEND_WEBHOOK_SECRET),
    }
    tav = _tavily_health()
    brave = _brave_health()
    email_health = _email_health()
    with _ddg_lock:
        ddg_streak = _ddg_fail_streak
    # DuckDuckGo is scraped, so it can be blocked outright. That only threatens
    # local search when Tavily isn't configured to take over.
    ddg = {
        "consecutive_empty_searches": ddg_streak,
        "degraded": ddg_streak >= DDG_TRIP_THRESHOLD,
        "is_sole_local_search": not (backends["brave_search"] or backends["tavily"]),
    }
    problems = []
    # Things worth saying that are not degradations. Kept separate from
    # problems on purpose: problems drive "status": "degraded", and a
    # configuration that is merely less durable than it could be should not
    # make a healthy service look sick.
    notes = []
    # A configured-but-rejected key is worse than an absent one: everything
    # keeps returning 200 and scans just quietly come back nearly empty.
    if backends["brave_search"] and brave["quota_or_auth_failure"]:
        problems.append(
            f"Brave Search is rejecting queries (HTTP {brave['last_status']}) — 429 means "
            "either the one-per-second limit or the monthly free credit is spent; "
            "401/403 means the key is wrong or the subscription lapsed. Local bid search "
            "has fallen back to Tavily or scraping DuckDuckGo.")
    if backends["tavily"] and tav["quota_or_auth_failure"]:
        problems.append(
            f"Tavily is rejecting searches (HTTP {tav['last_status']}) — most likely the "
            "monthly credit allowance is spent. Local bid search has fallen back to "
            "scraping DuckDuckGo and scans will look almost empty until this clears.")
    elif backends["tavily"] and tav["failing"]:
        problems.append("Every Tavily search this run has failed — local bid search is degraded.")
    # Same "configured but rejected is worse than absent" shape as Tavily
    # above: an API key being present tells you nothing about whether Resend
    # will actually accept a send (their sandbox from-address, the default,
    # can only deliver to the account's own verified email) -- this is what
    # actually caught /support silently 500ing in production.
    if backends["resend_email"] and email_health["failing"]:
        problems.append(
            f"Every email send this run has failed (HTTP {email_health['last_status']}: "
            f"{email_health['last_error']}) — support messages, license-key delivery, "
            "referral notices and admin alerts are all silently not arriving. If "
            "FROM_EMAIL is still the onboarding@resend.dev default, Resend's sandbox "
            "address can only send to the account's own verified email — verify a "
            "real domain or point SUPPORT_EMAIL at that verified address.")
    if not backends["openai"]:
        problems.append(
            "OPENAI_API_KEY unset — search-discovered bids and /upcoming are "
            "off. Direct portal reads and state lettings still work.")
    if ddg["is_sole_local_search"] and ddg["degraded"]:
        problems.append("DuckDuckGo appears blocked and no search API is configured — "
                        "local bid search is effectively down. Set BRAVE_API_KEY "
                        "(free tier, roughly 1,000 queries a month).")
    elif ddg["is_sole_local_search"]:
        problems.append("No search API configured — local search depends solely on "
                        "scraping DuckDuckGo. Set BRAVE_API_KEY.")
    if not backends["sam_gov"]:
        # Not a problem any more, just a downgrade: without a key the federal
        # reader falls back to sam.gov's public search, which serves the same
        # data but is an undocumented endpoint and so could change shape
        # without notice. Worth saying, not worth alarming about.
        notes.append("SAM_API_KEY unset — federal bids are using sam.gov's "
                     "public search rather than the documented API. Both "
                     "work; a free key at api.data.gov/signup makes the "
                     "federal source contractually stable.")
    if not kv_backend.is_durable():
        problems.append(
            "No durable storage configured — licences, trial records, the portal "
            "directory and the geocode cache are written to Render's disk, which "
            "is wiped on every deploy and whenever a free instance sleeps. Set "
            "UPSTASH_REDIS_REST_URL/TOKEN, or apply supabase_kv_schema.sql to use "
            "the Supabase project already configured here.")
    # Public vs admin. /health is unauthenticated on purpose -- uptime checks
    # and the keep-warm job need it, and "which backends are configured" is
    # not a secret. But the scan history is: `recent_scans` is a list of the
    # places this account's users have been prospecting, and at one or two
    # customers that is simply their territory, published at a guessable URL.
    # Provider error bodies can also echo request detail. Both move behind
    # the admin token.
    body = {
        "service": "Bid Caller Pro License Server",
        "status": "ok" if not problems else "degraded",
        # Which build is actually answering. Without this there is no way to
        # tell a deploy that picked up a fix from one that silently did not --
        # every other field looks identical either way. Render sets
        # RENDER_GIT_COMMIT; empty elsewhere, which is honest rather than
        # guessed.
        "version": _env_secret("RENDER_GIT_COMMIT", "")[:7],
        "backends": backends,
        "local_search": ddg,
        # Counts and states, but not the provider's response body.
        "brave_search": {k: brave[k] for k in
                          ("ok", "failed", "last_status",
                           "quota_or_auth_failure", "failing")},
        "tavily": {k: tav[k] for k in
                   ("ok", "failed", "last_status",
                    "quota_or_auth_failure", "failing")},
        "email": {k: email_health[k] for k in
                  ("ok", "failed", "last_status", "failing")},
        "problems": problems,
        "notes": notes,
    }
    if not _admin_ok(request.headers.get("X-Admin-Token")):
        return jsonify(body)

    body.update({
        "storage": kv_backend.health(),
        # The most recent scan's funnel. This is the fastest way to tell a
        # genuinely quiet area from a pipeline dropping everything it found.
        "last_scan": kv_backend.get("bidcaller:last_scan", None),
        # ...and the ones before it, so a change in recall reads as a trend
        # rather than a single number with nothing to compare it against.
        "recent_scans": _recent_scans(),
        # What the last nightly accuracy audit measured.
        "feed_audit": kv_backend.get(BID_AUDIT_KEY, None),
        "search_depth": TAVILY_DEPTH,
        # The recall knobs, so what's actually running is visible without
        # reading Render's env-var screen. All are env-tunable; raising them
        # trades scan time and OpenAI spend for coverage.
        "scan_config": {
            "max_pages_per_town": MAX_PAGES,          # SCAN_MAX_PAGES
            "page_workers": PAGE_WORKERS,             # SCAN_PAGE_WORKERS
            "max_pages_per_domain": MAX_PAGES_PER_DOMAIN,  # SCAN_MAX_PAGES_PER_DOMAIN
            "max_anchor_towns": MAX_ANCHOR_TOWNS,     # SCAN_MAX_ANCHORS
            "max_known_towns": MAX_KNOWN_TOWNS,       # SCAN_MAX_KNOWN_TOWNS
            "federal_window_days": SAM_WINDOW_DAYS,  # SAM_WINDOW_DAYS
            "geo_miss_retry_hours": GEO_MISS_RETRY_HOURS,
            "model": OPENAI_MODEL,                    # OPENAI_MODEL
        },
    })
    # Endpoint, not credentials. A stale SAM_SEARCH_URL env var pointing at
    # the old api.sam.gov/prod address is the likeliest reason a configured
    # key produces nothing, and it is invisible from the outside otherwise.
    body["sam_gov"] = {
        "endpoint": SAM_SEARCH_URL,
        "key_configured": bool(SAM_API_KEY),
        "window_days": SAM_WINDOW_DAYS,
        "last_status": _sam_health["last_status"],
        "last_error": _sam_health["last_error"],
    }
    body["brave_search"]["last_error"] = brave["last_error"]
    body["tavily"]["last_error"] = tav["last_error"]
    body["email"]["last_error"] = email_health["last_error"]
    return jsonify(body)
# ═══════════════════════════════════════════════════════════
# PAYMENTS: Stripe webhook -> auto-issue keys (survives restarts)
# ═══════════════════════════════════════════════════════════
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "Bid Caller Pro <onboarding@resend.dev>")
# Shown to customers -- "write to {SUPPORT_EMAIL}" -- so the default must be
# an address on the product's own domain, not a personal one. The old default
# was a private Gmail, which reads as a side project to anyone who sees it and
# meant a missing env var quietly published it. support@curbcallpro.com is
# live on Cloudflare Email Routing and forwards to the same inbox.
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "support@curbcallpro.com")

# ── Email delivery tracking ──
# Resend accepting the request doesn't mean the send worked. FROM_EMAIL's
# default, onboarding@resend.dev, is Resend's own sandbox address -- it can
# only deliver to the account's own verified email, so a domain that was
# never verified fails every real send while /health's "resend_email: true"
# (an API key is merely present) says everything's fine. That failure was
# invisible from every caller's side: /support just told the customer
# "couldn't send", key delivery silently never arrived, and _alert_admin --
# the thing meant to notice problems -- failed the exact same way with
# nobody watching. Tracked the same way _tavily_note/_tavily_health already
# track search failures, through the one function that actually calls
# Resend, so a systematic failure shows up in /health instead of needing a
# live test against production to find (see tests/test_licensing_gate.py).
_email_lock = threading.Lock()
_email_state = {"ok": 0, "failed": 0, "last_error": "", "last_status": 0}


def _email_note(ok, status=0, detail=""):
    with _email_lock:
        if ok:
            _email_state["ok"] += 1
        else:
            _email_state["failed"] += 1
            _email_state["last_status"] = status
            _email_state["last_error"] = (detail or "")[:200]


def _email_health():
    with _email_lock:
        st = dict(_email_state)
    total = st["ok"] + st["failed"]
    st["failing"] = total > 0 and st["ok"] == 0 and st["failed"] > 0
    return st


def _send_email(to, subject, text, reply_to=None, headers=None):
    """The one place that actually calls Resend. Every caller below is
    best-effort (a support message, a key delivery, an admin alert, a
    referral notice), so this never raises -- it reports through
    _email_note/_email_health instead."""
    if not RESEND_API_KEY:
        return False
    payload = {"from": FROM_EMAIL, "to": [to], "subject": subject, "text": text}
    if reply_to:
        payload["reply_to"] = reply_to
    if headers:
        payload["headers"] = headers
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=json.dumps(payload).encode("utf-8"),
        method="POST", headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                                "Content-Type": "application/json",
                                # Resend's API sits behind Cloudflare, which was
                                # answering 403 "error code: 1010" -- its
                                # ban-by-browser-signature response -- to
                                # urllib's default Python-urllib/3.x agent. That
                                # is what actually broke /support in production
                                # (NOT an unverified sending domain, the first
                                # theory). Same lesson this codebase already
                                # learned for DuckDuckGo and BidNet Direct: an
                                # honest, identifiable agent string gets through
                                # where the bare library default does not.
                                "User-Agent": "BidCallerPro/1.0 (+https://curbcallpro.netlify.app)",
                                "Accept": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
        _email_note(True)
        return True
    except urllib.error.HTTPError as ex:
        try:
            detail = ex.read().decode("utf-8", "ignore")
        except Exception:
            detail = ""
        _email_note(False, ex.code, detail or str(ex))
        print(f"[email] send to {to} failed: {ex.code} {detail}", flush=True)
        return False
    except Exception as ex:
        _email_note(False, 0, str(ex))
        print(f"[email] send to {to} failed: {ex}", flush=True)
        return False

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
    if not email:
        return
    _send_email(email, "Your Bid Caller Pro license key",
                "Thanks for subscribing to Bid Caller Pro!\n\n"
                f"Your license key:\n\n    {key}\n\n"
                "If the app didn't unlock automatically, open it, go to the Plan "
                "tab, paste the key under 'Have a license key?', and tap Activate.")


# ── Admin error alerts: know about a crash before a customer reports it ──
_alert_lock = threading.Lock()
_alert_last_sent = {}
ALERT_COOLDOWN_SEC = 1800  # don't re-alert the same error more than every 30 min


def _alert_admin(subject, detail):
    """Email SUPPORT_EMAIL on server errors (best-effort, never raises).
    Rate-limited per distinct subject so a flapping error doesn't spam."""
    if not SUPPORT_EMAIL:
        return
    now = time.time()
    with _alert_lock:
        last = _alert_last_sent.get(subject, 0)
        if now - last < ALERT_COOLDOWN_SEC:
            return
        _alert_last_sent[subject] = now
    _send_email(SUPPORT_EMAIL, f"[CurbCall Pro] {subject}", detail[:4000])


# ── Saved-search alerts: Supabase admin access + new-bid emails ──
# Uses the service-role key to read across ALL users' saved_searches (bypasses
# the row-level-security policies the anon key is normally scoped by) and to
# look up a user's email via the Auth admin API. See /run-saved-search-alerts.
def _supabase_admin_request(path, method="GET", data=None):
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return None
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    }
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Prefer"] = "return=minimal"
    req = urllib.request.Request(
        f"{SUPABASE_URL}{path}", data=body, method=method, headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else True
    except Exception as ex:
        print(f"[admin] supabase admin request failed ({method} {path}): {ex}", flush=True)
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
                if not _is_open_bid(b):
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


# ── Weekly "planned work" alerts (/run-upcoming-alerts) ──
# Same shape as the daily saved-search alerts above, against /upcoming instead
# of /scan. Deliberately weekly, not daily: a capital improvement plan or a
# council budget is republished a few times a year, so a daily email about it
# would be the same list over and over and get filtered as noise. Weekly is
# also the honest cadence for the lead time this feature exists to give -- the
# point is to know about the 2027 sidewalk program while there is still time to
# talk to the city, not to react within 24 hours.
def _send_upcoming_email(email, location, radius, new_items):
    lines = [f'Planned concrete work spotted near "{location}" ({int(radius)} mi):', ""]
    for city, b in new_items[:20]:
        line = f"- {b.get('title') or 'Untitled'} — {city}"
        if b.get("deadline"):
            line += f" (timeline: {b['deadline']})"
        lines.append(line)
        if b.get("url"):
            lines.append(f"  {b['url']}")
    if len(new_items) > 20:
        lines.append(f"...and {len(new_items) - 20} more.")
    lines.extend([
        "",
        "These are budgeted or planned projects, not open bids — nothing here",
        "can be bid on yet. That is the point: it is time to introduce yourself",
        "to the agency before the notice goes out.",
        "",
        "Open CurbCall Pro and check the Upcoming tab for full details.",
    ])
    if _send_email(email, f"{len(new_items)} planned project(s) near {location}",
                   "\n".join(lines)):
        print(f"[upcoming-alerts] sent {len(new_items)} planned items to {email}",
              flush=True)
        return True
    return False


def _run_upcoming_alerts():
    """Runs every saved search through _perform_upcoming once, diffs against
    what that search was already told about, and emails only what's new.

    Reuses the saved_searches table rather than adding a second one: a
    contractor who saved "Aurora, MO / 50mi" wants that area watched, and
    making them save the same area twice for two feeds would be silly.

    Kept separate from _run_saved_search_alerts (rather than folded into it)
    so a failure or a slow run in one cadence can't take the other down, and
    so the weekly job can be rescheduled without touching the daily one."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return {"ok": False, "reason": "supabase_not_configured"}
    if not RESEND_API_KEY:
        return {"ok": False, "reason": "email_not_configured"}
    if not OPENAI_API_KEY:
        # Every /upcoming result comes out of an extraction call. Without a
        # key this would quietly email nobody and report success.
        return {"ok": False, "reason": "ai_not_configured"}

    searches = _fetch_all_saved_searches()
    cdb = _cache()
    seen_store = cdb.setdefault("upcoming_alert_seen", {})
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
            outcome = _perform_upcoming(location, radius)
        except Exception as ex:
            errors.append(f"{user_id}/{location}: {ex}")
            print(f"[upcoming-alerts] run failed for {location!r}: {ex}", flush=True)
            continue
        if not outcome:
            continue

        seen_key = f"{user_id}|{location.lower()}|{int(radius)}"
        seen = set(seen_store.get(seen_key, []))
        all_sigs, new_items = [], []
        for city, items in (outcome.get("items") or {}).items():
            for b in items:
                # No _is_open_bid filter here, unlike the daily job: everything
                # /upcoming returns is status "Planned", which that function
                # correctly calls not-open. Filtering here would send nothing.
                sig = _bid_sig(city, b)
                all_sigs.append(sig)
                if sig not in seen:
                    new_items.append((city, b))
        seen_store[seen_key] = all_sigs[-300:]  # cap so this can't grow forever

        if new_items:
            if user_id not in email_cache:
                email_cache[user_id] = _get_user_email(user_id) or ""
            email = email_cache[user_id]
            if email and _send_upcoming_email(
                    email, outcome.get("location", location), radius, new_items):
                emails_sent += 1

    cdb["upcoming_alert_seen"] = seen_store
    _save_cache(cdb)
    return {"ok": True, "searches_checked": len(searches),
            "users_checked": len(users_checked),
            "emails_sent": emails_sent, "errors": errors}


# ── Full data export (admin-only) ──
# Everything a customer owns lives in one Supabase project on a free plan with
# limited backups. If that project is wiped, suspended or lapses there is no
# second copy anywhere: saved bids, pipeline notes and company profiles are
# user-authored and exist nowhere else.
#
# Deliberately pull-only and manual. The obvious "just back it up
# automatically" answer is a scheduled job committing a dump or uploading an
# Actions artifact, and BOTH leak customer data -- this repository is public.
# So the export is served once, to an authenticated admin, and whoever runs it
# decides where it lands.
_EXPORT_TABLES = ("company_profiles", "saved_bids", "saved_searches",
                  "user_feeds", "reviews")


def _export_table(name):
    """A whole table via the service-role key. Returns (rows, error)."""
    rows = _supabase_admin_request(f"/rest/v1/{name}?select=*")
    if rows is None:
        return [], "unreachable_or_missing"
    if not isinstance(rows, list):
        return [], "unexpected_shape"
    return rows, ""


# ── Read-only diagnostics ────────────────────────────────────────────────
# A second, deliberately weak credential. ADMIN_TOKEN can issue licences,
# send campaigns and export the user table, so it is the wrong thing to hand
# to anyone helping debug a scan -- the blast radius of a leak is the whole
# business. This one reads scan telemetry and nothing else: no user data, no
# licence keys, no email addresses, and no route that changes anything.
DIAG_TOKEN = _env_secret("DIAG_TOKEN", "")


def _diag_ok(supplied):
    if not DIAG_TOKEN:
        return False
    # Refusing the admin token here is deliberate. If the two were
    # interchangeable, "just use the admin one" would quietly become the
    # habit and the separation would buy nothing.
    if _admin_configured() and hmac.compare_digest(supplied or "", ADMIN_TOKEN):
        return False
    return hmac.compare_digest(supplied or "", DIAG_TOKEN)


@app.route("/diag", methods=["GET"])
def diag():
    """Everything needed to debug a scan, and nothing else.

    Read-only by construction: GET, no side effects, and the payload is
    assembled field by field rather than by filtering something larger, so a
    field added elsewhere cannot leak in here by default.
    """
    if not DIAG_TOKEN:
        return jsonify({"ok": False, "reason": "diag_not_configured"}), 503
    if not _diag_ok(request.headers.get("X-Diag-Token")):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403

    brave, tav = _brave_health(), _tavily_health()
    with _ddg_lock:
        ddg_streak = _ddg_fail_streak
    return jsonify({
        "ok": True,
        "version": _env_secret("RENDER_GIT_COMMIT", "")[:7],
        "providers": {
            "brave": {k: brave[k] for k in
                      ("ok", "failed", "last_status", "last_error")},
            "tavily": {k: tav[k] for k in
                       ("ok", "failed", "last_status", "last_error")},
            "ddg": {"consecutive_empty_searches": ddg_streak,
                    "degraded": ddg_streak >= DDG_TRIP_THRESHOLD},
            "benched_until": {k: round(v - time.time(), 1)
                              for k, v in _provider_down_until.items()},
        },
        # Delivery feedback. A campaign is only as good as the list it
        # leaves behind, and bounces are invisible without this.
        "email_events": kv_backend.get(_EMAIL_EVENTS_KEY, None),
        "suppressed_count": len(_suppression()),
        "webhook_configured": bool(RESEND_WEBHOOK_SECRET),
        "last_scan": kv_backend.get("bidcaller:last_scan", None),
        "recent_scans": _recent_scans(),
        "feed_audit": kv_backend.get(BID_AUDIT_KEY, None),
        "scan_config": {
            "max_pages_per_town": MAX_PAGES,
            "max_anchor_towns": MAX_ANCHOR_TOWNS,
            "max_known_towns": MAX_KNOWN_TOWNS,
            "known_town_budget_sec": KNOWN_TOWN_BUDGET_SEC,
            "detail_pages_per_portal": DETAIL_PAGES_PER_PORTAL,
            "probe_timeout": PROBE_TIMEOUT,
            "fetch_timeout": FETCH_TIMEOUT,
            "undated_max_days": UNDATED_MAX_DAYS,
            "model": OPENAI_MODEL,
        },
        "directory": {
            "portals": sum(len(v) for v in bid_portals._national_seeds().values()),
            "wikidata_portals": sum(len(v) for v in bid_portals._wikidata_seeds().values()),
            "geocoded_towns": len(bid_portals._coords()),
        },
    })


@app.route("/admin/whoami", methods=["POST"])
def admin_whoami():
    """Tells the caller, and only the caller, whether their signed-in account
    is an admin. Public and unauthenticated on purpose -- it never reveals
    the allowlist, only a yes/no about the one Supabase token presented, so
    the app can decide whether to show an Admin entry point without needing
    the admin token just to ask the question."""
    data = request.get_json(force=True, silent=True) or {}
    email = _verify_supabase_token(data.get("supabase_token", ""))
    return jsonify({"ok": True, "is_admin": _is_admin_email(email)})


@app.route("/admin/reviews", methods=["POST"])
def admin_reviews():
    """Admin: list every review, or approve/reject one. Admin token required.

    Moderation used to mean opening Supabase's table editor by hand -- fine
    when it was the only queue, awkward now that agency notices and campaign
    drafts already have a page. approve/reject take the review's numeric id;
    both are handled the same way (a boolean flip of `approved`), listed
    separately only so the caller's intent reads clearly in the request.
    """
    data = request.get_json(force=True, silent=True) or {}
    if not _admin_configured():
        return jsonify({"ok": False, "reason": "admin_not_configured"}), 503
    if not _admin_ok(data.get("admin_token")):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return jsonify({"ok": False, "reason": "supabase_not_configured"}), 503

    raw_id = data.get("approve") or data.get("reject")
    if raw_id:
        try:
            review_id = int(raw_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "invalid_review_id"}), 400
        _supabase_admin_request(
            f"/rest/v1/reviews?id=eq.{review_id}", method="PATCH",
            data={"approved": bool(data.get("approve"))})

    rows = _supabase_admin_request(
        "/rest/v1/reviews?select=id,rating,quote,display_name,company,"
        "approved,created_at&order=created_at.desc")
    return jsonify({"ok": True, "reviews": rows if isinstance(rows, list) else []})


@app.route("/admin/export", methods=["POST"])
def admin_export():
    """Full JSON dump of every user-owned table. Admin token required.

    This contains personal data by definition -- names, emails, phone numbers,
    and a contractor's private pipeline notes. Treat the response like a
    password: somewhere private, never a public repo or a shared drive.
    """
    data = request.get_json(force=True, silent=True) or {}
    if not _admin_configured():
        return jsonify({"ok": False, "reason": "admin_not_configured"}), 503
    if not _admin_ok(data.get("admin_token")):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return jsonify({"ok": False, "reason": "supabase_not_configured"}), 503

    tables, errors, total = {}, {}, 0
    for name in _EXPORT_TABLES:
        rows, err = _export_table(name)
        if err:
            # A table that was never created is a real finding, not a crash:
            # report it per-table and still return everything else.
            errors[name] = err
        tables[name] = rows
        total += len(rows)

    print(f"[export] {total} rows across {len(_EXPORT_TABLES)} tables"
          + (f", errors: {errors}" if errors else ""), flush=True)
    return jsonify({"ok": True,
                    "exported_at": datetime.datetime.now(
                        datetime.timezone.utc).isoformat(),
                    "tables": tables,
                    "row_counts": {k: len(v) for k, v in tables.items()},
                    "total_rows": total,
                    "errors": errors})


# ── Feed accuracy audit ──────────────────────────────────────────────────
# Every stale bid a customer has seen was found by the customer. An awarded
# job presented as live work costs trust in a way a missing bid does not, and
# the CivicPlus status bug that caused most of them sat there across ~2,400
# portals until someone happened to recognise a job they had already won.
#
# This samples real portals the way a scan does and measures what the parser
# would hand a customer. It changes nothing and stores a number, so a
# regression shows up as the number moving instead of as a complaint.
BID_AUDIT_KEY = "bidcaller:last_audit"
BID_AUDIT_SAMPLE = int(os.environ.get("BID_AUDIT_SAMPLE", "40"))


def _url_is_alive(url, timeout=12):
    """True if a bid link actually opens. None when we cannot tell.

    HEAD first because it is free, but plenty of government stacks answer 405
    or 404 to HEAD and 200 to GET, so a HEAD failure is retried as a GET
    before anything is called dead. Calling a live link dead would be a worse
    bug than the one this is measuring.
    """
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(
                url, method=method,
                headers={"User-Agent": "BidCallerPro/2.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status < 400:
                    return True
        except urllib.error.HTTPError as e:
            if e.code in (403, 405, 501) and method == "HEAD":
                continue          # server dislikes HEAD, not the URL
            if e.code == 404:
                return False
            return None           # 5xx, rate limit: unknown, not dead
        except Exception:
            continue
    return None


def _audit_portal(entry, link_checks=2):
    """Parse one portal the way a scan does; return per-row verdicts.

    Measures what a customer would be handed, not what the page contains:
    only rows the app would actually SHOW are judged on deadline, contact
    reachability and whether the link opens.
    """
    out = {"rows": 0, "niche_rows": 0, "shown_open": 0, "no_status": 0,
           "open_but_expired": 0, "awarded_shown_open": 0,
           "shown_no_deadline": 0, "links_checked": 0, "links_dead": 0}
    html = _fetch_raw_html(entry["url"])
    if not html:
        out["unreachable"] = 1
        return out
    try:
        # entry["url"], not entry["base"]. The scanner passes the LISTING url
        # here, and passing the site origin instead exercised a code path
        # production never takes -- which is how this audit reported 0 dead
        # links while every CivicPlus posting link was a 404. A monitor that
        # does not call the code the way production calls it will confirm
        # whatever you already believe.
        rows = bid_sources.parse_civicplus_html(html, entry["url"])
    except Exception:
        return out
    today = datetime.datetime.now().date()
    to_check = []
    for r in rows:
        out["rows"] += 1
        status = (r.get("status") or "").strip()
        bid = {"status": status, "deadline": r.get("deadline") or ""}
        if not status:
            out["no_status"] += 1
        # A listing page carries every trade the agency buys. The scan drops
        # anything off-niche BEFORE it reaches a customer, so counting those
        # as "shown" measured the wrong layer entirely -- the first live run
        # reported 100% off-niche, which was this bug, not the feed.
        if not bid_sources.looks_relevant(r.get("title"), r.get("scope")):
            continue
        out["niche_rows"] += 1
        # Collect the link before the open/closed split. Liveness tests how
        # the URL was BUILT, not whether the bid is current -- a closed
        # posting's page still exists. Sampling only open rows gave two links
        # a night, far too thin to catch the construction regression that
        # made every one of them a 404.
        if r.get("url"):
            to_check.append(r["url"])
        if not _is_open_bid(bid):
            continue
        out["shown_open"] += 1
        d = _parse_deadline(bid["deadline"])
        if d and d < today:
            # Shown as open with a deadline already past: the exact failure
            # this audit exists to catch.
            out["open_but_expired"] += 1
        elif not d:
            # No date at all. Nothing can ever age this out, so it would sit
            # in the feed indefinitely -- the remaining staleness hole.
            out["shown_no_deadline"] += 1
        if status.lower().startswith("award"):
            out["awarded_shown_open"] += 1

    for url in to_check[:link_checks]:
        alive = _url_is_alive(url)
        if alive is None:
            continue              # unknown is not evidence of a dead link
        out["links_checked"] += 1
        if not alive:
            out["links_dead"] += 1
    return out


def _fetch_raw_html(url, limit=250000, timeout=15):
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "BidCallerPro/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return resp.read(limit).decode("utf-8", "ignore")
    except Exception:
        return None


def _run_bid_audit(sample_size=None):
    """Sample CivicPlus portals and report what a customer would be shown."""
    size = int(sample_size or BID_AUDIT_SAMPLE)
    portals = []
    for (city, state), entries in bid_portals._national_seeds().items():
        for e in entries:
            if e.get("platform") == "civicplus" and e.get("url"):
                base = e["url"].split("/Bids.aspx")[0]
                # Audit the show-everything view, not the default one. A scan
                # reads both (see bid_sources.civicplus_endpoints), and the
                # default page lists only open bids -- so auditing it would
                # sample almost nothing and, worse, could never see the
                # awarded-shown-as-open failure this exists to catch.
                portals.append({
                    "url": base + "/Bids.aspx?catID=All&txtSort=Category"
                                  "&showAllBids=on",
                    "base": base, "city": city, "state": state})
    if not portals:
        return {"ok": False, "reason": "no_portals"}
    random.shuffle(portals)
    portals = portals[:size]

    totals = {"portals": 0, "unreachable": 0, "rows": 0, "niche_rows": 0,
              "shown_open": 0, "no_status": 0, "open_but_expired": 0,
              "awarded_shown_open": 0, "shown_no_deadline": 0,
              "links_checked": 0, "links_dead": 0}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for res in ex.map(_audit_portal, portals):
            totals["portals"] += 1
            for k, v in res.items():
                totals[k] = totals.get(k, 0) + v

    shown = max(totals["shown_open"], 1)
    bad = totals["open_but_expired"] + totals["awarded_shown_open"]
    totals["stale_rate_pct"] = round(100.0 * bad / shown, 2)
    # Undated rows are not stale today, but nothing can ever age them out.
    totals["undated_pct"] = round(100.0 * totals["shown_no_deadline"] / shown, 2)
    totals["dead_link_pct"] = round(
        100.0 * totals["links_dead"] / max(totals["links_checked"], 1), 2)
    totals["at"] = datetime.datetime.now().isoformat(timespec="seconds")
    totals["ok"] = True
    kv_backend.set(BID_AUDIT_KEY, totals)

    # A feed that is a few percent wrong is worth knowing about; a feed that
    # is badly wrong is worth being woken for.
    # Each threshold has a floor as well as a rate, so a tiny sample with one
    # bad row cannot page anyone.
    faults = []
    if totals["stale_rate_pct"] >= 5 and bad >= 3:
        faults.append(f"{bad} of {totals['shown_open']} shown rows are already "
                      f"expired or awarded ({totals['stale_rate_pct']}%)")
    if totals["dead_link_pct"] >= 10 and totals["links_dead"] >= 3:
        faults.append(f"{totals['links_dead']} of {totals['links_checked']} "
                      f"checked links are dead ({totals['dead_link_pct']}%)")
    if faults:
        _alert_admin(
            "Bid feed quality dropped: " + faults[0].split(" (")[0],
            "The nightly feed audit found:\n  - " + "\n  - ".join(faults) +
            f"\n\n{json.dumps(totals, indent=2)}")
    print(f"[audit] {json.dumps(totals)}", flush=True)
    return totals


@app.route("/run-bid-audit", methods=["POST"])
def run_bid_audit():
    """Nightly feed-accuracy check. Same CRON_SECRET gate as the alert jobs."""
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("token") or request.headers.get("X-Cron-Secret", "")
    if not CRON_SECRET or not hmac.compare_digest(token, CRON_SECRET):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    result = _run_bid_audit(data.get("sample"))
    return jsonify(result), (200 if result.get("ok") else 500)


@app.route("/run-upcoming-alerts", methods=["POST"])
def run_upcoming_alerts():
    """Weekly counterpart to /run-saved-search-alerts, same CRON_SECRET gate
    and same external-scheduler arrangement (see .github/workflows)."""
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("token") or request.headers.get("X-Cron-Secret", "")
    if not CRON_SECRET or not hmac.compare_digest(token, CRON_SECRET):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    result = _run_upcoming_alerts()
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


# ── Referrals: give-a-month-get-a-month ──
# client_reference_id already carries the buyer's device id (see index.html /
# app.html's checkout-link tagging); a pending referral is appended after
# this separator rather than sent as a second Stripe field, so no Stripe-side
# configuration changes were needed to add this. Keep this string identical
# to REFERRAL_SEP in app.html/index.html.
_REFERRAL_SEP = "~ref~"
REFERRAL_BONUS_DAYS_REFERRER = 30
REFERRAL_BONUS_DAYS_REFERRED = 14


def _referral_code_for(db, email):
    """Get-or-create this email's referral code. One code per email, so
    sharing the link twice doesn't mint two codes."""
    email = (email or "").strip().lower()
    if not email:
        return None
    owner_idx = db.setdefault("referral_owner", {})
    codes = db.setdefault("referral_codes", {})
    existing = owner_idx.get(email)
    if existing in codes:
        return existing
    code = secrets.token_hex(4).upper()
    while code in codes:
        code = secrets.token_hex(4).upper()
    codes[code] = {"email": email, "redemptions": 0}
    owner_idx[email] = code
    return code


def _grant_bonus_days(db, email, device, days):
    """Extend a user's plan by `days`, stacking on top of their current
    expiration (or from now, if they have none) rather than overwriting it
    -- an active subscriber shouldn't lose paid time to a bonus."""
    base = datetime.datetime.now()
    plan = "monthly"
    existing_key = (db.get("emails", {}).get((email or "").lower()) if email else None) \
        or (db.get("devices", {}).get(device) if device else None)
    if existing_key:
        info = db.get("issued", {}).get(existing_key)
        if info:
            plan = info.get("plan") or plan
            try:
                cur_exp = datetime.datetime.fromisoformat(info["expires"])
                if cur_exp > base:
                    base = cur_exp
            except (KeyError, ValueError):
                pass
    key, exp = _make_key_with_expiry(plan, base + datetime.timedelta(days=days))
    db.setdefault("issued", {})[key] = {
        "plan": plan, "expires": exp[:10], "email": email or "", "device": device or "",
        "updated": datetime.datetime.now().isoformat()[:10],
    }
    if email:
        db.setdefault("emails", {})[email.lower()] = key
    if device:
        db.setdefault("devices", {})[device] = key
    return key


def _apply_referral_reward(db, ref_code, referred_email, referred_device):
    """Both sides get bonus days once, the first time a referred signup
    completes checkout. Redemption is keyed by the REFERRED person's own
    identity, not the code, so the same new customer can't re-claim by
    reusing a link (or their own) more than once."""
    redeemed = db.setdefault("referral_redeemed", [])
    ident = (referred_email or "").lower() or referred_device
    if not ident or ident in redeemed:
        return
    info = db.get("referral_codes", {}).get(ref_code)
    if not info:
        return
    referrer_email = info.get("email", "")
    if referrer_email and referrer_email == (referred_email or "").lower():
        return  # no self-referral
    redeemed.append(ident)
    info["redemptions"] = info.get("redemptions", 0) + 1
    _grant_bonus_days(db, referrer_email, "", REFERRAL_BONUS_DAYS_REFERRER)
    _grant_bonus_days(db, referred_email, referred_device, REFERRAL_BONUS_DAYS_REFERRED)
    if referrer_email:
        _send_referral_email(referrer_email, REFERRAL_BONUS_DAYS_REFERRER)
    print(f"[referral] {ref_code} redeemed by {ident}", flush=True)


def _send_referral_email(email, days):
    if not email:
        return
    _send_email(email, "You earned a free month on Bid Caller Pro",
                "Someone you referred just subscribed to Bid Caller Pro -- "
                f"we've added {days} free days to your plan. Thanks for "
                "spreading the word!")


@app.route("/referral/code", methods=["POST"])
def referral_code():
    """Signed-in users only -- a referral link is tied to an email so the
    reward has somewhere to land, and Account/Settings (where this is
    surfaced) already requires being signed in."""
    data = request.get_json(force=True, silent=True) or {}
    email = _verify_supabase_token(data.get("supabase_token", ""))
    if not email:
        return jsonify({"ok": False, "reason": "sign_in_required"}), 401
    db = _db()
    code = _referral_code_for(db, email)
    _save_db(db)
    return jsonify({"ok": True, "code": code})


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
        raw_ref = obj.get("client_reference_id") or ""
        device, _, ref_code = raw_ref.partition(_REFERRAL_SEP)
        cust = obj.get("customer") or ""
        amount = obj.get("amount_total") or 0
        plan = "annual" if amount and amount >= 10000 else "monthly"
        key = _issue_for(db, email, device, plan)
        if cust:
            db.setdefault("customers", {})[cust] = {
                "email": email, "device": device, "plan": plan}
        if ref_code:
            _apply_referral_reward(db, ref_code, email, device)
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


# ═══════════════════════════════════════════════════════════
# OUTBOUND EMAIL CAMPAIGNS (admin-only)
# ═══════════════════════════════════════════════════════════
# Commercial email to a list you supply. CAN-SPAM is not optional and the
# rules are cheap to follow, so they are enforced here rather than left to
# whoever writes the campaign text:
#
#   * a real physical postal address must appear in every message -- set
#     MAILING_ADDRESS or this endpoint refuses to send at all, because an
#     accidentally non-compliant blast cannot be un-sent;
#   * every message carries a working one-click unsubscribe, both as a link
#     and as the List-Unsubscribe header mail clients surface themselves;
#   * an unsubscribe is honoured immediately and permanently, and is checked
#     before every send;
#   * the From address and subject are whatever you set -- keep them honest,
#     deceptive headers and subject lines are the part that actually gets
#     people fined.
#
# Deliberately NOT included: any way to harvest recipients. The list is
# supplied per request and never derived from scan data or permit records.
MAILING_ADDRESS = os.environ.get("MAILING_ADDRESS", "")
# Where the unsubscribe link points. Must be this server, since that's what
# serves /unsubscribe -- not the Netlify site.
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://bid-caller-pro.onrender.com").rstrip("/")
CAMPAIGN_MAX_PER_REQUEST = int(os.environ.get("CAMPAIGN_MAX_PER_REQUEST", "200"))
CAMPAIGN_PAUSE_SEC = float(os.environ.get("CAMPAIGN_PAUSE_SEC", "0.35"))
_SUPPRESSION_KEY = "bidcaller:email_suppression"
_DRAFTS_KEY = "bidcaller:campaign_drafts"
# A draft that's been sitting for a day is probably forgotten, and approving
# a forgotten campaign is how the wrong thing goes out. They expire.
DRAFT_TTL_HOURS = int(os.environ.get("CAMPAIGN_DRAFT_TTL_HOURS", "24"))


# Merge fields, written {{like_this}}. A campaign that cannot say "3 open
# jobs within 125 miles of Grimes, the nearest 8 miles out and closing
# September 2" is a generic pitch; one that can is a demonstration. The
# numbers come from a real scan of the recipient's own market, so the claim
# is checkable by the person reading it.
_MERGE_FIELD_RE = re.compile(r"\{\{\s*([a-z0-9_]{1,40})\s*\}\}", re.I)
# A merge value is a few lines of plain text, never a payload. Roomy enough
# for a short formatted list -- a campaign that names three solicitations
# with dates and distances needs about 260 characters for that one field --
# and still far too small to smuggle anything into a recipient's inbox.
MERGE_VALUE_MAX = int(os.environ.get("CAMPAIGN_MERGE_VALUE_MAX", "600"))


def _merge_fields(body):
    """The field names a body asks for, lowercased."""
    return {m.group(1).lower() for m in _MERGE_FIELD_RE.finditer(str(body or ""))}


def _render_body(body, variables):
    """Substitute {{fields}}. Every field must be present -- callers check
    with _missing_merge_fields first, and this asserts the same thing rather
    than quietly shipping a literal {{city}} to a stranger."""
    variables = {k.lower(): str(v) for k, v in (variables or {}).items()}

    def sub(m):
        key = m.group(1).lower()
        if key not in variables:
            raise KeyError(key)
        return variables[key][:MERGE_VALUE_MAX]

    return _MERGE_FIELD_RE.sub(sub, str(body or ""))


def _missing_merge_fields(body, recipients):
    """[(email, [missing field, ...]), ...] for recipients that cannot be
    rendered. Checked at DRAFT time, which is the reviewable step: a
    half-merged blast cannot be recalled, and "Hi {{company}}" reaching a
    real contractor is worse than not sending at all."""
    wanted = _merge_fields(body)
    if not wanted:
        return []
    out = []
    for addr, variables in recipients:
        have = {k.lower() for k, v in (variables or {}).items()
                if str(v or "").strip()}
        gap = sorted(wanted - have)
        if gap:
            out.append((addr, gap))
    return out


def _render_campaign(body, addr, variables=None):
    """The exact text a recipient receives -- one function so the preview
    shown at draft time cannot drift from what approval actually sends."""
    return (f"{_render_body(body, variables)}\n\n---\n{MAILING_ADDRESS.strip()}\n"
            f"Don't want these? Unsubscribe: {_unsub_url(addr)}")


def _clean_recipients(recipients):
    """([(email, vars), ...], skipped_unsubscribed, over_limit). Deduped
    case-insensitively, unsubscribes dropped, batch capped.

    A recipient is either a bare address or {"email": ..., "vars": {...}}, so
    an existing caller passing a flat list keeps working unchanged.
    """
    suppressed = _suppression()
    seen, queue, skipped = set(), [], 0
    for raw in recipients or []:
        if isinstance(raw, dict):
            addr = str(raw.get("email") or "").strip().lower()
            variables = raw.get("vars") if isinstance(raw.get("vars"), dict) else {}
        else:
            addr, variables = str(raw or "").strip().lower(), {}
        if not addr or "@" not in addr or addr in seen:
            continue
        seen.add(addr)
        if addr in suppressed:
            skipped += 1
            continue
        queue.append((addr, variables))
    over = max(0, len(queue) - CAMPAIGN_MAX_PER_REQUEST)
    return queue[:CAMPAIGN_MAX_PER_REQUEST], skipped, over


def _drafts():
    """Pending campaigns, with expired ones already dropped."""
    got = kv_backend.get(_DRAFTS_KEY, None)
    if not isinstance(got, dict):
        return {}
    cutoff = datetime.datetime.now() - datetime.timedelta(hours=DRAFT_TTL_HOURS)
    live = {}
    for k, v in got.items():
        try:
            if datetime.datetime.fromisoformat(v.get("created_at", "")) >= cutoff:
                live[k] = v
        except (TypeError, ValueError):
            continue  # unparseable timestamp -> treat as expired, never as sendable
    return live


def _save_drafts(drafts):
    kv_backend.set(_DRAFTS_KEY, drafts)


def _suppression():
    got = kv_backend.get(_SUPPRESSION_KEY, None)
    return set(got) if isinstance(got, list) else set()


def _suppress(email):
    email = (email or "").strip().lower()
    if not email:
        return False
    current = _suppression()
    if email in current:
        return True
    current.add(email)
    kv_backend.set(_SUPPRESSION_KEY, sorted(current))
    return True


def _unsub_token(email):
    """Signed so an unsubscribe link needs no stored state and cannot be used
    to unsubscribe somebody else by editing the address in the URL."""
    return hmac.new(LICENSE_SECRET.encode(), (email or "").strip().lower().encode(),
                    hashlib.sha256).hexdigest()[:32]


def _unsub_url(email):
    qs = urllib.parse.urlencode({"e": email, "t": _unsub_token(email)})
    return f"{PUBLIC_BASE_URL}/unsubscribe?{qs}"


@app.route("/unsubscribe", methods=["GET"])
def unsubscribe():
    """Public and GET on purpose -- this is the link in the email, and it has
    to work in one click from any mail client with no login and no form."""
    email = (request.args.get("e") or "").strip().lower()
    token = (request.args.get("t") or "").strip()
    page = ("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<body style=\"background:#0d0f18;color:#f1f5f9;font-family:system-ui,sans-serif;"
            "padding:3rem 1.5rem;max-width:32rem;margin:0 auto;\">{}</body>")
    if not email or not hmac.compare_digest(token, _unsub_token(email)):
        return page.format("<h2>That link isn't valid.</h2><p>If you're still getting mail "
                           f"you didn't ask for, reply to it or write to {SUPPORT_EMAIL} "
                           "and we'll take you off by hand.</p>"), 400
    _suppress(email)
    print(f"[campaign] unsubscribed {email}", flush=True)
    return page.format("<h2>You're unsubscribed.</h2><p>We won't email "
                       f"<b>{email}</b> again. Nothing else is needed.</p>")


@app.route("/campaign/send", methods=["POST"])
def campaign_send():
    """Prepare a campaign for a caller-supplied list. SENDS NOTHING.

    Despite the route name this only ever builds a draft and returns the
    exact message for review -- kept as /campaign/send deliberately, so that
    there is no path in this app named "send" that actually mails anybody
    without a separate approval. Sending is /campaign/approve.
    """
    data = request.get_json(force=True, silent=True) or {}
    if not _admin_configured():
        return jsonify({"ok": False, "reason": "admin_not_configured"}), 503
    if not _admin_ok(data.get("admin_token")):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    if not RESEND_API_KEY:
        return jsonify({"ok": False, "reason": "email_unavailable"}), 503
    # A physical address is a legal requirement for commercial email, and an
    # unlawful blast cannot be recalled -- so refuse rather than send.
    if not MAILING_ADDRESS.strip():
        return jsonify({"ok": False, "reason": "mailing_address_not_configured",
                        "detail": "Set MAILING_ADDRESS (a real postal address) before "
                                  "sending commercial email -- CAN-SPAM requires it in "
                                  "every message."}), 503

    subject = (data.get("subject") or "").strip()
    body = (data.get("body") or "").strip()
    recipients = data.get("recipients") or []
    if not subject or not body:
        return jsonify({"ok": False, "reason": "subject_and_body_required"}), 400
    if not isinstance(recipients, list) or not recipients:
        return jsonify({"ok": False, "reason": "no_recipients"}), 400

    queue, skipped, over = _clean_recipients(recipients)
    # Refuse the whole draft if any recipient is missing a field the body
    # asks for. Rejecting just those recipients would be friendlier and
    # wrong: the usual cause is a column named differently from the
    # placeholder, which silently halves the send. Fail here, where it is
    # still reviewable, rather than mailing "Hi {{company}}" to a stranger.
    gaps = _missing_merge_fields(body, queue)
    if gaps:
        return jsonify({
            "ok": False, "reason": "missing_merge_fields", "sent": 0,
            "detail": "Nothing was drafted. Every recipient must supply every "
                      "{{field}} the body uses.",
            "fields_used": sorted(_merge_fields(body)),
            "recipients_missing": [{"email": a, "missing": m}
                                   for a, m in gaps[:20]],
            "recipients_missing_total": len(gaps),
        }), 400

    draft_id = secrets.token_hex(6)
    drafts = _drafts()
    drafts[draft_id] = {
        "subject": subject, "body": body,
        # Stored as [email, vars] pairs. json round-trips these as lists.
        "recipients": [[a, v] for a, v in queue],
        "skipped_unsubscribed": skipped, "over_limit_not_sent": over,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    _save_drafts(drafts)
    print(f"[campaign] drafted {draft_id}: {len(queue)} recipient(s), nothing sent",
          flush=True)
    return jsonify({
        "ok": True, "status": "awaiting_approval", "sent": 0,
        "draft_id": draft_id,
        "would_send": len(queue), "skipped_unsubscribed": skipped,
        "over_limit_not_sent": over, "sample": [a for a, _ in queue[:5]],
        "merge_fields": sorted(_merge_fields(body)),
        # The full rendered message, footer and all -- what you approve is
        # exactly what goes out, not a summary of it. Merged for the FIRST
        # recipient, so a preview of a personalised campaign shows the real
        # numbers rather than the placeholders.
        "preview": _render_campaign(body, queue[0][0], queue[0][1]) if queue else "",
        "preview_for": queue[0][0] if queue else "",
        "expires_in_hours": DRAFT_TTL_HOURS,
        "next": ("Nothing has been sent. Review 'preview', then POST "
                 "/campaign/approve with this draft_id and confirm: true."),
    })


@app.route("/campaign/approve", methods=["POST"])
def campaign_approve():
    """The only endpoint in the app that actually sends a campaign.

    Separate from drafting on purpose: a cold-email blast cannot be
    recalled, so it takes a second, deliberate call naming the exact draft
    -- a fat-fingered request can create a draft, but it cannot mail
    anybody. `confirm: true` has to be passed explicitly as well, so no
    single mistyped field is the difference between reviewing and sending.
    """
    data = request.get_json(force=True, silent=True) or {}
    if not _admin_configured():
        return jsonify({"ok": False, "reason": "admin_not_configured"}), 503
    if not _admin_ok(data.get("admin_token")):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    if not RESEND_API_KEY:
        return jsonify({"ok": False, "reason": "email_unavailable"}), 503
    if not MAILING_ADDRESS.strip():
        return jsonify({"ok": False, "reason": "mailing_address_not_configured"}), 503
    if data.get("confirm") is not True:
        return jsonify({"ok": False, "reason": "confirmation_required",
                        "detail": "Pass confirm: true to send. Nothing was sent."}), 400

    draft_id = (data.get("draft_id") or "").strip()
    drafts = _drafts()
    draft = drafts.get(draft_id)
    if not draft:
        # Also covers a draft that aged out -- see _drafts().
        return jsonify({"ok": False, "reason": "unknown_or_expired_draft",
                        "detail": "Nothing was sent. Draft it again to get a fresh id."}), 404

    # Re-check suppression at approval time, not just at draft time: someone
    # may have unsubscribed in between, and the draft could be hours old.
    stored = draft.get("recipients") or []
    queue, skipped_now, _ = _clean_recipients(
        [{"email": r[0], "vars": r[1]} if isinstance(r, (list, tuple)) else r
         for r in stored])
    # Checked again here, not only at draft time. The draft may be hours old
    # and the body is re-read from storage, so this is the last point at
    # which an unrenderable message can still be stopped.
    gaps = _missing_merge_fields(draft.get("body"), queue)
    if gaps:
        return jsonify({"ok": False, "reason": "missing_merge_fields", "sent": 0,
                        "detail": "Nothing was sent. Re-draft with the missing "
                                  "fields supplied.",
                        "recipients_missing": [{"email": a, "missing": m}
                                               for a, m in gaps[:20]]}), 400

    sent, failed = 0, 0
    for addr, variables in queue:
        unsub = _unsub_url(addr)
        ok = _send_email(addr, draft["subject"],
                         _render_campaign(draft["body"], addr, variables),
                         headers={"List-Unsubscribe": f"<{unsub}>",
                                  "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
        sent += 1 if ok else 0
        failed += 0 if ok else 1
        time.sleep(CAMPAIGN_PAUSE_SEC)  # don't burst a provider into rate-limiting us

    # Consume the draft either way, so an approval can't be replayed into a
    # second copy of the same campaign landing in everyone's inbox.
    drafts.pop(draft_id, None)
    _save_drafts(drafts)
    print(f"[campaign] approved {draft_id}: sent {sent}, failed {failed}, "
          f"skipped {skipped_now}", flush=True)
    return jsonify({"ok": True, "status": "sent", "draft_id": draft_id,
                    "sent": sent, "failed": failed,
                    "skipped_unsubscribed": skipped_now})


@app.route("/campaign/drafts", methods=["POST"])
def campaign_drafts():
    """What's waiting for approval. Admin token required."""
    data = request.get_json(force=True, silent=True) or {}
    if not _admin_configured():
        return jsonify({"ok": False, "reason": "admin_not_configured"}), 503
    if not _admin_ok(data.get("admin_token")):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    discard = (data.get("discard") or "").strip()
    drafts = _drafts()
    if discard:
        drafts.pop(discard, None)
        _save_drafts(drafts)
    return jsonify({"ok": True, "drafts": [
        {"draft_id": k, "subject": v.get("subject", ""),
         "recipients": len(v.get("recipients") or []),
         "created_at": v.get("created_at", "")}
        for k, v in sorted(drafts.items(), key=lambda kv: kv[1].get("created_at", ""))
    ]})


@app.route("/campaign/suppression", methods=["POST"])
def campaign_suppression():
    """Read or add to the do-not-email list. Admin token required."""
    data = request.get_json(force=True, silent=True) or {}
    if not _admin_configured():
        return jsonify({"ok": False, "reason": "admin_not_configured"}), 503
    if not _admin_ok(data.get("admin_token")):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    for addr in (data.get("add") or []):
        _suppress(addr)
    current = sorted(_suppression())
    return jsonify({"ok": True, "count": len(current), "suppressed": current})


# ── Delivery feedback from Resend ───────────────────────────────────────────
# Sending to an address that no longer exists is how a domain's reputation
# dies: mailbox providers read repeated hard bounces and spam complaints as
# evidence the sender does not maintain a list, and start filing everything
# from that domain in junk -- including the trial keys and bid alerts real
# customers are waiting on. The list has to clean itself.
#
# Two events matter and they are treated differently:
#
#   email.bounced     suppress only a PERMANENT bounce. A transient one is a
#                     full mailbox or greylisting, and retiring a good address
#                     over a temporary condition loses a real prospect.
#   email.complained  suppress always, immediately. Somebody pressed "this is
#                     spam"; there is no reading of that which permits another
#                     message, and it is the single most damaging signal a
#                     sender can accumulate.
RESEND_WEBHOOK_SECRET = _env_secret("RESEND_WEBHOOK_SECRET", "")
# Replay window for a signed webhook, in seconds. Svix's own default.
WEBHOOK_TOLERANCE_SEC = int(os.environ.get("WEBHOOK_TOLERANCE_SEC", "300"))
_EMAIL_EVENTS_KEY = "bidcaller:email_events"


def _svix_signature_ok(secret, msg_id, timestamp, raw_body, header):
    """Verify a Svix-signed webhook, which is what Resend sends.

    Signed content is "{id}.{timestamp}.{body}", HMAC-SHA256 under the
    base64 secret, and the header carries a space-separated list of
    "v1,<sig>" so a secret can be rotated without dropping deliveries.

    Verification is not optional here. This endpoint writes to the
    suppression list, so an unauthenticated version would let anyone
    permanently silence any address we mail -- including every prospect at
    once, quietly, with no error anywhere.
    """
    if not secret or not msg_id or not timestamp or not header:
        return False
    try:
        age = abs(time.time() - int(timestamp))
    except (TypeError, ValueError):
        return False
    if age > WEBHOOK_TOLERANCE_SEC:
        return False       # replay of an old, legitimately-signed delivery
    key = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
    try:
        key_bytes = base64.b64decode(key)
    except Exception:
        return False
    signed = f"{msg_id}.{timestamp}.".encode() + raw_body
    expected = base64.b64encode(
        hmac.new(key_bytes, signed, hashlib.sha256).digest()).decode()
    for part in str(header).split():
        _, _, supplied = part.partition(",")
        if supplied and hmac.compare_digest(supplied, expected):
            return True
    return False


def _record_email_event(kind):
    counts = kv_backend.get(_EMAIL_EVENTS_KEY, None)
    counts = counts if isinstance(counts, dict) else {}
    counts[kind] = int(counts.get(kind, 0)) + 1
    counts["last_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    kv_backend.set(_EMAIL_EVENTS_KEY, counts)


def _is_permanent_bounce(data):
    """Resend reports the class on the bounce object. Anything not clearly
    permanent is left alone -- a full mailbox empties."""
    bounce = data.get("bounce") if isinstance(data.get("bounce"), dict) else {}
    blob = " ".join(str(bounce.get(k) or "") for k in
                    ("type", "subType", "sub_type", "message")).lower()
    if "transient" in blob or "temporary" in blob or "soft" in blob:
        return False
    return "permanent" in blob or "hard" in blob or "suppressed" in blob


@app.route("/webhooks/resend", methods=["POST"])
def resend_webhook():
    """Bounces and spam complaints, straight onto the do-not-email list.

    Answers 200 to anything correctly signed, including events it does not
    act on: a non-2xx tells Resend to retry, and retrying an event we simply
    do not care about accomplishes nothing but noise.
    """
    if not RESEND_WEBHOOK_SECRET:
        # Refusing beats accepting unsigned writes to the suppression list.
        return jsonify({"ok": False, "reason": "webhook_not_configured"}), 503
    raw = request.get_data() or b""
    if not _svix_signature_ok(RESEND_WEBHOOK_SECRET,
                              request.headers.get("svix-id"),
                              request.headers.get("svix-timestamp"),
                              raw, request.headers.get("svix-signature")):
        # Counted, because from our side a mistyped secret and a webhook
        # nobody has pointed at us yet look identical: both leave the event
        # counters empty. One is a five-second fix and the other needs no
        # action at all, and without this there is no way to tell which --
        # until months later when the list turns out never to have cleaned
        # itself. A signed request that fails is the loudest possible signal
        # that the secret does not match; it should not be silent.
        _record_email_event("rejected_bad_signature")
        return jsonify({"ok": False, "reason": "bad_signature"}), 401

    try:
        event = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return jsonify({"ok": False, "reason": "unparseable"}), 400
    kind = str(event.get("type") or "").strip().lower()
    data = event.get("data") if isinstance(event.get("data"), dict) else {}

    to = data.get("to")
    addresses = [to] if isinstance(to, str) else list(to or [])
    addresses = [str(a).strip().lower() for a in addresses if str(a or "").strip()]

    _record_email_event(kind or "unknown")
    suppressed = []
    if kind == "email.complained" or (kind == "email.bounced"
                                      and _is_permanent_bounce(data)):
        for addr in addresses:
            if _suppress(addr):
                suppressed.append(addr)
        if suppressed:
            _record_email_event("suppressed")
            print(f"[campaign] {kind}: suppressed {len(suppressed)} address(es)",
                  flush=True)
    return jsonify({"ok": True, "event": kind, "suppressed": len(suppressed)})


# ═══════════════════════════════════════════════════════════
# AGENCY-POSTED NOTICES (the "lister" side)
# ═══════════════════════════════════════════════════════════
# A city or county posts a bid notice directly, and once approved it shows
# up in scans covering that area.
#
# This exists because of what the crawl could NOT reach. Re-probing 269
# missed Missouri domains against 24 URL patterns found exactly one page:
# the rest are towns of a few hundred people and rural water districts with
# no bid page anywhere, and in three cases no working domain at all. Those
# agencies are far too small for Bonfire or OpenGov to sell to, so nobody
# offers them anything. A free form is the only way that work becomes
# visible, and it fills the exact hole crawling can't.
#
# Deliberately NOT a procurement system. It takes a notice and nothing
# else -- no bid submissions, no attachments, no sealed-bid handling, none
# of which can be done casually without real legal exposure. Most states
# also require legal notices in a newspaper of record, so this supplements
# an agency's statutory notice and never replaces it. Said plainly on the
# form itself.
_US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "DC": "district of columbia", "FL": "florida", "GA": "georgia", "HI": "hawaii",
    "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
    "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming", "PR": "puerto rico", "VI": "virgin islands",
    "GU": "guam", "AS": "american samoa", "MP": "northern mariana islands",
}
_STATE_BY_NAME = {name: code for code, name in _US_STATES.items()}


def _normalize_state(raw):
    """A real state code, or "" if it isn't one.

    Never truncate to two characters: "Missouri" cut to "MI" is Michigan, a
    valid code for the wrong state, and the notice would then surface for
    contractors 600 miles away. Accept the full name instead, and reject
    anything that isn't a state at all rather than guessing.
    """
    s = " ".join(str(raw or "").split()).strip()
    if not s:
        return ""
    if len(s) == 2 and s.upper() in _US_STATES:
        return s.upper()
    return _STATE_BY_NAME.get(s.lower(), "")


_AGENCY_KEY = "bidcaller:agency_bids"
_AGENCY_RATE_KEY = "bidcaller:agency_post_rate"
AGENCY_MAX_PER_IP_PER_DAY = int(os.environ.get("AGENCY_MAX_PER_IP_PER_DAY", "10"))
_SUPPORT_RATE_KEY = "bidcaller:support_rate"
SUPPORT_MAX_PER_IP_PER_DAY = int(os.environ.get("SUPPORT_MAX_PER_IP_PER_DAY", "20"))
SUPPORT_MAX_CHARS = int(os.environ.get("SUPPORT_MAX_CHARS", "8000"))
# Deliberately strict: this value becomes a Reply-To header.
_PLAIN_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}")


def _agency_bids():
    got = kv_backend.get(_AGENCY_KEY, None)
    return got if isinstance(got, dict) else {}


def _save_agency_bids(d):
    kv_backend.set(_AGENCY_KEY, d)


def _client_ip():
    """The caller's address, as far as it can be known behind Render's proxy."""
    raw = (request.headers.get("X-Forwarded-For", "")
           or request.remote_addr or "?")
    return raw.split(",")[0].strip()


def _ip_rate_ok(bucket_key, ip, limit):
    """Crude per-IP daily cap, shared by the unauthenticated endpoints.

    Not a real rate limiter -- it is a counter in the same storage the rest
    of the app uses, and it resets at midnight. It exists to stop one script
    from filling a queue or an inbox overnight, which is the actual failure
    mode for a small public form, not to survive a determined attacker.
    """
    today = datetime.datetime.now().strftime("%Y%m%d")
    got = kv_backend.get(bucket_key, None)
    counts = got if isinstance(got, dict) else {}
    if counts.get("day") != today:
        counts = {"day": today, "ips": {}}
    n = int(counts["ips"].get(ip, 0))
    if n >= limit:
        return False
    counts["ips"][ip] = n + 1
    kv_backend.set(bucket_key, counts)
    return True


def _agency_rate_ok(ip):
    """Crude per-IP daily cap. The form is unauthenticated by necessity --
    a rural clerk is not going to create an account -- so this is what
    stops one script filling the moderation queue overnight."""
    today = datetime.datetime.now().strftime("%Y%m%d")
    got = kv_backend.get(_AGENCY_RATE_KEY, None)
    counts = got if isinstance(got, dict) else {}
    if counts.get("day") != today:
        counts = {"day": today, "ips": {}}
    n = int(counts["ips"].get(ip, 0))
    if n >= AGENCY_MAX_PER_IP_PER_DAY:
        return False
    counts["ips"][ip] = n + 1
    kv_backend.set(_AGENCY_RATE_KEY, counts)
    return True


@app.route("/agency/submit", methods=["POST"])
def agency_submit():
    """Public: an agency posts a notice. Nothing is visible until approved."""
    data = request.get_json(force=True, silent=True) or {}
    ip = (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "?")
    ip = ip.split(",")[0].strip()
    if not _agency_rate_ok(ip):
        return jsonify({"ok": False, "reason": "rate_limited",
                        "detail": "That's a lot of notices from one place today. "
                                  f"Email {SUPPORT_EMAIL} and we'll sort it out."}), 429

    def s(key, limit):
        return str(data.get(key) or "").strip()[:limit]

    title, city = s("title", 200), s("city", 80)
    state = _normalize_state(data.get("state"))
    if not title or not city or not state:
        return jsonify({"ok": False, "reason": "title_city_state_required",
                        "detail": "Project title, city and a real US state are needed "
                                  "(two-letter code or the full name)."}), 400

    entry = {
        "title": title, "scope": s("scope", 2000), "city": city, "state": state,
        "deadline": s("deadline", 60), "contact": s("contact", 120),
        "email": s("email", 160), "phone": s("phone", 40), "url": s("url", 400),
        "agency": s("agency", 160), "status": "Open", "source": "agency_posted",
        "approved": False, "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    bids = _agency_bids()
    bid_id = secrets.token_hex(6)
    bids[bid_id] = entry
    _save_agency_bids(bids)
    _alert_admin("New agency bid notice awaiting approval",
                 f"{title}\n{city}, {state}\n\nApprove: /agency/review")
    print(f"[agency] notice {bid_id} submitted: {title[:60]} ({city}, {state})", flush=True)
    return jsonify({"ok": True, "id": bid_id,
                    "message": "Thanks — we'll review it and it'll appear for "
                               "contractors in your area, usually within a day."})


@app.route("/agency/review", methods=["POST"])
def agency_review():
    """Admin: list, approve or delete submitted notices."""
    data = request.get_json(force=True, silent=True) or {}
    if not _admin_configured():
        return jsonify({"ok": False, "reason": "admin_not_configured"}), 503
    if not _admin_ok(data.get("admin_token")):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    bids = _agency_bids()
    approve, delete = (data.get("approve") or "").strip(), (data.get("delete") or "").strip()
    if delete:
        bids.pop(delete, None)
        _save_agency_bids(bids)
    if approve and approve in bids:
        entry = bids[approve]
        # Geocode once, at approval, so a scan never pays for it. A notice we
        # can't place on the map can't be radius-filtered and would show up
        # for the wrong people, so it stays unapproved.
        g = _geo_from_city(entry["city"], entry["state"])
        if not g:
            return jsonify({"ok": False, "reason": "ungeocodable_city",
                            "detail": f"Couldn't place {entry['city']}, {entry['state']}. "
                                      "Fix the city/state on the notice first."}), 400
        entry["lat"], entry["lon"] = g["lat"], g["lon"]
        entry["approved"] = True
        entry["approved_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        _save_agency_bids(bids)
        print(f"[agency] approved {approve}", flush=True)
    return jsonify({"ok": True, "notices": [
        {"id": k, "approved": v.get("approved", False), "title": v.get("title", ""),
         "city": v.get("city", ""), "state": v.get("state", ""),
         "created_at": v.get("created_at", "")}
        for k, v in sorted(bids.items(), key=lambda kv: kv[1].get("created_at", ""))
    ]})


def _add_agency_bids(grouped, center, radius, cdb, city_coords, stats=None):
    """Merge approved agency-posted notices covering this area into a scan.

    Free by comparison with everything else in the pipeline: they're already
    geocoded (at approval), so this is arithmetic and no fetch at all.
    """
    added = 0
    for entry in _agency_bids().values():
        if not entry.get("approved"):
            continue
        lat, lon = entry.get("lat"), entry.get("lon")
        if lat is None or lon is None:
            continue
        if _miles_between(center["lat"], center["lon"], lat, lon) > radius:
            continue
        bid = {k: entry.get(k, "") for k in
               ("title", "scope", "deadline", "contact", "email", "phone", "url", "status")}
        bid["city"] = entry["city"]
        _place_bid(grouped, bid, center, radius, cdb, default_city=entry["city"],
                   city_coords=city_coords, default_state=entry["state"],
                   fallback_coords=(lat, lon), stats=stats)
        added += 1
    if added and stats is not None:
        stats["agency_posted"] = stats.get("agency_posted", 0) + added
    return added


@app.route("/support", methods=["POST"])
def support():
    """Emails a customer's in-app support message to SUPPORT_EMAIL via
    Resend — reuses the same shared, tracked send path as license-key
    delivery, referral notices and admin alerts (_send_email), so a
    systematic failure here also shows up in /health's email stats."""
    data = request.get_json(force=True, silent=True) or {}
    # Unauthenticated by necessity -- somebody whose sign-in is broken still
    # needs to be able to say so. That makes it a public path to our own
    # inbox and our own Resend quota, so it is capped per IP per day and the
    # payload is bounded. Worst case is a noisy day, not a spent quota and an
    # unusable inbox.
    if not _ip_rate_ok(_SUPPORT_RATE_KEY, _client_ip(), SUPPORT_MAX_PER_IP_PER_DAY):
        return jsonify({"ok": False, "reason": "rate_limited",
                        "detail": f"Too many messages from here today. "
                                  f"Email {SUPPORT_EMAIL} directly."}), 429
    email = (data.get("email") or "").strip()[:200]
    message = (data.get("message") or "").strip()[:SUPPORT_MAX_CHARS]
    if not message:
        return jsonify({"ok": False, "reason": "no_message"})
    if not RESEND_API_KEY:
        return jsonify({"ok": False, "reason": "email_unavailable"})
    # Only a plausible address may become Reply-To. It is caller-supplied and
    # ends up in a mail header, so anything else is passed along in the body
    # where it cannot be mistaken for a verified sender.
    reply_to = email if _PLAIN_EMAIL_RE.fullmatch(email) else None
    if email and not reply_to:
        message = f"[unverified contact: {email}]\n\n{message}"
    ok = _send_email(SUPPORT_EMAIL,
                     f"Bid Caller Pro support request{f' from {email}' if email else ''}",
                     message, reply_to=reply_to)
    if ok:
        return jsonify({"ok": True})
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
# Accounts that never trial-expire and never need a subscription -- for
# testing the live product with a real signed-in account instead of resetting
# a device trial. A comma-separated env var, same pattern as MAILING_ADDRESS
# and FROM_EMAIL: nobody's email address belongs in a public repo.
ADMIN_EMAILS = os.environ.get("ADMIN_EMAILS", "")


def _admin_email_set():
    return {e.strip().lower() for e in ADMIN_EMAILS.split(",") if e.strip()}


def _is_admin_email(email):
    return bool(email) and email.strip().lower() in _admin_email_set()


def _trial_identity(email):
    """Normalize an email for TRIAL-ELIGIBILITY purposes only -- never for
    license-key lookups, which must stay exact. Strips a +tag from the local
    part: josh+1@gmail.com and josh+2@gmail.com deliver to the same inbox on
    Gmail, Outlook, Fastmail and most other providers, so without this, a free
    7-day trial (no card required, and every scan spends real OpenAI/search
    budget) could be farmed indefinitely from one real inbox."""
    email = (email or "").strip().lower()
    local, sep, domain = email.partition("@")
    if not sep:
        return email
    return f"{local.split('+', 1)[0]}@{domain}"


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
            if _is_admin_email(email):
                return True
            # email has an active issued key?
            ekey = db.get("emails", {}).get(email)
            if ekey and ekey not in db.get("revoked", []):
                ev, _, _, _ = verify_key(ekey)
                if ev:
                    return True
            # email-based trial -- normalized, so josh+1@ and josh+2@ can't
            # each claim their own free trial off one real inbox
            trials = db.setdefault("trials", {})
            trial_key = f"email:{_trial_identity(email)}"
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


# Civil divisions that are not places anyone lets a bid from. A rural point
# reverse-geocodes to one of these constantly, and BigDataCloud writes them
# with an "of" prefix ("Township of Rock Prairie").
_NON_PLACE_RE = re.compile(
    r"\b(township|twp|unincorporated|unorganized|census\s+designated|"
    r"CDP|precinct|ward|survey|reservation)\b", re.I)


def _reverse_geocode_city(lat, lon):
    """Free, keyless reverse geocode (same provider the app uses client-side
    for auto-fill) -- turns a lat/lon into a {city, state} so we can search
    towns scattered across a wide radius, not just the one the user typed.

    The name this returns is not cosmetic: it becomes the label the user sees,
    every search query the scan builds, and the key the .gov directory is asked
    for. A live scan came back centred on "Township of Rock Prairie, MO" — a
    rural civil township with no procurement office, no registered domain, and
    no meaning in a search query. The structured portal path never ran at all,
    because there is nothing to look up. So candidates are considered in order
    and the first one that names a real government is taken, falling back to
    the county — which does let road and curb work, and which the registry
    knows — rather than to a township.
    """
    url = (f"https://api.bigdatacloud.net/data/reverse-geocode-client"
           f"?latitude={lat}&longitude={lon}&localityLanguage=en")
    data = _get_json(url)
    if not data:
        return None
    sub = (data.get("principalSubdivisionCode") or "").split("-")[-1].upper()
    country = (data.get("countryCode") or "").upper()
    if sub not in STATE_ABBRS or country != "US":
        return None

    # Best first. adminLevel 8 is the municipality, 7 the civil township, 6 the
    # county; the top-level "city" is usually 8 but is empty in rural areas.
    admin = ((data.get("localityInfo") or {}).get("administrative") or [])

    def _named(level):
        return [str(e.get("name") or "").strip() for e in admin
                if isinstance(e, dict) and e.get("adminLevel") == level
                and str(e.get("name") or "").strip()]

    # Kept as (cleaned, original) pairs: _normalize_place strips the very
    # "Township of" prefix that marks a name as unusable, so the check below
    # has to run against what the provider actually said.
    candidates, seen = [], set()
    for raw in ([str(data.get("city") or "").strip()] + _named(8)
                + [str(data.get("locality") or "").strip()]
                + _named(7) + _named(6)):
        cleaned = _normalize_place(raw)
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            candidates.append((cleaned, raw))
    if not candidates:
        return None

    # A name the .gov registry recognises is one we can actually search — but
    # a township still doesn't qualify just because stripping "Township of"
    # leaves something that happens to be registered. "Township of Franklin"
    # became "Franklin", which Missouri does have a domain for, and beat the
    # actual city the point was in.
    for cleaned, raw in candidates:
        if not _NON_PLACE_RE.search(raw) and gov_directory.known_in_state(cleaned, sub):
            return cleaned, sub
    # Otherwise anything that isn't a bare civil division.
    for cleaned, raw in candidates:
        if not _NON_PLACE_RE.search(raw):
            return cleaned, sub
    return None


def _nearby_anchor_towns(center, radius, pdb=None):
    """Pick a handful of towns scattered around the search radius (not just
    the center city) so a wide-radius scan actually looks in more places
    instead of only searching near the one city the user typed. Skipped for
    tight radii where the center-only search already covers the area well."""
    if radius < 40:
        return []
    # Guessed points are worth rationing by area -- most of them land on
    # nothing, so more of them only helps when there is more empty ground to
    # cover. A verified town is not the same trade: every one is a real
    # procurement office, so the budget (MAX_ANCHOR_TOWNS, env-tunable) is
    # worth spending in full rather than scaling it down for a mid-size
    # radius. round(radius/20) gave a 50mi scan just two anchors, which is
    # what left Springfield -- the single most productive source in the area
    # -- out of an Aurora scan entirely.
    n_guessed = max(2, min(MAX_ANCHOR_TOWNS, round(radius / 20)))
    n = MAX_ANCHOR_TOWNS

    # Prefer towns we already know have a real bid page over points guessed
    # off a compass bearing. Anchors are the only towns besides the centre
    # that get SEARCH queries, and that is what actually finds work: a town's
    # own portal lists only its own solicitations, while the queries reach the
    # county road department, the school district and the state portal around
    # it. A 50mi scan from Aurora, MO reaches Springfield (28mi) -- but
    # Springfield only ever got its portal read, which today holds an ice
    # machine rental, a PA system and a skate-shop concession and nothing in
    # this trade, so it contributed nothing, while the query budget went to
    # reverse-geocoded guesses that can land on a township with no
    # procurement office at all (see _reverse_geocode_city's own notes).
    # Same budget, verified targets.
    if pdb is not None:
        try:
            known = bid_portals.towns_within_radius(
                pdb, center["lat"], center["lon"], radius,
                exclude={(center["city"].lower(), center["state"])})
        except Exception as ex:  # never let anchor selection break a scan
            print(f"[scan] known-town anchor lookup failed: {ex}", flush=True)
            known = []
        if known:
            # Nearest first: the closest verified towns are both the most
            # likely to be worth driving to and, for someone in a small town
            # next to a metro, the way the metro itself gets reached. The
            # farther known towns in the radius are not dropped -- they still
            # get their portal read by the known-towns pass in _perform_scan,
            # they just don't get search queries too.
            known.sort(key=lambda t: _miles_between(
                center["lat"], center["lon"], t[2], t[3]))
            return known[:n]
    # One ring of towns at a single distance leaves the ground between it and
    # the centre unsearched, which on a 125mi scan is most of the area. Wide
    # radii get two rings, with the outer ring's bearings offset so the towns
    # interleave rather than lining up along the same spokes.
    rings = [0.7] if radius < 80 else [0.5, 0.85]
    per_ring = max(1, n_guessed // len(rings))
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
    if not _is_open_bid(bid):
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
    # Distance, once the radius is wide enough for it to mean anything. Worth
    # real weight but never as much as a deadline: a job closing in three days
    # forty miles out still beats one closing in a month next door, because
    # the near one will still be there tomorrow. Capped so a 125-mile bid is
    # penalised, not buried.
    miles = bid.get("miles")
    if isinstance(miles, (int, float)):
        score -= min(float(miles), 125.0) / 10.0
    return score


# US timezone abbreviations as UTC offsets. A bid page states its deadline in
# local time ("2:00 PM CST"), the server runs UTC, and getting this wrong in
# the wrong direction closes a bid that is still live -- so the table only
# covers abbreviations that are unambiguous in a US bidding context.
_TZ_OFFSETS = {
    "UTC": 0, "GMT": 0,
    "EST": -5, "EDT": -4, "ET": -5,
    "CST": -6, "CDT": -5, "CT": -6,
    "MST": -7, "MDT": -6, "MT": -7,
    "PST": -8, "PDT": -7, "PT": -8,
    "AKST": -9, "AKDT": -8,
    "HST": -10, "HAST": -10,
}

# The latest zone any US bid could be in. Used when a deadline states a time
# but no zone: assuming Hawaii means we only ever call a bid closed once it
# has closed everywhere, which is the safe direction to be wrong in.
_LATEST_US_OFFSET = -10

_TIME_RE = re.compile(
    r"(?<!\d)(\d{1,2})[:.](\d{2})\s*([ap])\.?m\.?\s*([A-Z]{2,4})?", re.I)


def _parse_deadline_moment(text):
    """The exact instant a deadline falls, as an aware UTC datetime.

    None when the text has no time of day -- a date-only deadline is handled
    by the plain date comparison, which correctly leaves "due today" open all
    day because nobody stated an hour.
    """
    d = _parse_deadline(text)
    if not d:
        return None
    m = _TIME_RE.search(" ".join(str(text or "").split()))
    if not m:
        return None
    hour, minute, half, tz = int(m.group(1)), int(m.group(2)), m.group(3).lower(), m.group(4)
    if hour > 12 or minute > 59:
        return None
    if half == "p" and hour != 12:
        hour += 12
    elif half == "a" and hour == 12:
        hour = 0
    offset = _TZ_OFFSETS.get((tz or "").upper(), _LATEST_US_OFFSET)
    local = datetime.datetime(d.year, d.month, d.day, hour, minute,
                              tzinfo=datetime.timezone(
                                  datetime.timedelta(hours=offset)))
    return local.astimezone(datetime.timezone.utc)


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
        # A deadline that names an hour is checked to the minute. Without this
        # a bid due "08/19/2026 01:00 AM EDT" read as open for the whole of
        # the 19th, hours after it had shut.
        moment = _parse_deadline_moment(deadline_text)
        if moment is not None:
            if moment < datetime.datetime.now(datetime.timezone.utc):
                bid["status"] = "Closed"
        elif d < datetime.datetime.now().date():
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
# Known-portal towns within radius are a direct fetch each, not a search
# query, so this can be far more generous than MAX_ANCHOR_TOWNS without a
# proportional cost increase -- capped so a scan centered in a very
# densely-covered metro doesn't try to fetch every known town in the state.
# How many already-verified bid pages a scan may read. This is the
# GUARANTEED half of a scan -- no search engine involved, just fetching pages
# the directory already knows serve bids -- so a low cap throws away the most
# reliable coverage there is. 40 was set when the directory held ~750
# agencies; it now holds 4,428, and at 40 a 125-mile scan of Emporia read 40
# of 109 known towns and silently ignored the other 69.
MAX_KNOWN_TOWNS = int(os.environ.get("SCAN_MAX_KNOWN_TOWNS", "120"))
# Raising the cap without a clock is how a dense metro turns into a timeout.
# Towns are ordered closest-first, so stopping on time keeps the nearest ones
# and drops the furthest -- the right ones to lose.
KNOWN_TOWN_BUDGET_SEC = float(os.environ.get("SCAN_KNOWN_TOWN_BUDGET", "40"))
KNOWN_TOWN_WORKERS = int(os.environ.get("SCAN_KNOWN_TOWN_WORKERS", "16"))

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


# ── Google Programmable Search — the free-tier primary (no scraping) ──
# 100 queries/day free, permanently, off Google's own index. Chosen over
# Tavily as the default because a scan is 12-40 searches: any paid allowance
# disappears fast, and the free tier here is 3x Tavily's while being a
# documented API rather than a scrape.
#
# Two values, both from Google and both required:
#   BRAVE_API_KEY   api-dashboard.search.brave.com -> subscribe to the free
#                   plan (card required as anti-fraud, not charged) and copy
#                   turn ON "Search the entire web", copy the Search engine ID
BRAVE_API_KEY = _env_secret("BRAVE_API_KEY", "")
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
# Brave's free tier allows one request per second.
BRAVE_MIN_INTERVAL = float(os.environ.get("BRAVE_MIN_INTERVAL", "1.1"))

_brave_state = {"ok": 0, "failed": 0, "last_error": "", "last_status": 0}


def _brave_note(ok, status=0, detail=""):
    with _tavily_lock:  # same lock: these counters are read together in /health
        if ok:
            _brave_state["ok"] += 1
        else:
            _brave_state["failed"] += 1
            _brave_state["last_status"] = status
            _brave_state["last_error"] = (detail or "")[:200]


def _brave_health():
    with _tavily_lock:
        st = dict(_brave_state)
    # 429 = the per-second or monthly cap; 401/403 = a bad or unsubscribed key.
    st["quota_or_auth_failure"] = st["last_status"] in (401, 403, 429)
    st["failing"] = st["failed"] > 0 and st["ok"] == 0
    return st


# Brave's free tier is one request per second. Scans fan out across threads,
# so without pacing here several land in the same second and come back 429 --
# which looks exactly like the monthly quota being spent.
_brave_pace_lock = threading.Lock()
_brave_last_call = [0.0]


def _brave_wait_turn():
    with _brave_pace_lock:
        gap = time.time() - _brave_last_call[0]
        if gap < BRAVE_MIN_INTERVAL:
            time.sleep(BRAVE_MIN_INTERVAL - gap)
        _brave_last_call[0] = time.time()


def _brave_search(query, max_results=5):
    """Search via the Brave Search API; returns [{url, content}].

    Like Google's, this returns only the snippet as `content` -- Brave does
    not serve page bodies, so callers fall through to _fetch_text on the URL
    exactly as they already do for a thin result.
    """
    if not BRAVE_API_KEY:
        return []
    params = urllib.parse.urlencode({
        "q": query,
        # Brave caps count at 20.
        "count": max(1, min(int(max_results or 5), 20)),
        "country": "us",
    })
    req = urllib.request.Request(
        f"{BRAVE_URL}?{params}", method="GET",
        headers={"X-Subscription-Token": BRAVE_API_KEY,
                 "Accept": "application/json"})
    _brave_wait_turn()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        print(f"[scan] Brave HTTP {e.code}: {detail[:160]}", flush=True)
        _brave_note(False, e.code, detail)
        _provider_mark_down("brave", e.code)
        if e.code in (401, 403, 429):
            _alert_admin(
                f"Brave Search returning HTTP {e.code} — local bid search degraded",
                "Brave rejected a query. 429 means either the one-per-second "
                "limit or the monthly free credit is spent; 401/403 means the "
                "key is wrong or the subscription lapsed. Until it clears, "
                "scans fall back to Tavily (if configured) and then to "
                f"scraping DuckDuckGo.\n\nResponse: {detail}")
        return []
    except Exception as ex:
        print(f"[scan] Brave error: {ex}", flush=True)
        _brave_note(False, 0, str(ex))
        return []
    _brave_note(True)
    _provider_clear("brave")
    items = ((data.get("web") or {}).get("results")) or []
    print(f"[scan] Brave: {len(items)} results for {query!r}", flush=True)
    return [{"url": it.get("url") or "",
             "content": it.get("description") or it.get("title") or ""}
            for it in items if it.get("url")][:max_results]


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
        # 402/432 are Tavily's "out of credit" codes, which no retry fixes.
        _provider_mark_down("tavily", 429 if e.code in (402, 432) else e.code)
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
    _provider_clear("tavily")
    results = data.get("results") or []
    print(f"[scan] Tavily: {len(results)} results for {query!r}", flush=True)
    out = []
    for r in results:
        url = r.get("url") or ""
        if url:
            out.append({"url": url,
                        "content": r.get("raw_content") or r.get("content") or ""})
    return out


# A provider that has just answered 401/403/429 will answer the same way for
# every remaining query in this scan. Calling it twelve more times costs a
# round trip each -- and for Brave, 1.1s of pacing each -- before falling
# through to the same fallback every time. Once it has refused for a reason
# that will not change in the next minute, stop asking until the cooldown.
_PROVIDER_COOLDOWN_SEC = float(os.environ.get("SEARCH_PROVIDER_COOLDOWN", "300"))
_provider_down_until = {}
_provider_lock = threading.Lock()


def _provider_is_down(name):
    with _provider_lock:
        return time.time() < _provider_down_until.get(name, 0)


def _provider_mark_down(name, status):
    """Bench a provider after a refusal that a retry will not fix."""
    if status not in (401, 403, 429):
        return
    with _provider_lock:
        _provider_down_until[name] = time.time() + _PROVIDER_COOLDOWN_SEC
    print(f"[scan] {name} benched for {_PROVIDER_COOLDOWN_SEC:.0f}s "
          f"after HTTP {status}", flush=True)


def _provider_clear(name):
    with _provider_lock:
        _provider_down_until.pop(name, None)


def _web_search(query, max_results=6):
    """One search, whichever provider is available. Returns ([{url, content}],
    used_scraper).

    Order is cheapest-reliable first: Brave's free tier (a real API, ~1,000
    queries a month on the free credit), then Tavily if a key is configured
    and still has credit, then scraping DuckDuckGo as the last resort.
    Callers only need `used_scraper` so they can apply DDG's pacing delay and
    skip it entirely otherwise -- the old code inferred that from "did Tavily
    return nothing", which stopped being true the moment there was more than
    one keyed provider.

    Google Programmable Search used to lead this chain. Google is deprecating
    the Custom Search JSON API and no longer lets a project enable it, so the
    integration was removed rather than left to fail 403 on every query.
    """
    if BRAVE_API_KEY and not _provider_is_down("brave"):
        results = _brave_search(query, max_results=max_results)
        if results:
            return results, False
    if TAVILY_API_KEY and not _provider_is_down("tavily"):
        results = _tavily_search(query, max_results=max_results)
        if results:
            return results, False
    return _ddg_search(query), True


# ── DuckDuckGo with a browser "disguise" — last-resort local search (no key) ──
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
# Contact details live on each individual posting, so reading them costs one
# fetch per bid. Bounded per portal, and given the shorter probe timeout: a
# missing phone number degrades a lead, a blown request budget loses every one.
DETAIL_PAGES_PER_PORTAL = int(os.environ.get("SCAN_DETAIL_PAGES", "10"))
DETAIL_WORKERS = int(os.environ.get("SCAN_DETAIL_WORKERS", "6"))


# Who we say we are when reading a government bid page. This is the vast
# majority of the traffic this service generates, and it goes to public
# agencies, so it says plainly what it is.
CRAWLER_UA = ("CurbCallBot/1.0 (+https://curbcallpro.com; "
              "concrete bid aggregator; contact support@curbcallpro.com)")


def _page_headers():
    """Complete headers, honestly identified.

    This used to send a random real-browser User-Agent, on the reasoning that
    municipal sites sit behind bot filters that reject on header fingerprint.
    Measured on 160 live portals from the directory: 157 answer a browser
    agent and 158 answer this one. The impersonation was buying nothing --
    what actually gets through a filter is sending the COMPLETE header set
    below, not lying about the User-Agent string.

    Two other places still rotate browser agents and are deliberately left
    alone, because they are different questions rather than oversights:
    _ddg_search scrapes DuckDuckGo, and _bidnet_direct_urls queries BidNet
    Direct's public search after a documented 403 from a bare fetch. Both are
    third-party services rather than public agencies, and changing either
    would most likely just break it. Worth a deliberate decision, not a
    drive-by edit.
    """
    return {
        "User-Agent": CRAWLER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


# ── robots.txt ──────────────────────────────────────────────────────────────
# The scanner read every page it could reach and never asked. That is not a
# legal problem -- these are public bid notices -- but it is a norm we should
# not be quietly breaking on a paying product, and a site that catches us
# doing it blocks the IP for every customer, not just the one scan.
#
# Measured before switching on: of 150 live portals sampled from the
# directory, 147 allow the bid page and 3 do not. Two percent is a cheap
# price. RESPECT_ROBOTS=0 turns it off if it ever proves otherwise.
#
# Fail-open by design. An unreachable robots.txt is not a refusal, and several
# state sites serve the bid page fine while blocking /robots.txt itself --
# treating that as "disallowed" would drop working sources for no reason.
RESPECT_ROBOTS = os.environ.get("RESPECT_ROBOTS", "1") != "0"
ROBOTS_TIMEOUT = float(os.environ.get("ROBOTS_TIMEOUT", "6"))
_robots_cache = {}
_robots_lock = threading.Lock()


def _robots_allows(url):
    if not RESPECT_ROBOTS:
        return True
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return True
    if not host:
        return True
    with _robots_lock:
        rp = _robots_cache.get(host, "miss")
    if rp == "miss":
        rp = None
        try:
            robots_url = "%s://%s/robots.txt" % (parts.scheme or "https",
                                                 parts.netloc)
            req = urllib.request.Request(robots_url, headers=_page_headers())
            with urllib.request.urlopen(req, timeout=ROBOTS_TIMEOUT) as resp:
                body = resp.read(200000).decode("utf-8", "replace")
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(body.splitlines())
            rp = parser
        except Exception:
            rp = None      # unreadable -> not a refusal, see note above
        with _robots_lock:
            _robots_cache[host] = rp
    if rp is None:
        return True
    try:
        return rp.can_fetch(_page_headers().get("User-Agent", "*"), url)
    except Exception:
        return True


def _fetch_page(url, timeout=None):
    """Fetch a page. Returns (text, outcome) where outcome explains a failure.

    The outcome matters: a 403 and an empty page were previously
    indistinguishable, both arriving as "" — so a portal being actively blocked
    looked exactly like a town with no bids.
    """
    if not _robots_allows(url):
        return "", "robots_disallow"
    try:
        req = urllib.request.Request(url, headers=_page_headers())
        with urllib.request.urlopen(req, timeout=timeout or FETCH_TIMEOUT) as resp:
            return resp.read(800000).decode("utf-8", "ignore"), "ok"
    except urllib.error.HTTPError as e:
        return "", f"http_{e.code}"
    except Exception as ex:
        name = type(ex).__name__.lower()
        return "", "timeout" if "timeout" in name else "unreachable"


def _resolve_bid_url(ai_url, page_url, source_text=""):
    """Which link a bid card should actually open.

    Two things were sending contractors to 404s:

    1. `b.setdefault("url", page_url)` never fired. The extraction prompt says
       'Use "" for any missing field', so the model returns "url": "" -- the
       key EXISTS, so setdefault left the empty string in place and the card
       rendered no link at all.

    2. Worse, when the model did supply a URL it was usually invented.
       _fetch_text strips every tag before the text reaches the model, so it
       never sees an href -- it only sees visible words. Asked for a "url"
       anyway, it reconstructs a plausible-looking one from the domain and a
       guessed path. Plausible-looking and wrong is exactly a 404.

    So the model's URL is trusted only when it appears verbatim in the text
    the model was actually shown -- i.e. the page printed it as visible text.
    Anything else falls back to the page we fetched the bid from, which is
    known-reachable and at worst one click from the real notice.
    """
    page_url = (page_url or "").strip()
    s = (ai_url or "").strip()
    if not s:
        return page_url
    low = s.lower()
    if low.startswith(("http://", "https://")):
        # Verbatim in the source text means the page really published it.
        return s if (source_text and s in source_text) else page_url
    if s.startswith("/") and page_url:
        # A root-relative path is a real path the model read off the page, not
        # a fabricated absolute URL -- resolving it against the page is safe.
        return urllib.parse.urljoin(page_url, s)
    # mailto:, javascript:, bare words, anything else: not a bid link.
    return page_url


def _html_to_text(raw):
    """Visible text from html. Split out of _fetch_text so a caller that
    needs the markup as well can fetch once and derive both."""
    if not raw:
        return ""
    raw = _SCRIPT_RE.sub(" ", raw)
    raw = _TAG_RE.sub(" ", raw)
    raw = re.sub(r"&[a-z#0-9]+;", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _fetch_text(url, timeout=None):
    return _html_to_text(_fetch_page(url, timeout=timeout)[0])


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
# api.data.gov, not api.sam.gov. The previous default,
# api.sam.gov/prod/opportunities/v2/search, answers 404 -- every path under
# that host does, including the ones the docs used to name. So federal bids
# could not have worked even with a key set, which is consistent with there
# never having been a funnel counter for them. api.data.gov/sam/... is live:
# it answers 429 OVER_RATE_LIMIT to the shared DEMO_KEY, which is a rate
# limit on a real endpoint rather than a wrong address.
SAM_SEARCH_URL = os.environ.get(
    "SAM_SEARCH_URL", "https://api.data.gov/sam/opportunities/v2/search")
SCAN_WINDOW_DAYS = int(os.environ.get("SCAN_WINDOW_DAYS", "60"))

# Title keywords, kept as the fallback for a notice with no NAICS code on it.
#
# The history matters: an earlier version OR'd this with "NAICS starts with
# 236/237/238", which is every construction trade there is -- electricians,
# roofers, painters -- and let in a flood. Dropping to keywords-only fixed
# that but overcorrected, because a federal title is written for a
# contracting file, not a search box. Three real jobs found in one probe --
# "Whiteman AFB - FY27 Airfield Pavement", "Ft Leavenworth Asphalt Pavement
# Rehabilitation", "NICO Interpretive Waysides and Walk Improvements" -- are
# all our trade and none of them match a single term below as it stood.
#
# The answer is not a longer keyword list. It is the six-digit NAICS code,
# which states the trade outright: see federal_bids.CONCRETE_NAICS. Keywords
# now only decide notices that arrive without one.
CONSTRUCTION_KEYWORDS = (
    "sidewalk", "ada ramp", "curb ramp", "curb and gutter", "curb & gutter",
    "concrete", "flatwork", "pedestrian ramp", "pavement", "paving",
    "resurfac", "walkway", "hardscape", "curb replacement",
)


def _opp_naics(opp):
    """The NAICS code on a notice, in either transport's spelling."""
    code = opp.get("naicsCode") or opp.get("naics") or ""
    if isinstance(code, list) and code:
        first = code[0]
        code = (first.get("code") if isinstance(first, dict) else first) or ""
        if isinstance(code, list) and code:
            code = code[0]
    return str(code or "").strip()


def _is_construction(opp):
    """True if this federal notice is our trade.

    NAICS first, because it is an assertion rather than an inference: 238110
    IS "poured concrete foundation and structure contractor". A title only
    hints. Keywords stay for notices posted without a code.
    """
    naics = _opp_naics(opp)
    if naics:
        return naics in federal_bids.CONCRETE_NAICS
    title = (opp.get("title") or "").lower()
    return any(k in title for k in CONSTRUCTION_KEYWORDS)


# How far back to ask SAM for postings. NOT SCAN_WINDOW_DAYS, which is 60:
# that window is about how fresh a municipal listing should be, and applying
# it here hid every solicitation posted more than two months ago no matter how
# far in the future its response date was. Federal construction work is
# routinely posted long before it closes -- "Little Rock AFB Base Pavements
# IDIQ FY25" is exactly that shape -- so the posting date is the wrong thing
# to filter on. SAM caps the range at one year, so this asks for all of it and
# lets _is_open_bid decide what is still biddable.
SAM_WINDOW_DAYS = int(os.environ.get("SAM_WINDOW_DAYS", "365"))


# What SAM said last time, so a failure can be diagnosed from /health instead
# of from a scan funnel that only says "failed". The API key is NEVER in here:
# it travels as a query parameter, so anything derived from the URL is scrubbed
# before it is stored.
_sam_health = {"last_status": None, "last_error": ""}


def _sam_scrub(text):
    """Remove the api_key from anything about to be shown or logged."""
    return re.sub(r"(api_key=)[^&\s]+", r"\1<redacted>", str(text or ""))


def _sam_fetch(state):
    """Opportunities for one state, or None if the request itself failed.

    The None/[] distinction is the whole point. This used to end with
    `(data or {}).get("opportunitiesData") or []`, which turned a rejected
    API key, a timeout and a genuinely empty state into the same empty list
    -- so a broken federal source looked exactly like a quiet one, and the
    first live scan after shipping it produced no federal counters at all
    with no way to tell which had happened.
    """
    if not SAM_API_KEY:
        return None
    today = datetime.datetime.now()
    params = {
        "api_key": SAM_API_KEY,
        "postedFrom": (today - datetime.timedelta(days=SAM_WINDOW_DAYS)).strftime("%m/%d/%Y"),
        "postedTo": today.strftime("%m/%d/%Y"),
        "state": state,
        "limit": "1000",
        "offset": "0",
    }
    url = SAM_SEARCH_URL + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/json",
                          "User-Agent": CRAWLER_UA})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # The status is the whole diagnosis: 403 is a rejected key, 404 is a
        # wrong endpoint (SAM_SEARCH_URL still pointing at the old
        # api.sam.gov/prod address), 429 is a rate limit. "failed" alone sent
        # us guessing once already.
        _sam_health["last_status"] = e.code
        _sam_health["last_error"] = _sam_scrub(str(e))
        return None
    except Exception as e:
        _sam_health["last_status"] = None
        _sam_health["last_error"] = _sam_scrub(
            "%s: %s" % (type(e).__name__, e))
        return None
    _sam_health["last_status"] = 200
    _sam_health["last_error"] = ""
    return data.get("opportunitiesData") or []


def _sam_notice_url(notice_id):
    """The public sam.gov page for a notice id, or "" if there isn't one."""
    nid = str(notice_id or "").strip()
    # Ids are hex; anything else is not something to build a URL from.
    return f"https://sam.gov/opp/{nid}/view" if re.fullmatch(r"[0-9a-fA-F]{8,}", nid) else ""


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
        # uiLink is not always present, but every notice has a noticeId and
        # sam.gov's public URL for one is stable. Without this the card
        # renders no link at all -- all the detail and nowhere to go.
        "url": opp.get("uiLink") or _sam_notice_url(opp.get("noticeId")),
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
        "A contract that has ALREADY BEEN AWARDED is not a lead. News stories "
        "reporting that a council awarded a contract, named a winning bid, or "
        "selected a low bidder describe work that is gone -- mark any such item "
        "\"Awarded\", never \"Open\", however recent it is.\n\n"
        "Respond ONLY with a JSON array. Each item has keys: \"title\", \"scope\", "
        "\"status\" (\"Open\", \"Closed\" or \"Awarded\"), \"deadline\", \"contact\", \"email\", "
        "\"phone\", \"value\", \"url\", \"city\". \"deadline\" must be an absolute "
        "calendar date exactly as the page states it (e.g. \"July 24, 2026\" or "
        "\"12/01/2026\"); NEVER a countdown or relative phrase like \"in 8 days\" "
        "or \"next week\" -- use \"\" if the page only gives one of those. "
        "\"city\" is the US city where the work "
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
        if not isinstance(bids, list):
            return []
        # Belt and braces: the prompt now says an awarded contract is Closed,
        # but the model returning "Open" (or nothing) on an award story is
        # exactly the failure that shipped, so decide it here too.
        if _looks_awarded(text):
            for b in bids:
                if isinstance(b, dict) and _is_open_bid(b):
                    b["status"] = "Awarded"
        closed_page = _page_declares_closed(text, len(bids))
        for b in bids:
            if not isinstance(b, dict):
                continue
            b["deadline"] = _clean_deadline(b.get("deadline"))
            if closed_page and _is_open_bid(b):
                b["status"] = "Closed"
        return bids
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
def _enrich_from_detail_pages(rows, stats=None, lock=None):
    """Fill in contact / email / phone by reading each posting's own page.

    A listing page carries titles and dates and nothing a contractor can act
    on. Every bid coming out of the structured path used to arrive with blank
    contact fields, so the app's Email and Call buttons had nothing to attach
    to and the only way to reach the buyer was to go find the posting by hand.

    Mutates `rows` in place. Failure is per-row and silent by design: a posting
    that won't load costs its own contact details, never the bid itself.
    """
    targets = [r for r in (rows or []) if r.get("url")]
    if not targets:
        return

    def _one(row):
        try:
            page, outcome = _fetch_page(row["url"], timeout=PROBE_TIMEOUT)
            if outcome != "ok" or not page:
                # Distinguished from "read it, found nothing new". A live scan
                # enriched 3 of 16 postings where a sandbox sample managed
                # 88%, and those are very different problems: one is the
                # posting pages being unreachable from the server, the other
                # is the extractors not matching what is on them. Guessing
                # between them wasted a round already.
                row["_fetch_failed"] = True
                return False
            found = bid_sources.parse_contact(page)
        except Exception:
            row["_fetch_failed"] = True
            return False
        got = False
        for field in ("contact", "email", "phone"):
            if found.get(field) and not row.get(field):
                row[field] = found[field]
                got = True
        # The posting also carries the closing date and the real scope, where
        # a listing row had only a title. The deadline matters most: without
        # one a bid gets no urgency ranking and cannot be recognised as
        # expired, so last year's programme shows as open indefinitely.
        if not str(row.get("deadline") or "").strip():
            due = bid_sources.detail_deadline(page)
            if due:
                row["deadline"] = due
                got = True
        if not row.get("scope"):
            body = bid_sources.detail_scope(page)
            if body:
                row["scope"] = body
        # The posting sometimes states an engineer's estimate. The structured
        # paths hardcoded value to "" and nothing ever filled it, so a page
        # that said "$220,000" reached the customer blank -- and the card's
        # Est. Value box invited them to guess a number the page had already
        # given them.
        if not str(row.get("value") or "").strip():
            amount = bid_sources.detail_value(page)
            if amount:
                row["value"] = amount
                got = True
        # Fields only the posting carries. The two flags are worth surfacing
        # out of proportion to how often they appear: a missed mandatory
        # pre-bid meeting is not a late bid, it is an ineligible one, and
        # pricing against a scope an addendum has superseded is worse than
        # not bidding at all.
        for field, fn in (("published", bid_sources.detail_published),
                          ("bid_number", bid_sources.detail_bid_number),
                          ("prebid", bid_sources.detail_prebid)):
            if not str(row.get(field) or "").strip():
                val = fn(page)
                if val:
                    row[field] = val
                    got = True
        if not row.get("addenda") and bid_sources.detail_has_addenda(page):
            row["addenda"] = True
            got = True
        if not row.get("documents"):
            docs = bid_sources.detail_documents(page, row["url"])
            if docs:
                row["documents"] = docs
                got = True
        return got

    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as ex:
        results = list(ex.map(_one, targets))

    if stats is not None:
        # These used to count "did the enricher fill ANY field", under names
        # that read as "did we get a contact". With the enricher now also
        # recovering deadline, value, publication date, bid number, pre-bid
        # and documents, that gap made the numbers unreadable: a posting that
        # yielded a deadline and no phone counted as a contact found.
        reachable = sum(1 for r in targets if r.get("email") or r.get("phone"))
        enriched = sum(1 for r in results if r)
        unreachable = sum(1 for r in targets if r.pop("_fetch_failed", False))
        def _bump():
            stats["contacts_found"] = stats.get("contacts_found", 0) + reachable
            missed = len(targets) - reachable
            if missed:
                stats["contacts_missing"] = stats.get("contacts_missing", 0) + missed
            stats["postings_enriched"] = \
                stats.get("postings_enriched", 0) + enriched
            stats["postings_read"] = stats.get("postings_read", 0) + len(targets)
            if unreachable:
                stats["postings_unreachable"] = \
                    stats.get("postings_unreachable", 0) + unreachable
        if lock is not None:
            with lock:
                _bump()
        else:
            _bump()


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
        lookups = gov_directory.lookup(city, state)[:2]
        probed = []
        for entry in lookups:
            for candidate in bid_sources.candidate_bid_urls(entry["domain"], limit=2):
                probed.append({"url": candidate, "probe": True,
                               "platform": "civicplus"
                               if candidate.lower().endswith("bids.aspx") else "custom"})

        # The guessed common paths above only cover CivicPlus and a couple of
        # others -- most platforms put their bid page at a path nothing here
        # would guess. Before falling all the way through to a generic web
        # search -- which has no way to tell a result ABOUT this city apart
        # from one that merely mentions it, see _place_bid's out_of_radius
        # counter, exactly what a city with no known portal and no lucky
        # guess above degrades to -- try an actual bid-shaped link off each
        # entity's own homepage. Same extraction tools/discover_bid_portals.py
        # uses for the offline national crawl, just live and per-scan instead
        # of pre-computed, so a city outside that crawl's coverage still gets
        # a real shot at its own bid page. Fetched concurrently: this runs
        # before the parallel probe stage below, so sequential homepage
        # fetches would add their full latency on top of it for nothing.
        def _homepage_links(entry):
            home_url = f"https://{entry['domain']}"
            html, outcome = _fetch_page(home_url, timeout=PROBE_TIMEOUT)
            if outcome != "ok" or not html:
                return []
            return bid_sources.extract_bid_link_candidates(html, home_url, max_candidates=2)

        if lookups:
            with ThreadPoolExecutor(max_workers=len(lookups)) as ex:
                for links in ex.map(_homepage_links, lookups):
                    for link in links:
                        probed.append({"url": link, "probe": True, "platform": "custom"})

        portals = probed[:8]
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
            page, outcome = _fetch_page(url, timeout=timeout)
            if outcome != "ok" and stats is not None:
                with lock:
                    k = f"portal_fetch_{outcome}"
                    stats[k] = stats.get(k, 0) + 1
            rows = bid_sources.parse_civicplus_html(page, base_url=url)
            if rows:
                keep = []
                for row in rows:
                    if not bid_sources.looks_relevant(row["title"], row.get("scope")):
                        if stats is not None:
                            with lock:
                                stats["filtered_not_niche"] = stats.get("filtered_not_niche", 0) + 1
                        continue
                    keep.append(row)
                # The listing page has no contact details — they live on each
                # individual posting, which we already hold the URL for. A bid
                # with nobody to call is barely a lead, so read them. Capped and
                # concurrent: a sequential pass over a dozen postings is exactly
                # what blew the request budget the last time.
                ordered = _enrichment_order(keep)
                _note_enrich_budget(ordered, DETAIL_PAGES_PER_PORTAL, stats, lock)
                _enrich_from_detail_pages(
                    ordered[:DETAIL_PAGES_PER_PORTAL], stats, lock)
                with lock:
                    bid_portals.record_result(pdb, city, state, url, True)
                    for row in keep:
                        raw[0] += 1
                        # Pass the enriched row THROUGH rather than copying a
                        # fixed list of fields out of it. The old allowlist
                        # named nine keys and hardcoded value to "", so every
                        # field the detail-page enricher had just recovered --
                        # value, published, bid_number, prebid, addenda,
                        # documents -- was read off the posting and then
                        # dropped on the floor one line later. CivicPlus is
                        # the platform behind ~2,400 of the portals in the
                        # directory, so that silently emptied those six rows
                        # of the bid card for most of the board.
                        payload = dict(row)
                        # The listing states its own status where it has one;
                        # trust that over assuming everything on the page is
                        # live.
                        payload["status"] = row.get("status") or "Open"
                        payload["city"] = default_city or city
                        _place_bid(grouped, payload,
                            center, radius, cdb, default_city=default_city or city,
                            city_coords=city_coords, default_state=state,
                            fallback_coords=town_coords, stats=stats)
                return
            if bid_sources.civicplus_page_is_empty(page):
                # The right page, with nothing posted. Sending it to the AI
                # would spend a call and seconds of the scan's town budget to
                # discover the same nothing -- and that budget is what decides
                # how many other towns get read at all.
                with lock:
                    bid_portals.record_result(pdb, city, state, url, True)
                    if stats is not None:
                        stats["civicplus_no_open_bids"] = \
                            stats.get("civicplus_no_open_bids", 0) + 1
                return
            if bid_sources.page_is_missing(page):
                # Checked before the two below because it is the most
                # specific answer: this is not a bid page with a layout we
                # cannot read, it is a 404 or a lapsed domain. Same outcome,
                # but the funnel should say which.
                with lock:
                    bid_portals.record_result(pdb, city, state, url, False)
                    if stats is not None:
                        stats["portal_page_missing"] = \
                            stats.get("portal_page_missing", 0) + 1
                return
            # Before writing this off as a parser gap: the commonest reason a
            # CivicPlus Bids page holds no bids is that the city has MOVED its
            # solicitations to a hosted platform and left this page behind as
            # a signpost -- "View Open Solicitations" pointing at BeaconBid,
            # OpenGov, BidNet. Handing the signpost to the AI reads a page
            # with no bids on it, every scan, forever. Follow it instead, and
            # write the real address into the directory so the next scan goes
            # straight there.
            moved = bid_sources.hosted_portal_link(page, url)
            if moved:
                with lock:
                    bid_portals.record_result(pdb, city, state, url, True)
                    bid_portals.learn_portal(pdb, city, state, moved,
                                             platform="custom",
                                             allow_hosted=True)
                    if stats is not None:
                        stats["portal_moved_to_hosted"] = \
                            stats.get("portal_moved_to_hosted", 0) + 1
                url = moved
                timeout = None
            elif bid_sources.page_is_wrong_module(page):
                # A /Bids.aspx URL serving "Home - Lake County, Ohio" or
                # "Sitka Police Department". The bid module is gone and the
                # site is answering with something else, so there is nothing
                # here to parse and nothing worth an AI call. Let it fail
                # towards MAX_FAIL like any other dead entry.
                with lock:
                    bid_portals.record_result(pdb, city, state, url, False)
                    if stats is not None:
                        stats["portal_wrong_module"] = \
                            stats.get("portal_wrong_module", 0) + 1
                return
            elif stats is not None:
                with lock:
                    stats["civicplus_parse_miss"] = stats.get("civicplus_parse_miss", 0) + 1

        # Fetch once and KEEP the html. _fetch_text discards it, which is
        # why a non-CivicPlus portal's bids could never be given their own
        # posting link: the model is shown text only and never sees an href.
        page_html = _fetch_page(url, timeout=timeout)[0] or ""
        text = _html_to_text(page_html)
        # "200 OK" is not proof the URL is right. Municipal sites overwhelmingly
        # serve their not-found page with a 200, and a parked or lapsed domain
        # serves a sales page the same way -- both are long enough to clear the
        # length check below, so the entry was recorded as a SUCCESS on every
        # scan and could never age out via bid_portals.MAX_FAIL. Sampling 400
        # CivicPlus entries found 21 in that state, one of them a lapsed domain
        # now serving an online-casino page to our customers.
        if bid_sources.page_is_missing(page_html):
            with lock:
                bid_portals.record_result(pdb, city, state, url, False)
                if stats is not None:
                    stats["portal_page_missing"] = \
                        stats.get("portal_page_missing", 0) + 1
            return
        ok = len(text) >= 200
        with lock:
            bid_portals.record_result(pdb, city, state, url, ok)
        if not ok:
            return
        # Skip the extraction call on a portal with nothing of ours anywhere
        # on it. 21 of 33 sampled agency portals are in that state at any
        # moment -- each one an AI call, and seconds of the scan's town
        # budget, spent to find nothing. The portal is still recorded above as
        # a working source; there is simply nothing for us on it today.
        #
        # page_may_hold_work, NOT looks_relevant. This used looks_relevant,
        # which is the per-posting test and vetoes on words like "professional
        # services" before it ever looks for a trade term. On a whole page
        # that made one unrelated RFP enough to discard every real bid beside
        # it: 44 of 166 town portals were skipped this way while their text
        # said concrete, sidewalk, curb or paving. It also contradicted the
        # contract _run_local_queries documents for this function -- that a
        # known portal is trusted and a gap here is our problem, not evidence
        # the page is irrelevant. Which postings are actually ours is still
        # decided per posting, below and in the CivicPlus branch above.
        if not bid_sources.page_may_hold_work(text):
            if stats is not None:
                with lock:
                    stats["portal_no_niche_content"] = \
                        stats.get("portal_no_niche_content", 0) + 1
            return
        bids = _ai_extract(ai_label, text)
        if not bids:
            return
        # Bids that ended up with their own posting URL can be enriched the
        # same way the CivicPlus path already is -- that recovers a deadline
        # on 95% of postings and a phone on 81%. Ones still pointing at the
        # listing page are skipped: re-reading the page we just read adds
        # nothing.
        own_pages = [b for b in bids if isinstance(b, dict)
                     and bid_sources.link_for_title(page_html, url, b.get("title"))]
        for b in own_pages:
            b["url"] = bid_sources.link_for_title(page_html, url, b.get("title"))
        if own_pages:
            ordered = _enrichment_order(own_pages)
            _note_enrich_budget(ordered, DETAIL_PAGES_PER_PORTAL, stats, lock)
            _enrich_from_detail_pages(ordered[:DETAIL_PAGES_PER_PORTAL],
                                      stats, lock)
        with lock:
            raw[0] += len(bids)
            for b in bids:
                if isinstance(b, dict):
                    # Prefer this bid's own posting link, matched from the
                    # page's anchors by title. Falling back to the listing
                    # page is correct but lands the contractor on a list, and
                    # leaves the enricher nothing per-posting to read.
                    own = bid_sources.link_for_title(page_html, url, b.get("title"))
                    b["url"] = own or _resolve_bid_url(b.get("url"), url, text)
                    # `or city`, matching the CivicPlus branch above. This is
                    # a known portal -- this town's OWN bid page -- so a bid
                    # on it that doesn't restate the town is still that
                    # town's. Without the fallback _place_bid dropped it as
                    # no_location, losing bids from the most reliable source
                    # in the pipeline. The fix was applied to the CivicPlus
                    # branch and missed here, which is where every non-
                    # CivicPlus portal is read -- 1,260 of them.
                    _place_bid(grouped, b, center, radius, cdb, pdb=pdb,
                              default_city=default_city or city,
                              city_coords=city_coords, default_state=state,
                              fallback_coords=town_coords, stats=stats)

    if portals:
        with ThreadPoolExecutor(max_workers=min(PORTAL_WORKERS, len(portals))) as ex:
            list(ex.map(_read_portal, portals))
    return raw[0]


# Aggregator platforms a city's own bid page structurally cannot show. Packed
# into two OR-ed site: queries rather than one per domain: every engine we use
# supports OR-ed site: filters, and eight separate searches for the same
# question was the single biggest line item in a scan's search budget.
_AGG_SITES = (
    ("bidnetdirect.com", "demandstar.com", "planetbids.com", "publicpurchase.com"),
    ("questcdn.com", "opengov.com", "bonfirehub.com", "bidexpress.com",
     "bidsearch.com"),
)

# State procurement portals, folded into the packed query for that state
# rather than costing a search of their own.
_STATE_PORTALS = {"MO": "missouribuys.mo.gov"}


def _agg_sites(group, state=""):
    """An OR-ed site: filter for one group of aggregator domains."""
    sites = list(_AGG_SITES[group])
    extra = _STATE_PORTALS.get((state or "").upper())
    if extra and group == len(_AGG_SITES) - 1:
        sites.append(extra)
    return " OR ".join("site:" + d for d in sites)


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
        results, used_ddg = _web_search(q, max_results=6)
        for r in results:
            with lock:
                if r["url"] in seen_urls:
                    continue
                seen_urls.add(r["url"])
            # Place the result before paying for it. A search for "Aurora MO
            # sidewalk bid" reliably returns auroragov.org -- Aurora,
            # COLORADO -- and the old order fetched the page and spent an AI
            # extraction before _place_bid worked out it was 700 miles away.
            # 21 of 38 extractions on a live Aurora scan died that way. Only
            # acts on domains the directory can actually place; an unknown
            # domain is still fetched, since absence proves nothing.
            known = bid_portals.town_for_url(r["url"])
            if known:
                pt = bid_portals.coords_for_town(*known)
                if pt and _miles_between(center["lat"], center["lon"],
                                         pt[0], pt[1]) > radius:
                    if stats is not None:
                        with lock:
                            stats["search_hit_out_of_area"] = \
                                stats.get("search_hit_out_of_area", 0) + 1
                    continue
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
        # These pages are unverified search hits, unlike a known portal's own
        # listing (see _run_known_portals, which deliberately does NOT gate
        # on content -- a parser gap there is our problem, not evidence the
        # page is irrelevant). Here there's no such trust to lean on, so a
        # page with none of the niche terms anywhere in it is essentially
        # never going to yield a bid -- skip the OpenAI call rather than pay
        # for a page about janitorial services or a council-meeting agenda
        # that happened to rank for the search query.
        if not bid_sources.looks_relevant(text):
            if stats is not None:
                with lock:
                    stats["filtered_not_niche"] = stats.get("filtered_not_niche", 0) + 1
            return
        bids = _ai_extract(ai_label, text)
        if not bids:
            return
        with lock:
            raw[0] += len(bids)
            for b in bids:
                if isinstance(b, dict):
                    b["url"] = _resolve_bid_url(b.get("url"), it["url"], text)
                    bid_city = (b.get("city") or default_city or "").split(",")[0].strip()
                    _place_bid(grouped, b, center, radius, cdb, pdb=pdb,
                               default_city=default_city,
                              city_coords=city_coords, default_state=state,
                              fallback_coords=town_coords, stats=stats)
                    if bid_city and state:
                        bid_portals.learn_portal(pdb, bid_city, state, it["url"])

    with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as ex:
        list(ex.map(_process, items[:max_pages]))

    return raw[0]


# Words that mean a solicitation can't be bid on right now — either it is over,
# or (in /upcoming's case) it hasn't been let yet. Anything else counts as open.
# Local news coverage of a council awarding a contract reads almost exactly
# like a bid notice to the extraction model: same project, same agency, same
# dollar figure. It is the opposite of a lead -- the work is already gone. A
# real example that reached a customer: "The City Council awarded the contract
# for a new sidewalk to be installed at Westwood Drive... The winning bid was
# $748,908 from Cardenas Concrete". These phrases only exist once a winner does.
_AWARD_PHRASES = (
    "awarded the contract", "award the contract", "contract was awarded",
    "contract awarded", "was awarded to", "awarded to ", "winning bid",
    "winning bidder", "successful bidder", "apparent low bidder",
    "low bidder was", "bid was accepted", "council awarded",
    "commission awarded", "notice of award", "has been awarded",
)

# A live bid listing page routinely carries past awards next to current
# solicitations, so award language on its own must never close a page that is
# still plainly asking for bids.
_OPEN_SOLICITATION_PHRASES = (
    "bids due", "bid due", "proposals due", "due date", "accepting bids",
    "now accepting", "sealed bids will be received", "will be received until",
    "bid opening", "submit bids", "submittal deadline", "closes on",
    "responses due", "questions due", "request for proposals due",
    "bids will be accepted", "deadline for bids",
)


# A countdown is not a deadline. "In 8 days" was rendered by somebody's page
# at some unknown moment; kept as-is it displays as fact, scores as urgent,
# and -- because it carries no date and no year -- _apply_deadline_status
# cannot tell that it expired. A real listing showed "In 8 days" for a
# solicitation that had closed 26 days earlier.
_RELATIVE_DEADLINE_RE = re.compile(
    r"^(in\s+(a|an|\d+)\s+(day|week|month|hour)s?|tomorrow|today|tonight|"
    r"next\s+(week|month|year)|this\s+(week|month)|\d+\s+days?\s+(left|remaining)|"
    r"closing\s+soon|due\s+soon|asap|open\s+now|ongoing|tbd|n/?a)\.?$", re.I)


def _clean_deadline(text):
    """Deadline text with unverifiable countdowns removed.

    Anything naming a date or even a bare year is kept -- "FY2027" and
    "December 1, 2026" are both checkable. A pure countdown is not, and
    showing "Not listed" beats showing a number we cannot stand behind.
    """
    s = " ".join(str(text or "").split())
    if not s or _parse_deadline(s):
        return s
    return "" if _RELATIVE_DEADLINE_RE.match(s) else s


# Phrases where a page states, about itself, that the thing is over.
_EXPLICIT_CLOSED_MARKERS = (
    "status: closed", "status:closed", "past due", "bidding is closed",
    "bidding has closed", "this solicitation is closed",
    "solicitation is closed", "no longer accepting", "submissions are closed",
    "closed to bidding", "bid is closed", "closed for bidding",
)


def _page_declares_closed(text, bid_count):
    """True when the page itself says this solicitation is closed.

    Only trusted on a single-solicitation detail page. A listing page carries
    a status per row and one row reading "Closed" says nothing about the
    others -- closing all of them would throw away real work, which is a
    worse failure than showing one stale bid. On a page describing exactly
    one solicitation there is no such ambiguity.
    """
    if bid_count != 1:
        return False
    low = (text or "").lower()
    return any(m in low for m in _EXPLICIT_CLOSED_MARKERS)


def _looks_awarded(text):
    """True when this page is reporting a finished award, not soliciting one."""
    low = (text or "").lower()
    if not any(p in low for p in _AWARD_PHRASES):
        return False
    return not any(p in low for p in _OPEN_SOLICITATION_PHRASES)


_CLOSED_STATUS_WORDS = ("closed", "close date", "awarded", "award to",
                        "cancel", "expired", "withdrawn", "archived",
                        "no longer", "not accepting", "complete",
                        "planned", "upcoming", "anticipated")


def _is_open_bid(bid):
    """A bid the client will actually display. Mirrors isOpen() in app.html.

    This used to require the status to be exactly "open", and dropped anything
    else. That is not how the real world writes it: agencies and the extraction
    model both produce "Accepting Bids", "Active", "Advertised", "Open - Bids
    Due 12/1", "Currently Open". Every one of those was placed into the result
    by _place_bid, counted as kept in the funnel, and then reported as zero and
    hidden by the app — a scan could find seven genuine local bids and still
    tell the contractor there was nothing out there. So the test is inverted:
    a bid is open unless it says it isn't.
    """
    status = str((bid or {}).get("status") or "").strip().lower()
    if not status:
        return True  # unstated status is not evidence of a closed bid
    return not any(word in status for word in _CLOSED_STATUS_WORDS)


def _closed_on_arrival(row):
    """True if the listing row already says this bid is shut.

    Deliberately built out of the two functions that decide this everywhere
    else, on a throwaway copy, so it can never drift from them: a stated
    Closed/Awarded status, or a deadline already past. An undated row is
    never closed on arrival — reading its posting is the only thing that can
    date it, so it is the last row we would want to skip.
    """
    probe = {"status": (row or {}).get("status"),
             "deadline": (row or {}).get("deadline")}
    _apply_deadline_status(probe)
    return not _is_open_bid(probe)


def _enrichment_order(rows):
    """Rows ordered by how much reading each posting is worth.

    Enrichment is rationed twice: ten postings per portal, and fourteen across
    the whole result. The second cap is where this matters. A scan keeps every
    bid it finds and lets the client hide the closed ones, so the pile handed
    to _enrich_placed_bids is mostly dead: a 125-mile scan from New Salem, MA
    kept 93 bids of which 83 had already closed. Sorted only by whether a
    deadline was present, roughly nine of the fourteen reads went to bids
    nobody can bid on, and the handful of live ones arrived with no contact,
    no scope and no engineer's estimate.

    The per-portal cap turns out not to have this problem — 38 live CivicPlus
    portals were checked and not one had enough closed relevant rows to push
    an open bid out of its ten. The ordering is applied there anyway because
    it costs nothing and the pathological page is only a matter of time, but
    the measured win is at the whole-result stage.

    Undated first — a missing deadline is the one thing only the posting can
    supply, and until it is supplied the bid cannot even be recognised as
    expired. Then open dated rows. Closed rows last: they are still kept and
    still shown with their Closed badge, they just stop taking a slot from a
    bid somebody could actually still bid on. Stable, so listing order is
    preserved within each group.
    """
    def rank(row):
        if not str((row or {}).get("deadline") or "").strip():
            return 0
        return 2 if _closed_on_arrival(row) else 1
    return sorted(rows or [], key=rank)


def _note_enrich_budget(rows, budget, stats, lock=None):
    """Count postings the budget could not reach, split by whether it mattered.

    Without this the reordering above is unfalsifiable. `enrich_budget_spared`
    is the number of already-closed postings that fell outside the budget —
    slots the old order would have spent on a dead bid. `enrich_budget_short`
    is the number of open or undated ones that did not fit, which is the
    honest cost of a cap of this size and the number to watch if it grows.
    """
    if stats is None or len(rows) <= budget:
        return
    missed = rows[budget:]
    spared = sum(1 for r in missed if _closed_on_arrival(r))
    short = len(missed) - spared

    def _bump():
        if spared:
            stats["enrich_budget_spared"] = \
                stats.get("enrich_budget_spared", 0) + spared
        if short:
            stats["enrich_budget_short"] = \
                stats.get("enrich_budget_short", 0) + short
    if lock is not None:
        with lock:
            _bump()
    else:
        _bump()


def _status_breakdown(grouped):
    """How many bids in a result carry each status, e.g. {"Open": 2, "Closed": 7}."""
    out = {}
    for bids in (grouped or {}).values():
        for b in bids:
            label = str((b or {}).get("status") or "(none)").strip() or "(none)"
            out[label] = out.get(label, 0) + 1
    return out


def _scan_sample(grouped, limit=8):
    """A handful of what the last scan actually placed, for /health.

    Only fields that already appear on a public bid notice — no contact
    details, nothing about who ran the scan.
    """
    out = []
    for city, bids in sorted((grouped or {}).items()):
        for b in bids:
            out.append({"city": city,
                        "title": str((b or {}).get("title") or "")[:120],
                        "deadline": str((b or {}).get("deadline") or ""),
                        "status": str((b or {}).get("status") or ""),
                        "open": _is_open_bid(b)})
            if len(out) >= limit:
                return out
    return out


def _bid_dupe_key(bid):
    """Identity of a solicitation for de-duplication: title + deadline.

    Both halves are normalised, because the same job routinely arrives twice
    -- once off the agency's own page and once via search or an aggregator --
    written slightly differently each time. Comparing the deadline as raw text
    meant "9/3/2026" and "09/03/2026" were different bids, as were
    "09/03/2026" and "09/03/2026 02:00 PM EDT", so the contractor got two
    cards for one job and starring one did nothing to the other.
    """
    raw = (bid or {}).get("title") or ""
    title = re.sub(r"\s+", " ", str(raw)).strip().lower().strip(" .,-\u2013\u2014:;")
    due = str((bid or {}).get("deadline") or "").strip()
    parsed = _parse_deadline(due)
    # An unparseable deadline keeps its text, so two genuinely different
    # free-text dates still separate two genuinely different bids.
    return title, (parsed.isoformat() if parsed else due.lower())


# Bodies that let concrete work but are not places a gazetteer can find:
# counties, road districts, school districts, transit and housing authorities.
_AUTHORITY_RE = re.compile(
    r"\b(count(?:y|ies)|parish|borough|township|twp|district|authority|"
    r"commission|department|board|schools?|university|college|port|"
    r"utilit(?:y|ies)|water|sewer|drainage|levee|transit|housing|airport|"
    r"regional|council|association|consolidated|R-[IVX]+)\b", re.I)


_COUNTY_SHAPED_RE = re.compile(r"\bcount(?:y|ies)\b|\bparish\b|\bborough\b", re.I)


def _looks_like_authority(name):
    """True if a name is an organisation rather than a town.

    This is the dividing line for the search-town fallback in _place_bid. The
    fallback exists because "Greene County" and "Ozark R-VI School District"
    don't geocode and their bids were being thrown away. It must NOT catch an
    ordinary city name that failed to resolve — a plain town that doesn't
    exist in the state we searched is evidence the bid is somewhere else
    entirely, and pinning it here presents a job a thousand miles away as
    local. That is worse than missing it: the contractor drives, or bids, on a
    job that was never theirs.
    """
    return bool(_AUTHORITY_RE.search(str(name or "")))


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


# A bid with no stated deadline cannot be aged by _apply_deadline_status --
# there is nothing to compare against today -- so it sits in the feed forever.
# The nightly audit measured this at half of everything shown. Recording when
# a dateless bid was first seen gives the only clock available.
UNDATED_MAX_DAYS = int(os.environ.get("SCAN_UNDATED_MAX_DAYS", "60"))
# Typical solicitations run two to four weeks, so 60 days is deliberately
# generous: retiring a job that is genuinely still open costs a customer real
# work, while showing a dead one costs them a wasted phone call.
_UNDATED_STORE_MAX = 5000


def _age_out_undated(bid, city, db, stats=None):
    """Retire a dateless bid once it has been in the feed too long."""
    if not _is_open_bid(bid) or _parse_deadline(bid.get("deadline")):
        return
    today = datetime.datetime.now().date()
    # If the posting states when it went up, that is a real age rather than
    # an inferred one -- and it works on the first sighting instead of
    # starting a clock we then have to wait out. 88% of postings carry it.
    posted = _parse_deadline(bid.get("published"))
    if posted:
        if (today - posted).days >= UNDATED_MAX_DAYS:
            bid["status"] = "Closed"
            if stats is not None:
                stats["aged_out_undated"] = stats.get("aged_out_undated", 0) + 1
        return

    store = db.setdefault("undated_first_seen", {})
    sig = _bid_sig(city, bid)
    first = store.get(sig)
    if not first:
        store[sig] = today.isoformat()
        # Unbounded growth would eventually be the whole cache. Evict oldest
        # first; a re-seen bid simply restarts its clock, which errs towards
        # showing work rather than hiding it.
        if len(store) > _UNDATED_STORE_MAX:
            for old in sorted(store, key=store.get)[:len(store) - _UNDATED_STORE_MAX]:
                store.pop(old, None)
        return
    try:
        age = (today - datetime.date.fromisoformat(first)).days
    except (ValueError, TypeError):
        store[sig] = today.isoformat()
        return
    if age >= UNDATED_MAX_DAYS:
        bid["status"] = "Closed"
        if stats is not None:
            stats["aged_out_undated"] = stats.get("aged_out_undated", 0) + 1


def _place_bid(grouped, bid, center, radius, db, default_city="", city_coords=None,
               default_state="", fallback_coords=None, stats=None, pdb=None):
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
    # Whether the bid named its OWN city, or we are about to lend it the town
    # whose scan turned it up. That fallback exists because a town's own bid
    # page is that town's by definition -- a posting there that doesn't
    # restate the city is still local.
    #
    # An aggregator page is the opposite case. PlanetBids, BidNet, DemandStar
    # and the rest host every agency in the country behind one domain, so the
    # search town says nothing about where the work is. A live scan of
    # Rollingwood, CA surfaced a City of DUARTE job -- 358 miles away, on the
    # far side of the state -- and lending it Rollingwood's name and
    # coordinates put it on the board as local, past the radius check, under
    # the wrong town's heading.
    stated_city = str(bid.get("city") or "").strip()
    from_aggregator = bid_portals.is_aggregator_url(bid.get("url") or "")
    if from_aggregator and not stated_city:
        _count("aggregator_no_location")
        return
    # Same failure, on an ordinary municipal CMS rather than a bid platform.
    # A bid found while searching Charlestown, IN and read off
    # cms3.revize.com/revize/fairfield/... is not Charlestown's work. With no
    # city of its own it was lent the search town's name, geocoded cleanly
    # because "Charlestown" is a real place, and reached a live board reading
    # "Charlestown - 16 mi" for a job in a Fairfield hundreds of miles away.
    # Note this is checked BEFORE the coordinate lookup, not in the
    # unresolvable-place fallback further down: that fallback only runs when
    # the borrowed name fails to geocode, and a borrowed name that geocodes
    # perfectly is precisely the dangerous case.
    if not stated_city and bid_portals.url_names_other_place(
            bid.get("url"), default_city, pdb):
        _count("url_names_another_town")
        return
    city, stated_state = _split_city_state(stated_city or default_city or "")
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
    # The model reads city names off page text and sometimes drops a
    # character -- a real scan filed a Missouri bid under "Ashlan". That town
    # does not exist, so it never geocodes, radius search never sees it, and
    # it never groups with the rest of Ashland's work. Correct it against the
    # towns we actually know, but only on an unambiguous single-edit match.
    for st in candidates:
        snapped = bid_portals.snap_city_name(city, st.upper())
        if snapped != city:
            city = snapped
            _count("city_name_corrected")
            break

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
        # ...but only for names that are plausibly unmappable in the first
        # place: an authority, or the very town we searched. A live 125mi scan
        # of Russellville MO returned bids in Binghamton NY, Barrington IL and
        # Aledo IL — ordinary cities that simply don't exist in Missouri, so
        # they failed to geocode, got stamped with a Missouri anchor town's
        # coordinates, and passed the radius check on borrowed location.
        # The town this search was run against is local by definition, and must
        # never be second-guessed below: the .gov registry only covers bodies
        # that own a domain (194 of Missouri's ~950 incorporated places), so a
        # small town's absence from it means nothing at all.
        is_search_town = (city.strip().lower()
                          == str(default_city or "").strip().lower()
                          and bool(str(default_city or "").strip()))
        anchorable = _looks_like_authority(city) or is_search_town
        # An authority we can place elsewhere is not local, whatever page it
        # was found on. A Missouri scan returned "DuPage County" sidewalk work
        # — DuPage is in Illinois, 350 miles away — and it passed the check
        # above because "County" makes a name look unmappable.
        #
        # Only acted on when the registry knows the name in exactly ONE state
        # and it isn't ours. Coverage is far too thin to read absence as
        # evidence: Kansas is missing 40% of its own counties, so "not
        # registered here" would throw away real local work. A name in several
        # states is ambiguous and left alone; a name in none is the road
        # district / drainage board case this fallback exists to catch.
        # Counties only. They are the one tier the registry covers densely
        # (3,137 entries against ~3,143 real counties), so "known in exactly
        # one state" is meaningful for them. For townships and districts it is
        # not — Ohio's Wayne Township owning the only registered domain of that
        # name says nothing about whether Missouri has one.
        if anchorable and not stated_state and not is_search_town \
                and _COUNTY_SHAPED_RE.search(city):
            elsewhere = gov_directory.states_for_org(city)
            if len(elsewhere) == 1 and search_state not in elsewhere:
                _count("authority_in_another_state")
                return
        # Same reasoning as above: an aggregator page's buyer may be anywhere,
        # so a name we could not geocode must not be pinned to the search
        # town's coordinates just because it looks like an authority.
        if fallback_coords and anchorable and not from_aggregator \
                and (not stated_state or stated_state == search_state):
            coords, used_state = fallback_coords, search_state
            _count("placed_by_search_town")
        else:
            _count("unresolvable_place")
            return
    miles = _miles_between(center["lat"], center["lon"], coords[0], coords[1])
    if miles > radius:
        _count("out_of_radius")
        return  # outside the chosen radius
    # Keep it. The radius check has always computed this and thrown it away,
    # which was survivable while the app defaulted to 25 miles and everything
    # on the board was near. At 125 it is the first thing a contractor needs:
    # a job 8 miles out and one 120 miles out are different propositions and
    # the card had no way to tell them apart.
    bid["miles"] = int(round(miles))
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
    # Before it can be shown: if it carries no date, has it been sitting in
    # the feed since before anyone would still want it?
    _age_out_undated(bid, label, db, stats)

    bucket = grouped.setdefault(label, [])
    key = _bid_dupe_key(bid)
    if key[0] and any(_bid_dupe_key(existing) == key for existing in bucket):
        _count("duplicate")
        return
    bucket.append(bid)
    _count("kept")
    # "kept" counts bids that made it into the result; the reported total counts
    # only the OPEN ones. That gap used to be invisible, and a scan that placed
    # seven bids and reported zero looked like a bug in the geography rather
    # than what it was — everything found had been ruled expired. The closed
    # count is taken at the end of the scan, once enrichment has had its say.
    if city_coords is not None:
        city_coords[label] = {"lat": coords[0], "lon": coords[1]}


# ═══════════════════════════════════════════════════════════
# State DOT lettings
#
# Every source above is a city or county: the page belongs to one place, so
# the place is known before a single row is read. A state letting page is the
# opposite -- one table carrying work from every corner of the state, where
# the only location is a county named inside the row. That is why these get
# their own reader and their own placement, and why counties.py exists.
#
# The yield is worth the separate path. A random live sample of 90 CivicPlus
# city portals produced 8 concrete-relevant bids between them; Florida's
# letting page alone produces 75, and a 50-mile scan from Tampa picks up 23 of
# them. Missouri adds 6 statewide, 4 of them inside 125 miles of Springfield.
#
# Only two states are wired today, and that is a supply fact rather than a
# missing feature -- see SEARCH_PLAN.md Phase 6 for what the other 48 are
# blocked on, and for the four false positives that make the strictness here
# non-negotiable.
# ═══════════════════════════════════════════════════════════

STATE_SOURCES_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "data", "state_bid_sources.csv")
STATE_SOURCE_MIN_USABLE = int(os.environ.get("STATE_SOURCE_MIN_USABLE", "2"))
STATE_SOURCE_TIMEOUT = float(os.environ.get("STATE_SOURCE_TIMEOUT", "20"))
_state_sources_cache = {"at": 0.0, "rows": None}


def _resolve_state_listing(url, kind, page):
    """(listing_url, listing_html) for a source, following an index if needed.

    A "listing" source is read directly. An "index" source is a page of dated
    letting links, and the listing is whichever is current -- Alabama's lives
    at .../NTC_August_28_2026.html and the address changes every letting, so
    storing the dated URL means the source dies silently when it rotates.
    """
    if kind != "index":
        return url, page
    link = bid_sources.newest_letting_link(
        page, url, today=datetime.date.today().timetuple()[:3])
    if not link:
        return url, page
    body, outcome = _fetch_page(link, timeout=STATE_SOURCE_TIMEOUT)
    if outcome != "ok" or not body:
        return url, page
    return link, body


def _state_sources():
    """{state: (url, kind)} for states VERIFIED to yield usable rows.

    Gated on the measured `usable` column, not on whether a URL was found. The
    discovery crawl reported convincing listings in 22 states; running the real
    parser over them, 2 produce placeable concrete-relevant rows. Shipping the
    other 20 would mean fetching South Dakota's fuel price index on every scan
    of the region and showing a contractor nothing for it.
    """
    now = time.time()
    if _state_sources_cache["rows"] is not None and \
            now - _state_sources_cache["at"] < 600:
        return _state_sources_cache["rows"]
    out = {}
    try:
        with open(STATE_SOURCES_CSV, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    usable = int(row.get("usable") or 0)
                except (TypeError, ValueError):
                    usable = 0
                url = (row.get("url") or "").strip()
                st = (row.get("state") or "").strip().upper()
                if st and url and usable >= STATE_SOURCE_MIN_USABLE:
                    out[st] = (url, (row.get("kind") or "listing").strip())
    except OSError:
        out = {}
    _state_sources_cache.update({"at": now, "rows": out})
    return out


def _place_state_bid(grouped, row, center, radius, city_coords=None, stats=None):
    """Place a state letting row, which already knows exactly where it is.

    _place_bid resolves a city name against a gazetteer because that is all a
    municipal posting gives you. A state row has been matched to a county
    centroid already, so running it through name resolution could only lose
    it -- "Cole" is not a city.

    The bucket is labelled "<County> County, ST" rather than a town, because
    that is the truth about the work: a resurfacing job spanning eleven miles
    of Route 163 is not in any one town, and inventing one would put it on the
    map in the wrong spot.
    """
    def _count(reason):
        if stats is not None:
            stats[reason] = stats.get(reason, 0) + 1

    # A row can name several counties, because a state "call" bundles several
    # jobs. Place it at the one NEAREST the contractor: the work really is in
    # all of them, so the nearest is both true and the only useful answer to
    # "how far is this". Picking any other way is arbitrary -- an earlier
    # version took the most populous and labelled a Henry County job as Polk.
    places = row.get("places") or []
    if not places and row.get("lat") is not None:
        places = [(row.get("county") or "", row["lat"], row["lon"])]
    if not places:
        _count("state_row_unplaceable")
        return
    county, lat, lon = min(
        places,
        key=lambda p: _miles_between(center["lat"], center["lon"], p[1], p[2]))
    miles = _miles_between(center["lat"], center["lon"], lat, lon)
    if miles > radius:
        _count("out_of_radius")
        return
    # This is an allowlist, so anything not named here is silently dropped --
    # which is exactly how "call" got lost once already, leaving every state
    # bid at the plan-holder fetcher with nothing to look up. "documents"
    # carries the job's own Bid Book and Plans links; without it the card
    # would send the contractor to the letting index instead.
    bid = {k: row[k] for k in
           ("title", "scope", "url", "deadline", "status", "source", "call",
            "documents")
           if k in row}
    bid["miles"] = int(round(miles))
    bid["county"] = county
    others = [c for c in (row.get("all_counties") or []) if c != county]
    if others:
        bid["also_in"] = others
    _apply_deadline_status(bid)
    # A letting that has already happened cannot be bid, and unlike a city
    # posting somebody may have saved, a state row is re-read from scratch on
    # every scan -- so a closed one has no purpose at all. Florida publishes
    # every letting from January onward on one page, which put 43 dead jobs
    # on a Tampa board against 8 live ones.
    if not _is_open_bid(bid):
        _count("state_letting_already_held")
        return None
    label = "%s County, %s" % (str(county).title(),
                               row.get("state") or center["state"])
    bucket = grouped.setdefault(label, [])
    key = _bid_dupe_key(bid)
    if key[0] and any(_bid_dupe_key(x) == key for x in bucket):
        _count("duplicate")
        return
    bucket.append(bid)
    _count("kept")
    _count("state_dot_kept")
    if city_coords is not None:
        city_coords[label] = {"lat": lat, "lon": lon}
    return bid


PLAN_HOLDER_MAX = int(os.environ.get("SCAN_PLAN_HOLDER_MAX", "12"))
PLAN_HOLDER_WORKERS = int(os.environ.get("SCAN_PLAN_HOLDER_WORKERS", "4"))


def _attach_plan_holders(bids, letting_html, letting_url, stats=None):
    """Name the contractors bidding each state job, so a sub knows who to call.

    This is the answer to "why show me a highway contract I cannot win as
    prime". The prime bidders on that job need somebody to price the ramps and
    the sidewalk, and the letting publishes exactly who they are. Two of the
    eight holders on one MoDOT call were themselves concrete companies, which
    is the clearest evidence that subs already work this list.

    Only runs for bids that survived the radius, and only up to
    PLAN_HOLDER_MAX of them: it is one extra fetch per job, so it is spent on
    work the contractor can actually reach. Failure is per-bid and silent.

    NOT exported. These are named individuals' business contacts on a
    government page, shown in the context of the job they are bidding. The
    CSV export deliberately omits them -- see exportCSV in app.html.
    """
    index_url = bid_sources.plan_holder_index(letting_html, letting_url)
    if not index_url:
        return 0
    targets = [b for b in bids if b.get("call")][:PLAN_HOLDER_MAX]
    if not targets:
        return 0

    def _one(bid):
        url = bid_sources.plan_holder_url_for_call(index_url, bid["call"])
        if not url:
            return 0
        page, outcome = _fetch_page(url, timeout=PROBE_TIMEOUT)
        if outcome != "ok" or not page:
            return 0
        holders = bid_sources.parse_plan_holders(page)
        if holders:
            bid["plan_holders"] = holders
            bid["plan_holder_url"] = url
        return len(holders)

    with ThreadPoolExecutor(max_workers=PLAN_HOLDER_WORKERS) as ex:
        found = sum(ex.map(_one, targets))
    if stats is not None and found:
        stats["plan_holders_found"] = stats.get("plan_holders_found", 0) + found
        stats["plan_holder_jobs"] = stats.get("plan_holder_jobs", 0) + sum(
            1 for b in targets if b.get("plan_holders"))
    return found


def _run_state_sources(center, radius, grouped, city_coords=None, stats=None):
    """Read the letting page of every verified state the radius touches.

    Cheap by construction: one fetch per state, and a 125-mile scan touches at
    most four. Failure is per-state and silent -- a state page that is down
    costs its own rows, never the scan.
    """
    sources = _state_sources()
    if not sources:
        return 0
    try:
        states = counties.states_within(center["lat"], center["lon"], radius)
    except Exception:
        states = [center.get("state", "").upper()]
    todo = [(st,) + sources[st] for st in states if st in sources]
    if not todo:
        return 0

    placed = 0
    for st, url, kind in todo:
        try:
            page, outcome = _fetch_page(url, timeout=STATE_SOURCE_TIMEOUT)
            if outcome != "ok" or not page:
                if stats is not None:
                    k = "state_fetch_%s" % outcome
                    stats[k] = stats.get(k, 0) + 1
                continue
            url, page = _resolve_state_listing(url, kind, page)
            rows = bid_sources.parse_state_letting(
                page, st, url, counties.counties_named)
            if stats is not None:
                stats["state_rows_read"] = \
                    stats.get("state_rows_read", 0) + len(rows)
            landed = []
            for row in rows:
                before = sum(len(v) for v in grouped.values())
                kept = _place_state_bid(grouped, row, center, radius,
                                        city_coords, stats)
                if sum(len(v) for v in grouped.values()) > before:
                    placed += 1
                    if kept is not None:
                        landed.append(kept)
            # Only for jobs that made the board -- see _attach_plan_holders.
            if landed:
                _attach_plan_holders(landed, page, url, stats)
        except Exception as ex:      # never let a state page break a scan
            print("[scan] state source %s failed: %s" % (st, ex), flush=True)
    print("[scan] %d bids from %d state letting page(s)"
          % (placed, len(todo)), flush=True)
    return placed


# One scan's ceiling on detail reads on the keyless transport. Its search
# response carries no location and no contact, so a detail fetch is the only
# way to learn either -- one request per candidate. Measured over MO/KS/AR the
# real number was twelve, so this is headroom, not a constraint that bites.
# The keyed transport needs none of this: its search payload is already full.
FEDERAL_DETAIL_MAX = int(os.environ.get("SCAN_FEDERAL_MAX", "30"))
FEDERAL_WORKERS = int(os.environ.get("SCAN_FEDERAL_WORKERS", "6"))
FEDERAL_TIMEOUT = int(os.environ.get("SCAN_FEDERAL_TIMEOUT", "30"))


def _federal_states(center, radius):
    """Every state the radius touches, not just the one under the pin.

    The old federal block asked SAM for center["state"] alone. That is the
    same bug _place_bid documents for cities: a 125-mile circle is usually
    several states wide, and the whole reason somebody picks that radius is
    to see across the line.
    """
    try:
        states = counties.states_within(center["lat"], center["lon"], radius)
    except Exception:
        states = [str(center.get("state") or "").upper()]
    return [s for s in states if s]


def _federal_keyed(states, stats):
    """Candidates from the documented, keyed API.

    Its payload is complete -- place of performance and point of contact
    arrive with the search row -- so this needs no second request per notice.
    """
    out, failed = [], 0
    for st in states:
        try:
            opps = _sam_fetch(st)
        except Exception:
            opps = None
        if opps is None:
            failed += 1
            _bump(stats, "federal_search_failed")
            continue
        if not opps:
            _bump(stats, "federal_search_empty")
            continue
        _bump(stats, "federal_search_ok")
        for opp in opps:
            if not _is_construction(opp):
                continue
            bid, city, perf_state = _normalize_opp(opp)
            bid["city"], bid["state"] = city, perf_state
            out.append(bid)
    # A rejected key must not mean no federal bids at all. The public
    # transport reads the same public-domain data and needs no credentials,
    # so fall back to it rather than going quiet -- which is what happened
    # here: the keyed path returned nothing, bumped nothing, and the public
    # one could see eight active Texas notices the whole time.
    if failed and failed == len(states):
        _bump(stats, "federal_fell_back_to_public")
        return _federal_public(states, stats)
    return out


def _federal_public(states, stats):
    """Candidates from sam.gov's own unauthenticated search.

    Exists so federal bids work before anybody has registered a key. Same
    public-domain data, one extra fetch per notice because this transport's
    search index omits location and contact.
    """
    seen, candidates = set(), []
    # NAICS queries are trusted outright; PSC queries are not, because the
    # code is broader than the trade. See federal_bids.CONCRETE_PSC.
    queries = ([({"naics": n}, True) for n in federal_bids.CONCRETE_NAICS] +
               [({"psc": c}, False) for c in federal_bids.CONCRETE_PSC])
    for st in states:
        for params, trusted in queries:
            try:
                page, outcome = _fetch_page(
                    federal_bids.search_url(state=st, **params),
                    timeout=FEDERAL_TIMEOUT)
            except Exception:
                _bump(stats, "federal_search_error")
                continue
            if outcome != "ok" or not page:
                _bump(stats, "federal_search_%s" % outcome)
                continue
            try:
                rows = federal_bids.parse_search(page)
            except Exception:
                _bump(stats, "federal_search_unparsed")
                continue
            for row in rows:
                if not row.get("active") or not row.get("id"):
                    continue
                if not trusted and not bid_sources.looks_relevant(
                        row.get("title")):
                    _bump(stats, "federal_psc_off_trade")
                    continue
                # Collapse amendments BEFORE spending a detail fetch. A
                # solicitation reappears with every amendment and each is the
                # same job -- Fort Leavenworth's asphalt contract showed up
                # three times in one probe. The notice id changes; the
                # solicitation number does not, so key on that.
                key = (row.get("solicitation") or row["id"]).lower()
                if key in seen:
                    _bump(stats, "federal_amendment_collapsed")
                    continue
                seen.add(key)
                candidates.append(row)
    if len(candidates) > FEDERAL_DETAIL_MAX:
        _bump(stats, "federal_over_budget")
        candidates = candidates[:FEDERAL_DETAIL_MAX]

    def _detail(row):
        try:
            raw, outcome = _fetch_page(federal_bids.detail_url(row["id"]),
                                       timeout=FEDERAL_TIMEOUT)
        except Exception:
            return None
        if outcome != "ok" or not raw:
            return None
        try:
            return federal_bids.to_bid(row, federal_bids.parse_detail(raw))
        except Exception:
            return None

    if not candidates:
        return []
    with ThreadPoolExecutor(max_workers=FEDERAL_WORKERS) as ex:
        return [b for b in ex.map(_detail, candidates) if b]


def _bump(stats, reason):
    if stats is not None:
        stats[reason] = stats.get(reason, 0) + 1


def _run_federal_sources(center, radius, grouped, cdb, city_coords=None,
                         stats=None, pdb=None):
    """Add federal solicitations whose work falls inside the radius.

    Federal work was already wired up but could never have produced a bid:
    the default endpoint 404s, the relevance test was title-keywords that
    real federal titles do not use, and only the centre state was asked.
    Roughly 1,200 notices in the concrete NAICS codes are open nationwide at
    any moment and the app saw none of them.

    Worth more than its count suggests, because of what arrives with each
    one. Of nine placeable jobs in a MO/KS/AR probe, nine had a named
    contracting officer with a phone or an email, and all nine were
    small-business set-asides. Contacts are the app's second-biggest quality
    gap everywhere else; here they are simply part of the record, and no
    extraction call is spent to get them.
    """
    states = _federal_states(center, radius)
    if not states:
        return 0
    bids = (_federal_keyed(states, stats) if SAM_API_KEY
            else _federal_public(states, stats))
    placed = 0
    for bid in bids:
        if not bid:
            _bump(stats, "federal_unplaceable")
            continue
        _apply_deadline_status(bid)
        # Same rule the state reader applies. "Active" at SAM means the
        # notice is live, not that its response date is still ahead: three
        # of twelve probe hits were already past theirs.
        if not _is_open_bid(bid):
            _bump(stats, "federal_already_closed")
            continue
        before = sum(len(v) for v in grouped.values())
        _place_bid(grouped, bid, center, radius, cdb,
                   default_city=bid.get("city", ""),
                   city_coords=city_coords,
                   default_state=bid.get("state", ""),
                   stats=stats, pdb=pdb)
        if sum(len(v) for v in grouped.values()) > before:
            placed += 1
            _bump(stats, "federal_kept")
    print("[scan] %d federal bids from %d candidate(s) in %s (%s transport)"
          % (placed, len(bids), ",".join(states),
             "keyed" if SAM_API_KEY else "public"), flush=True)
    return placed


ENRICH_MAX = int(os.environ.get("SCAN_ENRICH_MAX", "14"))

SCAN_HISTORY_KEY = "bidcaller:scan_history"
SCAN_HISTORY_MAX = int(os.environ.get("SCAN_HISTORY_MAX", "25"))


def _append_scan_history(record):
    """Keep a rolling log of recent scans.

    Only the single most recent scan was ever stored, overwritten every time,
    so there was nothing to compare against — "is search getting better?" could
    not be answered from the data, only from whoever happened to be watching.
    A short history makes a change in recall visible as a trend.

    Deliberately compact: no sample, no per-bid detail. The point is to see
    twenty scans at once, and the newest one is in last_scan in full.
    """
    slim = {k: record.get(k) for k in
            ("at", "location", "radius", "kept", "raw_local", "anchor_towns")}
    slim["funnel"] = record.get("funnel") or {}
    try:
        history = kv_backend.get(SCAN_HISTORY_KEY, None) or []
        if not isinstance(history, list):
            history = []
    except Exception:
        history = []
    history.append(slim)
    # Read-modify-write is not atomic here. Scans are infrequent enough that
    # the worst case is one lost row in a log, which is not worth a lock.
    kv_backend.set(SCAN_HISTORY_KEY, history[-SCAN_HISTORY_MAX:])


def _count_kept_closed(grouped, stats):
    """Record how many kept bids are not open, so the funnel agrees with the
    reported total. Taken at the end of a scan rather than during placement,
    because enrichment can turn up a deadline that closes a bid after the fact.
    """
    if stats is None:
        return
    stats.pop("kept_but_closed", None)
    closed = sum(1 for v in (grouped or {}).values()
                 for b in v if not _is_open_bid(b))
    if closed:
        stats["kept_but_closed"] = closed


def _flag_misplaced_bids(grouped, pdb, stats=None):
    """Count bids whose own URL names a town other than the one they sit under.

    A self-check, not a filter. The guard in _place_bid stops the known way a
    bid gets lent the wrong town, and this counts anything that still slips
    through by some other route -- so the next instance of this class shows up
    in /diag instead of waiting for a customer to notice a card claiming a job
    is 16 miles away when it is in another state.

    Deliberately does not drop anything. A counter that is wrong costs a line
    in a funnel; a filter that is wrong costs real work.
    """
    if stats is None or not pdb:
        return 0
    bad = 0
    for label, bids in (grouped or {}).items():
        town = str(label or "").split(",")[0].strip()
        # County buckets come from state lettings and are placed by centroid,
        # not by a town name, so the URL has nothing to agree with.
        if not town or town.lower().endswith(" county"):
            continue
        for bid in bids:
            try:
                if bid_portals.url_names_other_place(bid.get("url"), town, pdb):
                    bad += 1
            except Exception:
                pass
    if bad:
        stats["placed_url_town_mismatch"] = bad
    return bad


def _enrich_placed_bids(grouped, stats=None):
    """Fill in contacts and deadlines for bids that survived the radius filter.

    Runs against every source, not just the structured reader, and only on
    bids that actually made it into the result — so the cost is a dozen fetches
    at most, rather than one per thing the search turned up.
    """
    # A name is not a way to reach anybody. The AI extractor returns a
    # "contact" field and routinely fills it with something like "Purchasing
    # Department" while leaving email and phone blank -- and treating that as
    # already-enriched meant the posting behind it was never read for the
    # actual phone number. On a live Springfield 75mi scan that left 24 kept
    # bids with exactly ONE eligible for enrichment and contacts_found: 0.
    # Only a phone or an email makes a bid callable, so only those count as
    # done; a name already present is preserved either way, since
    # _enrich_from_detail_pages only fills fields that are empty.
    # A state letting row's url is the LETTING PAGE, shared by every row on it,
    # not that job's own posting. Enriching them would fetch one page up to
    # fourteen times to learn nothing -- the listing carries no per-job contact
    # -- and would spend the whole budget doing it. Same reasoning the AI path
    # already applies to bids still pointing at the listing they came from.
    todo = [b for bids in (grouped or {}).values() for b in bids
            if isinstance(b, dict) and b.get("url")
            and b.get("source") != "state_dot"
            and not (b.get("email") or b.get("phone"))]
    # Undated bids first (a missing deadline is worse than a missing phone
    # number, because it also stops an expired listing being recognised), then
    # open ones, then bids already known to be closed -- see _enrichment_order.
    todo = _enrichment_order(todo)
    _note_enrich_budget(todo, ENRICH_MAX, stats)
    _enrich_from_detail_pages(todo[:ENRICH_MAX], stats=stats)
    for bid in todo[:ENRICH_MAX]:
        _apply_deadline_status(bid)
    for bids in (grouped or {}).values():
        for bid in bids:
            _apply_stale_year(bid)


_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
# A project code is not a date. Alabama numbers jobs "ATRP2-52-2024-263" and
# the 2024 in the middle is a sequence, not the year the work is from -- read
# as a year it closed a live August 2026 job on sight. Letter-led hyphenated
# codes are removed before the year scan; purely numeric ones like "2024-17"
# are left alone, because after "Bid No." that usually IS the year it was
# issued and closing it is right.
_PROJECT_CODE_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+\b")

# Where a posting is FILED is often the only statement of how old it is. A
# live board showed "Concrete Sidewalks and ADA Ramps Project" as open with no
# closing date; neither its title nor its scope named a year, and the only
# evidence it was from July 2025 sat in the URL:
#   .../fairfield/Purchasing/2025/2025-07 ITB Southport Community...
#
# Only DATE-SHAPED PATH SEGMENTS count -- a segment that is a year, or that
# begins with year-month. Not the query string and not stray digits: a
# CivicPlus posting is addressed "Bids.aspx?bidID=2024", where 2024 is a row
# id, and reading that as a year would close a brand new bid.
_PATH_YEAR_RE = re.compile(r"^(20\d{2})(?:[-_/]\d{1,2})?(?:\b|_|$)")


def _url_path_years(url):
    """Years stated by a URL's path segments, e.g. ".../2025/2025-07 ITB..."."""
    try:
        path = urllib.parse.urlsplit(str(url or "")).path
        path = urllib.parse.unquote(path)
    except ValueError:
        return []
    out = []
    for segment in path.split("/"):
        m = _PATH_YEAR_RE.match(segment.strip())
        if m:
            out.append(int(m.group(1)))
    return out


def _apply_stale_year(bid):
    """Close an undated bid whose own title is from a past year.

    A live scan returned four rows titled "2025 Sidewalk Program - Scope of
    Work SW-1..4" as open, in August 2026. They carried no deadline, and with
    nothing to check against, an undated bid is assumed open — which is right
    for a genuinely new posting and wrong for last year's programme still
    sitting on a portal. Only fires when the deadline is empty AND every year
    named is in the past, so "2025-2026 Programme" is left alone.
    """
    if not isinstance(bid, dict) or str(bid.get("deadline") or "").strip():
        return bid
    if not _is_open_bid(bid):
        return bid
    blob = _PROJECT_CODE_RE.sub(
        " ", f"{bid.get('title') or ''} {bid.get('scope') or ''}")
    years = [int(y) for y in _YEAR_RE.findall(blob)]
    years += _url_path_years(bid.get("url"))
    if years and max(years) < datetime.datetime.now().year:
        bid["status"] = "Closed"
    return bid


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
    # Reading a town's own bid page costs nothing and needs no AI: the
    # CivicPlus parser is plain regex. This whole block used to sit inside
    # "if OPENAI_API_KEY", so an expired or exhausted OpenAI balance took
    # away every local bid, including all the free ones -- a paid dependency
    # switching off a free code path. Only the SEARCH queries are gated now,
    # because a search result is useless without an extractor to read it.
    SEARCH_ENABLED = bool(OPENAI_API_KEY)
    if True:
        c, s = center["city"], center["state"]
        seen_urls = set()
        lock = threading.Lock()
        # Queries that check something a direct read of the city's own bid
        # page structurally cannot: a different entity entirely (school
        # district, county, state portal) or a platform aggregator. Always
        # worth running, known-portal hit or not.
        center_queries_always = [
            f"{c} {s} school district sidewalk ADA concrete project bid",
            f"{c} {s} sidewalk ADA curb bid {_agg_sites(0)}",
            f"{c} {s} sidewalk ADA curb bid {_agg_sites(1, center['state'])}",
            f"{c} {s} county road department concrete curb bid notice",
            f"{c} {s} Safe Routes to School OR ADA transition plan sidewalk bid",
            f"{c} {s} CDBG sidewalk curb ramp bid notice to contractors",
        ]
        # Generic re-phrasings of "does this city have a sidewalk bid" --
        # redundant once _run_known_portals already read the city's own bid
        # page directly and found something real there, since a working
        # direct source is the authoritative answer to that exact question.
        # Only worth the Tavily-credit cost when there's no working direct
        # source to trust instead.
        # Six near-identical rephrasings returned heavily overlapping results;
        # three distinct angles (the work, the process, the department) cover
        # the same ground for half the queries.
        center_queries_generic = [
            f"{c} {s} sidewalk replacement ADA ramp curb gutter concrete bid",
            f"{c} {s} invitation to bid concrete sidewalk solicitation",
            f"{c} {s} public works concrete flatwork RFP bid opportunities",
        ]

        anchors = _nearby_anchor_towns(center, radius, pdb)

        # _nearby_anchor_towns samples at most a handful of geographically-
        # guessed points regardless of how large the radius is -- a 125mi
        # scan covers ~49,000 sq mi, and 6 sample points leaves most of that
        # area unsearched. Separately from that guessing, we already have a
        # real, verified bid page for 3,151 towns nationally (the offline
        # crawl) -- this asks a cheaper, more direct question: of the towns
        # we ALREADY trust, which ones are actually inside this radius. No
        # search credits involved, just reading pages we already know about.
        # Sorted closest-first and capped so a scan centered in a
        # well-covered metro doesn't try to fetch every known town in the
        # state at once.
        known_exclude = {(c.lower(), s)} | {(a[0].lower(), a[1]) for a in anchors}
        known_towns = bid_portals.towns_within_radius(
            pdb, center["lat"], center["lon"], radius, exclude=known_exclude)
        known_towns.sort(key=lambda t: _miles_between(center["lat"], center["lon"], t[2], t[3]))
        known_towns = known_towns[:MAX_KNOWN_TOWNS]

        # Each "town job" (center + every anchor + every known-portal town)
        # is fully independent work, so they run concurrently instead of one
        # after another — this is the biggest lever on wall-clock scan time.
        # Search-driven jobs (center/anchor) stay capped at 4 workers so we
        # don't fire too many simultaneous search-engine requests at once
        # (DuckDuckGo in particular will start blocking if hammered); the
        # known-town jobs below are just a direct page fetch each (no search
        # queries), so they get their own, more generous pool.
        center_coords = (center["lat"], center["lon"])

        def _run_center():
            # default_city=c, not "": a known portal IS this city's own bid
            # page, so a bid on it that doesn't restate the city is still that
            # city's. Defaulting to blank made _place_bid drop those outright,
            # losing bids from the single most reliable source in the pipeline.
            got = _run_known_portals(c, s, f"{c}, {s}", grouped, center, radius,
                                      cdb, city_coords, lock, pdb, default_city=c,
                                      town_coords=center_coords, stats=drop_stats)
            if not SEARCH_ENABLED:
                print(f"[scan] {got} raw bids from {c}, {s} (center, "
                      f"portal only -- no OPENAI_API_KEY)", flush=True)
                return got
            # A hit here (got > 0) means the city's own bid page was read
            # directly and had something real on it -- the generic queries
            # would only be re-asking a question that page already answered.
            queries = (center_queries_always if got > 0
                      else center_queries_always + center_queries_generic)
            got += _run_local_queries(queries, f"{c}, {s}", MAX_PAGES,
                                      grouped, center, radius, cdb, city_coords,
                                      seen_urls, lock, pdb, default_city="", state=s,
                                      town_coords=center_coords, stats=drop_stats)
            print(f"[scan] {got} raw bids from {c}, {s} (center)", flush=True)
            return got

        def _run_anchor(anchor):
            ac, ast, alat, alon = anchor
            # Anchors get one query, not five. They used to be the main way a
            # scan saw past the centre town, but the portal directory has
            # since grown from ~750 agencies to 4,400+, so _run_known_portals
            # above now reads most anchor towns' bid pages directly. What it
            # structurally cannot see is an aggregator listing, so that is the
            # one thing left worth searching for here.
            anchor_queries_always = [
                f"{ac} {ast} sidewalk ADA curb bid {_agg_sites(0)}",
            ]
            anchor_queries_generic = [
                f"{ac} {ast} sidewalk ADA curb concrete bid invitation",
            ]
            got = _run_known_portals(ac, ast, f"{ac}, {ast}", grouped, center, radius,
                                      cdb, city_coords, lock, pdb, default_city=ac,
                                      town_coords=(alat, alon), stats=drop_stats)
            if not SEARCH_ENABLED:
                print(f"[scan] {got} raw bids from {ac}, {ast} (anchor, "
                      f"portal only -- no OPENAI_API_KEY)", flush=True)
                return got
            queries = (anchor_queries_always if got > 0
                      else anchor_queries_always + anchor_queries_generic)
            got += _run_local_queries(queries, f"{ac}, {ast}", 5,
                                      grouped, center, radius, cdb, city_coords,
                                      seen_urls, lock, pdb, default_city=ac, state=ast,
                                      town_coords=(alat, alon), stats=drop_stats)
            print(f"[scan] {got} raw bids from {ac}, {ast} (anchor)", flush=True)
            return got

        def _run_known_town(town):
            # No search queries here at all -- this town wasn't picked by
            # guessing, it's one we already have a verified real bid page
            # for. Reading it directly is the entire job.
            kc, kst, klat, klon = town
            got = _run_known_portals(kc, kst, f"{kc}, {kst}", grouped, center, radius,
                                      cdb, city_coords, lock, pdb, default_city=kc,
                                      town_coords=(klat, klon), stats=drop_stats)
            print(f"[scan] {got} raw bids from {kc}, {kst} (known portal)", flush=True)
            return got

        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = [ex.submit(_run_center)] + [ex.submit(_run_anchor, a) for a in anchors]
            for f in as_completed(futures):
                local_raw += f.result()

        # Separate pool from the search-driven jobs above: these are a direct
        # fetch each against a different domain, not a search-engine query,
        # so there's no shared backend to overwhelm the way DuckDuckGo/Tavily
        # would be by running many at once.
        if known_towns:
            deadline = time.time() + KNOWN_TOWN_BUDGET_SEC
            with ThreadPoolExecutor(max_workers=KNOWN_TOWN_WORKERS) as ex:
                futures = [ex.submit(_run_known_town, t) for t in known_towns]
                try:
                    for f in as_completed(futures,
                                          timeout=max(1.0, deadline - time.time())):
                        local_raw += f.result()
                except FuturesTimeout:
                    # Out of time. Cancel whatever has not started; the few
                    # already in flight finish under their own request
                    # timeouts. Their results are still collected below.
                    skipped = sum(1 for f in futures if f.cancel())
                    for f in futures:
                        if f.done() and not f.cancelled():
                            try:
                                local_raw += f.result()
                            except Exception:
                                pass
                    if skipped:
                        drop_stats["known_towns_out_of_time"] = skipped
                        print(f"[scan] known-town budget spent, {skipped} town(s) "
                              f"not read", flush=True)

        print(f"[scan] {local_raw} raw local bids extracted total "
              f"({len(anchors)} anchor town(s), {len(known_towns)} known-portal "
              f"town(s) searched)", flush=True)

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

    # ---- STATE: DOT letting pages for every state the radius touches ----
    # Placed before SAM.gov and before enrichment so state rows go through the
    # same deadline, dedupe and enrichment passes as everything else. One
    # fetch per state, at most four states in a 125-mile circle.
    _run_state_sources(center, radius, grouped, city_coords, drop_stats)

    # ---- FEDERAL: SAM.gov across every state the radius touches ----
    # No longer gated on SAM_API_KEY: with a key it uses the documented API,
    # without one it reads sam.gov's own public search. Same public-domain
    # data either way, so the feature is not dark until somebody registers.
    _run_federal_sources(center, radius, grouped, cdb, city_coords,
                         drop_stats, pdb)

    # Read the posting behind every bid that still has no contact and no
    # deadline. Enrichment used to happen only inside the structured CivicPlus
    # reader, so a bid that arrived via search — which is most of them — kept
    # its blank fields: a live Springfield scan placed eleven bids and reported
    # no contacts_found at all, because that branch never ran. Doing it here
    # instead covers every source, and it runs on the handful of bids that
    # survived the radius rather than everything the search turned up.
    # Agencies that posted a notice directly (the lister side). Already
    # geocoded at approval, so this adds no fetch and no search credit --
    # and it's the only way work from towns with no bid page at all reaches
    # anybody. Added before enrichment so a notice missing a deadline still
    # gets the same treatment as any other bid.
    _add_agency_bids(grouped, center, radius, cdb, city_coords, drop_stats)

    _enrich_placed_bids(grouped, drop_stats)
    _flag_misplaced_bids(grouped, pdb, drop_stats)

    for city_bids in grouped.values():
        city_bids.sort(key=_score_bid, reverse=True)
    _count_kept_closed(grouped, drop_stats)

    # Open bids only. Closed ones are still returned (ranked last) but counting
    # them made the reported total disagree with what the app shows: a scan
    # that turned up nothing but expired listings announced "12 bids" and then
    # dropped the user on an empty feed.
    total = sum(1 for v in grouped.values() for b in v if _is_open_bid(b))
    funnel = ", ".join(f"{k}={v}" for k, v in sorted(drop_stats.items())) or "none"
    print(f"[scan] {int(radius)} mi from {center['city']},{center['state']} "
          f"-> {total} bids kept (local_raw={local_raw}; {funnel})", flush=True)

    # Kept so the last scan can be inspected from /health. Diagnosing recall
    # otherwise means asking someone to run a scan and relay numbers back,
    # which is slow and lossy; a URL anyone can open is not.
    record = {
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "location": f"{center['city']}, {center['state']}",
        "radius": int(radius),
        "kept": total,
        "raw_local": local_raw,
        "anchor_towns": len(anchors) if OPENAI_API_KEY else 0,
        "funnel": drop_stats,
        # Counts alone can't distinguish "found nothing" from "found real
        # work and threw it away", which is exactly the question that
        # matters. These two make the last scan legible without asking
        # anyone to re-run it and read numbers back.
        "statuses": _status_breakdown(grouped),
        "sample": _scan_sample(grouped),
    }
    try:
        kv_backend.set("bidcaller:last_scan", record)
        _append_scan_history(record)
    except Exception:
        pass

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


def _perform_upcoming(location, radius, force=False):
    """Core of /upcoming: resolve a location, search for planned/budgeted
    concrete work near it, extract and cache. Extracted out of the /upcoming
    route -- exactly as _perform_scan was -- so the weekly alert job can run
    the same pipeline instead of a second copy of it that drifts.

    Returns a dict of response fields, or None if the location can't be
    resolved. Callers are responsible for checking OPENAI_API_KEY first;
    without it this finds nothing, since every page here needs extraction."""
    center = _resolve_center(location)
    if not center:
        return None

    cdb = _cache()
    today = datetime.datetime.now().strftime("%Y%m%d")
    cache = cdb.setdefault("upcoming_cache", {})
    ckey = f"{center['state']}|{center['city'].lower()}|{int(radius)}|{today}"
    if ckey in cache and not force:
        c = cache[ckey]
        return {"location": f"{center['city']}, {center['state']}",
                "items": c["items"], "total": c["total"],
                "city_coords": c.get("city_coords", {}), "cached": True}

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
        results, used_ddg = _web_search(q, max_results=6)
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
                    b["url"] = _resolve_bid_url(b.get("url"), it["url"], text)
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

    return {"location": f"{center['city']}, {center['state']}",
            "items": grouped, "total": total, "city_coords": city_coords,
            "debug": {"pages": len(pages), "kept": total, "funnel": drop_stats}}


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
    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "reason": "ai_unavailable"})

    outcome = _perform_upcoming(location, radius)
    if outcome is None:
        return jsonify({"ok": False, "reason": "location_not_found"})
    return jsonify(dict(outcome, ok=True))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
