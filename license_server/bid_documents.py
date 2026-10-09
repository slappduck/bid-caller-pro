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


def _robots_allows_checked(url, opener):
    """robots.txt, fetched through the same checked opener as the document.

    The scanner's shared _robots_allows follows redirects unchecked, which is
    fine for URLs the scanner found itself but not here: a site could point
    its robots.txt at an internal address and have this server request it.
    Unreadable robots.txt is not a refusal, matching the shared rule.
    """
    parts = urllib.parse.urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        req = urllib.request.Request(robots_url, headers=_page_headers())
        with opener.open(req, timeout=ROBOTS_TIMEOUT) as resp:
            body = resp.read(200000).decode("utf-8", "replace")
    except Exception:
        return True
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(body.splitlines())
    try:
        return parser.can_fetch(_page_headers().get("User-Agent", "*"), url)
    except Exception:
        return True


def _fetch_document(url):
    """(bytes, content_type, outcome). Never raises."""
    if not _public_http_url(url):
        return b"", "", "not_public"
    opener = urllib.request.build_opener(_CheckedRedirect())
    if RESPECT_ROBOTS and not _robots_allows_checked(url, opener):
        return b"", "", "robots_disallow"
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
    "\"questions_due\": the deadline for written questions, as stated.\n"
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
            "questions_due": text("questions_due", 300), "required_forms": forms}


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


# ── Filling in the bid form ─────────────────────────────────────────────────
#
# Many agencies post their bid form as a fillable PDF. Once the contractor has
# priced the job in the app, typing the same company details and unit prices
# into that form again is pure transcription. This fills it.
#
# The AI is only allowed to MATCH: it sees the form's field names and the
# names of the values the contractor supplied ("company.name",
# "line.3.unit_price", "total"), and returns which field takes which value
# name. The values themselves are put in here, from what the contractor sent,
# so nothing on the form can be invented. Signatures, notary blocks, tax IDs
# and anything the contractor hasn't supplied are left blank for them, and
# every filled field is listed back so they can check it.
BID_FILL_MAX_PER_KEY_PER_DAY = int(os.environ.get("BID_FILL_MAX_PER_KEY_PER_DAY", "30"))
_BID_FILL_RATE_KEY = "bidcaller:bid_fill_rate"
BID_FILL_MAX_FIELDS = 400
# Never filled, whatever the matcher says: these are the contractor's to sign
# or swear to.
_NEVER_FILL = re.compile(
    r"sign|notar|seal|sworn|subscribed|commission\s*exp|ssn|social\s*sec|"
    r"tax\s*id|\btin\b|\bein\b|fein|federal\s*id|witness|attest", re.I)

_BID_FILL_PROMPT = (
    "You match the fields of a construction bid form PDF to values a "
    "contractor supplied. You do NOT write values. For each form field that "
    "clearly asks for one of the supplied values, return the value's KEY. "
    "Leave out any field you are not sure about, any field asking for "
    "something not supplied, and every signature, notary, seal, witness or "
    "tax-ID field.\n"
    "Line keys are line.<n>.<part> where <n> is the line's position and its "
    "item_no is shown; match a form row to the line with the same item number "
    "or the same description.\n"
    "Return ONE JSON object: {\"field name\": \"value key\", ...}. No markdown.\n\n"
)

_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
         "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
         "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _words_under_1000(n):
    out = []
    if n >= 100:
        out.append(_ONES[n // 100] + " hundred")
        n %= 100
    if n >= 20:
        out.append(_TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else ""))
    elif n:
        out.append(_ONES[n])
    return " ".join(out)


def _dollars_in_words(amount):
    """"One hundred twenty thousand five hundred dollars and 25/100"."""
    cents = int(round(amount * 100))
    dollars, cents = divmod(cents, 100)
    if dollars >= 10 ** 12:
        return ""
    parts = []
    for size, name in ((10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand"), (1, "")):
        chunk = (dollars // size) % 1000
        if chunk:
            parts.append((_words_under_1000(chunk) + (" " + name if name else "")).strip())
    words = " ".join(parts) or "zero"
    return f"{words[0].upper()}{words[1:]} dollars and {cents:02d}/100"


def _money(x):
    return f"{x:,.2f}"


def _fill_values(data):
    """Flat {key: (label, text)} from what the app sent; nothing else exists."""
    vals = {}

    def put(key, label, v, n=200):
        v = re.sub(r"\s+", " ", str(v if v is not None else "")).strip()[:n]
        if v:
            vals[key] = (label, v)

    co = data.get("company") if isinstance(data.get("company"), dict) else {}
    for k, label in (("name", "Company name"), ("contact", "Contact person"), ("title", "Title"),
                     ("phone", "Phone"), ("email", "Email"), ("address", "Street address"),
                     ("city_state_zip", "City, state, ZIP"), ("license", "License / registration"),
                     ("years", "Years in business")):
        put("company." + k, label, co.get(k))
    bid = data.get("bid") if isinstance(data.get("bid"), dict) else {}
    put("bid.number", "Bid / project number", bid.get("number"), 80)
    put("bid.title", "Project", bid.get("title"))
    put("bid.addenda", "Addenda acknowledged", bid.get("addenda"), 120)
    put("date", "Date", bid.get("date"), 40)
    try:
        total = float(bid.get("total"))
    except (TypeError, ValueError):
        total = None
    if total is not None and 0 < total < 1e10:
        put("total", "Total bid", _money(total))
        put("total_words", "Total bid in words", _dollars_in_words(total))
    for i, ln in enumerate((data.get("lines") or [])[:200], start=1):
        if not isinstance(ln, dict):
            continue
        tag = f"Line {i}" + (f" (item {str(ln.get('item_no'))[:20]})" if ln.get("item_no") else "")
        put(f"line.{i}.item_no", f"{tag} item number", ln.get("item_no"), 20)
        put(f"line.{i}.description", f"{tag} description", ln.get("description"))
        for part, label in (("quantity", "quantity"), ("unit_price", "unit price"), ("amount", "amount")):
            try:
                x = float(ln.get(part))
            except (TypeError, ValueError):
                continue
            if 0 <= x < 1e10:
                put(f"line.{i}.{part}", f"{tag} {label}",
                    _money(x) if part != "quantity" else (f"{x:,.2f}".rstrip("0").rstrip(".")))
        put(f"line.{i}.unit", f"{tag} unit", ln.get("unit"), 20)
    return vals


def _form_text_fields(reader):
    """[(name, tooltip)] of the PDF's fillable text fields."""
    try:
        fields = reader.get_fields() or {}
    except Exception:
        return []
    out = []
    for name, f in fields.items():
        if not isinstance(f, dict) or f.get("/FT") != "/Tx":
            continue
        out.append((str(name), str(f.get("/TU") or "")[:120]))
        if len(out) >= BID_FILL_MAX_FIELDS:
            break
    return out


def _ai_match_fields(fields, vals):
    """{field name: value key} from the model, or None if it couldn't run."""
    if not OPENAI_API_KEY:
        return None
    form = "\n".join(f"- {n}" + (f"  (tooltip: {t})" if t else "") for n, t in fields)
    keys = "\n".join(f"- {k}: {label}" for k, (label, _v) in vals.items())
    body = json.dumps({
        "model": OPENAI_MODEL,
        "messages": [{"role": "user", "content":
                      _BID_FILL_PROMPT + "FORM FIELDS:\n" + form + "\n\nVALUE KEYS:\n" + keys}],
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
        got = json.loads(data["choices"][0]["message"]["content"])
        return got if isinstance(got, dict) else None
    except Exception as ex:
        print(f"[bid-fill] ai error: {ex}", flush=True)
        return None


def _resolve_fill(fields, vals, matched):
    """Only real fields, only supplied values, never a signature-type field."""
    names = {n: t for n, t in fields}
    out = {}
    for field, key in (matched or {}).items():
        field, key = str(field), str(key)
        if field not in names or key not in vals:
            continue
        if _NEVER_FILL.search(field) or _NEVER_FILL.search(names[field]):
            continue
        out[field] = key
    return out


def _filled_pdf(reader, values):
    from pypdf import PdfWriter
    writer = PdfWriter(clone_from=reader)
    for page in writer.pages:
        if page.get("/Annots"):
            writer.update_page_form_field_values(page, values, auto_regenerate=False)
    writer.set_need_appearances_writer(True)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@app.route("/bid-documents/fill", methods=["POST"])
def bid_documents_fill():
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key", "")
    device = data.get("device_id", "")
    if not _license_is_active(key, device, data.get("supabase_token", "")):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    url = str(data.get("url") or "").strip()
    if not _public_http_url(url):
        return jsonify({"ok": False, "reason": "bad_url"}), 400
    vals = _fill_values(data)
    if not vals:
        return jsonify({"ok": False, "reason": "nothing_to_fill"}), 400
    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "reason": "ai_unavailable"})
    if not _ip_rate_ok(_BID_FILL_RATE_KEY, key or device or _client_ip(),
                       BID_FILL_MAX_PER_KEY_PER_DAY):
        return jsonify({"ok": False, "reason": "rate_limited"}), 429

    raw, ctype, outcome = _fetch_document(url)
    if outcome != "ok":
        return jsonify({"ok": False, "reason": "fetch_failed", "detail": outcome})
    if raw[:5] != b"%PDF-":
        return jsonify({"ok": False, "reason": "not_fillable", "detail": "not_pdf"})
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        fields = _form_text_fields(reader)
    except Exception:
        return jsonify({"ok": False, "reason": "not_fillable", "detail": "unreadable"})
    if not fields:
        return jsonify({"ok": False, "reason": "not_fillable", "detail": "no_fields"})

    matched = _ai_match_fields(fields, vals)
    if matched is None:
        return jsonify({"ok": False, "reason": "ai_error"}), 502
    plan = _resolve_fill(fields, vals, matched)
    if not plan:
        return jsonify({"ok": False, "reason": "nothing_matched", "fields": len(fields)})
    values = {field: vals[k][1] for field, k in plan.items()}
    try:
        pdf = _filled_pdf(reader, values)
    except Exception as ex:
        print(f"[bid-fill] write error: {ex}", flush=True)
        return jsonify({"ok": False, "reason": "not_fillable", "detail": "write_failed"})
    tips = dict(fields)
    filled = [{"field": f, "label": tips.get(f) or f, "value": values[f], "key": k}
              for f, k in plan.items()]
    return jsonify({"ok": True, "fields": len(fields), "filled": filled,
                    "left_blank": len(fields) - len(filled),
                    "pdf_b64": base64.b64encode(pdf).decode("ascii")})


# ── Watching saved bids for addenda ─────────────────────────────────────────
#
# A contractor who misses an addendum can have the whole bid thrown out. The
# app sends the postings of its open saved bids here a few times a day; each
# comes back as what a change would show up in: the documents linked from
# it, the addendum numbers its text mentions, and a hash of its text. The
# app compares with its last answer. Same safe fetch as the reader, and each
# posting is fetched at most once every few hours however many users save it.
BID_WATCH_MAX_URLS = 25
BID_WATCH_MAX_PER_KEY_PER_DAY = int(os.environ.get("BID_WATCH_MAX_PER_KEY_PER_DAY", "300"))
BID_WATCH_CACHE_HOURS = 4
_BID_WATCH_RATE_KEY = "bidcaller:bid_watch_rate"
_BID_WATCH_CACHE_PREFIX = "bidcaller:bid_watch:"
_ADDENDUM_RE = re.compile(r"\baddend(?:um|a)\s*(?:no\.?|number|#)?\s*(\d{1,2})\b", re.I)


def _watch_fingerprint(url, data, ctype):
    """{docs, addenda, hash} for one fetched posting."""
    if data[:5] == b"%PDF-" or "pdf" in ctype:
        return {"docs": [], "addenda": [], "hash": hashlib.sha256(data).hexdigest()[:16]}
    html = data.decode("utf-8", "ignore")
    text = _html_to_text(html)
    docs = [d["name"] for d in bid_sources.detail_documents(html, url, limit=40)]
    addenda = sorted({int(n) for n in _ADDENDUM_RE.findall(text) if 0 < int(n) < 100})
    norm = re.sub(r"\s+", " ", text).strip().lower()
    return {"docs": docs, "addenda": addenda,
            "hash": hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]}


@app.route("/bid-watch/check", methods=["POST"])
def bid_watch_check():
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key", "")
    device = data.get("device_id", "")
    if not _license_is_active(key, device, data.get("supabase_token", "")):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    urls = data.get("urls")
    if not isinstance(urls, list):
        return jsonify({"ok": False, "reason": "bad_request"}), 400
    urls = [str(u).strip() for u in urls[:BID_WATCH_MAX_URLS] if isinstance(u, str)]
    results, fetched = {}, 0
    for url in urls:
        if not _public_http_url(url):
            results[url] = {"ok": False, "reason": "bad_url"}
            continue
        cache_key = _BID_WATCH_CACHE_PREFIX + hashlib.sha256(url.encode()).hexdigest()[:32]
        try:
            hit = kv_backend.get(cache_key, None)
        except Exception:
            hit = None
        if isinstance(hit, dict) and time.time() - float(hit.get("at") or 0) < BID_WATCH_CACHE_HOURS * 3600:
            results[url] = {"ok": True, **hit["fp"]}
            continue
        # Counted per fresh fetch, like the reader: a cached answer is free.
        if not _ip_rate_ok(_BID_WATCH_RATE_KEY, key or device or _client_ip(),
                           BID_WATCH_MAX_PER_KEY_PER_DAY):
            results[url] = {"ok": False, "reason": "rate_limited"}
            continue
        raw, ctype, outcome = _fetch_document(url)
        fetched += 1
        if outcome != "ok":
            results[url] = {"ok": False, "reason": outcome}
            continue
        fp = _watch_fingerprint(url, raw, ctype)
        try:
            kv_backend.set(cache_key, {"at": time.time(), "fp": fp})
        except Exception:
            pass
        results[url] = {"ok": True, **fp}
    return jsonify({"ok": True, "results": results, "fetched": fetched})
