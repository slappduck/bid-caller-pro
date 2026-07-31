"""
subscription.py — Online license client for Bid Caller Pro
═══════════════════════════════════════════════════════════

This talks to YOUR license_server. The secret is no longer here — it lives
only on the server — so customers can't mint their own keys by reading this
file. Keys and trials are validated online and cached briefly so the app
still works during short internet/server outages (offline grace period).

SETUP:
  1. Deploy license_server.py and put its public URL in SERVER_URL below.
  2. Put your real Stripe links in the STRIPE_*_URL values.
  3. Generate keys for customers by POSTing to /issue on your server, e.g.:
       curl -X POST https://your-server/issue \
         -H "Content-Type: application/json" \
         -d '{"admin_token":"YOUR_ADMIN_TOKEN","plan":"monthly"}'
"""

import os
import sys
import json
import uuid
import datetime

try:
    import requests
except ImportError:
    requests = None

# ── YOUR SERVER + STRIPE ──────────────────────────────────
SERVER_URL = "https://bid-caller-pro.onrender.com"   # live license server
STRIPE_MONTHLY_URL = "https://buy.stripe.com/5kQcN43YEbge0YafanejK01"  # $19/mo — matches web app
STRIPE_ANNUAL_URL  = "https://buy.stripe.com/8x26oG7aQesqgX87HVejK03"  # $149/yr — matches web app
STRIPE_PORTAL_URL  = "https://billing.stripe.com/p/login/3cIcN4an28420Yad2fejK00"

# How long the app keeps working if it can't reach the server
OFFLINE_GRACE_DAYS = 5
NETWORK_TIMEOUT = 60  # Render free tier can take ~30-50s to wake from sleep

# ── Local storage ─────────────────────────────────────────
if getattr(sys, 'frozen', False):
    _BASE = os.path.dirname(sys.executable)
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))

CACHE_FILE  = os.path.join(_BASE, "license_cache.json")
DEVICE_FILE = os.path.join(_BASE, "device_id.txt")


def _device_id():
    """Stable per-install id, created once and reused."""
    if os.path.exists(DEVICE_FILE):
        try:
            with open(DEVICE_FILE) as f:
                v = f.read().strip()
                if v:
                    return v
        except Exception:
            pass
    v = uuid.uuid4().hex
    try:
        with open(DEVICE_FILE, "w") as f:
            f.write(v)
    except Exception:
        pass
    return v


def _load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(d):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass


def _post(path, payload):
    if requests is None:
        return None
    try:
        r = requests.post(SERVER_URL.rstrip("/") + path, json=payload,
                          timeout=NETWORK_TIMEOUT)
        if r.status_code in (200, 401):
            return r.json()
    except Exception:
        return None
    return None


# ── Public API used by the app ────────────────────────────
def get_status():
    """
    Returns a dict with at least {"active": bool}.
    When active, also includes plan, renews, and (for trials) trial + days_left.
    Validates online; falls back to a short offline grace period.
    """
    cache = _load_cache()
    key = cache.get("key")

    # 1) Try to validate a stored paid key online
    if key:
        resp = _post("/validate", {"key": key, "device_id": _device_id()})
        if resp is not None:
            if resp.get("valid"):
                cache.update({
                    "active": True, "plan": resp.get("plan", "Pro").capitalize(),
                    "renews": resp.get("expires", "—"), "trial": False,
                    "last_ok": datetime.datetime.now().isoformat(),
                })
                _save_cache(cache)
                return {"active": True, "plan": cache["plan"],
                        "renews": cache["renews"]}
            else:
                # Server says invalid/expired/revoked → deny
                cache["active"] = False
                _save_cache(cache)
                return {"active": False, "reason": resp.get("reason")}

    # 2) Try trial status online (does NOT auto-start one)
    tresp = _post("/trial", {"device_id": _device_id()}) if cache.get("trial_started") else None
    if tresp is not None and tresp.get("active"):
        cache.update({
            "active": True, "trial": True, "plan": "Free Trial",
            "renews": tresp.get("expires", "—"),
            "days_left": tresp.get("days_left", "?"),
            "last_ok": datetime.datetime.now().isoformat(),
        })
        _save_cache(cache)
        return {"active": True, "trial": True, "plan": "Free Trial",
                "renews": cache["renews"], "days_left": cache["days_left"]}
    if tresp is not None and not tresp.get("active") and cache.get("trial_started"):
        cache["active"] = False
        _save_cache(cache)

    # 3) Offline grace — could not reach server, use last good check
    last_ok = cache.get("last_ok")
    if cache.get("active") and last_ok:
        try:
            then = datetime.datetime.fromisoformat(last_ok)
            if datetime.datetime.now() - then < datetime.timedelta(days=OFFLINE_GRACE_DAYS):
                out = {"active": True, "plan": cache.get("plan", "Pro"),
                       "renews": cache.get("renews", "—")}
                if cache.get("trial"):
                    out.update({"trial": True, "days_left": cache.get("days_left", "?")})
                out["offline"] = True
                return out
        except Exception:
            pass

    return {"active": False}


def start_trial():
    """Asks the server to start a trial for this device."""
    resp = _post("/trial", {"device_id": _device_id()})
    if resp is None:
        return False, "Couldn't reach the license server. Check your internet and try again."
    if resp.get("active"):
        cache = _load_cache()
        cache.update({
            "trial_started": True, "active": True, "trial": True,
            "plan": "Free Trial", "renews": resp.get("expires", "—"),
            "days_left": resp.get("days_left", "?"),
            "last_ok": datetime.datetime.now().isoformat(),
        })
        _save_cache(cache)
        return True, f"Your free trial is active — {resp.get('days_left','?')} days of full access!"
    if resp.get("reason") == "trial_expired":
        return False, "Your free trial has already ended. Subscribe to keep access."
    return False, "Your free trial has already been used."


def trial_used():
    cache = _load_cache()
    if cache.get("trial_started"):
        return True
    # Ask server (best-effort)
    resp = _post("/trial", {"device_id": _device_id()})
    if resp is not None and (resp.get("reason") == "trial_expired"
                             or (resp.get("active") and not resp.get("new"))):
        return True
    return False


def activate_key(key):
    """Validates a key with the server and stores it locally if good."""
    key = key.strip().upper()
    if not key.startswith("BCP-"):
        return False, "Invalid key format. Keys start with BCP-"
    resp = _post("/validate", {"key": key, "device_id": _device_id()})
    if resp is None:
        return False, "Couldn't reach the license server. Check your internet and try again."
    if resp.get("valid"):
        cache = _load_cache()
        cache.update({
            "key": key, "active": True, "trial": False,
            "plan": resp.get("plan", "Pro").capitalize(),
            "renews": resp.get("expires", "—"),
            "last_ok": datetime.datetime.now().isoformat(),
        })
        _save_cache(cache)
        return True, f"Activated! Your {cache['plan']} plan is active until {cache['renews']}."
    reasons = {
        "expired": "This key has expired.",
        "revoked": "This key has been cancelled.",
        "bad_signature": "This key is invalid.",
        "bad_format": "This key is invalid.",
        "bad_date": "This key is invalid.",
    }
    return False, reasons.get(resp.get("reason"), "This key could not be validated.")


def cancel():
    """Clears the locally stored key (does not cancel Stripe billing)."""
    cache = _load_cache()
    cache.pop("key", None)
    cache["active"] = False
    _save_cache(cache)
