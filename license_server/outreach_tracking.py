# ── Outreach link clicks, counted first-party ──
#
# /go/<slug> is a 200 rewrite to the landing page, one slug per contractor
# emailed, so the address bar identifies the recipient without a second page
# existing. Counting those visits was left to Cloudflare Web Analytics, which
# is a third-party JavaScript beacon: ad blockers strip it, and the reported
# number is therefore a fraction of the truth with no way to tell what
# fraction. Eight clicks read as "nobody opened it" when the real figure was
# unknowable.
#
# This counts the same visits from the page to its own backend. A first-party
# request to the domain the reader is already on survives blockers that drop
# a third-party script, the number lands in storage we own rather than a
# retention window we rent, and reading it needs no Cloudflare credential.
#
# Deliberately stores no IP, no user agent and no timestamp per visit -- a
# count and two dates per slug. It answers "did this contractor open it",
# which is all the outreach needs, and nothing about who or from where.
_CLICK_KEY = "bidcaller:go_clicks"
# Slugs are ours, written into _redirects by hand. Anything else is noise or
# someone poking the endpoint, and a cap stops either filling the store.
_CLICK_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_CLICK_MAX_SLUGS = int(os.environ.get("CLICK_MAX_SLUGS", "500"))
_click_lock = threading.Lock()


@app.route("/click", methods=["POST"])
def click():
    """Record that someone opened a /go/<slug> outreach link.

    Public and unauthenticated, like /coverage, because the page doing the
    reporting is public. The blast radius of that is a wrong count on a
    private dashboard, which is why nothing here is trusted for anything
    else and why the store cannot grow without bound.
    """
    data = request.get_json(force=True, silent=True) or {}
    slug = str(data.get("slug") or "").strip().lower()
    if not _CLICK_SLUG_RE.match(slug):
        return jsonify({"ok": False, "reason": "bad_slug"}), 400
    now = datetime.datetime.now().isoformat(timespec="seconds")
    with _click_lock:
        store = kv_backend.get(_CLICK_KEY, None)
        if not isinstance(store, dict):
            store = {}
        row = store.get(slug)
        if not isinstance(row, dict):
            if len(store) >= _CLICK_MAX_SLUGS:
                # Full. Keep counting the slugs already known rather than
                # letting an unknown one evict real data.
                return jsonify({"ok": True, "recorded": False})
            row = {"n": 0, "first": now}
        row["n"] = int(row.get("n") or 0) + 1
        row["last"] = now
        store[slug] = row
        kv_backend.set(_CLICK_KEY, store)
    return jsonify({"ok": True, "recorded": True})


@app.route("/coverage", methods=["POST"])
def coverage():
    """How many verified bid pages we hold within a radius of somewhere.

    Public and unauthenticated on purpose: this is what lets someone check
    their own area BEFORE paying. Coverage is genuinely uneven -- a 50mi
    radius around Boston reaches ~149 verified agencies and one around
    Springfield, MO reaches ~9 -- and a contractor who finds that out after
    subscribing is a refund and a bad review. Better they see it first.

    Cheap by construction: a geocode (cached) plus arithmetic against
    coordinates already on disk. No search credits, no AI call, nothing that
    scales with cost -- so leaving it open costs essentially nothing.
    """
    data = request.get_json(force=True, silent=True) or {}
    location = (data.get("location") or "").strip()
    try:
        radius = float(data.get("radius") or 50)
    except (TypeError, ValueError):
        radius = 50.0
    radius = max(5.0, min(radius, 250.0))
    if not location:
        return jsonify({"ok": False, "reason": "no_location"}), 400
    center = _resolve_center(location)
    if not center:
        return jsonify({"ok": False, "reason": "unresolved_location"}), 404
    try:
        towns = bid_portals.towns_within_radius(
            bid_portals.load_directory(), center["lat"], center["lon"], radius)
    except Exception as ex:
        print(f"[coverage] lookup failed: {ex}", flush=True)
        return jsonify({"ok": False, "reason": "lookup_failed"}), 500
    towns.sort(key=lambda t: _miles_between(center["lat"], center["lon"], t[2], t[3]))
    return jsonify({
        "ok": True,
        "location": f"{center['city']}, {center['state']}".strip(", "),
        "radius": int(radius),
        # Direct-read coverage only. The scan ALSO searches for county,
        # school-district and state-portal work that has no entry here, so
        # this is a floor on what a scan reaches, not a ceiling -- said
        # plainly in the UI rather than quietly inflating the number.
        "agencies": len(towns),
        "nearest": [f"{c}, {s}" for c, s, _, _ in towns[:8]],
        # How far the closest one actually is. Without this a caller can only
        # test whether the asked-for town appears in `nearest`, which is a
        # bad proxy: Skippack PA has no bid page of its own and its three
        # closest are Lansdale, Conshohocken and Souderton -- all inside ten
        # miles, and the location resolved perfectly. That test also passes
        # things it should not. The honest question is "how far is the
        # nearest work", and that is a number, so return it.
        "nearest_mi": (round(_miles_between(center["lat"], center["lon"],
                                            towns[0][2], towns[0][3]), 1)
                       if towns else None),
    })


@app.route("/health", methods=["GET"])
def health_detail():
    """Which backends are actually wired up, and is local search still working.

    Exists because the failure that matters most here is silent: if a search
    backend is unset or has started getting blocked, /scan still returns 200
    with a smaller, worse set of bids and nothing anywhere says why. This
    answers "is the search engine actually at full strength right now" from a
    browser, without running a scan or spending an API call.

    Reports only whether each secret is present, never any part of its value,
    so it is safe to leave unauthenticated the way the plain / probe is.
    """
    # Never call out to a provider here: this endpoint has to stay instant and
    # free, and a hung upstream must not make the health check itself look down.
    # Read once, from the durable record: in-memory is empty after
    # every restart, which is exactly when somebody is looking.
    sam = _sam_health_read()
    backends = {
        "openai": bool(OPENAI_API_KEY),          # AI bid extraction — /scan is inert without it
        "brave_search": bool(BRAVE_API_KEY),     # primary local search
        "tavily": bool(TAVILY_API_KEY),          # optional paid fallback
        "sam_gov": bool(SAM_API_KEY),            # federal bids
        "supabase": bool(SUPABASE_URL and SUPABASE_ANON_KEY),
        "upstash_redis": bool(UPSTASH_URL and UPSTASH_TOKEN),
        "durable_storage": kv_backend.is_durable(),
        "resend_email": bool(RESEND_API_KEY),
        "saved_search_alerts": bool(SUPABASE_SERVICE_ROLE_KEY and CRON_SECRET and RESEND_API_KEY),
        # Weekly planned-work alerts need everything the daily job needs, plus
        # an extraction key -- /upcoming has no non-AI path.
        "upcoming_alerts": bool(SUPABASE_SERVICE_ROLE_KEY and CRON_SECRET
                                and RESEND_API_KEY and OPENAI_API_KEY),
        # The campaign sender refuses outright without a postal address, so
        # "did MAILING_ADDRESS actually take effect" is answerable from the
        # Diagnostics screen instead of by attempting a real send.
        "campaign_sender": bool(RESEND_API_KEY and MAILING_ADDRESS.strip()
                                and _admin_configured()),
        # Without this, bounces and spam complaints are invisible and the
        # list never cleans itself -- which is how a sending domain ends up
        # in everyone's junk folder, taking the trial keys and bid alerts
        # real customers are waiting on with it.
        "resend_webhook": bool(RESEND_WEBHOOK_SECRET),
    }
    tav = _tavily_health()
    brave = _brave_health()
    email_health = _email_health()
    with _ddg_lock:
        ddg_streak = _ddg_fail_streak
    # DuckDuckGo is scraped, so it can be blocked outright. That only threatens
    # local search when Tavily isn't configured to take over.
    ddg = {
        "consecutive_empty_searches": ddg_streak,
        "degraded": ddg_streak >= DDG_TRIP_THRESHOLD,
        "is_sole_local_search": not (backends["brave_search"] or backends["tavily"]),
    }
    problems = []
    # Things worth saying that are not degradations. Kept separate from
    # problems on purpose: problems drive "status": "degraded", and a
    # configuration that is merely less durable than it could be should not
    # make a healthy service look sick.
    notes = []
    # A configured-but-rejected key is worse than an absent one: everything
    # keeps returning 200 and scans just quietly come back nearly empty.
    if backends["brave_search"] and brave["quota_or_auth_failure"]:
        problems.append(
            f"Brave Search is rejecting queries (HTTP {brave['last_status']}) — 429 means "
            "either the one-per-second limit or the monthly free credit is spent; "
            "401/403 means the key is wrong or the subscription lapsed. Local bid search "
            "has fallen back to Tavily or scraping DuckDuckGo.")
    if backends["tavily"] and tav["quota_or_auth_failure"]:
        problems.append(
            f"Tavily is rejecting searches (HTTP {tav['last_status']}) — most likely the "
            "monthly credit allowance is spent. Local bid search has fallen back to "
            "scraping DuckDuckGo and scans will look almost empty until this clears.")
    elif backends["tavily"] and tav["failing"]:
        problems.append("Every Tavily search this run has failed — local bid search is degraded.")
    # Same "configured but rejected is worse than absent" shape as Tavily
    # above: an API key being present tells you nothing about whether Resend
    # will actually accept a send (their sandbox from-address, the default,
    # can only deliver to the account's own verified email) -- this is what
    # actually caught /support silently 500ing in production.
    if backends["resend_email"] and email_health["failing"]:
        problems.append(
            f"Every email send this run has failed (HTTP {email_health['last_status']}: "
            f"{email_health['last_error']}) — support messages, license-key delivery, "
            "referral notices and admin alerts are all silently not arriving. If "
            "FROM_EMAIL is still the onboarding@resend.dev default, Resend's sandbox "
            "address can only send to the account's own verified email — verify a "
            "real domain or point SUPPORT_EMAIL at that verified address.")
    if not backends["openai"]:
        problems.append(
            "OPENAI_API_KEY unset — search-discovered bids and /upcoming are "
            "off. Direct portal reads and state lettings still work.")
    if ddg["is_sole_local_search"] and ddg["degraded"]:
        problems.append("DuckDuckGo appears blocked and no search API is configured — "
                        "local bid search is effectively down. Set BRAVE_API_KEY "
                        "(free tier, roughly 1,000 queries a month).")
    elif ddg["is_sole_local_search"]:
        problems.append("No search API configured — local search depends solely on "
                        "scraping DuckDuckGo. Set BRAVE_API_KEY.")
    # A configured-but-rejected key is worse than no key: /health says
    # sam_gov is True, everything returns 200, and federal bids quietly come
    # only from the fallback. Name it, and name the fix -- 403 from
    # api.sam.gov is literally API_KEY_INVALID, and the commonest cause is
    # now the reverse of what it used to be: an api.data.gov/signup key,
    # which stopped working here once SAM.gov moved the Get Opportunities
    # API off the api.data.gov proxy. The credential that works is requested
    # from sam.gov itself (Account Details -> Public API Key). Confirmed by
    # tools/diagnose_sam_endpoint.py against both key types before this
    # flipped -- do not flip it back without re-running that script.
    if _keyed_breaker_open() and not _federal_breaker_open():
        notes.append(
            "SAM's documented API is being skipped after %d failed scans "
            "(last: %s); federal bids are coming from sam.gov's public "
            "search instead, which is working. Retries automatically."
            % ((_breakers().get("keyed") or {}).get("fails", 0),
                 sam["last_status"]))
    if _federal_breaker_open():
        notes.append(
            "Federal bids are paused: SAM has failed %d scans in a row "
            "(last: %s). It retries automatically. Municipal and state bids "
            "are unaffected -- federal contributed 2 bids across the last "
            "ten scans, so this costs little."
            % ((_breakers().get("federal") or {}).get("fails", 0),
                 sam["last_status"]))
    if backends["sam_gov"] and sam["last_status"] == 403:
        problems.append(
            "SAM_API_KEY is being rejected (403 API_KEY_INVALID). Federal "
            "bids are still arriving via sam.gov's public search, so this is "
            "not an outage, but the documented API is unavailable. Sign in "
            "to sam.gov, open Account Details, and request a Public API Key "
            "there, then set SAM_API_KEY to it — a key from "
            "https://api.data.gov/signup is a different credential and will "
            "not work here.")
    elif backends["sam_gov"] and sam["last_status"] == 429:
        notes.append("SAM_API_KEY is rate-limited (429). Federal bids fall "
                     "back to sam.gov's public search meanwhile.")
    if not backends["sam_gov"]:
        # Not a problem any more, just a downgrade: without a key the federal
        # reader falls back to sam.gov's public search, which serves the same
        # data but is an undocumented endpoint and so could change shape
        # without notice. Worth saying, not worth alarming about.
        notes.append("SAM_API_KEY unset — federal bids are using sam.gov's "
                     "public search rather than the documented API. Both "
                     "work; a free key at api.data.gov/signup makes the "
                     "federal source contractually stable.")
    if not kv_backend.is_durable():
        problems.append(
            "No durable storage configured — licences, trial records, the portal "
            "directory and the geocode cache are written to Render's disk, which "
            "is wiped on every deploy and whenever a free instance sleeps. Set "
            "UPSTASH_REDIS_REST_URL/TOKEN, or apply supabase_kv_schema.sql to use "
            "the Supabase project already configured here.")
    # Public vs admin. /health is unauthenticated on purpose -- uptime checks
    # and the keep-warm job need it, and "which backends are configured" is
    # not a secret. But the scan history is: `recent_scans` is a list of the
    # places this account's users have been prospecting, and at one or two
    # customers that is simply their territory, published at a guessable URL.
    # Provider error bodies can also echo request detail. Both move behind
    # the admin token.
    body = {
        "service": "Bid Caller Pro License Server",
        "status": "ok" if not problems else "degraded",
        # Which build is actually answering. Without this there is no way to
        # tell a deploy that picked up a fix from one that silently did not --
        # every other field looks identical either way. Render sets
        # RENDER_GIT_COMMIT; empty elsewhere, which is honest rather than
        # guessed.
        "version": _env_secret("RENDER_GIT_COMMIT", "")[:7],
        "backends": backends,
        "local_search": ddg,
        # Counts and states, but not the provider's response body.
        "brave_search": {k: brave[k] for k in
                          ("ok", "failed", "last_status",
                           "quota_or_auth_failure", "failing")},
        "tavily": {k: tav[k] for k in
                   ("ok", "failed", "last_status",
                    "quota_or_auth_failure", "failing")},
        "email": {k: email_health[k] for k in
                  ("ok", "failed", "last_status", "failing")},
        # Endpoint and status, never credentials -- the same split "search"
        # and "email" use above. A stale SAM_SEARCH_URL env var still
        # pointing at the old api.sam.gov/prod address is the likeliest
        # reason a configured key yields nothing, and it was invisible from
        # outside: the Smiley scan could only report "failed". last_status
        # names it -- 403 rejected key, 404 wrong endpoint, 429 rate limit.
        "sam_gov": {
            "endpoint": SAM_SEARCH_URL,
            "key_configured": bool(SAM_API_KEY),
            "window_days": SAM_WINDOW_DAYS,
            "last_status": sam["last_status"],
            # When that status was recorded. A status with no time
            # beside it cannot be told from one three restarts old.
            "last_status_at": sam["at"],
            # Whether the source is currently being rested, and how close it
            # is to being. Without this a federal count of zero looks the
            # same whether SAM is down or the radius is simply quiet.
            "resting": _federal_breaker_open(),
            "consecutive_failures": (_breakers().get("federal") or {}).get("fails", 0),
            # Reported separately from the above, because they mean
            # different things: a rested KEY means the documented contract
            # is unavailable and the public endpoint is carrying the source,
            # which is not a federal outage.
            "keyed_resting": _keyed_breaker_open(),
            "keyed_failures": (_breakers().get("keyed") or {}).get("fails", 0),
        },
        "problems": problems,
        "notes": notes,
    }
    if not _admin_ok(request.headers.get("X-Admin-Token")):
        return jsonify(body)

    body.update({
        "storage": kv_backend.health(),
        # The most recent scan's funnel. This is the fastest way to tell a
        # genuinely quiet area from a pipeline dropping everything it found.
        "last_scan": kv_backend.get("bidcaller:last_scan", None),
        # ...and the ones before it, so a change in recall reads as a trend
        # rather than a single number with nothing to compare it against.
        "recent_scans": _recent_scans(),
        # What the last nightly accuracy audit measured.
        "feed_audit": kv_backend.get(BID_AUDIT_KEY, None),
        "search_depth": TAVILY_DEPTH,
        # The recall knobs, so what's actually running is visible without
        # reading Render's env-var screen. All are env-tunable; raising them
        # trades scan time and OpenAI spend for coverage.
        "scan_config": {
            "max_pages_per_town": MAX_PAGES,          # SCAN_MAX_PAGES
            "page_workers": PAGE_WORKERS,             # SCAN_PAGE_WORKERS
            "max_pages_per_domain": MAX_PAGES_PER_DOMAIN,  # SCAN_MAX_PAGES_PER_DOMAIN
            "max_anchor_towns": MAX_ANCHOR_TOWNS,     # SCAN_MAX_ANCHORS
            "max_known_towns": MAX_KNOWN_TOWNS,       # SCAN_MAX_KNOWN_TOWNS
            "federal_window_days": SAM_WINDOW_DAYS,  # SAM_WINDOW_DAYS
            "geo_miss_retry_hours": GEO_MISS_RETRY_HOURS,
            "model": OPENAI_MODEL,                    # OPENAI_MODEL
        },
    })
    body["sam_gov"]["last_error"] = sam["last_error"]
    body["brave_search"]["last_error"] = brave["last_error"]
    body["tavily"]["last_error"] = tav["last_error"]
    body["email"]["last_error"] = email_health["last_error"]
    return jsonify(body)
