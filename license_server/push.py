# ── Phone alerts that arrive with the app closed (Web Push) ─────────────────
#
# The app could only check saved bids and scan for new ones while it was
# open, so a contractor heard about an addendum when they next happened to
# look. This sends real push notifications instead: an addendum or new
# document on a saved bid, a bid due tomorrow, and new bids for a saved
# search (the daily alert job pushes as well as emails).
#
# Web Push needs two pieces of crypto, both done here with the cryptography
# package rather than pywebpush (whose http-ece dependency doesn't build
# everywhere):
#   - VAPID (RFC 8292): an ES256-signed token that tells the push service
#     the message is from this server;
#   - message encryption (RFC 8291, aes128gcm): only the subscriber's
#     browser can read the payload.
# The VAPID key comes from VAPID_PRIVATE_KEY if set, else is generated once
# and kept in KV, so nothing has to be configured to turn this on.
#
# A subscription's endpoint is chosen by the browser and stored by the
# client, so it is untrusted: the server only ever POSTs to the known push
# services' hosts, over https.
#
# Subscriptions live in Supabase (push_subscriptions, one row per device,
# RLS: a user manages their own). Endpoints that answer 404/410 are gone and
# are deleted.
import collections

_VAPID_KV_KEY = "bidcaller:vapid"
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
PUSH_TTL_SECONDS = 24 * 3600
_PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com",
               "push.services.mozilla.com", "web.push.apple.com",
               "notify.windows.com", "push.apple.com")


def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _unb64u(s):
    s = str(s or "")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _vapid_private():
    """The server's VAPID key (an EC P-256 private key object)."""
    from cryptography.hazmat.primitives.asymmetric import ec
    raw = VAPID_PRIVATE_KEY
    if not raw:
        try:
            stored = kv_backend.get(_VAPID_KV_KEY, None) or {}
        except Exception:
            stored = {}
        raw = stored.get("private", "") if isinstance(stored, dict) else ""
        if not raw:
            key = ec.generate_private_key(ec.SECP256R1())
            raw = _b64u(key.private_numbers().private_value.to_bytes(32, "big"))
            try:
                kv_backend.set(_VAPID_KV_KEY, {"private": raw, "created": time.time()})
            except Exception as ex:
                print(f"[push] could not store the VAPID key: {ex}", flush=True)
    return ec.derive_private_key(int.from_bytes(_unb64u(raw), "big"), ec.SECP256R1())


def _public_point(key):
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint)


def _vapid_header(endpoint, key=None):
    """Authorization header value for one push service (RFC 8292)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    key = key or _vapid_private()
    parts = urllib.parse.urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}",
              "exp": int(time.time()) + 12 * 3600, "sub": f"mailto:{SUPPORT_EMAIL}"}
    signing = (_b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
               + "." + _b64u(json.dumps(claims, separators=(",", ":")).encode()))
    r, s = decode_dss_signature(key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
    jwt = signing + "." + _b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={jwt}, k={_b64u(_public_point(key))}"


def _hkdf(salt, ikm, info, length):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def _encrypt_push(payload, p256dh, auth, _as_key=None, _salt=None):
    """RFC 8291 aes128gcm body for one subscription."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ua_public = _unb64u(p256dh)
    auth_secret = _unb64u(auth)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    as_key = _as_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _public_point(as_key)
    shared = as_key.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = _salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + (4096).to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + body


def _push_endpoint_ok(endpoint):
    try:
        parts = urllib.parse.urlsplit(str(endpoint or ""))
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and parts.port in (None, 443) and any(
        host == h or host.endswith("." + h) for h in _PUSH_HOSTS)


def _send_push(sub, message):
    """POST one notification. 'ok' | 'gone' (delete it) | 'failed'."""
    endpoint = sub.get("endpoint", "")
    if not _push_endpoint_ok(endpoint):
        return "gone"
    try:
        body = _encrypt_push(json.dumps(message).encode("utf-8"), sub.get("p256dh"), sub.get("auth"))
    except Exception:
        return "gone"   # keys that can't be used never will be
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Authorization": _vapid_header(endpoint), "TTL": str(PUSH_TTL_SECONDS),
        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
        "Urgency": "normal"})
    try:
        with urllib.request.urlopen(req, timeout=15):
            return "ok"
    except urllib.error.HTTPError as e:
        return "gone" if e.code in (404, 410) else "failed"
    except Exception:
        return "failed"


def _push_subscriptions(user_id=None):
    q = "/rest/v1/push_subscriptions?select=user_id,endpoint,p256dh,auth"
    if user_id:
        q += "&user_id=eq." + urllib.parse.quote(str(user_id))
    data = _supabase_admin_request(q)
    return data if isinstance(data, list) else []


def _notify_user(user_id, title, body, url="app.html", subs=None):
    """Push to every device the user turned alerts on for. Returns how many got it."""
    delivered = 0
    for sub in (subs if subs is not None else _push_subscriptions(user_id)):
        if sub.get("user_id") not in (None, user_id):
            continue
        outcome = _send_push(sub, {"title": title[:120], "body": body[:240], "url": url})
        if outcome == "ok":
            delivered += 1
        elif outcome == "gone":
            _supabase_admin_request(
                "/rest/v1/push_subscriptions?endpoint=eq." + urllib.parse.quote(sub.get("endpoint", ""), safe=""),
                method="DELETE")
    return delivered


@app.route("/push/vapid-public-key", methods=["GET"])
def push_vapid_public_key():
    try:
        return jsonify({"ok": True, "key": _b64u(_public_point(_vapid_private()))})
    except Exception as ex:
        print(f"[push] no VAPID key: {ex}", flush=True)
        return jsonify({"ok": False, "reason": "unavailable"}), 503


# ── The saved-bid watch (/run-bid-watch, every few hours) ──
# For every user's saved, still-open bids: tell them the day before one is
# due, and when its posting gains an addendum or document. Postings are
# shared between users who saved the same one, fetched once per run.
BID_WATCH_MAX_FETCHES = int(os.environ.get("BID_WATCH_MAX_FETCHES", "150"))
# Seconds of fetching per run, inside the scheduler's 280 s request limit.
BID_WATCH_TIME_BUDGET = int(os.environ.get("BID_WATCH_TIME_BUDGET", "180"))
_BID_WATCH_STATE_KEY = "bidcaller:bid_watch_state"
_BID_WATCH_REMINDED_KEY = "bidcaller:bid_watch_reminded"


def _today_central():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/Chicago")).date()
    except Exception:
        return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)).date()


def _watch_change(prev, cur):
    """What's new on a posting, in a few words, or ''."""
    before = {str(x).lower() for x in prev.get("docs", [])}
    new_docs = [d for d in cur.get("docs", []) if str(d).lower() not in before]
    new_add = [n for n in cur.get("addenda", []) if n not in set(prev.get("addenda", []))]
    if new_add:
        return "Addendum " + ", ".join(str(n) for n in new_add) + " posted"
    if new_docs:
        return "New document: " + ", ".join(new_docs[:2]) + (
            f" and {len(new_docs) - 2} more" if len(new_docs) > 2 else "")
    return ""


def _run_bid_watch():
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return {"ok": False, "reason": "supabase_not_configured"}
    rows = _supabase_admin_request(
        "/rest/v1/saved_bids?select=user_id,bid_id,title,deadline,url,pipeline&limit=5000")
    if not isinstance(rows, list):
        return {"ok": False, "reason": "saved_bids_unreadable"}
    today = _today_central()
    tomorrow = today + datetime.timedelta(days=1)
    try:
        state = kv_backend.get(_BID_WATCH_STATE_KEY, None) or {}
        reminded = kv_backend.get(_BID_WATCH_REMINDED_KEY, None) or {}
    except Exception:
        state, reminded = {}, {}
    messages = collections.defaultdict(list)   # user_id -> [(title, body, url)]
    by_url = collections.defaultdict(list)
    for r in rows:
        if str(r.get("pipeline") or "") in ("won", "lost", "passed", "submitted"):
            continue
        due = _parse_deadline(r.get("deadline"))
        if due and due < today:
            continue
        title = str(r.get("title") or "A saved bid")[:90]
        if due == tomorrow:
            k = f"{r.get('user_id')}|{r.get('bid_id')}|{tomorrow.isoformat()}"
            if k not in reminded:
                reminded[k] = today.isoformat()
                messages[r["user_id"]].append((f"Due tomorrow: {title}", f"Bids are due {r.get('deadline')}.", "app.html"))
        url = str(r.get("url") or "").strip()
        if url.startswith(("http://", "https://")):
            by_url[url].append(r)
    fetched, changed = 0, 0
    started = time.time()
    # Postings not reached this run are checked next run, oldest first.
    order = sorted(by_url, key=lambda u: (state.get(hashlib.sha256(u.encode()).hexdigest()[:24]) or {}).get("at", 0))
    for url in order:
        savers = by_url[url]
        if fetched >= BID_WATCH_MAX_FETCHES or time.time() - started > BID_WATCH_TIME_BUDGET:
            break
        if not _public_http_url(url):
            continue
        raw, ctype, outcome = _fetch_document(url)
        fetched += 1
        if outcome != "ok":
            continue
        cur = _watch_fingerprint(url, raw, ctype)
        key = hashlib.sha256(url.encode()).hexdigest()[:24]
        prev = state.get(key)
        state[key] = {"docs": cur["docs"], "addenda": cur["addenda"], "at": time.time()}
        change = _watch_change(prev, cur) if isinstance(prev, dict) else ""
        if change:
            changed += 1
            for r in savers:
                messages[r["user_id"]].append(
                    (f"{str(r.get('title') or 'A saved bid')[:80]}: {change}",
                     "Read it before you bid. Missing an addendum can get a bid thrown out.", "app.html"))
    # Old reminder stamps go after two weeks.
    cutoff = (today - datetime.timedelta(days=14)).isoformat()
    reminded = {k: v for k, v in reminded.items() if v >= cutoff}
    try:
        kv_backend.set(_BID_WATCH_STATE_KEY, state)
        kv_backend.set(_BID_WATCH_REMINDED_KEY, reminded)
    except Exception as ex:
        print(f"[bid-watch] state not saved: {ex}", flush=True)

    pushed = emailed = 0
    for user_id, msgs in messages.items():
        subs = _push_subscriptions(user_id)
        got = 0
        if subs:
            if len(msgs) > 3:
                msgs = msgs[:2] + [(f"{len(msgs) - 2} more updates on your saved bids", "Open CurbCall Pro to see them.", "app.html")]
            for title, body, link in msgs:
                got += 1 if _notify_user(user_id, title, body, link, subs=subs) else 0
            pushed += got
        # No phone alerts reached them: one email with the lot instead.
        if not got and RESEND_API_KEY:
            email = _get_user_email(user_id)
            if email and _send_email(email, msgs[0][0] if len(msgs) == 1 else f"{len(msgs)} updates on your saved bids",
                                     "\n\n".join(f"{t}\n{b}" for t, b, _ in msgs)
                                     + "\n\nOpen CurbCall Pro to see the details: https://curbcallpro.com/app.html"):
                emailed += 1
    return {"ok": True, "bids": len(rows), "postings_checked": fetched, "changed": changed,
            "users_notified": len(messages), "pushed": pushed, "emailed": emailed}


@app.route("/run-bid-watch", methods=["POST"])
def run_bid_watch():
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("token") or request.headers.get("X-Cron-Secret", "")
    if not CRON_SECRET or not hmac.compare_digest(token, CRON_SECRET):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    result = _run_bid_watch()
    if result.get("ok"):
        _cron_beat("bid-watch")
    return jsonify(result), (200 if result.get("ok") else 500)
