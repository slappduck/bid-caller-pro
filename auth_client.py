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

GOOGLE SIGN-IN — ONE-TIME SETUP IN SUPABASE:
  sign_in_with_google() will fail with a provider error until Google is
  enabled on this Supabase project:
    Dashboard → Authentication → Providers → Google → enable it, and fill
    in a Google Cloud OAuth Client ID + Secret (Google Cloud Console →
    APIs & Services → Credentials → OAuth client ID → Web application).
  No redirect URI needs to be added on the Google Cloud side beyond what
  Supabase's own docs ask for — the desktop app never talks to Google
  directly, it goes through Supabase's own /auth/v1/authorize endpoint,
  which redirects to a one-shot local HTTP server on 127.0.0.1 instead of
  a fixed web URL.
"""

import os
import sys
import json
import secrets
import hashlib
import base64
import threading
import webbrowser
import http.server
from urllib.parse import urlparse, parse_qs, urlencode

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


def current_access_token():
    """Returns the signed-in user's Supabase access token, or None."""
    return _load_session().get("access_token")


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


# ── Google sign-in (PKCE via a local loopback redirect) ────
# A desktop app can't receive a normal OAuth redirect the way a website
# can, so this opens the system browser to Supabase's Google auth URL with
# redirect_to pointed at a one-shot local HTTP server on 127.0.0.1. Using
# PKCE (instead of the implicit flow the web app uses) means Supabase sends
# the auth code back as a plain "?code=" query param — which a plain HTTP
# server can read directly — instead of a URL fragment, which only
# JavaScript in a real browser page can see.
class _OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        self.server.oauth_code = params.get("code", [None])[0]
        self.server.oauth_error = params.get("error_description", [None])[0]
        ok = bool(self.server.oauth_code)
        msg = ("Signed in! You can close this tab and return to Bid Caller Pro."
               if ok else "Sign-in failed or was cancelled. You can close this tab.")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            f"<html><body style='font-family:sans-serif;text-align:center;padding:60px;'>"
            f"<h2>{msg}</h2></body></html>".encode())

    def log_message(self, format, *args):
        pass  # don't spam stdout with per-request access logs


def sign_in_with_google(timeout=120):
    """Opens the system browser for Google sign-in via Supabase. Blocks
    until the browser redirect lands (or times out), so call this from a
    background thread — not the UI thread. Returns (ok, message)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    server = http.server.HTTPServer(("127.0.0.1", 0), _OAuthCallbackHandler)
    server.timeout = timeout
    server.oauth_code = None
    server.oauth_error = None
    port = server.server_address[1]

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    auth_url = f"{SUPABASE_URL}/auth/v1/authorize?" + urlencode({
        "provider": "google",
        "redirect_to": f"http://127.0.0.1:{port}",
        "code_challenge": challenge,
        "code_challenge_method": "s256",
    })
    webbrowser.open(auth_url)

    thread.join(timeout=timeout + 5)
    try:
        server.server_close()
    except Exception:
        pass

    if server.oauth_error:
        return False, server.oauth_error
    if not server.oauth_code:
        return False, "Sign-in timed out or was cancelled."

    data, err = _auth_post("/auth/v1/token", {
        "auth_code": server.oauth_code,
        "code_verifier": verifier,
    }, params={"grant_type": "pkce"})
    if err:
        return False, err
    access_token = (data or {}).get("access_token")
    if not access_token:
        return False, "Sign-in failed. Try again."

    email = ((data or {}).get("user") or {}).get("email", "")
    _save_session({
        "email": email,
        "access_token": access_token,
        "refresh_token": data.get("refresh_token", ""),
    })
    return True, f"Signed in as {email}." if email else "Signed in!"


def request_magic_link(email):
    """Emails a passwordless sign-in code. Returns (ok, message)."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False, "Enter a valid email address."

    data, err = _auth_post("/auth/v1/otp", {"email": email, "create_user": True})
    if err:
        return False, err
    return True, f"A sign-in code was just emailed to {email}."


def sign_in_with_code(email, code):
    """Verifies a magic-link code and signs the user in. Returns (ok, message)."""
    email = (email or "").strip().lower()
    code = (code or "").strip()
    if not code:
        return False, "Enter the code from your email."

    data, err = _auth_post("/auth/v1/verify", {
        "type": "magiclink", "email": email, "token": code,
    })
    if err:
        return False, err
    access_token = (data or {}).get("access_token")
    if not access_token:
        return False, "That code didn't work. Request a new one and try again."

    _save_session({
        "email": email,
        "access_token": access_token,
        "refresh_token": (data or {}).get("refresh_token", ""),
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
