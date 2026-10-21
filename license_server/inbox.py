# ── Emailing documents to the app ───────────────────────────────────────────
#
# Plan rooms usually email the documents once a contractor registers as a
# plan holder. Downloading them on a phone and uploading them again is the
# clumsy part, so every contractor gets a private address,
# docs+<code>@curbcallpro.com, to forward those emails to. Cloudflare Email
# Routing hands mail for that address to the site's Worker (worker/index.js),
# which posts the raw message here with a shared secret. The PDFs attached
# are kept in a private Supabase Storage bucket for 30 days and listed in the
# app, where they're read and filled like an upload. Links in the email are
# listed too: many plan rooms send a download link instead of a file.
#
# Off until INBOUND_EMAIL_SECRET is set on the server (and the same value on
# the Worker): the app hides the address while /inbox/address says so.
import email as _email_pkg
from email import policy as _email_policy

INBOUND_EMAIL_SECRET = os.environ.get("INBOUND_EMAIL_SECRET", "").strip()
INBOX_DOMAIN = os.environ.get("INBOX_DOMAIN", "curbcallpro.com")
INBOX_BUCKET = "inbox"
INBOX_KEEP_DAYS = 30
INBOX_MAX_ITEMS = 50
INBOX_MAX_FILES = 6
INBOX_MAX_MESSAGE = 40 * 1024 * 1024
_INBOX_ADDR_PREFIX = "bidcaller:inbox_addr:"
_INBOX_CODE_PREFIX = "bidcaller:inbox_code:"
_INBOX_ITEMS_PREFIX = "bidcaller:inbox:"
_INBOX_CODE_RE = re.compile(r"^docs\+([a-z0-9]{10})@", re.I)
_LINK_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


def _storage_request(path, method="GET", data=None, ctype=None):
    """(status, body bytes) from Supabase Storage with the service key."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return 0, b""
    headers = {"apikey": SUPABASE_SERVICE_ROLE_KEY,
               "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"}
    if ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(f"{SUPABASE_URL}/storage/v1/{path}", data=data,
                                 method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except Exception as ex:
        print(f"[inbox] storage {method} {path} failed: {ex}", flush=True)
        return 0, b""


def _storage_put(path, raw):
    url = f"object/{INBOX_BUCKET}/{path}"
    status, body = _storage_request(url, "POST", raw, "application/pdf")
    if status in (400, 404) and b"ucket" in body:
        # First file ever: make the private bucket, then try again.
        _storage_request("bucket", "POST", json.dumps({"id": INBOX_BUCKET, "name": INBOX_BUCKET,
                                                       "public": False}).encode(), "application/json")
        status, body = _storage_request(url, "POST", raw, "application/pdf")
    return 200 <= status < 300


def _storage_delete(paths):
    if paths:
        _storage_request(f"object/{INBOX_BUCKET}", "DELETE",
                         json.dumps({"prefixes": list(paths)}).encode(), "application/json")


def _inbox_user(data):
    """The signed-in user's id, or ("", error response)."""
    token = data.get("supabase_token", "")
    if not _license_is_active(data.get("key", ""), data.get("device_id", ""), token):
        return "", (jsonify({"ok": False, "reason": "not_licensed"}), 403)
    uid = str((_supabase_user(token) or {}).get("id") or "")
    if not uid:
        return "", (jsonify({"ok": False, "reason": "not_signed_in"}), 401)
    if not (INBOUND_EMAIL_SECRET and SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return "", (jsonify({"ok": False, "reason": "not_configured"}), 200)
    return uid, None


def _inbox_items(uid, prune=True):
    """The user's emailed documents, newest first, old ones removed."""
    try:
        rec = kv_backend.get(_INBOX_ITEMS_PREFIX + uid, None) or {}
    except Exception:
        rec = {}
    items = rec.get("items") if isinstance(rec, dict) else None
    items = items if isinstance(items, list) else []
    if prune:
        cutoff = time.time() - INBOX_KEEP_DAYS * 86400
        old = [it for it in items if float(it.get("at") or 0) < cutoff]
        if old:
            _storage_delete([f["path"] for it in old for f in it.get("files", [])])
            items = [it for it in items if it not in old]
            _save_inbox(uid, items)
    return items


def _save_inbox(uid, items):
    kv_backend.set(_INBOX_ITEMS_PREFIX + uid, {"items": items[:INBOX_MAX_ITEMS]})


def _inbox_code(uid):
    """The user's address code, made on first ask and kept."""
    code = kv_backend.get(_INBOX_ADDR_PREFIX + uid, None)
    if isinstance(code, str) and code:
        return code
    code = secrets.token_hex(5)
    kv_backend.set(_INBOX_CODE_PREFIX + code, uid)
    kv_backend.set(_INBOX_ADDR_PREFIX + uid, code)
    return code


def _public_item(it):
    return {"id": it.get("id"), "subject": it.get("subject", ""), "from": it.get("from", ""),
            "at": it.get("at"), "snippet": it.get("snippet", ""), "links": it.get("links", []),
            "addendum": bool(it.get("addendum")),
            "files": [{"n": i, "name": f.get("name"), "size": f.get("size")}
                      for i, f in enumerate(it.get("files", []))]}


def _email_parts(raw):
    """(subject, from, text, [(name, pdf bytes)], [links]) of a raw message."""
    msg = _email_pkg.message_from_bytes(raw, policy=_email_policy.default)
    subject = re.sub(r"\s+", " ", str(msg.get("Subject") or "")).strip()[:200]
    sender = re.sub(r"\s+", " ", str(msg.get("From") or "")).strip()[:200]
    texts, files = [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        name = part.get_filename() or ""
        if ctype == "application/pdf" or name.lower().endswith(".pdf"):
            try:
                body = part.get_payload(decode=True) or b""
            except Exception:
                body = b""
            if body[:5] == b"%PDF-" and len(body) <= BID_DOC_MAX_BYTES and len(files) < INBOX_MAX_FILES:
                clean = re.sub(r"[^\w .()&#,-]+", "", name)[:120] or f"document-{len(files) + 1}.pdf"
                files.append((clean, body))
        elif ctype in ("text/plain", "text/html") and not name:
            try:
                content = part.get_content()
            except Exception:
                continue
            texts.append(_html_to_text(content) if ctype == "text/html" else str(content))
    text = "\n".join(texts)
    links, seen = [], set()
    for u in _LINK_RE.findall(text if texts else ""):
        u = u.rstrip(".,;")
        low = u.lower()
        if low in seen or "unsubscribe" in low or len(u) > 500:
            continue
        # Only links that look like the documents, not every footer link.
        if re.search(r"\.pdf\b|download|document|plan|bid|spec|file", low):
            seen.add(low)
            links.append(u)
        if len(links) >= 8:
            break
    return subject, sender, re.sub(r"\s+", " ", text).strip(), files, links


@app.route("/inbound/email", methods=["POST"])
def inbound_email():
    if not INBOUND_EMAIL_SECRET:
        return jsonify({"ok": False, "reason": "not_configured"}), 503
    if not hmac.compare_digest(request.headers.get("X-Inbound-Secret", ""), INBOUND_EMAIL_SECRET):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    if (request.content_length or 0) > INBOX_MAX_MESSAGE:
        return jsonify({"ok": False, "reason": "too_large"}), 413
    m = _INBOX_CODE_RE.match(request.headers.get("X-Envelope-To", "").strip())
    uid = kv_backend.get(_INBOX_CODE_PREFIX + m.group(1).lower(), None) if m else None
    if not uid:
        # The Worker bounces it: nobody has this address.
        return jsonify({"ok": False, "reason": "unknown_address"}), 404
    try:
        subject, sender, text, files, links = _email_parts(request.get_data(cache=False))
    except Exception as ex:
        print(f"[inbox] unreadable message: {ex}", flush=True)
        return jsonify({"ok": False, "reason": "unreadable"}), 400
    item_id = secrets.token_hex(8)
    stored = []
    for name, body in files:
        path = f"{uid}/{item_id}/{len(stored)}.pdf"
        if _storage_put(path, body):
            stored.append({"name": name, "size": len(body), "path": path,
                           "sha": hashlib.sha256(body).hexdigest()[:16]})
    if not stored and not links:
        return jsonify({"ok": True, "kept": 0, "reason": "no_documents"})
    # Addenda sent only to registered plan holders arrive this way, never on
    # the posting, so they're marked and the contractor is told at once.
    addendum = bool(re.search(r"addend", " ".join([subject, text[:2000]] + [n for n, _b in files]), re.I))
    items = _inbox_items(uid)
    items.insert(0, {"id": item_id, "at": time.time(), "subject": subject, "from": sender,
                     "snippet": text[:400], "files": stored, "links": links, "addendum": addendum})
    for gone in items[INBOX_MAX_ITEMS:]:
        _storage_delete([f["path"] for f in gone.get("files", [])])
    _save_inbox(uid, items)
    try:
        _notify_user(uid, ("Addendum emailed in: " if addendum else "Bid documents emailed in: ") + (subject or sender)[:90],
                     "Open the bid in CurbCall to read " + ("what changed." if addendum else "them."), "app.html")
    except Exception as ex:
        print(f"[inbox] alert not sent: {ex}", flush=True)
    return jsonify({"ok": True, "kept": len(stored), "links": len(links), "addendum": addendum})


@app.route("/inbox/address", methods=["POST"])
def inbox_address():
    data = request.get_json(force=True, silent=True) or {}
    uid, err = _inbox_user(data)
    if err:
        return err
    return jsonify({"ok": True, "address": f"docs+{_inbox_code(uid)}@{INBOX_DOMAIN}",
                    "keep_days": INBOX_KEEP_DAYS})


@app.route("/inbox/list", methods=["POST"])
def inbox_list():
    data = request.get_json(force=True, silent=True) or {}
    uid, err = _inbox_user(data)
    if err:
        return err
    return jsonify({"ok": True, "items": [_public_item(it) for it in _inbox_items(uid)]})


@app.route("/inbox/file", methods=["POST"])
def inbox_file():
    data = request.get_json(force=True, silent=True) or {}
    uid, err = _inbox_user(data)
    if err:
        return err
    item = next((it for it in _inbox_items(uid, prune=False) if it.get("id") == data.get("id")), None)
    try:
        f = item["files"][int(data.get("n"))] if item else None
    except (TypeError, ValueError, IndexError):
        f = None
    if not f:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    status, body = _storage_request(f"object/{INBOX_BUCKET}/{f['path']}")
    if status != 200 or body[:5] != b"%PDF-":
        return jsonify({"ok": False, "reason": "not_found"}), 404
    from flask import Response
    return Response(body, mimetype="application/pdf",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.route("/inbox/delete", methods=["POST"])
def inbox_delete():
    data = request.get_json(force=True, silent=True) or {}
    uid, err = _inbox_user(data)
    if err:
        return err
    items = _inbox_items(uid, prune=False)
    gone = [it for it in items if it.get("id") == data.get("id")]
    if gone:
        _storage_delete([f["path"] for f in gone[0].get("files", [])])
        _save_inbox(uid, [it for it in items if it.get("id") != data.get("id")])
    return jsonify({"ok": True, "deleted": len(gone)})
