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
import socket
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
# NOTE: this file now lives one directory deeper than the original
# license_server.py did (repo_root/license_server.py -> repo_root/
# license_server/<this file>.py), and its __file__ reflects that. The
# extra os.path.dirname(...) below is only to keep this path resolving
# to the exact same repo_root-relative location as before the split --
# not a behavior change.
_LOCAL_LIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "license_db.json")
_LOCAL_CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scan_cache.json")


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

