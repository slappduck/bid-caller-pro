"""
kv_backend.py — one durable key/value store for everything that must survive
════════════════════════════════════════════════════════════════════════════

Render's filesystem is ephemeral. It is wiped on every deploy and every time a
free-tier instance spins down after inactivity, which is constantly. Anything
written to a local JSON file there is not storage, it is a scratchpad.

Four things depend on surviving that:

  * the licence database — issued keys, email and device mappings, revocations,
    and trial start dates. Losing it hands out fresh 7-day trials on every
    restart and resurrects revoked keys;
  * the portal directory — which page holds each town's bids. This is the whole
    "gets better the more it runs" mechanism; without persistence it learns
    something on every scan and forgets it immediately;
  * the geocode cache — without it every scan re-geocodes from scratch, which
    is slow and runs into the geocoders' rate limits;
  * the same-day scan cache.

Three backends, tried in order, all speaking the same tiny interface:

  1. Upstash Redis, if UPSTASH_REDIS_REST_* are set.
  2. Supabase Postgres, if SUPABASE_SERVICE_ROLE_KEY is set. Needs the table in
     supabase_kv_schema.sql. This exists so persistence is available without
     signing up for anything new — the project already has Supabase.
  3. A local file. Correct for development, and on Render a guarantee of data
     loss, so `backend_name()` reports it and /health calls it out.

Every function fails soft. A storage outage degrades the product; it must never
500 a request.
"""

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request

UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

TABLE = os.environ.get("SUPABASE_KV_TABLE", "kv_store")
_TIMEOUT = 15

_lock = threading.Lock()
_state = {"supabase_ok": None}   # None = untried, True/False = last outcome


def _local_path(key):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(key))
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), f"kv_{safe}.json")


# ── Upstash ─────────────────────────────────────────────────────────────────
def _upstash(*cmd):
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        return None, False
    req = urllib.request.Request(
        UPSTASH_URL, data=json.dumps(list(cmd)).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {UPSTASH_TOKEN}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8")).get("result"), True
    except Exception as ex:
        print(f"[kv] upstash error: {ex}", flush=True)
        return None, False


# ── Supabase ────────────────────────────────────────────────────────────────
def _supabase_configured():
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def _supabase_headers(extra=None):
    h = {"apikey": SUPABASE_SERVICE_ROLE_KEY,
         "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
         "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def _supabase_get(key):
    if not _supabase_configured():
        return None, False
    qs = urllib.parse.urlencode({"key": f"eq.{key}", "select": "value"})
    req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/{TABLE}?{qs}",
                                 headers=_supabase_headers())
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
        with _lock:
            _state["supabase_ok"] = True
        if not rows:
            return None, True          # reachable, simply no row yet
        return rows[0].get("value"), True
    except Exception as ex:
        # A missing table is the one failure worth naming precisely: it means
        # the schema hasn't been applied, not that Supabase is down.
        print(f"[kv] supabase get failed ({key}): {ex}", flush=True)
        with _lock:
            _state["supabase_ok"] = False
        return None, False


def _supabase_set(key, value):
    if not _supabase_configured():
        return False
    body = json.dumps([{"key": key, "value": value}]).encode("utf-8")
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{TABLE}?on_conflict=key", data=body, method="POST",
        headers=_supabase_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}))
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            resp.read()
        with _lock:
            _state["supabase_ok"] = True
        return True
    except Exception as ex:
        print(f"[kv] supabase set failed ({key}): {ex}", flush=True)
        with _lock:
            _state["supabase_ok"] = False
        return False


# ── Public interface ────────────────────────────────────────────────────────
def backend_name():
    """Which store writes are actually going to right now."""
    if UPSTASH_URL and UPSTASH_TOKEN:
        return "upstash"
    if _supabase_configured():
        return "supabase"
    return "file"


def is_durable():
    """False means every write is lost on the next restart."""
    return backend_name() != "file"


def get(key, default=None):
    """Read a JSON value. Returns `default` when absent or unreachable."""
    raw, ok = _upstash("GET", key)
    if ok:
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default

    value, ok = _supabase_get(key)
    if ok:
        return default if value is None else value

    try:
        with open(_local_path(key), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def set(key, value):  # noqa: A001 - the name matches what it does
    """Write a JSON value. True if it landed somewhere durable."""
    _, ok = _upstash("SET", key, json.dumps(value))
    if ok:
        return True

    if _supabase_set(key, value):
        return True

    try:
        with open(_local_path(key), "w", encoding="utf-8") as f:
            json.dump(value, f)
    except Exception:
        pass
    return False


def health():
    """What /health reports about storage."""
    return {
        "backend": backend_name(),
        "durable": is_durable(),
        "upstash_configured": bool(UPSTASH_URL and UPSTASH_TOKEN),
        "supabase_configured": _supabase_configured(),
        "supabase_last_ok": _state["supabase_ok"],
    }
