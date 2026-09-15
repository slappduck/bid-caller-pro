
@app.route("/validate", methods=["POST"])
def validate():
    data = request.get_json(force=True, silent=True) or {}
    # .get(x) or default, not .get(x, default) -- a JSON body of {"key": null}
    # has "key" present with value None, so the second form's default never
    # applies and .strip() below throws. Same pattern already used safely by
    # trial()'s device_id and revoke()'s key, just below.
    key = data.get("key") or ""
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
    _bi_note(db, "trial_started", email)
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
    # Same {"plan": null} hazard as /validate's key -- .get(x) or default,
    # not .get(x, default). A null months value hits the same class of bug
    # via int(None), so it gets the same treatment.
    plan = data.get("plan") or "monthly"
    months = 12 if plan == "annual" else int(data.get("months") or 1)
    key, exp = make_key(plan, months)
    db = _db()
    db.setdefault("issued", {})[key] = {
        "plan": plan, "expires": exp[:10], "email": data.get("email") or "",
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


