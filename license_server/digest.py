
# ── Free weekly bid email (/digest/*) ───────────────────────────────────────
#
# A contractor who lands on a price page from Google and isn't ready to start
# a trial had nothing to do but leave. This gives them one thing to do: leave
# an email and a ZIP, and get the open sidewalk, curb and ramp bids near them
# every Monday. No account, no app. Seeing real bids in the inbox each week is
# the pitch; the email says what the app adds on top.
#
# Rules this keeps, all for the same reason (a free form that sends email is
# a way to make this server email strangers):
#
#   * Double opt-in. The form only ever sends ONE email -- a confirmation
#     link -- and nothing weekly goes anywhere until that link is clicked. A
#     typo'd or malicious signup costs the owner of that address one message.
#   * At most one confirmation per address per CONFIRM_RESEND_HOURS, and a
#     per-IP daily cap on the form, so the form can't be used to flood one
#     inbox or to send in bulk.
#   * An address on the suppression list (it unsubscribed from anything we
#     send, bounced, or complained) is never emailed, confirmation included.
#     The form answers the same either way, so it can't be used to test who
#     is on the list.
#   * Every weekly email carries the postal address and the same signed
#     one-click unsubscribe as the outreach mail (referrals.py), which
#     suppresses the address for good. Without MAILING_ADDRESS nothing sends.
#
# Subscribers live in the KV store: {email: {zip, place, lat, lon, ...}}.
# Only the email, the ZIP and the town it resolves to are kept.
#
# The weekly send is /run-bid-digest, driven by bid-digest.yml. Scans are the
# expensive part, so subscribers are grouped by ZIP and each area is scanned
# once (the same-day scan cache means an area a user already scanned today is
# free). A call handles DIGEST_AREAS_PER_CALL areas and reports how many are
# left; the workflow calls again until none are, so no single request runs
# into the server's timeout however many areas there are. Who has already
# been sent this week's email is recorded, so a repeated call never sends
# twice.
from html import escape as html_escape

_DIGEST_SUBS_KEY = "bidcaller:digest_subs"
_DIGEST_SENT_KEY = "bidcaller:digest_sent"
_DIGEST_RATE_KEY = "bidcaller:digest_rate"
DIGEST_RADIUS_MI = int(os.environ.get("DIGEST_RADIUS_MI", "50"))
DIGEST_MAX_SUBSCRIBERS = int(os.environ.get("DIGEST_MAX_SUBSCRIBERS", "2000"))
DIGEST_AREAS_PER_CALL = int(os.environ.get("DIGEST_AREAS_PER_CALL", "4"))
DIGEST_SIGNUPS_PER_IP_PER_DAY = int(os.environ.get("DIGEST_SIGNUPS_PER_IP_PER_DAY", "5"))
DIGEST_MAX_BIDS = 15
CONFIRM_RESEND_HOURS = 6
SITE_URL = os.environ.get("SITE_URL", "https://curbcallpro.com").rstrip("/")
_DIGEST_EMAIL_RE = re.compile(r"^[^@\s<>\"',;]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
_DIGEST_ZIP_RE = re.compile(r"^\d{5}$")


def _digest_subs():
    got = kv_backend.get(_DIGEST_SUBS_KEY, None)
    return got if isinstance(got, dict) else {}


def _save_digest_subs(subs):
    kv_backend.set(_DIGEST_SUBS_KEY, subs)


def _digest_token(purpose, email, zip_code=""):
    """Signed with the server secret, so a link works without stored state
    and can't be edited into a link for someone else's address or ZIP."""
    msg = f"digest|{purpose}|{(email or '').strip().lower()}|{zip_code or ''}"
    return hmac.new(LICENSE_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]


def _digest_page(inner, status=200):
    body = ("<!doctype html><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Weekly bid email - CurbCall Pro</title>"
            "<body style=\"background:#0d0f18;color:#f1f5f9;font-family:system-ui,sans-serif;"
            "padding:3rem 1.5rem;max-width:34rem;margin:0 auto;line-height:1.6;\">"
            f"{inner}<p style=\"margin-top:2rem;\"><a style=\"color:#f59e0b;\" "
            f"href=\"{SITE_URL}/\">&larr; Back to CurbCall Pro</a></p></body>")
    return body, status, {"Content-Type": "text/html; charset=utf-8"}


def _digest_wants_json():
    return request.is_json or "application/json" in (request.headers.get("Accept") or "")


def _digest_reply(ok, html, reason="", status=200):
    if _digest_wants_json():
        return jsonify({"ok": ok, **({"reason": reason} if reason else {})}), status
    return _digest_page(html, status)


_DIGEST_CHECK_EMAIL = ("<h2>Check your email</h2><p>We sent a link to confirm it's you. "
                       "Click it and the first list of bids arrives next Monday morning.</p>"
                       "<p style=\"color:#9aa4ba;\">Nothing there in a few minutes? Check "
                       "spam or promotions.</p>")


@app.route("/digest/subscribe", methods=["POST"])
def digest_subscribe():
    data = request.get_json(silent=True) if request.is_json else request.form
    data = data or {}
    # Hidden field people never see. A bot that fills every box gets the
    # normal answer and nothing is sent.
    if str(data.get("website") or "").strip():
        return _digest_reply(True, _DIGEST_CHECK_EMAIL)
    email = str(data.get("email") or "").strip().lower()
    zip_code = str(data.get("zip") or "").strip()
    if not _DIGEST_EMAIL_RE.match(email) or not _DIGEST_ZIP_RE.match(zip_code):
        return _digest_reply(False, "<h2>Something's missing</h2><p>Enter your email and a "
                             "5-digit ZIP code, then try again.</p>", "invalid", 400)
    if not (RESEND_API_KEY and MAILING_ADDRESS.strip()):
        return _digest_reply(False, "<h2>Not quite ready</h2><p>The weekly email isn't "
                             f"switched on yet. Write to {SUPPORT_EMAIL} and we'll add you "
                             "by hand.</p>", "not_configured", 503)
    if not _ip_rate_ok(_DIGEST_RATE_KEY, _client_ip(), DIGEST_SIGNUPS_PER_IP_PER_DAY):
        return _digest_reply(False, "<h2>Too many tries</h2><p>Please try again tomorrow.</p>",
                             "rate_limited", 429)
    geo = _geo_from_zip(zip_code)
    if not geo:
        return _digest_reply(False, f"<h2>We couldn't find ZIP {zip_code}</h2><p>Check it and "
                             "try again.</p>", "unknown_zip", 400)
    if email in _suppression():
        return _digest_reply(True, _DIGEST_CHECK_EMAIL)

    subs = _digest_subs()
    rec = subs.get(email) or {}
    if not rec and len(subs) >= DIGEST_MAX_SUBSCRIBERS:
        return _digest_reply(False, "<h2>We're full for now</h2><p>The free list is at "
                             f"capacity. Write to {SUPPORT_EMAIL} and we'll add you.</p>",
                             "full", 503)
    now = datetime.datetime.now(datetime.timezone.utc)
    last = rec.get("confirm_sent_at")
    if last and rec.get("pending_zip") == zip_code:
        try:
            if now - datetime.datetime.fromisoformat(last) < datetime.timedelta(hours=CONFIRM_RESEND_HOURS):
                return _digest_reply(True, _DIGEST_CHECK_EMAIL)
        except (TypeError, ValueError):
            pass
    place = f"{geo.get('city') or zip_code}, {geo.get('state') or ''}".strip(", ")
    link = (f"{PUBLIC_BASE_URL}/digest/confirm?" + urllib.parse.urlencode(
        {"e": email, "z": zip_code, "t": _digest_token("confirm", email, zip_code)}))
    text = (f"Confirm your free weekly bid email for {place} ({zip_code}):\n\n{link}\n\n"
            "Every Monday you'll get the open sidewalk, curb and ADA ramp bids within "
            f"{DIGEST_RADIUS_MI} miles. Free, and one click to stop.\n\n"
            "If you didn't ask for this, ignore this email and you won't hear from us.\n\n"
            f"CurbCall Pro\n{MAILING_ADDRESS.strip()}")
    if not _send_email(email, "Confirm your free weekly bid email", text):
        return _digest_reply(False, "<h2>That didn't send</h2><p>We couldn't send the "
                             "confirmation just now. Please try again in a few minutes.</p>",
                             "send_failed", 502)
    rec.update({"pending_zip": zip_code, "confirm_sent_at": now.isoformat()})
    rec.setdefault("created_at", now.isoformat())
    subs[email] = rec
    _save_digest_subs(subs)
    print(f"[digest] confirmation sent to {email} for {zip_code}", flush=True)
    return _digest_reply(True, _DIGEST_CHECK_EMAIL)


@app.route("/digest/confirm", methods=["GET"])
def digest_confirm():
    email = (request.args.get("e") or "").strip().lower()
    zip_code = (request.args.get("z") or "").strip()
    token = (request.args.get("t") or "").strip()
    if (not email or not _DIGEST_ZIP_RE.match(zip_code)
            or not hmac.compare_digest(token, _digest_token("confirm", email, zip_code))):
        return _digest_page("<h2>That link isn't valid</h2><p>Sign up again from "
                            f"<a style=\"color:#f59e0b;\" href=\"{SITE_URL}/concrete-cost/index.html\">"
                            "the price pages</a> and use the newest email.</p>", 400)
    if email in _suppression():
        return _digest_page("<h2>This address unsubscribed earlier</h2><p>We keep that "
                            f"promise, so we won't start sending. Write to {SUPPORT_EMAIL} "
                            "from this address if you'd like back on.</p>", 409)
    geo = _geo_from_zip(zip_code)
    if not geo:
        return _digest_page("<h2>Please try again in a minute</h2><p>We couldn't look up "
                            "that ZIP just now.</p>", 503)
    subs = _digest_subs()
    rec = subs.get(email) or {}
    rec.update({"zip": zip_code, "place": f"{geo.get('city') or zip_code}, {geo.get('state') or ''}".strip(", "),
                "lat": geo.get("lat"), "lon": geo.get("lon"), "confirmed": True,
                "confirmed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    rec.pop("pending_zip", None)
    rec.pop("confirm_sent_at", None)
    subs[email] = rec
    _save_digest_subs(subs)
    print(f"[digest] confirmed {email} for {zip_code}", flush=True)
    return _digest_page(f"<h2>You're in</h2><p>Every Monday morning you'll get the open "
                        f"sidewalk, curb and ADA ramp bids within {DIGEST_RADIUS_MI} miles of "
                        f"<b>{html_escape(rec['place'])}</b>. Each email has a one-click "
                        "unsubscribe.</p><p>Want them now, with the going rate and a target "
                        f"price for each? <a style=\"color:#f59e0b;\" href=\"{SITE_URL}/app.html\">"
                        "Try CurbCall Pro free for 7 days</a>.</p>")


def _digest_week():
    y, w, _ = datetime.date.today().isocalendar()
    return f"{y}-W{w:02d}"


def _digest_open_bids(outcome):
    """[(town, bid)] still open, soonest deadline first, capped."""
    today = datetime.date.today()
    rows = []
    for town, bids in (outcome.get("bids") or {}).items():
        for b in bids or []:
            if not _is_open_bid(b):
                continue
            due = _parse_deadline(b.get("deadline"))
            if due and due < today:
                continue
            rows.append((due or datetime.date.max, town, b))
    rows.sort(key=lambda r: r[0])
    return [(town, b) for _, town, b in rows[:DIGEST_MAX_BIDS]]


def _bid_email_text(email, rec, rows):
    place = rec.get("place") or rec.get("zip")
    lines = [f"Open sidewalk, curb and ADA ramp bids within {DIGEST_RADIUS_MI} miles of "
             f"{place} this week:", ""]
    for town, b in rows:
        line = f"- {b.get('title') or 'Untitled'} ({town})"
        if b.get("deadline"):
            line += f", due {b['deadline']}"
        lines.append(line)
        if b.get("url"):
            lines.append(f"  {b['url']}")
    lines += ["",
              "Want the going rate for each one, a target price from what won, the bid form "
              "filled in, and a phone alert when an addendum drops? Try CurbCall Pro free "
              f"for 7 days: {SITE_URL}/app.html",
              "",
              f"Concrete prices by state: {SITE_URL}/concrete-cost/index.html",
              "",
              "---",
              f"You get this because you signed up for the free weekly bid email for ZIP "
              f"{rec.get('zip')} at curbcallpro.com.",
              MAILING_ADDRESS.strip(),
              f"Unsubscribe: {_unsub_url(email)}"]
    return "\n".join(lines)


def _run_bid_digest(max_areas=None):
    """One batch of this week's emails. Returns {ok, remaining, ...}."""
    if not RESEND_API_KEY:
        return {"ok": False, "reason": "email_not_configured"}
    if not MAILING_ADDRESS.strip():
        return {"ok": False, "reason": "mailing_address_not_set"}
    max_areas = max(1, int(max_areas or DIGEST_AREAS_PER_CALL))
    week = _digest_week()
    sent_log = kv_backend.get(_DIGEST_SENT_KEY, None)
    sent_log = sent_log if isinstance(sent_log, dict) and sent_log.get("week") == week \
        else {"week": week, "done": []}
    done = set(sent_log["done"])
    suppressed = _suppression()

    subs = _digest_subs()
    for email in [e for e in subs if e in suppressed]:
        subs.pop(email)                  # unsubscribed: forget them entirely
    _save_digest_subs(subs)

    areas = {}
    for email, rec in sorted(subs.items()):
        if rec.get("confirmed") and rec.get("zip") and email not in done:
            areas.setdefault(rec["zip"], []).append(email)
    todo = sorted(areas)
    sent = failed = no_bids = 0
    errors = []
    for zip_code in todo[:max_areas]:
        try:
            outcome = _perform_scan(zip_code, DIGEST_RADIUS_MI)
        except Exception as ex:
            outcome = None
            errors.append(f"{zip_code}: {ex}")
            print(f"[digest] scan failed for {zip_code}: {ex}", flush=True)
        rows = _digest_open_bids(outcome) if outcome else []
        for email in areas[zip_code]:
            # Marked done whatever happens, so one bad address or a dead
            # area can't make every later call retry it forever.
            done.add(email)
            if not rows:
                no_bids += 1             # nothing to say is not worth an email
                continue
            unsub = _unsub_url(email)
            ok = _send_email(email,
                             f"{len(rows)} open concrete bid{'s' if len(rows) != 1 else ''} "
                             f"near {subs[email].get('place') or zip_code}",
                             _bid_email_text(email, subs[email], rows),
                             reply_to=SUPPORT_EMAIL or None,
                             headers={"List-Unsubscribe": f"<{unsub}>",
                                      "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
            sent += 1 if ok else 0
            failed += 0 if ok else 1
        sent_log["done"] = sorted(done)
        kv_backend.set(_DIGEST_SENT_KEY, sent_log)
    remaining = len(todo[max_areas:])
    return {"ok": True, "week": week, "areas_done": min(len(todo), max_areas),
            "remaining": remaining, "sent": sent, "failed": failed,
            "no_bids": no_bids, "subscribers": sum(1 for r in subs.values() if r.get("confirmed")),
            "errors": errors}


@app.route("/run-bid-digest", methods=["POST"])
def run_bid_digest():
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("token") or request.headers.get("X-Cron-Secret", "")
    if not CRON_SECRET or not hmac.compare_digest(token, CRON_SECRET):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    result = _run_bid_digest(data.get("max_areas"))
    if result.get("ok") and result.get("remaining") == 0:
        _cron_beat("bid-digest")
    return jsonify(result), (200 if result.get("ok") else 500)
