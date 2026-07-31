"""
data_sync.py — Cross-device sync for Bid Caller Pro (Supabase Postgres)
══════════════════════════════════════════════════════════════════════
Saved bids, company profile, and saved searches sync to Supabase so
signing in on a second device (or the web app) sees the same data. Local
JSON files stay the source of truth when signed out or offline — this
only pushes/pulls when there's a valid session, and every function fails
soft (returns None/False) so a network hiccup never blocks local use.

Requires the tables in supabase_sync_schema.sql (run once in the Supabase
SQL Editor). Each user_id column defaults to auth.uid(), so the client
never sends its own id — Postgres derives it from the request's JWT, and
Row Level Security means the anon key + a user's own access token is all
that's needed here. No service-role key involved.
"""

import auth_client

try:
    import requests
except ImportError:
    requests = None

REST_URL = auth_client.SUPABASE_URL.rstrip("/") + "/rest/v1"
NETWORK_TIMEOUT = 15


def _headers(token, prefer=None):
    h = {
        "apikey": auth_client.SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _get(table, token, params=None):
    if requests is None or not token:
        return None
    try:
        r = requests.get(f"{REST_URL}/{table}", headers=_headers(token),
                         params=params, timeout=NETWORK_TIMEOUT)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _upsert(table, token, rows, on_conflict=None):
    if requests is None or not token:
        return False
    try:
        params = {"on_conflict": on_conflict} if on_conflict else None
        r = requests.post(f"{REST_URL}/{table}", params=params, json=rows,
                          headers=_headers(token, prefer="resolution=merge-duplicates"),
                          timeout=NETWORK_TIMEOUT)
        return r.status_code in (200, 201)
    except Exception:
        return False


def _delete(table, token, match):
    if requests is None or not token:
        return False
    try:
        params = {k: f"eq.{v}" for k, v in match.items()}
        r = requests.delete(f"{REST_URL}/{table}", headers=_headers(token),
                            params=params, timeout=NETWORK_TIMEOUT)
        return r.status_code in (200, 204)
    except Exception:
        return False


# ── Saved bids (starred bids + pipeline status + notes) ────
def pull_saved_bids(token):
    """Returns {bid_id: record} on success, or None on failure (network
    error, not signed in, tables not created yet) — callers should treat
    None as "couldn't reach the cloud" and keep whatever's local."""
    rows = _get("saved_bids", token)
    if rows is None:
        return None
    out = {}
    for row in rows:
        bid_id = row.pop("bid_id", None)
        if not bid_id:
            continue
        row.pop("user_id", None)
        row.pop("updated_at", None)
        row["_city"] = row.pop("city", "")
        out[bid_id] = row
    return out


def push_saved_bid(token, bid_id, record):
    row = {
        "bid_id": bid_id,
        "city": record.get("_city", ""),
        "title": record.get("title", ""),
        "scope": record.get("scope", ""),
        "value": record.get("value", ""),
        "deadline": record.get("deadline", ""),
        "contact": record.get("contact", ""),
        "email": record.get("email", ""),
        "phone": record.get("phone", ""),
        "url": record.get("url", ""),
        "status": record.get("status", ""),
        "pipeline": record.get("pipeline", ""),
        "note": record.get("note", ""),
        "saved_at": record.get("saved_at", ""),
    }
    return _upsert("saved_bids", token, [row], on_conflict="user_id,bid_id")


def delete_saved_bid(token, bid_id):
    return _delete("saved_bids", token, {"bid_id": bid_id})


def clear_all_saved_bids(token):
    return _delete("saved_bids", token, {})


def push_all_saved_bids(token, saved):
    """Bulk upsert — used right after sign-in to push whatever was saved
    locally while signed out, so it isn't lost/orphaned on this device."""
    if not saved:
        return True
    rows = [dict(push_row, bid_id=bid_id) for bid_id, rec in saved.items()
            for push_row in [{
                "city": rec.get("_city", ""), "title": rec.get("title", ""),
                "scope": rec.get("scope", ""), "value": rec.get("value", ""),
                "deadline": rec.get("deadline", ""), "contact": rec.get("contact", ""),
                "email": rec.get("email", ""), "phone": rec.get("phone", ""),
                "url": rec.get("url", ""), "status": rec.get("status", ""),
                "pipeline": rec.get("pipeline", ""), "note": rec.get("note", ""),
                "saved_at": rec.get("saved_at", ""),
            }]]
    return _upsert("saved_bids", token, rows, on_conflict="user_id,bid_id")


# ── Company profile (one row per user) ──────────────────────
def pull_company_profile(token):
    rows = _get("company_profiles", token)
    if rows is None:
        return None
    if not rows:
        return {}
    row = dict(rows[0])
    row.pop("user_id", None)
    row.pop("updated_at", None)
    return row


def push_company_profile(token, profile):
    row = {
        "name": profile.get("name", ""),
        "contact": profile.get("contact", ""),
        "phone": profile.get("phone", ""),
        "email": profile.get("email", ""),
        "specialty": profile.get("specialty", ""),
    }
    return _upsert("company_profiles", token, [row], on_conflict="user_id")


# ── Saved searches (auto-alert searches) ────────────────────
def pull_saved_searches(token):
    rows = _get("saved_searches", token)
    if rows is None:
        return None
    return [{"location": r["location"], "radius": r["radius"]} for r in rows]


def push_saved_search(token, location, radius):
    return _upsert("saved_searches", token, [{"location": location, "radius": radius}])


def delete_saved_search(token, location, radius):
    return _delete("saved_searches", token, {"location": location, "radius": radius})


def push_all_saved_searches(token, searches):
    if not searches:
        return True
    return _upsert("saved_searches", token, searches)
