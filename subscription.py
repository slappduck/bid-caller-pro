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

import auth_client

# ── YOUR SERVER + STRIPE ──────────────────────────────────
SERVER_URL = "https://bid-caller-pro.onrender.com"   # live license server
STRIPE_MONTHLY_URL = "https://buy.stripe.com/5kQcN43YEbge0YafanejK01"  # $19/mo — matches web app
STRIPE_ANNUAL_URL  = "https://buy.stripe.com/8x26oG7aQesqgX87HVejK03"  # $149/yr — matches web app
STRIPE_PORTAL_URL  = "https://billing.stripe.com/p/login/3cIcN4an28420Yad2fejK00"
SUPPORT_EMAIL      = "Yumiwave1@gmail.com"

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


def time_left_label(expires_at):
    """Turns an expires_at ISO timestamp into a friendly live countdown
    string ('3d 4h left', '2h 15m left', '40m left') instead of a static
    day count that only ticks over once every 24 hours."""
    if not expires_at:
        return None
    try:
        end = datetime.datetime.fromisoformat(expires_at)
    except ValueError:
        return None
    delta = end - datetime.datetime.now()
    total_seconds = delta.total_seconds()
    if total_seconds <= 0:
        return "Expired"
    days = delta.days
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h left"
    if hours > 0:
        return f"{hours}h {minutes}m left"
    return f"{minutes}m left"


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


# Real scans take a while and the backend can be asleep (Render free tier) —
# same generous timeout the web app uses for /scan. A real-world scan was
# clocked at ~100s, so 90s was cutting it too close and silently dropping
# good results — bumped with headroom for cold starts + larger radii.
SCAN_TIMEOUT = 150


def scan(location, radius):
    """Runs a live bid scan via the server (same /scan endpoint the web app
    uses — real procurement-platform search + SAM.gov federal bids, not just
    a guessed municipal URL). Returns the parsed response dict; on a network
    failure returns {"ok": False, "reason": "unreachable"}."""
    if requests is None:
        return {"ok": False, "reason": "unreachable"}
    cache = _load_cache()
    payload = {
        "key": cache.get("key", ""), "device_id": _device_id(),
        "supabase_token": auth_client.current_access_token(),
        "location": location, "radius": radius,
    }
    try:
        r = requests.post(SERVER_URL.rstrip("/") + "/scan", json=payload,
                          timeout=SCAN_TIMEOUT)
        return r.json()
    except Exception:
        return {"ok": False, "reason": "unreachable"}


def upcoming(location, radius):
    """Finds planned/pre-bid concrete work (council agendas, budgets, CIPs)
    via the server's /upcoming endpoint — same license gate and timeout
    budget as scan()."""
    if requests is None:
        return {"ok": False, "reason": "unreachable"}
    cache = _load_cache()
    payload = {
        "key": cache.get("key", ""), "device_id": _device_id(),
        "supabase_token": auth_client.current_access_token(),
        "location": location, "radius": radius,
    }
    try:
        r = requests.post(SERVER_URL.rstrip("/") + "/upcoming", json=payload,
                          timeout=SCAN_TIMEOUT)
        return r.json()
    except Exception:
        return {"ok": False, "reason": "unreachable"}


def residential_leads(location, radius):
    """Finds residential driveway/sidewalk permit leads via the server's
    /residential-leads endpoint — same license gate and timeout budget as
    scan()/upcoming(). Structured city permit data, not AI-extracted, so
    there's no OpenAI dependency for this one."""
    if requests is None:
        return {"ok": False, "reason": "unreachable"}
    cache = _load_cache()
    payload = {
        "key": cache.get("key", ""), "device_id": _device_id(),
        "supabase_token": auth_client.current_access_token(),
        "location": location, "radius": radius,
    }
    try:
        r = requests.post(SERVER_URL.rstrip("/") + "/residential-leads", json=payload,
                          timeout=SCAN_TIMEOUT)
        return r.json()
    except Exception:
        return {"ok": False, "reason": "unreachable"}


def my_key():
    """Checks whether this device has a key on file yet — the Stripe webhook
    records device_id -> key right after checkout completes (via
    client_reference_id), so this lets the app unlock automatically instead
    of making the customer copy-paste their key."""
    if requests is None:
        return {"ok": False, "reason": "unreachable"}
    try:
        r = requests.post(SERVER_URL.rstrip("/") + "/mykey",
                          json={"device_id": _device_id()}, timeout=NETWORK_TIMEOUT)
        return r.json()
    except Exception:
        return {"ok": False, "reason": "unreachable"}


def send_support_message(email, message):
    """Sends a support message via the server's /support endpoint (emailed
    to the support inbox via Resend). Returns (ok, message)."""
    if requests is None:
        return False, "The 'requests' library isn't installed."
    try:
        r = requests.post(SERVER_URL.rstrip("/") + "/support",
                          json={"email": email, "message": message},
                          timeout=NETWORK_TIMEOUT)
        data = r.json()
    except Exception:
        return False, "Couldn't reach the server. Check your internet and try again."
    if data.get("ok"):
        return True, "Message sent — we'll get back to you soon."
    reasons = {
        "no_message": "Enter a message first.",
        "email_unavailable": "Support messages aren't configured yet — email us directly instead.",
    }
    return False, reasons.get(data.get("reason"), "Couldn't send your message. Try again.")


def draft_proposal(bid, company):
    """Asks the server's AI to draft a bid-proposal cover letter, personalized
    with the contractor's own saved company info. Nothing about the company
    is stored server-side — it's only sent on this one request."""
    if requests is None:
        return {"ok": False, "reason": "unreachable"}
    cache = _load_cache()
    payload = {
        "key": cache.get("key", ""), "device_id": _device_id(),
        "supabase_token": auth_client.current_access_token(),
        "bid": bid, "company": company,
    }
    try:
        # Server's own OpenAI call budget is 45s -- give real headroom above
        # that (same class of bug as the old /scan timeout: a client timeout
        # equal to the server's internal timeout leaves no margin at all).
        r = requests.post(SERVER_URL.rstrip("/") + "/draft-proposal", json=payload,
                          timeout=75)
        return r.json()
    except Exception:
        return {"ok": False, "reason": "unreachable"}


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
    tresp = _post("/trial", {"device_id": _device_id(),
                              "supabase_token": auth_client.current_access_token()}) \
        if cache.get("trial_started") else None
    if tresp is not None and tresp.get("active"):
        cache.update({
            "active": True, "trial": True, "plan": "Free Trial",
            "renews": tresp.get("expires", "—"),
            "days_left": tresp.get("days_left", "?"),
            "expires_at": tresp.get("expires_at", ""),
            "last_ok": datetime.datetime.now().isoformat(),
        })
        _save_cache(cache)
        return {"active": True, "trial": True, "plan": "Free Trial",
                "renews": cache["renews"], "days_left": cache["days_left"],
                "expires_at": cache.get("expires_at", "")}
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
                    out.update({"trial": True, "days_left": cache.get("days_left", "?"),
                               "expires_at": cache.get("expires_at", "")})
                out["offline"] = True
                return out
        except Exception:
            pass

    return {"active": False}


def start_trial():
    """Asks the server to start a trial for this device. Requires a signed-in
    account — trials are keyed by verified email so they can't be reset just
    by deleting the local device_id file."""
    token = auth_client.current_access_token()
    if not token:
        return False, "Sign in to start your free trial — this keeps it tied to your account."
    resp = _post("/trial", {"device_id": _device_id(), "supabase_token": token})
    if resp is None:
        return False, "Couldn't reach the license server. Check your internet and try again."
    if resp.get("active"):
        cache = _load_cache()
        cache.update({
            "trial_started": True, "active": True, "trial": True,
            "plan": "Free Trial", "renews": resp.get("expires", "—"),
            "days_left": resp.get("days_left", "?"),
            "expires_at": resp.get("expires_at", ""),
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
    resp = _post("/trial", {"device_id": _device_id(),
                             "supabase_token": auth_client.current_access_token()})
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
