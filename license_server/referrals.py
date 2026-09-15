
# ── Referrals: give-a-month-get-a-month ──
# client_reference_id already carries the buyer's device id (see index.html /
# app.html's checkout-link tagging); a pending referral is appended after
# this separator rather than sent as a second Stripe field, so no Stripe-side
# configuration changes were needed to add this. Keep this string identical
# to REFERRAL_SEP in app.html/index.html.
_REFERRAL_SEP = "~ref~"
REFERRAL_BONUS_DAYS_REFERRER = 30
REFERRAL_BONUS_DAYS_REFERRED = 14


def _referral_code_for(db, email):
    """Get-or-create this email's referral code. One code per email, so
    sharing the link twice doesn't mint two codes."""
    email = (email or "").strip().lower()
    if not email:
        return None
    owner_idx = db.setdefault("referral_owner", {})
    codes = db.setdefault("referral_codes", {})
    existing = owner_idx.get(email)
    if existing in codes:
        return existing
    code = secrets.token_hex(4).upper()
    while code in codes:
        code = secrets.token_hex(4).upper()
    codes[code] = {"email": email, "redemptions": 0}
    owner_idx[email] = code
    return code


def _grant_bonus_days(db, email, device, days):
    """Extend a user's plan by `days`, stacking on top of their current
    expiration (or from now, if they have none) rather than overwriting it
    -- an active subscriber shouldn't lose paid time to a bonus."""
    base = datetime.datetime.now()
    plan = "monthly"
    existing_key = (db.get("emails", {}).get((email or "").lower()) if email else None) \
        or (db.get("devices", {}).get(device) if device else None)
    if existing_key:
        info = db.get("issued", {}).get(existing_key)
        if info:
            plan = info.get("plan") or plan
            try:
                cur_exp = datetime.datetime.fromisoformat(info["expires"])
                if cur_exp > base:
                    base = cur_exp
            except (KeyError, ValueError):
                pass
    key, exp = _make_key_with_expiry(plan, base + datetime.timedelta(days=days))
    db.setdefault("issued", {})[key] = {
        "plan": plan, "expires": exp[:10], "email": email or "", "device": device or "",
        "updated": datetime.datetime.now().isoformat()[:10],
    }
    if email:
        db.setdefault("emails", {})[email.lower()] = key
    if device:
        db.setdefault("devices", {})[device] = key
    return key


def _apply_referral_reward(db, ref_code, referred_email, referred_device):
    """Both sides get bonus days once, the first time a referred signup
    completes checkout. Redemption is keyed by the REFERRED person's own
    identity, not the code, so the same new customer can't re-claim by
    reusing a link (or their own) more than once."""
    redeemed = db.setdefault("referral_redeemed", [])
    ident = (referred_email or "").lower() or referred_device
    if not ident or ident in redeemed:
        return
    info = db.get("referral_codes", {}).get(ref_code)
    if not info:
        return
    referrer_email = info.get("email", "")
    if referrer_email and referrer_email == (referred_email or "").lower():
        return  # no self-referral
    redeemed.append(ident)
    info["redemptions"] = info.get("redemptions", 0) + 1
    _grant_bonus_days(db, referrer_email, "", REFERRAL_BONUS_DAYS_REFERRER)
    _grant_bonus_days(db, referred_email, referred_device, REFERRAL_BONUS_DAYS_REFERRED)
    if referrer_email:
        _send_referral_email(referrer_email, REFERRAL_BONUS_DAYS_REFERRER)
    print(f"[referral] {ref_code} redeemed by {ident}", flush=True)


def _send_referral_email(email, days):
    if not email:
        return
    _send_email(email, "You earned a free month on Bid Caller Pro",
                "Someone you referred just subscribed to Bid Caller Pro -- "
                f"we've added {days} free days to your plan. Thanks for "
                "spreading the word!")


@app.route("/referral/code", methods=["POST"])
def referral_code():
    """Signed-in users only -- a referral link is tied to an email so the
    reward has somewhere to land, and Account/Settings (where this is
    surfaced) already requires being signed in."""
    data = request.get_json(force=True, silent=True) or {}
    email = _verify_supabase_token(data.get("supabase_token", ""))
    if not email:
        return jsonify({"ok": False, "reason": "sign_in_required"}), 401
    db = _db()
    code = _referral_code_for(db, email)
    _save_db(db)
    return jsonify({"ok": True, "code": code})


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    if not _stripe_verify(payload, request.headers.get("Stripe-Signature", "")):
        return jsonify({"ok": False, "reason": "bad_signature"}), 400
    try:
        event = json.loads(payload.decode("utf-8"))
    except Exception:
        return jsonify({"ok": False}), 400

    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    db = _db()

    if etype == "checkout.session.completed":
        email = ((obj.get("customer_details") or {}).get("email")
                 or obj.get("customer_email") or "")
        raw_ref = obj.get("client_reference_id") or ""
        device, _, ref_code = raw_ref.partition(_REFERRAL_SEP)
        cust = obj.get("customer") or ""
        amount = obj.get("amount_total") or 0
        plan = "annual" if amount and amount >= 10000 else "monthly"
        key = _issue_for(db, email, device, plan)
        if cust:
            db.setdefault("customers", {})[cust] = {
                "email": email, "device": device, "plan": plan}
        if ref_code:
            _apply_referral_reward(db, ref_code, email, device)
        _save_db(db)
        _bi_note(db, "subscribed", email, plan)
        _save_db(db)
        _send_key_email(email, key, plan)
        print(f"[stripe] issued {plan} key for {email or device}", flush=True)

    elif etype == "invoice.paid":
        cust = obj.get("customer") or ""
        info = db.get("customers", {}).get(cust)
        if info:
            _issue_for(db, info.get("email", ""), info.get("device", ""),
                       info.get("plan", "monthly"))
            _bi_note(db, "renewed", info.get("email", ""),
                     info.get("plan", "monthly"))
            _save_db(db)
            print(f"[stripe] renewed for {cust}", flush=True)

    elif etype == "invoice.upcoming":
        outcome = _handle_upcoming_invoice(db, obj)
        print(f"[stripe] upcoming invoice: {outcome}", flush=True)

    elif etype in ("customer.subscription.deleted", "customer.subscription.paused"):
        cust = obj.get("customer") or ""
        info = db.get("customers", {}).get(cust)
        if info:
            email = (info.get("email") or "").lower()
            key = db.get("emails", {}).get(email)
            if key and key not in db.setdefault("revoked", []):
                db["revoked"].append(key)
            # The date, the plan and which of the two Stripe events it was.
            # A flat list of dead keys answered none of that.
            _bi_note(db, "churned", email, info.get("plan", ""),
                     {"reason": etype.rsplit(".", 1)[-1]})
            _save_db(db)
            print(f"[stripe] revoked for {cust}", flush=True)

    return jsonify({"ok": True})


@app.route("/mykey", methods=["POST"])
def mykey():
    """App calls this to auto-unlock after a purchase.

    Resolves by device id first, then by the signed-in account's email. The
    email path matters because a checkout started from the marketing site
    carries no device id at all -- those Stripe links can't know one -- so the
    webhook recorded no device mapping and this endpoint always answered
    "no_key". The buyer's Account tab then showed "Trial expired, subscribe
    below" to someone who had just paid, which invites a second subscription.
    It also covers paying on a laptop and then signing in on a phone.
    """
    data = request.get_json(force=True, silent=True) or {}
    device = (data.get("device_id") or "").strip()
    db = _db()
    key = db.get("devices", {}).get(device) if device else None
    matched_by_email = False
    if not key:
        email = _verify_supabase_token(data.get("supabase_token", ""))
        if email:
            key = db.get("emails", {}).get(email.lower())
            matched_by_email = bool(key)
    if not key:
        return jsonify({"ok": False, "reason": "no_key"})
    valid, plan, exp, _ = verify_key(key)
    if not valid or key in db.get("revoked", []):
        return jsonify({"ok": False, "reason": "inactive"})
    if matched_by_email and device:
        # Remember the device so later calls take the fast path.
        db.setdefault("devices", {})[device] = key
        _save_db(db)
    elif device:
        # Matched by DEVICE. File the key under the signed-in account too, so
        # the next device this person uses can find it.
        #
        # A key is filed under the address used at Stripe. Those are the same
        # string until somebody pays with a personal card that autofills a
        # different one -- and then the phone they bought on works (this
        # device mapping) while their laptop does not: unknown device,
        # mismatched email, both miss, and a paying customer is told the trial
        # has expired. The account email is taken from a verified Supabase
        # token, never from the request body, so this cannot be used to claim
        # somebody else's licence.
        acct = _verify_supabase_token(data.get("supabase_token", ""))
        if acct:
            emails = db.setdefault("emails", {})
            existing = emails.get(acct.lower())
            # Do not overwrite a mapping that still works -- that address may
            # belong to a different, live licence. Replacing a dead one is the
            # point: an expired key is exactly what a new purchase replaces.
            if existing != key:
                prior_ok = bool(existing) and verify_key(existing)[0] and \
                    existing not in db.get("revoked", [])
                if not prior_ok:
                    emails[acct.lower()] = key
                    _save_db(db)
    return jsonify({"ok": True, "key": key, "plan": plan, "expires": exp[:10]})


# ═══════════════════════════════════════════════════════════
# OUTBOUND EMAIL CAMPAIGNS (admin-only)
# ═══════════════════════════════════════════════════════════
# Commercial email to a list you supply. CAN-SPAM is not optional and the
# rules are cheap to follow, so they are enforced here rather than left to
# whoever writes the campaign text:
#
#   * a real physical postal address must appear in every message -- set
#     MAILING_ADDRESS or this endpoint refuses to send at all, because an
#     accidentally non-compliant blast cannot be un-sent;
#   * every message carries a working one-click unsubscribe, both as a link
#     and as the List-Unsubscribe header mail clients surface themselves;
#   * an unsubscribe is honoured immediately and permanently, and is checked
#     before every send;
#   * the From address and subject are whatever you set -- keep them honest,
#     deceptive headers and subject lines are the part that actually gets
#     people fined.
#
# Deliberately NOT included: any way to harvest recipients. The list is
# supplied per request and never derived from scan data or permit records.
MAILING_ADDRESS = os.environ.get("MAILING_ADDRESS", "")
# Where the unsubscribe link points. Must be this server, since that's what
# serves /unsubscribe -- not the Netlify site.
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://bid-caller-pro.onrender.com").rstrip("/")
CAMPAIGN_MAX_PER_REQUEST = int(os.environ.get("CAMPAIGN_MAX_PER_REQUEST", "200"))
CAMPAIGN_PAUSE_SEC = float(os.environ.get("CAMPAIGN_PAUSE_SEC", "0.35"))
_SUPPRESSION_KEY = "bidcaller:email_suppression"
_DRAFTS_KEY = "bidcaller:campaign_drafts"
# A draft that's been sitting for a day is probably forgotten, and approving
# a forgotten campaign is how the wrong thing goes out. They expire.
DRAFT_TTL_HOURS = int(os.environ.get("CAMPAIGN_DRAFT_TTL_HOURS", "24"))


# Merge fields, written {{like_this}}. A campaign that cannot say "3 open
# jobs within 125 miles of Grimes, the nearest 8 miles out and closing
# September 2" is a generic pitch; one that can is a demonstration. The
# numbers come from a real scan of the recipient's own market, so the claim
# is checkable by the person reading it.
_MERGE_FIELD_RE = re.compile(r"\{\{\s*([a-z0-9_]{1,40})\s*\}\}", re.I)
# A merge value is a few lines of plain text, never a payload. Roomy enough
# for a short formatted list -- a campaign that names three solicitations
# with dates and distances needs about 260 characters for that one field --
# and still far too small to smuggle anything into a recipient's inbox.
MERGE_VALUE_MAX = int(os.environ.get("CAMPAIGN_MERGE_VALUE_MAX", "600"))


def _merge_fields(body):
    """The field names a body asks for, lowercased."""
    return {m.group(1).lower() for m in _MERGE_FIELD_RE.finditer(str(body or ""))}


def _render_body(body, variables):
    """Substitute {{fields}}. Every field must be present -- callers check
    with _missing_merge_fields first, and this asserts the same thing rather
    than quietly shipping a literal {{city}} to a stranger."""
    variables = {k.lower(): str(v) for k, v in (variables or {}).items()}

    def sub(m):
        key = m.group(1).lower()
        if key not in variables:
            raise KeyError(key)
        return variables[key][:MERGE_VALUE_MAX]

    return _MERGE_FIELD_RE.sub(sub, str(body or ""))


def _missing_merge_fields(body, recipients):
    """[(email, [missing field, ...]), ...] for recipients that cannot be
    rendered. Checked at DRAFT time, which is the reviewable step: a
    half-merged blast cannot be recalled, and "Hi {{company}}" reaching a
    real contractor is worse than not sending at all."""
    wanted = _merge_fields(body)
    if not wanted:
        return []
    out = []
    for addr, variables in recipients:
        have = {k.lower() for k, v in (variables or {}).items()
                if str(v or "").strip()}
        gap = sorted(wanted - have)
        if gap:
            out.append((addr, gap))
    return out


def _render_campaign(body, addr, variables=None):
    """The exact text a recipient receives -- one function so the preview
    shown at draft time cannot drift from what approval actually sends."""
    return (f"{_render_body(body, variables)}\n\n---\n{MAILING_ADDRESS.strip()}\n"
            f"Don't want these? Unsubscribe: {_unsub_url(addr)}")


def _clean_recipients(recipients):
    """([(email, vars), ...], skipped_unsubscribed, over_limit). Deduped
    case-insensitively, unsubscribes dropped, batch capped.

    A recipient is either a bare address or {"email": ..., "vars": {...}}, so
    an existing caller passing a flat list keeps working unchanged.
    """
    suppressed = _suppression()
    seen, queue, skipped = set(), [], 0
    for raw in recipients or []:
        if isinstance(raw, dict):
            addr = str(raw.get("email") or "").strip().lower()
            variables = raw.get("vars") if isinstance(raw.get("vars"), dict) else {}
        else:
            addr, variables = str(raw or "").strip().lower(), {}
        if not addr or "@" not in addr or addr in seen:
            continue
        seen.add(addr)
        if addr in suppressed:
            skipped += 1
            continue
        queue.append((addr, variables))
    over = max(0, len(queue) - CAMPAIGN_MAX_PER_REQUEST)
    return queue[:CAMPAIGN_MAX_PER_REQUEST], skipped, over


def _drafts():
    """Pending campaigns, with expired ones already dropped."""
    got = kv_backend.get(_DRAFTS_KEY, None)
    if not isinstance(got, dict):
        return {}
    cutoff = datetime.datetime.now() - datetime.timedelta(hours=DRAFT_TTL_HOURS)
    live = {}
    for k, v in got.items():
        try:
            if datetime.datetime.fromisoformat(v.get("created_at", "")) >= cutoff:
                live[k] = v
        except (TypeError, ValueError):
            continue  # unparseable timestamp -> treat as expired, never as sendable
    return live


def _save_drafts(drafts):
    kv_backend.set(_DRAFTS_KEY, drafts)


def _suppression():
    got = kv_backend.get(_SUPPRESSION_KEY, None)
    return set(got) if isinstance(got, list) else set()


def _suppress(email):
    email = (email or "").strip().lower()
    if not email:
        return False
    current = _suppression()
    if email in current:
        return True
    current.add(email)
    kv_backend.set(_SUPPRESSION_KEY, sorted(current))
    return True


def _unsub_token(email):
    """Signed so an unsubscribe link needs no stored state and cannot be used
    to unsubscribe somebody else by editing the address in the URL."""
    return hmac.new(LICENSE_SECRET.encode(), (email or "").strip().lower().encode(),
                    hashlib.sha256).hexdigest()[:32]


def _unsub_url(email):
    qs = urllib.parse.urlencode({"e": email, "t": _unsub_token(email)})
    return f"{PUBLIC_BASE_URL}/unsubscribe?{qs}"


@app.route("/unsubscribe", methods=["GET"])
def unsubscribe():
    """Public and GET on purpose -- this is the link in the email, and it has
    to work in one click from any mail client with no login and no form."""
    email = (request.args.get("e") or "").strip().lower()
    token = (request.args.get("t") or "").strip()
    page = ("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<body style=\"background:#0d0f18;color:#f1f5f9;font-family:system-ui,sans-serif;"
            "padding:3rem 1.5rem;max-width:32rem;margin:0 auto;\">{}</body>")
    if not email or not hmac.compare_digest(token, _unsub_token(email)):
        return page.format("<h2>That link isn't valid.</h2><p>If you're still getting mail "
                           f"you didn't ask for, reply to it or write to {SUPPORT_EMAIL} "
                           "and we'll take you off by hand.</p>"), 400
    _suppress(email)
    print(f"[campaign] unsubscribed {email}", flush=True)
    return page.format("<h2>You're unsubscribed.</h2><p>We won't email "
                       f"<b>{email}</b> again. Nothing else is needed.</p>")


@app.route("/campaign/send", methods=["POST"])
def campaign_send():
    """Prepare a campaign for a caller-supplied list. SENDS NOTHING.

    Despite the route name this only ever builds a draft and returns the
    exact message for review -- kept as /campaign/send deliberately, so that
    there is no path in this app named "send" that actually mails anybody
    without a separate approval. Sending is /campaign/approve.
    """
    data = request.get_json(force=True, silent=True) or {}
    if not _admin_configured():
        return jsonify({"ok": False, "reason": "admin_not_configured"}), 503
    if not _admin_ok(data.get("admin_token")):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    if not RESEND_API_KEY:
        return jsonify({"ok": False, "reason": "email_unavailable"}), 503
    # A physical address is a legal requirement for commercial email, and an
    # unlawful blast cannot be recalled -- so refuse rather than send.
    if not MAILING_ADDRESS.strip():
        return jsonify({"ok": False, "reason": "mailing_address_not_configured",
                        "detail": "Set MAILING_ADDRESS (a real postal address) before "
                                  "sending commercial email -- CAN-SPAM requires it in "
                                  "every message."}), 503

    subject = (data.get("subject") or "").strip()
    body = (data.get("body") or "").strip()
    recipients = data.get("recipients") or []
    if not subject or not body:
        return jsonify({"ok": False, "reason": "subject_and_body_required"}), 400
    if not isinstance(recipients, list) or not recipients:
        return jsonify({"ok": False, "reason": "no_recipients"}), 400

    queue, skipped, over = _clean_recipients(recipients)
    # Refuse the whole draft if any recipient is missing a field the body
    # asks for. Rejecting just those recipients would be friendlier and
    # wrong: the usual cause is a column named differently from the
    # placeholder, which silently halves the send. Fail here, where it is
    # still reviewable, rather than mailing "Hi {{company}}" to a stranger.
    gaps = _missing_merge_fields(body, queue)
    if gaps:
        return jsonify({
            "ok": False, "reason": "missing_merge_fields", "sent": 0,
            "detail": "Nothing was drafted. Every recipient must supply every "
                      "{{field}} the body uses.",
            "fields_used": sorted(_merge_fields(body)),
            "recipients_missing": [{"email": a, "missing": m}
                                   for a, m in gaps[:20]],
            "recipients_missing_total": len(gaps),
        }), 400

    draft_id = secrets.token_hex(6)
    drafts = _drafts()
    drafts[draft_id] = {
        "subject": subject, "body": body,
        # Stored as [email, vars] pairs. json round-trips these as lists.
        "recipients": [[a, v] for a, v in queue],
        "skipped_unsubscribed": skipped, "over_limit_not_sent": over,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    _save_drafts(drafts)
    print(f"[campaign] drafted {draft_id}: {len(queue)} recipient(s), nothing sent",
          flush=True)
    return jsonify({
        "ok": True, "status": "awaiting_approval", "sent": 0,
        "draft_id": draft_id,
        "would_send": len(queue), "skipped_unsubscribed": skipped,
        "over_limit_not_sent": over, "sample": [a for a, _ in queue[:5]],
        "merge_fields": sorted(_merge_fields(body)),
        # The full rendered message, footer and all -- what you approve is
        # exactly what goes out, not a summary of it. Merged for the FIRST
        # recipient, so a preview of a personalised campaign shows the real
        # numbers rather than the placeholders.
        "preview": _render_campaign(body, queue[0][0], queue[0][1]) if queue else "",
        "preview_for": queue[0][0] if queue else "",
        "expires_in_hours": DRAFT_TTL_HOURS,
        "next": ("Nothing has been sent. Review 'preview', then POST "
                 "/campaign/approve with this draft_id and confirm: true."),
    })


@app.route("/campaign/approve", methods=["POST"])
def campaign_approve():
    """The only endpoint in the app that actually sends a campaign.

    Separate from drafting on purpose: a cold-email blast cannot be
    recalled, so it takes a second, deliberate call naming the exact draft
    -- a fat-fingered request can create a draft, but it cannot mail
    anybody. `confirm: true` has to be passed explicitly as well, so no
    single mistyped field is the difference between reviewing and sending.
    """
    data = request.get_json(force=True, silent=True) or {}
    if not _admin_configured():
        return jsonify({"ok": False, "reason": "admin_not_configured"}), 503
    if not _admin_ok(data.get("admin_token")):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    if not RESEND_API_KEY:
        return jsonify({"ok": False, "reason": "email_unavailable"}), 503
    if not MAILING_ADDRESS.strip():
        return jsonify({"ok": False, "reason": "mailing_address_not_configured"}), 503
    if data.get("confirm") is not True:
        return jsonify({"ok": False, "reason": "confirmation_required",
                        "detail": "Pass confirm: true to send. Nothing was sent."}), 400

    draft_id = (data.get("draft_id") or "").strip()
    drafts = _drafts()
    draft = drafts.get(draft_id)
    if not draft:
        # Also covers a draft that aged out -- see _drafts().
        return jsonify({"ok": False, "reason": "unknown_or_expired_draft",
                        "detail": "Nothing was sent. Draft it again to get a fresh id."}), 404

    # Re-check suppression at approval time, not just at draft time: someone
    # may have unsubscribed in between, and the draft could be hours old.
    stored = draft.get("recipients") or []
    queue, skipped_now, _ = _clean_recipients(
        [{"email": r[0], "vars": r[1]} if isinstance(r, (list, tuple)) else r
         for r in stored])
    # Checked again here, not only at draft time. The draft may be hours old
    # and the body is re-read from storage, so this is the last point at
    # which an unrenderable message can still be stopped.
    gaps = _missing_merge_fields(draft.get("body"), queue)
    if gaps:
        return jsonify({"ok": False, "reason": "missing_merge_fields", "sent": 0,
                        "detail": "Nothing was sent. Re-draft with the missing "
                                  "fields supplied.",
                        "recipients_missing": [{"email": a, "missing": m}
                                               for a, m in gaps[:20]]}), 400

    sent, failed = 0, 0
    for addr, variables in queue:
        unsub = _unsub_url(addr)
        ok = _send_email(addr, draft["subject"],
                         _render_campaign(draft["body"], addr, variables),
                         headers={"List-Unsubscribe": f"<{unsub}>",
                                  "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
        sent += 1 if ok else 0
        failed += 0 if ok else 1
        time.sleep(CAMPAIGN_PAUSE_SEC)  # don't burst a provider into rate-limiting us

    # Consume the draft either way, so an approval can't be replayed into a
    # second copy of the same campaign landing in everyone's inbox.
    drafts.pop(draft_id, None)
    _save_drafts(drafts)
    print(f"[campaign] approved {draft_id}: sent {sent}, failed {failed}, "
          f"skipped {skipped_now}", flush=True)
    return jsonify({"ok": True, "status": "sent", "draft_id": draft_id,
                    "sent": sent, "failed": failed,
                    "skipped_unsubscribed": skipped_now})


@app.route("/campaign/drafts", methods=["POST"])
def campaign_drafts():
    """What's waiting for approval. Admin token required."""
    data = request.get_json(force=True, silent=True) or {}
    if not _admin_configured():
        return jsonify({"ok": False, "reason": "admin_not_configured"}), 503
    if not _admin_ok(data.get("admin_token")):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    discard = (data.get("discard") or "").strip()
    drafts = _drafts()
    if discard:
        drafts.pop(discard, None)
        _save_drafts(drafts)
    return jsonify({"ok": True, "drafts": [
        {"draft_id": k, "subject": v.get("subject", ""),
         "recipients": len(v.get("recipients") or []),
         "created_at": v.get("created_at", "")}
        for k, v in sorted(drafts.items(), key=lambda kv: kv[1].get("created_at", ""))
    ]})


@app.route("/campaign/suppression", methods=["POST"])
def campaign_suppression():
    """Read or add to the do-not-email list. Admin token required."""
    data = request.get_json(force=True, silent=True) or {}
    if not _admin_configured():
        return jsonify({"ok": False, "reason": "admin_not_configured"}), 503
    if not _admin_ok(data.get("admin_token")):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    for addr in (data.get("add") or []):
        _suppress(addr)
    current = sorted(_suppression())
    return jsonify({"ok": True, "count": len(current), "suppressed": current})


