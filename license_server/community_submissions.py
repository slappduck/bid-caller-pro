

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


