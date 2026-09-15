
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
    if result.get("ok"):
        _cron_beat("saved-search-alerts")
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


