"""
auth_client.py — Supabase email/password auth for Bid Caller Pro
══════════════════════════════════════════════════════════════════
Handles account sign up, sign in, and "forgot password" using Supabase
Auth's REST API directly (no supabase-py dependency, so it packages
cleanly with PyInstaller — same pattern as subscription.py's requests
calls to the license server).

FORGOT PASSWORD FLOW (code-based, not a browser link):
  1. request_password_reset(email)  → Supabase emails the user a 6-digit
     recovery code (and, depending on your email template, a link too).
  2. reset_password_with_code(email, code, new_password) → verifies the
     code, gets a temporary session, and sets the new password.
  3. User signs back in with sign_in(email, new_password).

IMPORTANT ONE-TIME SETUP IN SUPABASE:
  Supabase's default "Reset Password" email template only includes a
  clickable link ({{ .ConfirmationURL }}), which doesn't work for a
  desktop app that can't catch a browser redirect. For the code above
  to work, add the raw token to your template so the email shows a
  code the user can type back into the app:
    Dashboard → Authentication → Email Templates → Reset Password
    Add somewhere in the body, e.g.:
        Your reset code is: {{ .Token }}
  Without this, request_password_reset() still sends an email, but it
  won't contain a code the user can enter — only the link.
"""

import os
import sys
import json

try:
    import requests
except ImportError:
    requests = None

# ── Your Supabase project ─────────────────────────────────
SUPABASE_URL = "https://novwdthapkorstdtloky.supabase.co"
SUPABASE_ANON_KEY = "sb_publishable_sUuQHkrN_wsJqakUMfL_VA_58koZ76C"

NETWORK_TIMEOUT = 30

# ── Local session storage (remembers who's signed in) ─────
if getattr(sys, 'frozen', False):
    _BASE = os.path.dirname(sys.executable)
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))

SESSION_FILE = os.path.join(_BASE, "auth_session.json")


def _load_session():
    try:
        with open(SESSION_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_session(d):
    try:
        with open(SESSION_FILE, "w") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass


def current_email():
    """Returns the signed-in user's email, or None."""
    return _load_session().get("email")


def sign_out():
    _save_session({})


# ── Low-level request helper ───────────────────────────────
def _auth_post(path, payload, access_token=None, params=None):
    if requests is None:
        return None, "The 'requests' library isn't installed."
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token or SUPABASE_ANON_KEY}",
    }
    try:
        r = requests.post(SUPABASE_URL.rstrip("/") + path, headers=headers,
                          params=params, json=payload, timeout=NETWORK_TIMEOUT)
        data = {}
        try:
            data = r.json()
        except Exception:
            pass
        if r.status_code >= 400:
            return None, _friendly_error(data, r.status_code)
        return data, None
    except Exception:
        return None, "Couldn't reach the login server. Check your internet and try again."


def _auth_put(path, payload, access_token):
    if requests is None:
        return None, "The 'requests' library isn't installed."
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    try:
        r = requests.put(SUPABASE_URL.rstrip("/") + path, headers=headers,
                         json=payload, timeout=NETWORK_TIMEOUT)
        data = {}
        try:
            data = r.json()
        except Exception:
            pass
        if r.status_code >= 400:
            return None, _friendly_error(data, r.status_code)
        return data, None
    except Exception:
        return None, "Couldn't reach the login server. Check your internet and try again."


def _friendly_error(data, status_code):
    msg = (data.get("error_description") or data.get("msg")
           or data.get("error") or data.get("message") or "")
    low = msg.lower()
    if "already registered" in low or "already exists" in low:
        return "An account with that email already exists. Try signing in instead."
    if "invalid login credentials" in low:
        return "Incorrect email or password."
    if "email not confirmed" in low:
        return "Please confirm your email first — check your inbox for a confirmation link."
    if "token has expired" in low or "invalid" in low and "token" in low:
        return "That code has expired or is incorrect. Request a new one."
    if "password" in low and ("least" in low or "short" in low or "weak" in low):
        return msg or "Password is too weak — use at least 6 characters."
    if "rate limit" in low or "429" in str(status_code) or status_code == 429:
        return ("Email limit reached — Supabase's built-in mailer caps how many "
                 "sign-up/reset emails it can send per hour. Wait about an hour and "
                 "try again, or set up custom SMTP in Supabase (Authentication → "
                 "Settings → SMTP Settings) to remove the cap.")
    return msg or f"Something went wrong (error {status_code})."


# ── Public API ──────────────────────────────────────────────
def sign_up(email, password):
    """Creates a new account. Returns (ok, message)."""
    email = (email or "").strip().lower()
    password = password or ""
    if not email or "@" not in email:
        return False, "Enter a valid email address."
    if len(password) < 6:
        return False, "Password must be at least 6 characters."

    data, err = _auth_post("/auth/v1/signup", {"email": email, "password": password})
    if err:
        return False, err

    if data and data.get("access_token"):
        _save_session({
            "email": email,
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", ""),
        })
        return True, "Account created! You're signed in."

    # No access_token in response usually means email confirmation is required.
    return True, "Account created! Check your email to confirm it, then sign in."


def sign_in(email, password):
    """Signs in an existing account. Returns (ok, message)."""
    email = (email or "").strip().lower()
    password = password or ""
    if not email or not password:
        return False, "Enter your email and password."

    data, err = _auth_post("/auth/v1/token", {"email": email, "password": password},
                           params={"grant_type": "password"})
    if err:
        return False, err
    if not data or not data.get("access_token"):
        return False, "Sign in failed. Try again."

    _save_session({
        "email": email,
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
    })
    return True, f"Signed in as {email}."


def request_password_reset(email):
    """Sends a password-reset code to the given email. Returns (ok, message)."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False, "Enter a valid email address."

    data, err = _auth_post("/auth/v1/recover", {"email": email})
    if err:
        return False, err
    return True, f"If an account exists for {email}, a reset code was just emailed to it."


def reset_password_with_code(email, code, new_password):
    """Verifies the emailed code and sets a new password. Returns (ok, message)."""
    email = (email or "").strip().lower()
    code = (code or "").strip()
    if not code:
        return False, "Enter the code from your email."
    if len(new_password or "") < 6:
        return False, "New password must be at least 6 characters."

    # Step 1: verify the recovery code → get a temporary session
    data, err = _auth_post("/auth/v1/verify", {
        "type": "recovery", "email": email, "token": code,
    })
    if err:
        return False, err
    access_token = (data or {}).get("access_token")
    if not access_token:
        return False, "That code didn't work. Request a new one and try again."

    # Step 2: use that session to set the new password
    data2, err2 = _auth_put("/auth/v1/user", {"password": new_password}, access_token)
    if err2:
        return False, err2

    # Sign the user in with their new password right away
    _save_session({
        "email": email,
        "access_token": access_token,
        "refresh_token": (data or {}).get("refresh_token", ""),
    })
    return True, "Password updated! You're signed back in."
