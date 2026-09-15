# ═══════════════════════════════════════════════════════════
# PAYMENTS: Stripe webhook -> auto-issue keys (survives restarts)
# ═══════════════════════════════════════════════════════════
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "Bid Caller Pro <onboarding@resend.dev>")
# Shown to customers -- "write to {SUPPORT_EMAIL}" -- so the default must be
# an address on the product's own domain, not a personal one. The old default
# was a private Gmail, which reads as a side project to anyone who sees it and
# meant a missing env var quietly published it. support@curbcallpro.com is
# live on Cloudflare Email Routing and forwards to the same inbox.
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "support@curbcallpro.com")

# ── Email delivery tracking ──
# Resend accepting the request doesn't mean the send worked. FROM_EMAIL's
# default, onboarding@resend.dev, is Resend's own sandbox address -- it can
# only deliver to the account's own verified email, so a domain that was
# never verified fails every real send while /health's "resend_email: true"
# (an API key is merely present) says everything's fine. That failure was
# invisible from every caller's side: /support just told the customer
# "couldn't send", key delivery silently never arrived, and _alert_admin --
# the thing meant to notice problems -- failed the exact same way with
# nobody watching. Tracked the same way _tavily_note/_tavily_health already
# track search failures, through the one function that actually calls
# Resend, so a systematic failure shows up in /health instead of needing a
# live test against production to find (see tests/test_licensing_gate.py).
_email_lock = threading.Lock()
_email_state = {"ok": 0, "failed": 0, "last_error": "", "last_status": 0}


def _email_note(ok, status=0, detail=""):
    with _email_lock:
        if ok:
            _email_state["ok"] += 1
        else:
            _email_state["failed"] += 1
            _email_state["last_status"] = status
            _email_state["last_error"] = (detail or "")[:200]


def _email_health():
    with _email_lock:
        st = dict(_email_state)
    total = st["ok"] + st["failed"]
    st["failing"] = total > 0 and st["ok"] == 0 and st["failed"] > 0
    return st


def _send_email(to, subject, text, reply_to=None, headers=None):
    """The one place that actually calls Resend. Every caller below is
    best-effort (a support message, a key delivery, an admin alert, a
    referral notice), so this never raises -- it reports through
    _email_note/_email_health instead."""
    if not RESEND_API_KEY:
        return False
    payload = {"from": FROM_EMAIL, "to": [to], "subject": subject, "text": text}
    if reply_to:
        payload["reply_to"] = reply_to
    if headers:
        payload["headers"] = headers
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=json.dumps(payload).encode("utf-8"),
        method="POST", headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                                "Content-Type": "application/json",
                                # Resend's API sits behind Cloudflare, which was
                                # answering 403 "error code: 1010" -- its
                                # ban-by-browser-signature response -- to
                                # urllib's default Python-urllib/3.x agent. That
                                # is what actually broke /support in production
                                # (NOT an unverified sending domain, the first
                                # theory). Same lesson this codebase already
                                # learned for DuckDuckGo and BidNet Direct: an
                                # honest, identifiable agent string gets through
                                # where the bare library default does not.
                                "User-Agent": "BidCallerPro/1.0 (+https://curbcallpro.netlify.app)",
                                "Accept": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
        _email_note(True)
        return True
    except urllib.error.HTTPError as ex:
        try:
            detail = ex.read().decode("utf-8", "ignore")
        except Exception:
            detail = ""
        _email_note(False, ex.code, detail or str(ex))
        print(f"[email] send to {to} failed: {ex.code} {detail}", flush=True)
        return False
    except Exception as ex:
        _email_note(False, 0, str(ex))
        print(f"[email] send to {to} failed: {ex}", flush=True)
        return False



# ── Delivery feedback from Resend ───────────────────────────────────────────
# Sending to an address that no longer exists is how a domain's reputation
# dies: mailbox providers read repeated hard bounces and spam complaints as
# evidence the sender does not maintain a list, and start filing everything
# from that domain in junk -- including the trial keys and bid alerts real
# customers are waiting on. The list has to clean itself.
#
# Two events matter and they are treated differently:
#
#   email.bounced     suppress only a PERMANENT bounce. A transient one is a
#                     full mailbox or greylisting, and retiring a good address
#                     over a temporary condition loses a real prospect.
#   email.complained  suppress always, immediately. Somebody pressed "this is
#                     spam"; there is no reading of that which permits another
#                     message, and it is the single most damaging signal a
#                     sender can accumulate.
RESEND_WEBHOOK_SECRET = _env_secret("RESEND_WEBHOOK_SECRET", "")
# Replay window for a signed webhook, in seconds. Svix's own default.
WEBHOOK_TOLERANCE_SEC = int(os.environ.get("WEBHOOK_TOLERANCE_SEC", "300"))
_EMAIL_EVENTS_KEY = "bidcaller:email_events"


def _svix_signature_ok(secret, msg_id, timestamp, raw_body, header):
    """Verify a Svix-signed webhook, which is what Resend sends.

    Signed content is "{id}.{timestamp}.{body}", HMAC-SHA256 under the
    base64 secret, and the header carries a space-separated list of
    "v1,<sig>" so a secret can be rotated without dropping deliveries.

    Verification is not optional here. This endpoint writes to the
    suppression list, so an unauthenticated version would let anyone
    permanently silence any address we mail -- including every prospect at
    once, quietly, with no error anywhere.
    """
    if not secret or not msg_id or not timestamp or not header:
        return False
    try:
        age = abs(time.time() - int(timestamp))
    except (TypeError, ValueError):
        return False
    if age > WEBHOOK_TOLERANCE_SEC:
        return False       # replay of an old, legitimately-signed delivery
    key = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
    try:
        key_bytes = base64.b64decode(key)
    except Exception:
        return False
    signed = f"{msg_id}.{timestamp}.".encode() + raw_body
    expected = base64.b64encode(
        hmac.new(key_bytes, signed, hashlib.sha256).digest()).decode()
    for part in str(header).split():
        _, _, supplied = part.partition(",")
        if supplied and hmac.compare_digest(supplied, expected):
            return True
    return False


def _record_email_event(kind):
    counts = kv_backend.get(_EMAIL_EVENTS_KEY, None)
    counts = counts if isinstance(counts, dict) else {}
    counts[kind] = int(counts.get(kind, 0)) + 1
    counts["last_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    kv_backend.set(_EMAIL_EVENTS_KEY, counts)


def _is_permanent_bounce(data):
    """Resend reports the class on the bounce object. Anything not clearly
    permanent is left alone -- a full mailbox empties."""
    bounce = data.get("bounce") if isinstance(data.get("bounce"), dict) else {}
    blob = " ".join(str(bounce.get(k) or "") for k in
                    ("type", "subType", "sub_type", "message")).lower()
    if "transient" in blob or "temporary" in blob or "soft" in blob:
        return False
    return "permanent" in blob or "hard" in blob or "suppressed" in blob


@app.route("/webhooks/resend", methods=["POST"])
def resend_webhook():
    """Bounces and spam complaints, straight onto the do-not-email list.

    Answers 200 to anything correctly signed, including events it does not
    act on: a non-2xx tells Resend to retry, and retrying an event we simply
    do not care about accomplishes nothing but noise.
    """
    if not RESEND_WEBHOOK_SECRET:
        # Refusing beats accepting unsigned writes to the suppression list.
        return jsonify({"ok": False, "reason": "webhook_not_configured"}), 503
    raw = request.get_data() or b""
    if not _svix_signature_ok(RESEND_WEBHOOK_SECRET,
                              request.headers.get("svix-id"),
                              request.headers.get("svix-timestamp"),
                              raw, request.headers.get("svix-signature")):
        # Counted, because from our side a mistyped secret and a webhook
        # nobody has pointed at us yet look identical: both leave the event
        # counters empty. One is a five-second fix and the other needs no
        # action at all, and without this there is no way to tell which --
        # until months later when the list turns out never to have cleaned
        # itself. A signed request that fails is the loudest possible signal
        # that the secret does not match; it should not be silent.
        _record_email_event("rejected_bad_signature")
        return jsonify({"ok": False, "reason": "bad_signature"}), 401

    try:
        event = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return jsonify({"ok": False, "reason": "unparseable"}), 400
    kind = str(event.get("type") or "").strip().lower()
    data = event.get("data") if isinstance(event.get("data"), dict) else {}

    to = data.get("to")
    # isinstance-gated rather than list(to or []) directly: Resend's own API
    # always sends a string or a list of strings, but a non-iterable value
    # here (an int, a bool) would make list(to) raise instead of degrading
    # to "no addresses in this event" -- and this endpoint's whole point is
    # to never 500 on webhook data it doesn't recognise.
    addresses = [to] if isinstance(to, str) else (to if isinstance(to, list) else [])
    addresses = [str(a).strip().lower() for a in addresses if str(a or "").strip()]

    _record_email_event(kind or "unknown")
    suppressed = []
    if kind == "email.complained" or (kind == "email.bounced"
                                      and _is_permanent_bounce(data)):
        for addr in addresses:
            if _suppress(addr):
                suppressed.append(addr)
        if suppressed:
            _record_email_event("suppressed")
            print(f"[campaign] {kind}: suppressed {len(suppressed)} address(es)",
                  flush=True)
    return jsonify({"ok": True, "event": kind, "suppressed": len(suppressed)})
