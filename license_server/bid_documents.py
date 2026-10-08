# ── Reading a bid's own documents ───────────────────────────────────────────
#
# The bid form usually carries a schedule of items -- "3. 4 in. concrete
# sidewalk, 1,250 SY" -- which is the exact list a contractor has to price.
# The app's Prepare-bid pricing sheet otherwise starts from the posting's
# summary at best. This reads one document the app links for the bid and
# returns that schedule, plus what the documents say about submitting, bid
# security, the pre-bid meeting and required forms.
#
# The URL comes from the client, so unlike every other fetch on this server
# it is untrusted: only http(s) to a public address, re-checked on every
# redirect, robots.txt respected, size and time capped. The AI is told to
# copy what the document prints and leave anything else blank; the result
# is then validated here rather than trusted.
# hashlib and socket come from core.py: every file here shares one namespace.
import io
import ipaddress

BID_DOC_MAX_BYTES = int(os.environ.get("BID_DOC_MAX_BYTES", str(15 * 1024 * 1024)))
BID_DOC_TIMEOUT = int(os.environ.get("BID_DOC_TIMEOUT", "30"))
BID_DOC_MAX_PAGES = int(os.environ.get("BID_DOC_MAX_PAGES", "40"))
BID_DOC_TEXT_CHARS = int(os.environ.get("BID_DOC_TEXT_CHARS", "60000"))
BID_DOC_MAX_PER_KEY_PER_DAY = int(os.environ.get("BID_DOC_MAX_PER_KEY_PER_DAY", "25"))
_BID_DOC_RATE_KEY = "bidcaller:bid_doc_rate"
_BID_DOC_CACHE_PREFIX = "bidcaller:bid_doc:"
BID_DOC_CACHE_TTL_DAYS = 30


def _public_http_url(url):
    """True only for http(s) URLs whose host resolves to public addresses.

    Blocks the server being pointed at itself, its cloud metadata endpoint
    or anything on a private network.
    """
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    if parts.port not in (None, 80, 443):
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """Re-applies the public-address check to every redirect hop."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _public_http_url(newurl):
            raise urllib.error.URLError("redirect to a non-public address")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_document(url):
    """(bytes, content_type, outcome). Never raises."""
    if not _public_http_url(url):
        return b"", "", "not_public"
    if not _robots_allows(url):
        return b"", "", "robots_disallow"
    opener = urllib.request.build_opener(_CheckedRedirect())
    try:
        req = urllib.request.Request(url, headers=_page_headers())
        with opener.open(req, timeout=BID_DOC_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            data = resp.read(BID_DOC_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        return b"", "", f"http_{e.code}"
    except Exception:
        return b"", "", "unreachable"
    if len(data) > BID_DOC_MAX_BYTES:
        return b"", ctype, "too_large"
    return data, ctype, "ok"


def _document_text(data, ctype):
    """(text, pages_read, truncated). PDF or HTML/plain text."""
    if data[:5] == b"%PDF-" or "pdf" in ctype:
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            pages = reader.pages
            n = min(len(pages), BID_DOC_MAX_PAGES)
            text = "\n".join((pages[i].extract_text() or "") for i in range(n))
            truncated = len(pages) > n
        except Exception:
            return "", 0, False
    else:
        text = _html_to_text(data.decode("utf-8", "ignore"))
        n, truncated = 1, False
    if len(text) > BID_DOC_TEXT_CHARS:
        text, truncated = text[:BID_DOC_TEXT_CHARS], True
    return text, n, truncated


_BID_DOC_PROMPT = (
    "You read a public construction bid document for a concrete contractor. "
    "Copy facts EXACTLY as the document prints them. Never estimate, infer or "
    "fill in a value the document does not state; use \"\" or [] instead.\n\n"
    "Return ONE JSON object with keys:\n"
    "\"line_items\": the bid schedule / bid form items, each "
    "{\"item_no\", \"description\", \"quantity\" (number as printed, or \"\"), "
    "\"unit\" (as printed, e.g. SY, LF, EA, LS)}. Only items the document "
    "lists for pricing. [] if there is no schedule.\n"
    "\"submission\": how and where bids are submitted and the deadline, as stated.\n"
    "\"bid_security\": the bid bond / bid security requirement, as stated.\n"
    "\"prebid\": pre-bid meeting date, place and whether mandatory, as stated.\n"
    "\"required_forms\": names of forms, affidavits or certificates the bid "
    "must include, as stated.\n"
    "No markdown, nothing outside the object.\n\nDOCUMENT TEXT:\n"
)

_UNIT_RE = re.compile(r"^[A-Za-z. /]{1,12}$")


def _clean_doc_result(raw):
    """Validate the model's object; anything malformed is dropped, not fixed."""
    if not isinstance(raw, dict):
        return None
    items = []
    for it in (raw.get("line_items") or [])[:200]:
        if not isinstance(it, dict):
            continue
        desc = re.sub(r"\s+", " ", str(it.get("description") or "")).strip()[:200]
        if not desc:
            continue
        q = str(it.get("quantity") if it.get("quantity") is not None else "").replace(",", "").strip()
        try:
            qty = float(q) if q else None
        except ValueError:
            qty = None
        if qty is not None and not (0 < qty < 1e9):
            qty = None
        unit = str(it.get("unit") or "").strip()
        items.append({
            "item_no": str(it.get("item_no") or "").strip()[:20],
            "description": desc,
            "quantity": qty,
            "unit": unit if _UNIT_RE.match(unit) else "",
        })

    def text(k, n=600):
        v = raw.get(k)
        return re.sub(r"\s+", " ", v).strip()[:n] if isinstance(v, str) else ""

    forms = [re.sub(r"\s+", " ", str(f)).strip()[:160]
             for f in (raw.get("required_forms") or []) if str(f).strip()][:30]
    return {"line_items": items, "submission": text("submission"),
            "bid_security": text("bid_security"), "prebid": text("prebid"),
            "required_forms": forms}


def _ai_read_bid_document(text):
    if not OPENAI_API_KEY or not text.strip():
        return None
    body = json.dumps({
        "model": OPENAI_MODEL,
        "messages": [{"role": "user", "content": _BID_DOC_PROMPT + text}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _clean_doc_result(json.loads(data["choices"][0]["message"]["content"]))
    except Exception as ex:
        print(f"[bid-doc] ai error: {ex}", flush=True)
        return None


@app.route("/bid-documents/read", methods=["POST"])
def bid_documents_read():
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key", "")
    device = data.get("device_id", "")
    if not _license_is_active(key, device, data.get("supabase_token", "")):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    url = str(data.get("url") or "").strip()
    if not _public_http_url(url):
        return jsonify({"ok": False, "reason": "bad_url"}), 400
    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "reason": "ai_unavailable"})

    cache_key = _BID_DOC_CACHE_PREFIX + hashlib.sha256(url.encode()).hexdigest()[:32]
    try:
        hit = kv_backend.get(cache_key, None)
    except Exception:
        hit = None
    if isinstance(hit, dict) and hit.get("result"):
        try:
            age = time.time() - float(hit.get("at") or 0)
        except (TypeError, ValueError):
            age = 1e12
        if age < BID_DOC_CACHE_TTL_DAYS * 86400:
            return jsonify({"ok": True, "cached": True, **hit["result"]})

    # Counted only for real reads: a cached answer costs nothing.
    if not _ip_rate_ok(_BID_DOC_RATE_KEY, key or device or _client_ip(),
                       BID_DOC_MAX_PER_KEY_PER_DAY):
        return jsonify({"ok": False, "reason": "rate_limited",
                        "detail": "Too many documents read today. Try again tomorrow."}), 429

    raw, ctype, outcome = _fetch_document(url)
    if outcome != "ok":
        return jsonify({"ok": False, "reason": "fetch_failed", "detail": outcome})
    text, pages, truncated = _document_text(raw, ctype)
    if not text.strip():
        # A scanned PDF has no text layer; there is nothing to read without OCR.
        return jsonify({"ok": False, "reason": "no_text",
                        "detail": "This document has no readable text (it may be a scan)."})
    result = _ai_read_bid_document(text)
    if result is None:
        return jsonify({"ok": False, "reason": "ai_error"}), 502
    result.update({"pages_read": pages, "truncated": truncated})
    try:
        kv_backend.set(cache_key, {"at": time.time(), "result": result})
    except Exception:
        pass
    return jsonify({"ok": True, "cached": False, **result})
