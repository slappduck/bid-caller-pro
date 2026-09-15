
def _stripe_verify(payload, sig_header):
    """Verify a Stripe webhook signature without the stripe library."""
    if not STRIPE_WEBHOOK_SECRET or not sig_header:
        return False
    try:
        parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
        signed = f"{parts.get('t')}.{payload.decode('utf-8')}".encode("utf-8")
        expected = hmac.new(STRIPE_WEBHOOK_SECRET.encode(), signed,
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, parts.get("v1", ""))
    except Exception:
        return False


BILLING_PORTAL = ("https://billing.stripe.com/p/login/"
                  "3cIcN4an28420Yad2fejK00")

# What a subscriber is owed in writing after they pay, beyond the key.
#
# California's automatic-renewal law wants the renewal terms and the way to
# cancel restated in an acknowledgement AFTER the purchase, not only on the
# page where the buyer clicked. It has no revenue threshold, so it applies
# from the first California customer -- and the states that copied it work the
# same way. The purchase screen already says this; saying it again here is the
# part that was missing.
def _renewal_terms_text(plan):
    price = "$399/year" if plan == "annual" else "$49/month"
    return ("Your subscription renews automatically at "
            f"{price} until you cancel.\n"
            "Cancel any time from the Account screen in the app, or here:\n"
            f"    {BILLING_PORTAL}\n"
            "Cancelling stops future charges; access continues to the end of "
            "the period you have already paid for.")


def _send_key_email(email, key, plan="monthly"):
    """Email the license key to the buyer (only if Resend is configured)."""
    if not email:
        return
    _send_email(email, "Your Bid Caller Pro license key",
                "Thanks for subscribing to Bid Caller Pro!\n\n"
                f"Your license key:\n\n    {key}\n\n"
                "If the app didn't unlock automatically, open it, go to the Plan "
                "tab, paste the key under 'Have a license key?', and tap Activate.\n\n"
                + _renewal_terms_text(plan) + "\n\n"
                + (MAILING_ADDRESS or ""))


# ── Renewal reminders ───────────────────────────────────────────────────────
#
# A subscription of a year or more has to be reminded before it renews --
# California asks for 15 to 45 days' notice, and an annual plan is exactly the
# case the rule exists for: a charge arriving twelve months after the last
# time anyone thought about it.
#
# Driven by Stripe's invoice.upcoming webhook, which needs no Stripe API key --
# this server holds only the webhook secret. Stripe sends it 7 days ahead by
# DEFAULT, which is outside the window the law asks for, so the notice period
# is set in the Stripe dashboard (Settings -> Billing -> Subscriptions ->
# upcoming invoice webhook) and checked here rather than assumed.
RENEWAL_NOTICE_MIN_DAYS = int(os.environ.get("RENEWAL_NOTICE_MIN_DAYS", "15"))
RENEWAL_NOTICE_MAX_DAYS = int(os.environ.get("RENEWAL_NOTICE_MAX_DAYS", "45"))
_RENEWAL_SENT_KEY = "bidcaller:renewal_reminders"


def _days_until(unix_ts):
    try:
        delta = datetime.datetime.fromtimestamp(
            float(unix_ts), datetime.timezone.utc) - datetime.datetime.now(
                datetime.timezone.utc)
        return delta.days
    except Exception:
        return None


def _send_renewal_reminder(email, plan, renews_on, days_out):
    if not email:
        return False
    when = renews_on or "shortly"
    price = "$399" if plan == "annual" else "$49"
    _send_email(
        email,
        "Your CurbCall Pro subscription renews soon",
        f"This is a reminder that your annual CurbCall Pro subscription "
        f"renews on {when} (about {days_out} days from now).\n\n"
        f"You will be charged {price}.\n\n"
        + _renewal_terms_text(plan) + "\n\n"
        "No action is needed if you want to continue.\n\n"
        + (MAILING_ADDRESS or ""))
    return True


def _handle_upcoming_invoice(db, obj):
    """Remind an annual subscriber before the card is charged again.

    Returns a short string for the log and for tests. Monthly plans are not
    reminded: the rule is about terms of a year or more, and a monthly notice
    every month is noise that trains people to ignore the one that matters.
    """
    cust = obj.get("customer") or ""
    info = (db.get("customers", {}) or {}).get(cust)
    if not info:
        return "unknown_customer"
    if (info.get("plan") or "monthly") != "annual":
        return "monthly_no_reminder"

    days_out = _days_until(obj.get("next_payment_attempt")
                           or obj.get("period_end"))
    if days_out is None:
        return "no_date"
    if not (RENEWAL_NOTICE_MIN_DAYS <= days_out <= RENEWAL_NOTICE_MAX_DAYS):
        # Loud: a reminder outside the window is not a reminder that counts,
        # and the cause is a Stripe dashboard setting nobody would think to
        # check. Silence here would look exactly like compliance.
        print(f"[stripe] upcoming invoice {days_out} days out, outside the "
              f"{RENEWAL_NOTICE_MIN_DAYS}-{RENEWAL_NOTICE_MAX_DAYS} day "
              f"notice window -- change the upcoming-invoice webhook timing "
              f"in Stripe", flush=True)
        return "outside_window"

    # Stripe retries webhooks. Two identical warnings read as a billing error.
    invoice_id = obj.get("id") or f"{cust}:{obj.get('next_payment_attempt')}"
    try:
        sent = kv_backend.get(_RENEWAL_SENT_KEY, None) or {}
    except Exception:
        sent = {}
    if invoice_id in sent:
        return "already_sent"

    renews_on = ""
    try:
        renews_on = datetime.datetime.fromtimestamp(
            float(obj.get("next_payment_attempt") or obj.get("period_end")),
            datetime.timezone.utc).strftime("%B %-d, %Y")
    except Exception:
        pass
    _send_renewal_reminder(info.get("email", ""), "annual", renews_on, days_out)
    sent[invoice_id] = int(time.time())
    # Keep the newest few hundred; this only exists to stop a duplicate.
    if len(sent) > 500:
        for k in sorted(sent, key=sent.get)[:len(sent) - 500]:
            sent.pop(k, None)
    try:
        kv_backend.set(_RENEWAL_SENT_KEY, sent)
    except Exception:
        pass
    return "sent"


# ── Admin error alerts: know about a crash before a customer reports it ──
_alert_lock = threading.Lock()
_alert_last_sent = {}
ALERT_COOLDOWN_SEC = 1800  # don't re-alert the same error more than every 30 min


def _alert_admin(subject, detail):
    """Email SUPPORT_EMAIL on server errors (best-effort, never raises).
    Rate-limited per distinct subject so a flapping error doesn't spam."""
    if not SUPPORT_EMAIL:
        return
    now = time.time()
    with _alert_lock:
        last = _alert_last_sent.get(subject, 0)
        if now - last < ALERT_COOLDOWN_SEC:
            return
        _alert_last_sent[subject] = now
    _send_email(SUPPORT_EMAIL, f"[CurbCall Pro] {subject}", detail[:4000])


# ── Are the scheduled jobs actually running? ──
#
# Every alert in this file fires from inside a request Flask completed. The
# federal refresh failed ten consecutive scheduled runs without sending one,
# because its worker was killed at the platform timeout before any handler
# ran -- a dead job is exactly the case the error handler cannot report.
#
# So the jobs check in when they succeed, and this notices when one stops.
# A heartbeat catches what reading the run history would not: a workflow
# disabled, a secret rotated, a schedule quietly dropped, GitHub Actions
# down. Silence is the signal, so silence has to be what is measured.
CRON_HEARTBEAT_KEY = "bidcaller:cron_heartbeats"

# job -> how many hours may pass before it is overdue. Generous: this should
# fire when a job is broken, not when a run was slow, or it gets ignored,
# which is how the last one went unnoticed for three days.
CRON_EXPECTED = {
    "saved-search-alerts": 36,
    "upcoming-alerts": 192,      # weekly
    "bid-audit": 36,
    "federal-refresh": 36,       # twice daily; matches the cache max age
    "trial-reminders": 36,
    "weekly-digest": 192,        # weekly; its absence is the outer alarm
    "purge-expired-consent": 960,  # monthly
}


def _cron_beat(job):
    """Record that `job` finished successfully. Never raises."""
    try:
        beats = kv_backend.get(CRON_HEARTBEAT_KEY, None) or {}
        beats[job] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        kv_backend.set(CRON_HEARTBEAT_KEY, beats)
    except Exception as ex:
        print(f"[cron] heartbeat for {job} failed: {ex}", flush=True)


def _cron_status(record=False):
    """[{job, last, hours, overdue}] for every job we expect to hear from.

    A job that has never reported is not yet late: it may simply not have
    come round. The first check of an unknown job records when it was first
    seen and gives it one full interval to report before it counts as
    overdue -- otherwise the monthly consent purge is "overdue" every day
    for a month after deploy, and a watchdog that cries daily is one nobody
    reads, which is the failure this whole thing exists to prevent.

    `record` is only true for the watchdog itself, so that merely rendering
    the weekly digest cannot start somebody's grace period.
    """
    # An empty record and an unreadable one are different answers. Empty
    # means "nothing has reported yet", which may be fine. Unreadable means
    # the store this depends on is broken, and then nothing can be confirmed
    # about anything -- which is a fault in its own right, not a grace period.
    unreadable = False
    try:
        beats = kv_backend.get(CRON_HEARTBEAT_KEY, None) or {}
    except Exception:
        beats, unreadable = {}, True
    now = datetime.datetime.now(datetime.timezone.utc)
    first_seen = beats.get("_first_seen") or {}
    out, changed = [], False
    for job, max_h in sorted(CRON_EXPECTED.items()):
        last = beats.get(job)
        hours, corrupt = None, False
        if last:
            try:
                hours = round(
                    (now - datetime.datetime.fromisoformat(last))
                    .total_seconds() / 3600.0, 1)
            except Exception:
                # Something reported, and we cannot read when. Never grace:
                # a job with a garbled beat has already run at least once,
                # so there is nothing to wait for.
                last, corrupt = None, True
        if hours is not None:
            overdue = hours > max_h
        elif corrupt or unreadable:
            overdue = True
        else:
            seen = first_seen.get(job)
            if not seen and record:
                first_seen[job] = now.isoformat()
                changed = True
                seen = first_seen[job]
            try:
                waited = ((now - datetime.datetime.fromisoformat(seen))
                          .total_seconds() / 3600.0) if seen else 0.0
            except Exception:
                waited = max_h + 1     # unreadable: assume the worst
            overdue = waited > max_h
        out.append({"job": job, "last": last, "hours": hours,
                    "max_hours": max_h, "overdue": overdue})
    if changed:
        try:
            beats["_first_seen"] = first_seen
            kv_backend.set(CRON_HEARTBEAT_KEY, beats)
        except Exception as ex:
            print(f"[cron] could not record first_seen: {ex}", flush=True)
    return out


def _cron_watchdog():
    """Alert on jobs that have gone quiet. Returns the full status either way."""
    rows = _cron_status(record=True)
    late = [r for r in rows if r["overdue"]]
    if late:
        lines = []
        for r in late:
            when = ("has never reported a success" if r["hours"] is None
                    else "last succeeded %.1fh ago (expected every %dh)"
                         % (r["hours"], r["max_hours"]))
            lines.append("  - %s %s" % (r["job"], when))
        _alert_admin(
            "Scheduled job not reporting: " + late[0]["job"],
            "These jobs have gone quiet:\n" + "\n".join(lines) +
            "\n\nA job that stops running fails silently -- the server only "
            "alerts on errors it can catch inside a request, and a worker "
            "killed at the platform timeout never gets that far.\n\n"
            "Check https://github.com/slappduck/bid-caller-pro/actions")
    return {"ok": True, "jobs": rows, "overdue": [r["job"] for r in late]}


# ── Telling somebody their trial is about to end ──
#
# There was a reminder for people already paying and none for people who had
# not started. A seven-day trial that ends without a word is a cancellation
# nobody had to decide on, and the whole funnel to date has converted zero.
#
# One email, two days out, once. Not a sequence: five a day of hand-written
# outreach is the acquisition channel, and a drip campaign aimed at the
# handful of people who did sign up would be the same mail-merge mistake in
# a different envelope.
APP_URL = os.environ.get("APP_URL", "https://curbcallpro.com/app.html")
TRIAL_NOTICE_DAYS_OUT = int(os.environ.get("TRIAL_NOTICE_DAYS_OUT", "2"))


def _has_paid_licence(db, email):
    """True if this address already holds a live key. Deliberately not
    _license_is_active, which counts an active trial as active -- here that
    would suppress the email for exactly the people it is written for."""
    key = ((db.get("emails") or {}).get((email or "").lower()) or "").strip()
    if not key or key in (db.get("revoked") or []):
        return False
    try:
        return bool(verify_key(key)[0])
    except Exception:
        return False


def _send_trial_ending(email, days_left, ends_on):
    when = "tomorrow" if days_left <= 1 else "in %d days" % days_left
    _send_email(
        email,
        "Your CurbCall Pro trial ends %s" % when,
        "Your free trial ends %s (%s).\n\n"
        "Nothing happens automatically -- there is no card on file, so it "
        "simply stops. If you want to keep the bid feed running, you can "
        "pick a plan from the Account screen:\n\n"
        "    %s\n\n"
        "If it was not useful, no reply is needed. If it was nearly useful, "
        "tell me what was missing and I will read it myself.\n\n"
        "Joshua Hukel\nCurbCall Pro\n%s"
        % (when, ends_on, APP_URL, MAILING_ADDRESS or ""))


def _run_trial_reminders(now=None):
    """One notice per trial, TRIAL_NOTICE_DAYS_OUT before it lapses."""
    now = now or datetime.datetime.now()
    db = _db()
    trials = db.get("trials") or {}
    sent, skipped, changed = [], 0, False
    for rec in trials.values():
        email = (rec.get("email") or "").strip()
        if not email or rec.get("ending_notice_at"):
            skipped += 1
            continue
        try:
            ends = (datetime.datetime.fromisoformat(rec["started"])
                    + datetime.timedelta(days=TRIAL_DAYS))
        except Exception:
            skipped += 1
            continue
        left_days = (ends - now).total_seconds() / 86400.0
        if left_days <= 0 or left_days > TRIAL_NOTICE_DAYS_OUT:
            skipped += 1
            continue
        if _has_paid_licence(db, email):
            skipped += 1
            continue
        try:
            _send_trial_ending(email, max(1, int(left_days) + 1),
                               ends.isoformat()[:10])
        except Exception as ex:
            print(f"[trial] reminder failed for {email}: {ex}", flush=True)
            continue
        # Written whether or not the send is later found to have bounced:
        # a duplicate "your trial ends" is worse than a missed one.
        rec["ending_notice_at"] = now.isoformat()
        changed = True
        sent.append(email)
    if changed:
        _save_db(db)
    return {"ok": True, "sent": len(sent), "skipped": skipped}


@app.route("/run-trial-reminders", methods=["POST"])
def run_trial_reminders():
    """Daily. Emails trials that lapse within TRIAL_NOTICE_DAYS_OUT."""
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("token") or request.headers.get("X-Cron-Secret", "")
    if not CRON_SECRET or not hmac.compare_digest(token, CRON_SECRET):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    result = _run_trial_reminders()
    if result.get("ok"):
        _cron_beat("trial-reminders")
    return jsonify(result), (200 if result.get("ok") else 500)


# ── The weekly note about how the business is actually doing ──
#
# _bi_summary, _funnel_summary, _engagement and _wins_summary were all built
# and then left where somebody had to remember to go and look at them, which
# is the same as not having them. Numbers nobody reads do not inform anything.
#
# This is also the outer dead-man's-switch. The watchdog reports only when
# something is wrong, so a watchdog that has itself stopped is indistinguishable
# from a quiet week. A mail that is supposed to arrive every Monday is a
# failure you notice by its absence, which is the one kind of monitoring that
# does not need monitoring of its own.
def _digest_sections():
    """[(heading, {k: v})]. Every source wrapped: one bad summary must not
    cost the whole mail, since the weeks it breaks are the interesting ones."""
    out = []
    for heading, fn in (("Funnel", _funnel_summary),
                        ("Lifecycle", _bi_summary),
                        ("Engagement", _engagement),
                        ("Outcomes", _wins_summary)):
        try:
            data = fn()
        except Exception as ex:
            out.append((heading, {"unavailable": type(ex).__name__}))
            continue
        out.append((heading, data if isinstance(data, dict) else {"value": data}))
    return out


def _digest_text(sections, crons):
    lines = ["CurbCall Pro — week to %s"
             % datetime.date.today().isoformat(), ""]
    for heading, data in sections:
        lines.append(heading)
        if not data:
            lines.append("  (nothing recorded)")
        for k, v in data.items():
            if isinstance(v, dict):
                v = ", ".join("%s %s" % (a, b) for a, b in v.items())
            lines.append("  %-28s %s" % (k, "-" if v is None else v))
        lines.append("")
    late = [c for c in crons if c["overdue"]]
    lines.append("Scheduled jobs")
    if late:
        for c in late:
            lines.append("  OVERDUE  %-24s %s" % (
                c["job"], "never" if c["hours"] is None
                else "%.1fh ago" % c["hours"]))
    else:
        lines.append("  all %d reporting" % len(crons))
    lines.append("")
    lines.append("This arrives every Monday. If it stops, something is wrong "
                 "with the scheduler itself.")
    return "\n".join(lines)


def _run_weekly_digest():
    sections = _digest_sections()
    crons = _cron_status()
    text = _digest_text(sections, crons)
    if not SUPPORT_EMAIL:
        return {"ok": False, "reason": "no_support_email"}
    _send_email(SUPPORT_EMAIL, "[CurbCall Pro] Weekly numbers", text)
    return {"ok": True, "sections": len(sections),
            "overdue": [c["job"] for c in crons if c["overdue"]]}


@app.route("/run-weekly-digest", methods=["POST"])
def run_weekly_digest():
    """Weekly. The numbers, plus whether the schedulers are alive."""
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("token") or request.headers.get("X-Cron-Secret", "")
    if not CRON_SECRET or not hmac.compare_digest(token, CRON_SECRET):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    result = _run_weekly_digest()
    if result.get("ok"):
        _cron_beat("weekly-digest")
    return jsonify(result), (200 if result.get("ok") else 500)


@app.route("/run-cron-watchdog", methods=["POST"])
def run_cron_watchdog():
    """Daily. Emails only when a scheduled job has gone quiet."""
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("token") or request.headers.get("X-Cron-Secret", "")
    if not CRON_SECRET or not hmac.compare_digest(token, CRON_SECRET):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    return jsonify(_cron_watchdog()), 200

