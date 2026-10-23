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
# hashlib and socket come from core.py, collections from push.py: every file
# here shares one namespace.
import io
import ipaddress

BID_DOC_MAX_BYTES = int(os.environ.get("BID_DOC_MAX_BYTES", str(15 * 1024 * 1024)))
BID_DOC_TIMEOUT = int(os.environ.get("BID_DOC_TIMEOUT", "30"))
BID_DOC_MAX_PAGES = int(os.environ.get("BID_DOC_MAX_PAGES", "40"))
BID_DOC_TEXT_CHARS = int(os.environ.get("BID_DOC_TEXT_CHARS", "60000"))
BID_DOC_MAX_PER_KEY_PER_DAY = int(os.environ.get("BID_DOC_MAX_PER_KEY_PER_DAY", "25"))
_BID_DOC_RATE_KEY = "bidcaller:bid_doc_rate"
_BID_DOC_CACHE_PREFIX = "bidcaller:bid_doc:"
# Bump when the reader starts returning something new, so documents read
# before the change are read again instead of served from the cache.
BID_DOC_READER_VERSION = 4
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
    "\"prebid_place\": just where the pre-bid meeting is held: the building "
    "and street address exactly as printed, \"\" if the document gives none.\n"
    "\"project_location\": where the work itself is: the street address, the "
    "streets or intersections, as printed, \"\" if the document gives none.\n"
    "\"required_forms\": names of forms, affidavits or certificates the bid "
    "must include, as stated.\n"
    "\"questions_due\": the deadline for written questions, as stated.\n"
    "\"retainage\": the retainage withheld from progress payments, as stated.\n"
    "\"completion\": the time allowed or completion date, and any liquidated damages, as stated.\n"
    "\"prevailing_wage\": whether prevailing wage or Davis-Bacon wages apply, as stated; \"\" if not mentioned.\n"
    "\"testing\": concrete testing and inspection required (cylinders, slump, air, "
    "who pays, notice to the inspector), as stated.\n"
    "\"contact\": the person bidders are told to contact about this bid "
    "(purchasing agent, project manager or engineer), as "
    "{\"name\", \"title\", \"organization\", \"phone\", \"email\"} exactly as printed; "
    "\"\" for any part not printed.\n"
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
    c = raw.get("contact") if isinstance(raw.get("contact"), dict) else {}

    def part(k, n):
        v = c.get(k)
        return re.sub(r"\s+", " ", v).strip()[:n] if isinstance(v, str) else ""
    email = part("email", 120)
    phone = part("phone", 40)
    contact = {"name": part("name", 80), "title": part("title", 80), "organization": part("organization", 120),
               # Only a real address and a phone with a full number are kept.
               "email": email if re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", email) else "",
               "phone": phone if len(re.sub(r"\D", "", phone)) >= 10 else ""}
    return {"line_items": items, "submission": text("submission"),
            "bid_security": text("bid_security"), "prebid": text("prebid"),
            "prebid_place": text("prebid_place", 200),
            "project_location": text("project_location", 200),
            "questions_due": text("questions_due", 300), "required_forms": forms,
            "contact": contact if any(contact.values()) else {},
            "retainage": text("retainage", 200), "completion": text("completion", 300),
            "prevailing_wage": text("prevailing_wage", 200), "testing": text("testing", 400)}


# ── Reading an addendum ──
# An addendum is a few pages of changes in agency language. What a
# contractor needs from it is the list of changes: a moved due date, changed
# quantities, added or deleted items, answers to bidders' questions. Same
# rule as the document reader: copy what it prints, invent nothing.
BID_ADDENDUM_READER_VERSION = 1

_ADDENDUM_PROMPT = (
    "You read an ADDENDUM to a public construction bid, for a concrete contractor. "
    "List only what the addendum changes or answers. Copy facts EXACTLY as printed. "
    "Never estimate or infer; use \"\" or [] when the addendum does not say.\n\n"
    "Return ONE JSON object with keys:\n"
    "\"addendum_number\": the addendum's number as printed (e.g. \"2\"), \"\" if none.\n"
    "\"issued\": the date the addendum is dated, as printed.\n"
    "\"bid_due\": the NEW bid due date and time if the addendum changes it, else \"\".\n"
    "\"prebid\": any change to the pre-bid meeting, as stated, else \"\".\n"
    "\"quantity_changes\": bid items whose quantity changes, each {\"item_no\", "
    "\"description\", \"old_quantity\" (as printed or \"\"), \"new_quantity\", \"unit\"}.\n"
    "\"items_added\": new bid items, each {\"item_no\", \"description\", \"quantity\", \"unit\"}.\n"
    "\"items_removed\": deleted bid items, each {\"item_no\", \"description\"}.\n"
    "\"answers\": questions from bidders and the answers given, each {\"question\", \"answer\"}.\n"
    "\"other_changes\": every other change in a short plain sentence each "
    "(specifications, drawings, forms, scope).\n"
    "No markdown, nothing outside the object.\n\nADDENDUM TEXT:\n"
)


def _num_or_none(v):
    q = str(v if v is not None else "").replace(",", "").strip()
    try:
        x = float(q) if q else None
    except ValueError:
        return None
    return x if x is not None and 0 <= x < 1e9 else None


def _clean_addendum_result(raw):
    """Validate the model's addendum object; malformed parts are dropped."""
    if not isinstance(raw, dict):
        return None

    def text(v, n=400):
        return re.sub(r"\s+", " ", v).strip()[:n] if isinstance(v, str) else ""

    def rows(key, n=100):
        return [r for r in (raw.get(key) or [])[:n] if isinstance(r, dict)]

    def unit(v):
        u = str(v or "").strip()
        return u if _UNIT_RE.match(u) else ""
    changes = [{"item_no": text(r.get("item_no"), 20), "description": text(r.get("description"), 200),
                "old_quantity": _num_or_none(r.get("old_quantity")),
                "new_quantity": _num_or_none(r.get("new_quantity")), "unit": unit(r.get("unit"))}
               for r in rows("quantity_changes")]
    changes = [c for c in changes if c["new_quantity"] is not None and (c["item_no"] or c["description"])]
    added = [{"item_no": text(r.get("item_no"), 20), "description": text(r.get("description"), 200),
              "quantity": _num_or_none(r.get("quantity")), "unit": unit(r.get("unit"))}
             for r in rows("items_added")]
    added = [a for a in added if a["description"]]
    removed = [{"item_no": text(r.get("item_no"), 20), "description": text(r.get("description"), 200)}
               for r in rows("items_removed")]
    removed = [r for r in removed if r["item_no"] or r["description"]]
    answers = [{"question": text(r.get("question"), 400), "answer": text(r.get("answer"), 600)}
               for r in rows("answers", 40)]
    answers = [a for a in answers if a["question"] and a["answer"]]
    other = [text(x, 300) for x in (raw.get("other_changes") or [])[:40] if text(x, 300)]
    num = text(raw.get("addendum_number"), 40)
    m = re.search(r"\d{1,2}", num)
    return {"addendum_number": m.group(0) if m else "", "issued": text(raw.get("issued"), 80),
            "bid_due": text(raw.get("bid_due"), 200), "prebid": text(raw.get("prebid"), 300),
            "quantity_changes": changes, "items_added": added, "items_removed": removed,
            "answers": answers, "other_changes": other}


# ── Measuring from the plans ──
# A lump-sum job often comes with drawings and no schedule of items. The
# quantities a concrete contractor prices -- sidewalk area, curb length,
# ramp count -- are on the sheets as dimensions, callouts and quantity
# tables. The model may only work them out from what's printed, and must
# say how (the basis), so the contractor can check each one.
BID_TAKEOFF_READER_VERSION = 1
_TAKEOFF_PROMPT = (
    "You measure concrete flatwork quantities from construction PLANS for a concrete contractor. "
    "Use ONLY quantity tables, dimensions, stationing and callouts printed on the sheets. "
    "Never guess a dimension that isn't shown; leave out anything you can't work out.\n\n"
    "Return ONE JSON object: {\"items\": [{\"description\" (e.g. \"4 in. concrete sidewalk\"), "
    "\"quantity\" (number), \"unit\" (SY, SF, LF, EA or CY), \"basis\" (how you got it, e.g. "
    "\"Quantity table, sheet C-2\" or \"5 ft wide x 1,240 ft, sta 10+00 to 22+40\")}], "
    "\"notes\": anything the contractor should check by hand}. "
    "Sidewalk, curb and gutter, ADA ramps, driveway approaches, flatwork slabs only. No markdown.\n\nPLAN TEXT:\n"
)


def _clean_takeoff_result(raw):
    if not isinstance(raw, dict):
        return None
    items = []
    for it in (raw.get("items") or [])[:60]:
        if not isinstance(it, dict):
            continue
        desc = re.sub(r"\s+", " ", str(it.get("description") or "")).strip()[:200]
        qty = _num_or_none(it.get("quantity"))
        unit = str(it.get("unit") or "").strip()
        basis = re.sub(r"\s+", " ", str(it.get("basis") or "")).strip()[:300]
        if not desc or not qty or not basis:
            continue
        items.append({"item_no": "", "description": desc, "quantity": qty,
                      "unit": unit if _UNIT_RE.match(unit) else "", "basis": basis, "estimated": True})
    notes = raw.get("notes")
    return {"line_items": items, "notes": re.sub(r"\s+", " ", notes).strip()[:600] if isinstance(notes, str) else ""}


def _ai_read_bid_document(text, images=None, addendum=False, kind=None):
    images = images or []
    if not OPENAI_API_KEY or not (text.strip() or images):
        return None
    kind = kind or ("addendum" if addendum else "doc")
    prompt = {"addendum": _ADDENDUM_PROMPT, "takeoff": _TAKEOFF_PROMPT}.get(kind, _BID_DOC_PROMPT)
    content = prompt + (text if text.strip() else "(none: the pages are scanned images, attached)")
    if images:
        content = [{"type": "text", "text": content + "\n\nSCANNED PAGES are attached as images; read them too."}] + [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(im).decode("ascii"),
                                                "detail": "high"}} for im in images]
    body = json.dumps({
        "model": OPENAI_MODEL,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120 if images else 90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        got = json.loads(data["choices"][0]["message"]["content"])
        return {"addendum": _clean_addendum_result, "takeoff": _clean_takeoff_result}.get(kind, _clean_doc_result)(got)
    except Exception as ex:
        print(f"[bid-doc] ai error: {ex}", flush=True)
        return None


# ── A document the contractor uploads ──
# Many agencies hand out the plans, specs and bid form only to registered
# plan holders, so the server can't download them. The contractor can: they
# register, download, and upload the PDF here to be read or filled the same
# way. Multipart, with the files in "file" (several at once for reading: the
# plans, the bid form and an addendum are read together) and everything else
# as JSON in "json". PDFs only, each under the same cap as a download.
# Nothing uploaded is kept except the reading itself (cached by the files'
# hashes, like a URL's).
BID_DOC_MAX_FILES = 6
BID_DOC_MAX_TOTAL = int(os.environ.get("BID_DOC_MAX_TOTAL", str(40 * 1024 * 1024)))


def _doc_request(max_files=None):
    """(data, [(name, bytes)] or None, error or "")."""
    ctype = (request.content_type or "").lower()
    if not ctype.startswith("multipart/form-data"):
        return request.get_json(force=True, silent=True) or {}, None, ""
    if (request.content_length or 0) > BID_DOC_MAX_TOTAL + 1_000_000:
        return {}, None, "too_large"
    try:
        data = json.loads(request.form.get("json") or "{}")
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    files = request.files.getlist("file")[:max_files or BID_DOC_MAX_FILES]
    if not files:
        return data, None, "no_file"
    out = []
    for f in files:
        raw = f.read(BID_DOC_MAX_BYTES + 1)
        if len(raw) > BID_DOC_MAX_BYTES:
            return data, None, "too_large"
        if raw[:5] != b"%PDF-":
            return data, None, "not_pdf"
        name = re.sub(r"[^\w .()&#,-]+", "", str(f.filename or "document.pdf"))[:120] or "document.pdf"
        out.append((name, raw))
    return data, out, ""


# ── Scanned pages ──
# A photocopied bid form has no text layer: every page is a picture. Those
# pages are drawn as images (pdfium) and read by the same model, looking at
# them, with the same instructions. Only pages with next to no text are sent,
# so a typed packet with one scanned affidavit costs one image, not forty.
BID_DOC_SCAN_PAGES = int(os.environ.get("BID_DOC_SCAN_PAGES", "8"))
_SCAN_TEXT_CHARS = 40


def _scan_page_images(data, limit=None, every_page=False, width=1400):
    """JPEG bytes of the pages that are pictures rather than text (or of
    every page, for plan sheets whose drawings matter). Never raises."""
    limit = BID_DOC_SCAN_PAGES if limit is None else limit
    out = []
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(data)
    except Exception:
        return out
    try:
        for i in range(min(len(pdf), BID_DOC_MAX_PAGES)):
            if len(out) >= limit:
                break
            page = pdf[i]
            tp = page.get_textpage()
            if not every_page and len(re.sub(r"\s+", "", tp.get_text_range() or "")) >= _SCAN_TEXT_CHARS:
                continue
            w = page.get_width() or 612
            img = page.render(scale=max(0.3, min(3.0, float(width) / w))).to_pil().convert("L")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=70)
            out.append(buf.getvalue())
    except Exception as ex:
        print(f"[bid-doc] scan render error: {ex}", flush=True)
    finally:
        try:
            pdf.close()
        except Exception:
            pass
    return out


@app.route("/bid-documents/read", methods=["POST"])
def bid_documents_read():
    data, upload, err = _doc_request()
    key = data.get("key", "")
    device = data.get("device_id", "")
    if not _license_is_active(key, device, data.get("supabase_token", "")):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    if err:
        return jsonify({"ok": False, "reason": err}), 413 if err == "too_large" else 400
    url = str(data.get("url") or "").strip()
    if upload is None and not _public_http_url(url):
        return jsonify({"ok": False, "reason": "bad_url"}), 400
    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "reason": "ai_unavailable"})

    # "kind": "addendum" reads the same files for what they change instead.
    kind = data.get("kind") if data.get("kind") in ("addendum", "takeoff") else "doc"
    addendum = kind == "addendum"
    version = {"addendum": BID_ADDENDUM_READER_VERSION, "takeoff": BID_TAKEOFF_READER_VERSION}.get(kind, BID_DOC_READER_VERSION)
    ident = (b"upload:" + b"".join(hashlib.sha256(raw).digest() for _n, raw in upload)) if upload is not None else url.encode()
    cache_key = _BID_DOC_CACHE_PREFIX + ("" if kind == "doc" else kind + ":") + hashlib.sha256(ident).hexdigest()[:32]
    try:
        hit = kv_backend.get(cache_key, None)
    except Exception:
        hit = None
    # A result saved before the reader learned a field (the meeting place,
    # the job site) would hand back the old answer without it for weeks, so
    # only a result read by the current reader counts as a hit.
    if isinstance(hit, dict) and hit.get("result") and hit.get("v") == version:
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

    if upload is not None:
        docs = [(name, raw, "application/pdf") for name, raw in upload]
    else:
        raw, ctype, outcome = _fetch_document(url)
        if outcome != "ok":
            return jsonify({"ok": False, "reason": "fetch_failed", "detail": outcome})
        docs = [("", raw, ctype)]
    texts, images, pages, truncated = [], [], 0, False
    for name, raw, ctype in docs:
        t, n, cut = _document_text(raw, ctype)
        pages += n
        truncated = truncated or cut
        if t.strip():
            texts.append(f"=== FILE: {name} ===\n{t}" if len(docs) > 1 else t)
        if raw[:5] == b"%PDF-" and len(images) < BID_DOC_SCAN_PAGES:
            # Plan sheets are read as drawings, every page: a dimension on a
            # detail is only on the picture.
            images += (_scan_page_images(raw, BID_DOC_SCAN_PAGES - len(images), every_page=True, width=2000)
                       if kind == "takeoff" else _scan_page_images(raw, BID_DOC_SCAN_PAGES - len(images)))
    text = "\n\n".join(texts)[:BID_DOC_TEXT_CHARS]
    if not text.strip() and not images:
        return jsonify({"ok": False, "reason": "no_text",
                        "detail": "This document has no readable text (it may be a scan)."})
    result = (_ai_read_bid_document(text, images, addendum=True) if addendum
              else _ai_read_bid_document(text, images, kind="takeoff") if kind == "takeoff"
              else _ai_read_bid_document(text, images))
    if result is None:
        return jsonify({"ok": False, "reason": "ai_error"}), 502
    result.update({"pages_read": pages, "truncated": truncated, "scanned_pages": len(images),
                   "files": len(docs)})
    try:
        kv_backend.set(cache_key, {"at": time.time(), "v": version, "result": result})
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
    # Affidavits ask for the pieces separately: "of the City of ____,
    # County of ____, State of ____".
    m = re.match(r"\s*([^,]+),\s*([A-Za-z .]+?)\s+(\d{5}(?:-\d{4})?)\s*$", str(co.get("city_state_zip") or ""))
    if m:
        put("company.city", "Company's city", m.group(1), 80)
        put("company.state", "Company's state", m.group(2), 40)
        put("company.zip", "Company's ZIP code", m.group(3), 12)
    bid = data.get("bid") if isinstance(data.get("bid"), dict) else {}
    put("bid.number", "Bid / project number", bid.get("number"), 80)
    put("bid.title", "Project", bid.get("title"))
    put("bid.agency", "Owner / agency the bid is for", bid.get("agency"), 120)
    put("bid.location", "Project location / job site", bid.get("location"), 160)
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


# ── Forms that aren't fillable ──
# Most small-town bid forms are typed documents with underlined blanks
# ("Name of Bidder: ________"), not fillable PDFs. pdfium gives the exact
# position of every character, so each run of underscores is a blank and
# the words before it on the same line are its label. The same matcher
# pairs blanks with value names, and the values are then typed onto the page
# just above each line. Scanned forms have no characters to find and are
# reported as not fillable.
_BLANK_MIN = 3


def _flat_blanks(data):
    """[{id, page, x0, x1, y, label, line}] for the underlined blanks. Never raises."""
    blanks = []
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(data)
    except Exception:
        return blanks
    try:
        for pno in range(min(len(pdf), BID_DOC_MAX_PAGES)):
            tp = pdf[pno].get_textpage()
            n = tp.count_chars()
            text = tp.get_text_range() or ""
            if len(text) != n:
                text = "".join(tp.get_text_range(i, 1) or " " for i in range(n))
            # pdfium puts a line break between the lines it finds.
            lines, cur = [], []
            for i in range(n):
                if text[i] in "\r\n":
                    if cur:
                        lines.append(cur)
                    cur = []
                    continue
                cur.append((text[i], tp.get_charbox(i)))
            if cur:
                lines.append(cur)
            for line in lines:
                runs, i = [], 0
                while i < len(line):
                    if line[i][0] == "_":
                        j = i
                        while j < len(line) and line[j][0] == "_":
                            j += 1
                        if j - i >= _BLANK_MIN:
                            runs.append((i, j))
                        i = j
                    else:
                        i += 1
                if not runs:
                    continue
                marked, prev = [], 0
                for k, (i, j) in enumerate(runs):
                    marked.append("".join(c for c, _b in line[prev:i]))
                    marked.append(f"[B{len(blanks) + k + 1}]")
                    prev = j
                marked.append("".join(c for c, _b in line[prev:]))
                line_text = re.sub(r"\s+", " ", "".join(marked)).strip()[:240]
                prev = 0
                for i, j in runs:
                    label = re.sub(r"\s+", " ", "".join(c for c, _b in line[prev:i]).replace("_", " "))
                    label = label.strip(" :$#.-").strip()[-80:]
                    prev = j
                    xs = [b for _c, b in line[i:j]]
                    blanks.append({"id": f"B{len(blanks) + 1}", "page": pno,
                                   "x0": min(b[0] for b in xs), "x1": max(b[2] for b in xs),
                                   "y": min(b[1] for b in xs), "label": label, "line": line_text})
                    if len(blanks) >= BID_FILL_MAX_FIELDS:
                        return blanks
    except Exception as ex:
        print(f"[bid-fill] blank scan error: {ex}", flush=True)
    finally:
        try:
            pdf.close()
        except Exception:
            pass
    return blanks


def _pdf_str(t):
    t = str(t).encode("latin-1", "replace").decode("latin-1")
    return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _text_width(t, size):
    # Helvetica averages about half an em a character.
    return len(t) * size * 0.5


def _overlay_filled(data, placed):
    """The original PDF with each value typed onto its blank."""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(io.BytesIO(data))
    writer = PdfWriter(clone_from=reader)
    by_page = collections.defaultdict(list)
    for b, value in placed:
        by_page[b["page"]].append((b, value))
    for pno, items in by_page.items():
        page = writer.pages[pno]
        box = page.mediabox
        ops = []
        for b, value in items:
            room = max(20.0, b["x1"] - b["x0"] - 2)
            size = 10.0
            while size > 6 and _text_width(value, size) > room:
                size -= 0.5
            ops.append(f"BT /CCF {size:.1f} Tf {b['x0'] + 1:.2f} {b['y'] + 1.5:.2f} Td ({_pdf_str(value)}) Tj ET")
        stream = ("q 0 0 0.55 rg\n" + "\n".join(ops) + "\nQ\n").encode("latin-1", "replace")
        overlay = _simple_pdf_bytes([stream], [float(box.width), float(box.height)],
                                    [float(box.left), float(box.bottom)])
        page.merge_page(PdfReader(io.BytesIO(overlay)).pages[0])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _simple_pdf_bytes(streams, size=(612.0, 792.0), origin=(0.0, 0.0)):
    """A PDF with one page per content stream; /CCF is Helvetica, /CCB bold."""
    w, h = size
    x0, y0 = origin
    n = len(streams)
    font, bold = 3 + 2 * n, 4 + 2 * n
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            ("<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join(f"{3 + 2 * i} 0 R" for i in range(n)), n)).encode()]
    for i, st in enumerate(streams):
        objs.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [{x0:.2f} {y0:.2f} {x0 + w:.2f} {y0 + h:.2f}] "
                     f"/Resources << /Font << /CCF {font} 0 R /CCB {bold} 0 R >> >> /Contents {4 + 2 * i} 0 R >>").encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(st) + st + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def _fill_flat(raw, vals):
    """The flat-form fill: (response dict, status)."""
    blanks = _flat_blanks(raw)
    if not blanks:
        return {"ok": False, "reason": "not_fillable", "detail": "no_blanks"}, 200
    # The matcher sees each blank with its whole line, so "$ [B4]" after
    # "Total Base Bid" is still the total; the never-fill rule looks only at
    # the blank's own label, so "Signature" blocks its blank and not the
    # date beside it.
    matched = _ai_match_fields([(b["id"], b["line"]) for b in blanks], vals)
    if matched is None:
        return {"ok": False, "reason": "ai_error"}, 502
    plan = _resolve_fill([(b["id"], b["label"]) for b in blanks], vals, matched)
    if not plan:
        return {"ok": False, "reason": "nothing_matched", "fields": len(blanks)}, 200
    by_id = {b["id"]: b for b in blanks}
    placed = [(by_id[f], vals[k][1]) for f, k in plan.items()]
    try:
        pdf = _overlay_filled(raw, placed)
    except Exception as ex:
        print(f"[bid-fill] overlay error: {ex}", flush=True)
        return {"ok": False, "reason": "not_fillable", "detail": "write_failed"}, 200
    filled = [{"field": f, "label": by_id[f]["label"] or by_id[f]["line"], "value": vals[k][1], "key": k}
              for f, k in plan.items()]
    return {"ok": True, "flat": True, "fields": len(blanks), "filled": filled,
            "left_blank": len(blanks) - len(filled),
            "pdf_b64": base64.b64encode(pdf).decode("ascii")}, 200


@app.route("/bid-documents/fill", methods=["POST"])
def bid_documents_fill():
    data, upload, err = _doc_request()
    key = data.get("key", "")
    device = data.get("device_id", "")
    if not _license_is_active(key, device, data.get("supabase_token", "")):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    if err:
        return jsonify({"ok": False, "reason": err}), 413 if err == "too_large" else 400
    url = str(data.get("url") or "").strip()
    if upload is None and not _public_http_url(url):
        return jsonify({"ok": False, "reason": "bad_url"}), 400
    vals = _fill_values(data)
    if not vals:
        return jsonify({"ok": False, "reason": "nothing_to_fill"}), 400
    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "reason": "ai_unavailable"})
    if not _ip_rate_ok(_BID_FILL_RATE_KEY, key or device or _client_ip(),
                       BID_FILL_MAX_PER_KEY_PER_DAY):
        return jsonify({"ok": False, "reason": "rate_limited"}), 429

    if upload is not None:
        raw = upload[0][1]
    else:
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
        # No fillable boxes: type onto its underlined blanks instead.
        out, status = _fill_flat(raw, vals)
        return jsonify(out), status

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


# ── The bid packet ─────────────────────────────────────────────────────────
#
# Everything that goes in the envelope, in one PDF to print: a cover sheet
# with what to sign, attach and check before sealing; the bid summary; then
# each filled form. Filled forms are flattened (their typed values become
# part of the page) so two forms with a box of the same name can't overwrite
# each other once they're in one file, and so the printout is what was
# checked. No AI and nothing kept: it is assembled from what the app sends.
BID_PACKET_MAX_FILES = 10


class _PageLayout:
    """Lays out wrapped lines of Helvetica on letter pages."""

    def __init__(self):
        self.streams, self.ops, self.y = [], [], 740.0

    def _need(self, h):
        if self.y - h < 54:
            self.page()

    def page(self):
        if self.ops:
            self.streams.append("\n".join(self.ops).encode("latin-1", "replace"))
        self.ops, self.y = [], 740.0

    def text(self, t, size=10.5, bold=False, x=54.0, width=504.0, gap=None):
        gap = size * 0.45 if gap is None else gap
        words, line, lines = str(t).split(), "", []
        for w in words:
            trial = (line + " " + w).strip()
            if _text_width(trial, size) > width and line:
                lines.append(line)
                line = w
            else:
                line = trial
        lines.append(line)
        self.first_y = None
        for ln in lines:
            self._need(size + gap)
            self.y -= size + gap
            if self.first_y is None:
                self.first_y = self.y
            self.ops.append(f"BT /{'CCB' if bold else 'CCF'} {size:.1f} Tf {x:.1f} {self.y:.1f} Td ({_pdf_str(ln)}) Tj ET")

    def row(self, cells, size=9.0, bold=False):
        """cells: [(x, width, text, right-aligned)]; one line each, clipped."""
        self._need(size + 5)
        self.y -= size + 5
        for x, width, t, right in cells:
            t = str(t)
            while t and _text_width(t, size) > width:
                t = t[:-1]
            tx = x + width - _text_width(t, size) if right else x
            self.ops.append(f"BT /{'CCB' if bold else 'CCF'} {size:.1f} Tf {tx:.1f} {self.y:.1f} Td ({_pdf_str(t)}) Tj ET")

    def check(self, t, size=10.5):
        """A tick box and its words."""
        self.space(3)
        self.text(t, size, x=74, width=480)
        if self.first_y is not None and self.y <= self.first_y:
            # On the first line's page unless the words broke across pages.
            self.ops.append(f"0.8 w 58 {self.first_y - 1:.1f} 9 9 re S")

    def rule(self):
        self._need(6)
        self.y -= 4
        self.ops.append(f"0.6 w 54 {self.y:.1f} m 558 {self.y:.1f} l S")

    def space(self, h=8.0):
        self.y -= h

    def done(self):
        self.page()
        return self.streams


def _packet_front(data, names):
    """Cover sheet and bid summary pages for the packet, as PDF bytes."""
    def s(v, n=300):
        return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()[:n]
    bid = data.get("bid") if isinstance(data.get("bid"), dict) else {}
    co = data.get("company") if isinstance(data.get("company"), dict) else {}
    L = _PageLayout()
    L.text("Bid packet", 20, True)
    L.text(s(bid.get("title")) or "Bid", 13, True)
    for label, v in (("Owner", bid.get("agency")), ("Job site", bid.get("location")), ("Bid number", bid.get("number")),
                     ("Bids due", bid.get("due")), ("Bidder", co.get("name"))):
        if s(v):
            L.text(f"{label}: {s(v)}", 10.5)
    try:
        total = float(bid.get("total"))
    except (TypeError, ValueError):
        total = 0.0
    if total > 0:
        L.text(f"Total bid: ${_money(total)}  ({_dollars_in_words(total)})", 10.5, True)
    L.space(10)
    L.rule()
    L.text("Before you seal the envelope", 13, True)
    L.space(2)
    checks = [s(c, 240) for c in (data.get("checklist") or [])[:30] if s(c)]
    for c in checks:
        L.check(c)
    if s(bid.get("submission")):
        L.space(4)
        L.text("How to submit: " + s(bid.get("submission"), 600), 10)
    L.space(10)
    L.rule()
    L.text("In this packet", 13, True)
    L.space(2)
    L.text("1. This cover sheet (keep it, don't submit it)", 10.5, x=60)
    L.text("2. Bid summary", 10.5, x=60)
    for i, n in enumerate(names, start=3):
        L.text(f"{i}. {n}", 10.5, x=60)
    lines = [ln for ln in (data.get("lines") or [])[:200] if isinstance(ln, dict)]
    L.page()
    L.text("Bid summary", 16, True)
    L.text(" · ".join(x for x in (s(co.get("name")), s(co.get("phone")), s(co.get("email"))) if x), 10)
    L.space(6)
    cols = [(54, 34), (92, 250), (346, 80), (430, 60), (494, 64)]
    L.row([(cols[0][0], cols[0][1], "Item", False), (cols[1][0], cols[1][1], "Description", False),
           (cols[2][0], cols[2][1], "Quantity", True), (cols[3][0], cols[3][1], "Unit price", True),
           (cols[4][0], cols[4][1], "Amount", True)], bold=True)
    L.rule()
    for ln in lines:
        def num(k):
            try:
                return float(ln.get(k))
            except (TypeError, ValueError):
                return None
        q, up, amt = num("quantity"), num("unit_price"), num("amount")
        qty = (f"{q:,.2f}".rstrip("0").rstrip(".") + " " + s(ln.get("unit"), 12)).strip() if q else ""
        L.row([(cols[0][0], cols[0][1], s(ln.get("item_no"), 10), False),
               (cols[1][0], cols[1][1], s(ln.get("description"), 120), False),
               (cols[2][0], cols[2][1], qty, True),
               (cols[3][0], cols[3][1], _money(up) if up else "", True),
               (cols[4][0], cols[4][1], _money(amt) if amt else "", True)])
    if not lines:
        L.text("No priced lines.", 10)
    L.rule()
    if total > 0:
        L.row([(54, 370, "Total bid", False), (430, 128, "$" + _money(total), True)], size=11, bold=True)
    if s(bid.get("addenda")):
        L.space(6)
        L.text("Addenda acknowledged: " + s(bid.get("addenda"), 120), 10)
    return _simple_pdf_bytes(L.done())


def _flattened(raw):
    """A filled form with its typed values printed into the page and its
    form boxes removed. Falls back to the PDF as it came."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DictionaryObject, NameObject
    reader = PdfReader(io.BytesIO(raw))
    try:
        fields = reader.get_fields() or {}
    except Exception:
        fields = {}
    values = {k: str(f.get("/V")) for k, f in fields.items()
              if isinstance(f, dict) and f.get("/FT") == "/Tx" and f.get("/V")}
    writer = PdfWriter(clone_from=reader)
    if values:
        for page in writer.pages:
            if "/Resources" not in page:
                page[NameObject("/Resources")] = DictionaryObject()
            if page.get("/Annots"):
                writer.update_page_form_field_values(page, values, auto_regenerate=False, flatten=True)
    if fields:
        writer.remove_annotations(subtypes="/Widget")
        if "/AcroForm" in writer._root_object:
            del writer._root_object["/AcroForm"]
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@app.route("/bid-documents/packet", methods=["POST"])
def bid_documents_packet():
    data, uploads, err = _doc_request(BID_PACKET_MAX_FILES)
    key = data.get("key", "")
    device = data.get("device_id", "")
    if not _license_is_active(key, device, data.get("supabase_token", "")):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    if err and err != "no_file":
        return jsonify({"ok": False, "reason": err}), 413 if err == "too_large" else 400
    uploads = uploads or []
    from pypdf import PdfReader, PdfWriter
    try:
        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(_packet_front(data, [n for n, _r in uploads]))))
        for name, raw in uploads:
            try:
                part = _flattened(raw)
            except Exception as ex:
                print(f"[bid-packet] flatten failed for {name}: {ex}", flush=True)
                part = raw
            writer.append(PdfReader(io.BytesIO(part)))
        buf = io.BytesIO()
        writer.write(buf)
    except Exception as ex:
        print(f"[bid-packet] build error: {ex}", flush=True)
        return jsonify({"ok": False, "reason": "packet_failed"}), 500
    return jsonify({"ok": True, "pages": len(writer.pages),
                    "pdf_b64": base64.b64encode(buf.getvalue()).decode("ascii")})


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
        return {"docs": [], "addenda": [], "addendum_docs": [], "hash": hashlib.sha256(data).hexdigest()[:16]}
    html = data.decode("utf-8", "ignore")
    text = _html_to_text(html)
    found = bid_sources.detail_documents(html, url, limit=40)
    docs = [d["name"] for d in found]
    addenda = sorted({int(n) for n in _ADDENDUM_RE.findall(text) if 0 < int(n) < 100})
    # The addendum documents themselves, so the app can open and read them
    # rather than only say one exists.
    addendum_docs = [d for d in found if re.search(r"addend", d["name"] + " " + d["url"], re.I)][:10]
    norm = re.sub(r"\s+", " ", text).strip().lower()
    return {"docs": docs, "addenda": addenda, "addendum_docs": addendum_docs,
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
