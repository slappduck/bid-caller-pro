
# ── Live scan progress ──────────────────────────────────────────────────────
#
# A scan takes over a minute and the app showed a bar driven by a guess at how
# long it usually takes. It counts up whether anything is happening or not,
# which is the one thing a progress bar must not do -- a stalled scan and a
# working one looked identical, and a minute of that is what makes a tool feel
# broken even when it is fine.
#
# The scan publishes where it actually is, and the app reads it. Deliberately
# additive: /scan is unchanged, the record is best-effort, and a client that
# cannot read it falls back to the old estimate. Nothing here is allowed to
# fail a scan.
_PROGRESS_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_PROGRESS_TTL_SEC = int(os.environ.get("SCAN_PROGRESS_TTL", "600"))


def _progress_key(token):
    return "bidcaller:scan_progress:" + token


def _progress_note(token, phase, found=None, done=False):
    """Publish one step of a running scan. Never raises."""
    if not token or not _PROGRESS_RE.match(str(token)):
        return
    try:
        kv_backend.set(_progress_key(token), {
            "phase": phase,
            "found": found,
            "done": bool(done),
            "at": time.time(),
        })
    except Exception:
        pass


@app.route("/scan/progress", methods=["POST"])
def scan_progress():
    """Where the caller's scan has got to. Cheap, and safe to poll.

    Carries no bid content -- a phase name and a count -- so it needs no
    licence check: the token is a random string the caller just minted, and
    knowing one tells you nothing you did not already know.
    """
    data = request.get_json(force=True, silent=True) or {}
    token = str(data.get("token") or "")
    if not _PROGRESS_RE.match(token):
        return jsonify({"ok": False, "reason": "bad_token"}), 400
    try:
        rec = kv_backend.get(_progress_key(token), None)
    except Exception:
        rec = None
    if not rec:
        return jsonify({"ok": True, "known": False})
    # Anything older than the TTL is a scan that died without finishing.
    if time.time() - float(rec.get("at") or 0) > _PROGRESS_TTL_SEC:
        return jsonify({"ok": True, "known": False})
    return jsonify({"ok": True, "known": True, "phase": rec.get("phase"),
                    "found": rec.get("found"), "done": rec.get("done")})


def _stage(stats, name, deadline, fn, *args, **kw):
    """Run one optional scan stage, timing it and skipping it if the scan is
    out of time.

    The timing is the point as much as the skipping. Where a scan spends its
    seconds was, until now, something to be inferred from the outside -- the
    state stage turned out to be 2s of letting page and 18s of plan-holder
    fetches, which no amount of reading the code made obvious.
    """
    if deadline is not None and time.time() >= deadline:
        if stats is not None:
            stats["skipped_" + name] = 1
        return None
    t0 = time.time()
    try:
        return fn(*args, **kw)
    finally:
        if stats is not None:
            stats["ms_" + name] = int((time.time() - t0) * 1000)

SCAN_HISTORY_KEY = "bidcaller:scan_history"
SCAN_HISTORY_MAX = int(os.environ.get("SCAN_HISTORY_MAX", "500"))
# How many of those rows the diagnostic endpoints print by default.
#
# Retention and display are two different questions and used to share one
# number. Twenty-five rows is the right amount to read on a health page and
# the wrong amount to keep: a handful of people trying the product roll the
# log over within an afternoon, so the scans worth studying are exactly the
# ones thrown away. Keep a launch week, print a screenful, and let
# /diag?scans=N reach further back when there is a reason to.
#
# Size is the thing that caps this rather than taste: each row is roughly
# half a kilobyte and the whole list is rewritten on every scan, so 500 rows
# is about a 250KB read-modify-write per scan. That is comfortable on the
# current store and would not be at 5,000.
SCAN_HISTORY_SHOW = int(os.environ.get("SCAN_HISTORY_SHOW", "25"))


def _scan_effort(stats, towns):
    """How much work the scan did, in terms a contractor understands.

    A scan that finds nothing currently says "No open bids posted here right
    now" and stops. That reads as a broken app rather than a quiet week --
    the effort is completely invisible, and it is considerable: a 125-mile
    scan reads dozens of agency bid pages across dozens of towns.

    Counted here rather than in the browser so the app never has to know
    what a funnel key means; these names change as the pipeline does.
    """
    stats = stats or {}
    # Every counter that represents one portal having been READ, whatever
    # the outcome was.
    portal_keys = ("civicplus_no_open_bids", "portal_no_niche_content",
                   "civicplus_parse_miss", "portal_page_missing",
                   "portal_wrong_module", "portal_moved_to_hosted")
    # Agency bid pages, counted separately from the individual solicitations
    # opened off them. Both are pages a contractor would otherwise open by
    # hand, but they are different things and a number shown to a customer
    # should not quietly merge them: 25 bid boards and 7 postings is not the
    # same claim as "32 sites".
    sites = sum(int(stats.get(k) or 0) for k in portal_keys)
    sites += sum(int(v or 0) for k, v in stats.items()
                 if k.startswith("portal_fetch_"))
    postings = int(stats.get("postings_read") or 0)
    # Everything the relevance filter actually looked at.
    examined = (int(stats.get("filtered_not_niche") or 0)
                + int(stats.get("kept") or 0))
    return {"sites_read": sites,
            "postings_read": postings,
            # Kept for the scan record, which already reports this name.
            "portals_read": sites + postings,
            "postings_examined": examined,
            "towns_read": int(towns or 0)}


def _append_scan_history(record):
    """Keep a rolling log of recent scans.

    Only the single most recent scan was ever stored, overwritten every time,
    so there was nothing to compare against — "is search getting better?" could
    not be answered from the data, only from whoever happened to be watching.
    A short history makes a change in recall visible as a trend.

    Deliberately compact: no sample, no per-bid detail. The point is to see
    twenty scans at once, and the newest one is in last_scan in full.
    """
    slim = {k: record.get(k) for k in
            ("at", "location", "radius", "kept", "raw_local", "anchor_towns")}
    slim["funnel"] = record.get("funnel") or {}
    try:
        history = kv_backend.get(SCAN_HISTORY_KEY, None) or []
        if not isinstance(history, list):
            history = []
    except Exception:
        history = []
    history.append(slim)
    # Read-modify-write is not atomic here. Scans are infrequent enough that
    # the worst case is one lost row in a log, which is not worth a lock.
    kv_backend.set(SCAN_HISTORY_KEY, history[-SCAN_HISTORY_MAX:])


def _count_kept_closed(grouped, stats):
    """Record how many kept bids are not open, so the funnel agrees with the
    reported total. Taken at the end of a scan rather than during placement,
    because enrichment can turn up a deadline that closes a bid after the fact.
    """
    if stats is None:
        return
    stats.pop("kept_but_closed", None)
    closed = sum(1 for v in (grouped or {}).values()
                 for b in v if not _is_open_bid(b))
    if closed:
        stats["kept_but_closed"] = closed


def _flag_misplaced_bids(grouped, pdb, stats=None):
    """Count bids whose own URL names a town other than the one they sit under.

    A self-check, not a filter. The guard in _place_bid stops the known way a
    bid gets lent the wrong town, and this counts anything that still slips
    through by some other route -- so the next instance of this class shows up
    in /diag instead of waiting for a customer to notice a card claiming a job
    is 16 miles away when it is in another state.

    Deliberately does not drop anything. A counter that is wrong costs a line
    in a funnel; a filter that is wrong costs real work.
    """
    if stats is None or not pdb:
        return 0
    bad = 0
    for label, bids in (grouped or {}).items():
        town = str(label or "").split(",")[0].strip()
        # County buckets come from state lettings and are placed by centroid,
        # not by a town name, so the URL has nothing to agree with.
        if not town or town.lower().endswith(" county"):
            continue
        for bid in bids:
            try:
                if bid_portals.url_names_other_place(bid.get("url"), town, pdb):
                    bad += 1
            except Exception:
                pass
    if bad:
        stats["placed_url_town_mismatch"] = bad
    return bad


def _enrich_placed_bids(grouped, stats=None):
    """Fill in contacts and deadlines for bids that survived the radius filter.

    Runs against every source, not just the structured reader, and only on
    bids that actually made it into the result — so the cost is a dozen fetches
    at most, rather than one per thing the search turned up.
    """
    # A name is not a way to reach anybody. The AI extractor returns a
    # "contact" field and routinely fills it with something like "Purchasing
    # Department" while leaving email and phone blank -- and treating that as
    # already-enriched meant the posting behind it was never read for the
    # actual phone number. On a live Springfield 75mi scan that left 24 kept
    # bids with exactly ONE eligible for enrichment and contacts_found: 0.
    # Only a phone or an email makes a bid callable, so only those count as
    # done; a name already present is preserved either way, since
    # _enrich_from_detail_pages only fills fields that are empty.
    # A state letting row's url is the LETTING PAGE, shared by every row on it,
    # not that job's own posting. Enriching them would fetch one page up to
    # fourteen times to learn nothing -- the listing carries no per-job contact
    # -- and would spend the whole budget doing it. Same reasoning the AI path
    # already applies to bids still pointing at the listing they came from.
    todo = [b for bids in (grouped or {}).values() for b in bids
            if isinstance(b, dict) and b.get("url")
            and b.get("source") != "state_dot"
            and not (b.get("email") or b.get("phone"))]
    # Undated bids first (a missing deadline is worse than a missing phone
    # number, because it also stops an expired listing being recognised), then
    # open ones, then bids already known to be closed -- see _enrichment_order.
    todo = _enrichment_order(todo)
    _note_enrich_budget(todo, ENRICH_MAX, stats)
    _enrich_from_detail_pages(todo[:ENRICH_MAX], stats=stats)
    for bid in todo[:ENRICH_MAX]:
        _apply_deadline_status(bid)
    for bids in (grouped or {}).values():
        for bid in bids:
            _apply_stale_year(bid)


_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
# A project code is not a date. Alabama numbers jobs "ATRP2-52-2024-263" and
# the 2024 in the middle is a sequence, not the year the work is from -- read
# as a year it closed a live August 2026 job on sight. Letter-led hyphenated
# codes are removed before the year scan; purely numeric ones like "2024-17"
# are left alone, because after "Bid No." that usually IS the year it was
# issued and closing it is right.
_PROJECT_CODE_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+\b")

# Where a posting is FILED is often the only statement of how old it is. A
# live board showed "Concrete Sidewalks and ADA Ramps Project" as open with no
# closing date; neither its title nor its scope named a year, and the only
# evidence it was from July 2025 sat in the URL:
#   .../fairfield/Purchasing/2025/2025-07 ITB Southport Community...
#
# Only DATE-SHAPED PATH SEGMENTS count -- a segment that is a year, or that
# begins with year-month. Not the query string and not stray digits: a
# CivicPlus posting is addressed "Bids.aspx?bidID=2024", where 2024 is a row
# id, and reading that as a year would close a brand new bid.
_PATH_YEAR_RE = re.compile(r"^(20\d{2})(?:[-_/]\d{1,2})?(?:\b|_|$)")


def _url_path_years(url):
    """Years stated by a URL's path segments, e.g. ".../2025/2025-07 ITB..."."""
    try:
        path = urllib.parse.urlsplit(str(url or "")).path
        path = urllib.parse.unquote(path)
    except ValueError:
        return []
    out = []
    for segment in path.split("/"):
        m = _PATH_YEAR_RE.match(segment.strip())
        if m:
            out.append(int(m.group(1)))
    return out


def _apply_stale_year(bid):
    """Close an undated bid whose own title is from a past year.

    A live scan returned four rows titled "2025 Sidewalk Program - Scope of
    Work SW-1..4" as open, in August 2026. They carried no deadline, and with
    nothing to check against, an undated bid is assumed open — which is right
    for a genuinely new posting and wrong for last year's programme still
    sitting on a portal. Only fires when the deadline is empty AND every year
    named is in the past, so "2025-2026 Programme" is left alone.
    """
    if not isinstance(bid, dict) or str(bid.get("deadline") or "").strip():
        return bid
    if not _is_open_bid(bid):
        return bid
    blob = _PROJECT_CODE_RE.sub(
        " ", f"{bid.get('title') or ''} {bid.get('scope') or ''}")
    years = [int(y) for y in _YEAR_RE.findall(blob)]
    years += _url_path_years(bid.get("url"))
    if years and max(years) < datetime.datetime.now().year:
        bid["status"] = "Closed"
    return bid


def _perform_scan(location, radius, force=False, _progress_token=""):
    """Core of /scan: resolve a location, search local + federal sources, rank
    and cache the result. Extracted out of the /scan route so the saved-search
    alert job can run the exact same pipeline (portal directory, DDG failover,
    SAM.gov, fit ranking, same-day cache) instead of duplicating it.
    Returns a dict of response fields, or None if the location can't be resolved."""
    center = _resolve_center(location)
    if not center:
        return None

    cdb = _cache()
    today = datetime.datetime.now().strftime("%Y%m%d")
    cache = cdb.setdefault("scan_cache", {})
    ckey = f"{center['state']}|{center['city'].lower()}|{int(radius)}|{today}"
    # The cache is per calendar day, so the FIRST scan of an area sets what
    # everyone sees until midnight. If that scan ran while a search backend was
    # down — or against a cold server mid-deploy — its thin result was locked in
    # and every retry returned the same disappointment with no way to force a
    # real re-run. `force` is that way.
    if ckey in cache and not force:
        c = cache[ckey]
        return {"location": f"{center['city']}, {center['state']}",
                "bids": c["bids"], "total_bids": c["total"],
                "city_coords": c.get("city_coords", {}),
                "center": c.get("center", {"lat": center["lat"], "lon": center["lon"],
                                           "label": f"{center['city']}, {center['state']}"}),
                "cached": True}

    pdb = bid_portals.load_directory()
    grouped = {}
    city_coords = {}
    local_raw = 0
    # Why extracted bids did or did not make it into the result. Recall is the
    # thing that matters most here and it fails silently, so the funnel is
    # reported in the response rather than left to be guessed at.
    drop_stats = {}
    # The clock the optional stages are measured against. Set here rather
    # than at the request boundary so a cached hit, which returns above,
    # never starts one.
    scan_deadline = time.time() + SCAN_BUDGET_SEC
    # Initialised here, not only inside the read block below, so the record
    # cannot NameError if that block is ever made conditional again -- it
    # used to sit behind "if OPENAI_API_KEY".
    towns_read = 0

    # ---- LOCAL: disguised DuckDuckGo first, Tavily fallback if it's empty ----
    # A wide radius is only useful if we actually search more than the one
    # town the user typed -- otherwise a 125mi scan just returns whatever the
    # engines happen to surface near the center point. So for radius >= 40mi
    # we also pick a handful of towns scattered around the radius (via free
    # reverse geocoding) and run a lighter query set against each of them.
    # Reading a town's own bid page costs nothing and needs no AI: the
    # CivicPlus parser is plain regex. This whole block used to sit inside
    # "if OPENAI_API_KEY", so an expired or exhausted OpenAI balance took
    # away every local bid, including all the free ones -- a paid dependency
    # switching off a free code path. Only the SEARCH queries are gated now,
    # because a search result is useless without an extractor to read it.
    SEARCH_ENABLED = bool(OPENAI_API_KEY)
    if True:
        c, s = center["city"], center["state"]
        seen_urls = set()
        lock = threading.Lock()
        # Queries that check something a direct read of the city's own bid
        # page structurally cannot: a different entity entirely (school
        # district, county, state portal) or a platform aggregator. Always
        # worth running, known-portal hit or not.
        center_queries_always = [
            f"{c} {s} school district sidewalk ADA concrete project bid",
            f"{c} {s} sidewalk ADA curb bid {_agg_sites(0)}",
            f"{c} {s} sidewalk ADA curb bid {_agg_sites(1, center['state'])}",
            f"{c} {s} county road department concrete curb bid notice",
            f"{c} {s} Safe Routes to School OR ADA transition plan sidewalk bid",
            f"{c} {s} CDBG sidewalk curb ramp bid notice to contractors",
        ]
        # Generic re-phrasings of "does this city have a sidewalk bid" --
        # redundant once _run_known_portals already read the city's own bid
        # page directly and found something real there, since a working
        # direct source is the authoritative answer to that exact question.
        # Only worth the Tavily-credit cost when there's no working direct
        # source to trust instead.
        # Six near-identical rephrasings returned heavily overlapping results;
        # three distinct angles (the work, the process, the department) cover
        # the same ground for half the queries.
        center_queries_generic = [
            f"{c} {s} sidewalk replacement ADA ramp curb gutter concrete bid",
            f"{c} {s} invitation to bid concrete sidewalk solicitation",
            f"{c} {s} public works concrete flatwork RFP bid opportunities",
        ]

        anchors = _nearby_anchor_towns(center, radius, pdb)
        # Every place the scan actually visits: the centre, the guessed
        # anchor towns, and each town we already hold a verified bid page
        # for. Reported to the app so an empty result can say what was
        # searched instead of just "nothing found".
        towns_read = 1 + len(anchors)

        # _nearby_anchor_towns samples at most a handful of geographically-
        # guessed points regardless of how large the radius is -- a 125mi
        # scan covers ~49,000 sq mi, and 6 sample points leaves most of that
        # area unsearched. Separately from that guessing, we already have a
        # real, verified bid page for 3,151 towns nationally (the offline
        # crawl) -- this asks a cheaper, more direct question: of the towns
        # we ALREADY trust, which ones are actually inside this radius. No
        # search credits involved, just reading pages we already know about.
        # Sorted closest-first and capped so a scan centered in a
        # well-covered metro doesn't try to fetch every known town in the
        # state at once.
        known_exclude = {(c.lower(), s)} | {(a[0].lower(), a[1]) for a in anchors}
        known_towns = bid_portals.towns_within_radius(
            pdb, center["lat"], center["lon"], radius, exclude=known_exclude)
        known_towns.sort(key=lambda t: _miles_between(center["lat"], center["lon"], t[2], t[3]))
        known_towns = known_towns[:MAX_KNOWN_TOWNS]
        towns_read += len(known_towns)

        # Each "town job" (center + every anchor + every known-portal town)
        # is fully independent work, so they run concurrently instead of one
        # after another — this is the biggest lever on wall-clock scan time.
        # Search-driven jobs (center/anchor) stay capped at 4 workers so we
        # don't fire too many simultaneous search-engine requests at once
        # (DuckDuckGo in particular will start blocking if hammered); the
        # known-town jobs below are just a direct page fetch each (no search
        # queries), so they get their own, more generous pool.
        center_coords = (center["lat"], center["lon"])

        def _run_center():
            # default_city=c, not "": a known portal IS this city's own bid
            # page, so a bid on it that doesn't restate the city is still that
            # city's. Defaulting to blank made _place_bid drop those outright,
            # losing bids from the single most reliable source in the pipeline.
            got = _run_known_portals(c, s, f"{c}, {s}", grouped, center, radius,
                                      cdb, city_coords, lock, pdb, default_city=c,
                                      town_coords=center_coords, stats=drop_stats)
            if not SEARCH_ENABLED:
                print(f"[scan] {got} raw bids from {c}, {s} (center, "
                      f"portal only -- no OPENAI_API_KEY)", flush=True)
                return got
            # A hit here (got > 0) means the city's own bid page was read
            # directly and had something real on it -- the generic queries
            # would only be re-asking a question that page already answered.
            queries = (center_queries_always if got > 0
                      else center_queries_always + center_queries_generic)
            got += _run_local_queries(queries, f"{c}, {s}", MAX_PAGES,
                                      grouped, center, radius, cdb, city_coords,
                                      seen_urls, lock, pdb, default_city="", state=s,
                                      town_coords=center_coords, stats=drop_stats)
            print(f"[scan] {got} raw bids from {c}, {s} (center)", flush=True)
            return got

        def _run_anchor(anchor):
            ac, ast, alat, alon = anchor
            # Anchors get one query, not five. They used to be the main way a
            # scan saw past the centre town, but the portal directory has
            # since grown from ~750 agencies to 4,400+, so _run_known_portals
            # above now reads most anchor towns' bid pages directly. What it
            # structurally cannot see is an aggregator listing, so that is the
            # one thing left worth searching for here.
            anchor_queries_always = [
                f"{ac} {ast} sidewalk ADA curb bid {_agg_sites(0)}",
            ]
            anchor_queries_generic = [
                f"{ac} {ast} sidewalk ADA curb concrete bid invitation",
            ]
            got = _run_known_portals(ac, ast, f"{ac}, {ast}", grouped, center, radius,
                                      cdb, city_coords, lock, pdb, default_city=ac,
                                      town_coords=(alat, alon), stats=drop_stats)
            if not SEARCH_ENABLED:
                print(f"[scan] {got} raw bids from {ac}, {ast} (anchor, "
                      f"portal only -- no OPENAI_API_KEY)", flush=True)
                return got
            queries = (anchor_queries_always if got > 0
                      else anchor_queries_always + anchor_queries_generic)
            got += _run_local_queries(queries, f"{ac}, {ast}", 5,
                                      grouped, center, radius, cdb, city_coords,
                                      seen_urls, lock, pdb, default_city=ac, state=ast,
                                      town_coords=(alat, alon), stats=drop_stats)
            print(f"[scan] {got} raw bids from {ac}, {ast} (anchor)", flush=True)
            return got

        def _run_known_town(town):
            # No search queries here at all -- this town wasn't picked by
            # guessing, it's one we already have a verified real bid page
            # for. Reading it directly is the entire job.
            kc, kst, klat, klon = town
            got = _run_known_portals(kc, kst, f"{kc}, {kst}", grouped, center, radius,
                                      cdb, city_coords, lock, pdb, default_city=kc,
                                      town_coords=(klat, klon), stats=drop_stats)
            print(f"[scan] {got} raw bids from {kc}, {kst} (known portal)", flush=True)
            return got

        # Timed like the optional stages are. Instrumenting only the guarded
        # ones meant optimising what could be seen: federal showed up at 30
        # seconds and got three rounds of attention while the search-and-read
        # phase, which is most of a scan, reported nothing at all.
        _progress_note(_progress_token, "searching")
        _t_search = time.time()
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = [ex.submit(_run_center)] + [ex.submit(_run_anchor, a) for a in anchors]
            for f in as_completed(futures):
                local_raw += f.result()
        drop_stats["ms_search"] = int((time.time() - _t_search) * 1000)
        _progress_note(_progress_token, "reading_towns",
                       sum(len(v) for v in grouped.values()))
        _t_towns = time.time()

        # Separate pool from the search-driven jobs above: these are a direct
        # fetch each against a different domain, not a search-engine query,
        # so there's no shared backend to overwhelm the way DuckDuckGo/Tavily
        # would be by running many at once.
        if known_towns:
            deadline = time.time() + KNOWN_TOWN_BUDGET_SEC
            # NOT a `with` block. Its __exit__ calls shutdown(wait=True), so
            # once the budget expired the phase still sat waiting for every
            # fetch already in flight -- sixteen of them, each under its own
            # request timeout. The budget bounded how long we WAITED for
            # results and not how long the phase took, which is not a budget.
            # A live scan spent 88 seconds inside a 40-second one.
            ex = ThreadPoolExecutor(max_workers=KNOWN_TOWN_WORKERS)
            try:
                futures = [ex.submit(_run_known_town, t) for t in known_towns]
                try:
                    for f in as_completed(futures,
                                          timeout=max(1.0, deadline - time.time())):
                        local_raw += f.result()
                except FuturesTimeout:
                    # Out of time. Take what finished and stop waiting.
                    done = 0
                    for f in futures:
                        if f.done() and not f.cancelled():
                            try:
                                local_raw += f.result()
                                done += 1
                            except Exception:
                                pass
                    unfinished = len(futures) - done
                    if unfinished:
                        drop_stats["known_towns_out_of_time"] = unfinished
                        print(f"[scan] known-town budget spent, {unfinished} "
                              f"town(s) not read", flush=True)
            finally:
                # Cancel what has not started and do not wait for what has.
                # The running threads finish into nothing; their work is lost,
                # which is exactly what a spent budget is supposed to mean.
                try:
                    ex.shutdown(wait=False, cancel_futures=True)
                except TypeError:          # cancel_futures is 3.9+
                    ex.shutdown(wait=False)

        drop_stats["ms_towns"] = int((time.time() - _t_towns) * 1000)

        print(f"[scan] {local_raw} raw local bids extracted total "
              f"({len(anchors)} anchor town(s), {len(known_towns)} known-portal "
              f"town(s) searched)", flush=True)

        if not TAVILY_API_KEY and _ddg_is_degraded():
            _alert_admin(
                "DuckDuckGo search appears blocked",
                "DuckDuckGo has returned empty results on 8+ consecutive "
                "searches and no TAVILY_API_KEY is configured, so local bid "
                "search may be completely down (only SAM.gov federal bids "
                "would still work). Add a TAVILY_API_KEY (tavily.com, free "
                "tier) as a fallback search backend, or investigate whether "
                "Render's outbound IP has been blocked by DuckDuckGo.",
            )

    # ---- STATE: DOT letting pages for every state the radius touches ----
    # Sequential, deliberately, and this was tried the other way.
    #
    # Overlapping these with the search-and-read phase looked free: different
    # hosts, no shared backend. Measured on the real instance it made every
    # phase slower -- towns went from 17-47s to 88s, search from 24s to 91s.
    # Render Starter is half a vCPU, and HTML parsing is CPU-bound behind the
    # GIL, so two more busy threads slow the sixteen already running rather
    # than fitting beside them. Worse, the known-town budget only bounds how
    # long we WAIT: fetches already in flight when it expires still have to
    # finish, so slowing each fetch blew a 40-second budget out to 88.
    #
    # The lesson is about the machine, not the idea. On a bigger instance this
    # is worth revisiting -- with a measurement, not an assumption.
    _stage(drop_stats, "state", scan_deadline, _run_state_sources,
           center, radius, grouped, city_coords, drop_stats)

    # ---- FEDERAL: SAM.gov across every state the radius touches ----
    _stage(drop_stats, "federal", scan_deadline, _run_federal_sources,
           center, radius, grouped, cdb, city_coords, drop_stats, pdb)

    _progress_note(_progress_token, "checking_details",
                   sum(len(v) for v in grouped.values()))

    # Read the posting behind every bid that still has no contact and no
    # deadline. Enrichment used to happen only inside the structured CivicPlus
    # reader, so a bid that arrived via search — which is most of them — kept
    # its blank fields: a live Springfield scan placed eleven bids and reported
    # no contacts_found at all, because that branch never ran. Doing it here
    # instead covers every source, and it runs on the handful of bids that
    # survived the radius rather than everything the search turned up.
    # Agencies that posted a notice directly (the lister side). Already
    # geocoded at approval, so this adds no fetch and no search credit --
    # and it's the only way work from towns with no bid page at all reaches
    # anybody. Added before enrichment so a notice missing a deadline still
    # gets the same treatment as any other bid.
    _stage(drop_stats, "agency", scan_deadline, _add_agency_bids,
           grouped, center, radius, cdb, city_coords, drop_stats)

    _stage(drop_stats, "enrich", scan_deadline, _enrich_placed_bids,
           grouped, drop_stats)
    _progress_note(_progress_token, "finishing",
                   sum(len(v) for v in grouped.values()))
    _flag_misplaced_bids(grouped, pdb, drop_stats)

    for city_bids in grouped.values():
        city_bids.sort(key=_score_bid, reverse=True)
    _count_kept_closed(grouped, drop_stats)

    # Open bids only. Closed ones are still returned (ranked last) but counting
    # them made the reported total disagree with what the app shows: a scan
    # that turned up nothing but expired listings announced "12 bids" and then
    # dropped the user on an empty feed.
    total = sum(1 for v in grouped.values() for b in v if _is_open_bid(b))
    funnel = ", ".join(f"{k}={v}" for k, v in sorted(drop_stats.items())) or "none"
    print(f"[scan] {int(radius)} mi from {center['city']},{center['state']} "
          f"-> {total} bids kept (local_raw={local_raw}; {funnel})", flush=True)

    # Kept so the last scan can be inspected from /health. Diagnosing recall
    # otherwise means asking someone to run a scan and relay numbers back,
    # which is slow and lossy; a URL anyone can open is not.
    record = {
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "location": f"{center['city']}, {center['state']}",
        "radius": int(radius),
        "kept": total,
        "raw_local": local_raw,
        "anchor_towns": len(anchors) if OPENAI_API_KEY else 0,
        "towns_read": towns_read,
        "funnel": drop_stats,
        # Counts alone can't distinguish "found nothing" from "found real
        # work and threw it away", which is exactly the question that
        # matters. These two make the last scan legible without asking
        # anyone to re-run it and read numbers back.
        "statuses": _status_breakdown(grouped),
        "sample": _scan_sample(grouped),
    }
    try:
        kv_backend.set("bidcaller:last_scan", record)
        _append_scan_history(record)
    except Exception:
        pass

    result = {"bids": grouped, "total": total, "city_coords": city_coords,
              "center": {"lat": center["lat"], "lon": center["lon"],
                        "label": f"{center['city']}, {center['state']}"}}

    # cache (today only) + persist geo cache
    cache[ckey] = {"ts": datetime.datetime.now().isoformat(), **result}
    cdb["scan_cache"] = {k: v for k, v in cache.items() if k.endswith(today)}
    _save_cache(cdb)
    bid_portals.save_directory(pdb)

    return {"location": f"{center['city']}, {center['state']}",
            "bids": grouped, "total_bids": total, "city_coords": city_coords,
            "center": result["center"],
            "debug": {"raw_local": local_raw, "kept": total,
                      "funnel": drop_stats, **_scan_effort(drop_stats, towns_read)}}


@app.route("/scan", methods=["POST"])
def scan():
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key", "")
    device = data.get("device_id", "")
    supabase_token = data.get("supabase_token", "")
    location = (data.get("location") or "").strip()
    try:
        radius = float(data.get("radius") or 25)
    except (TypeError, ValueError):
        radius = 25.0

    if not _license_is_active(key, device, supabase_token):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    if not location:
        return jsonify({"ok": False, "reason": "no_location"})

    # Who scanned, for the engagement rollup. The address is resolved from
    # the verified token and immediately reduced to the same salted handle the
    # rest of the lifecycle log uses -- a scan record must not become a way to
    # read off who the customers are or where they work.
    try:
        _who = _verify_supabase_token(supabase_token) if supabase_token else ""
        if _who:
            _db_ = _db()
            _bi_note(_db_, "scanned", _who)
            _save_db(_db_)
    except Exception:
        pass    # A metric is never worth failing a scan over.

    outcome = _perform_scan(location, radius, force=bool(data.get("force")),
                            _progress_token=str(data.get("progress_token") or ""))
    if outcome is None:
        return jsonify({"ok": False, "reason": "location_not_found"})
    return jsonify({"ok": True, **outcome})


# ═══════════════════════════════════════════════════════════
# /residential-leads — new driveway/sidewalk permits from city open-data
# (see residential_permits.py). No AI involved at all: this is clean
# structured data straight from each city's own permit system, not text an
# LLM has to interpret -- more reliable than the bid-scan path, not less.
# Coverage is narrow and hand-verified city by city (see that module's
# docstring for why some candidate cities were rejected), so the response
# always says whether the area is covered yet rather than a bare empty list
# that could just look like a bug.
# ═══════════════════════════════════════════════════════════
def _lead_within_radius(lead, center, radius, cdb):
    """True if we can confirm the lead is within radius; also true (kept,
    not dropped) when we genuinely can't determine distance at all -- an
    address with no coordinates and no zip isn't grounds to hide a real
    lead, just to not be able to sort it by distance."""
    lat, lon = lead.get("lat"), lead.get("lon")
    if lat is None or lon is None:
        z = (lead.get("zip") or "").strip()
        if not z:
            return True

        def _fetch():
            g = _geo_from_zip(z)
            return (g["lat"], g["lon"]) if g else None

        coords = _cached_point(cdb.setdefault("zip_geo_cache", {}), z, _fetch)
        if not coords:
            return True
        lat, lon = coords
    return _miles_between(center["lat"], center["lon"], lat, lon) <= radius


# A source city's permits sit inside that city, but its centroid can be just
# outside the search radius while its near edge is well inside. Reaching a bit
# further when picking sources costs one extra fetch and nothing else, because
# _lead_within_radius still checks every individual lead exactly.
LEAD_SOURCE_MARGIN_MI = 25.0


def _nearby_lead_sources(center, radius, cdb):
    """Every configured permit source close enough to hold leads in range.

    Coverage used to be an exact match on the city the user typed, so someone
    in a suburb twenty miles from Austin was told residential leads "aren't
    set up for your area yet" while Austin's permit data covered addresses
    comfortably inside their chosen radius. The radius was only ever used to
    filter leads after the fact, never to work out which sources to read.
    """
    reach = radius + LEAD_SOURCE_MARGIN_MI
    found = []
    for (city, state), src in residential_permits.SOURCES.items():
        # Coordinates ship with the registry, so choosing sources never depends
        # on a geocoder being up. Anything without them falls back to a lookup.
        point = src.get("center") or _city_coords(city, state, cdb)
        if not point:
            continue
        if _miles_between(center["lat"], center["lon"], point[0], point[1]) <= reach:
            found.append((city, state))
    return found


@app.route("/residential-leads", methods=["POST"])
def residential_leads():
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key", "")
    device = data.get("device_id", "")
    supabase_token = data.get("supabase_token", "")
    location = (data.get("location") or "").strip()
    try:
        radius = float(data.get("radius") or 25)
    except (TypeError, ValueError):
        radius = 25.0

    if not _license_is_active(key, device, supabase_token):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    if not location:
        return jsonify({"ok": False, "reason": "no_location"})

    center = _resolve_center(location)
    if not center:
        return jsonify({"ok": False, "reason": "location_not_found"})

    cdb = _cache()
    sources = _nearby_lead_sources(center, radius, cdb)
    covered = bool(sources)

    today = datetime.datetime.now().strftime("%Y%m%d")
    cache = cdb.setdefault("leads_cache", {})
    ckey = f"{center['state']}|{center['city'].lower()}|{int(radius)}|{today}"
    if ckey in cache:
        c = cache[ckey]
        return jsonify({"ok": True, "location": f"{center['city']}, {center['state']}",
                        "leads": c["leads"], "total": len(c["leads"]),
                        "covered": covered, "cached": True})

    leads = []
    for scity, sstate in sources:
        leads.extend(residential_permits.fetch_leads(scity, sstate))
    kept = [l for l in leads if _lead_within_radius(l, center, radius, cdb)]

    cache[ckey] = {"ts": datetime.datetime.now().isoformat(), "leads": kept}
    cdb["leads_cache"] = {k: v for k, v in cache.items() if k.endswith(today)}
    _save_cache(cdb)

    return jsonify({"ok": True, "location": f"{center['city']}, {center['state']}",
                    "leads": kept, "total": len(kept), "covered": covered})


# ═══════════════════════════════════════════════════════════
# /upcoming — planned work spotted in council agendas, budgets & CIPs,
# BEFORE it becomes a formal bid. Same license gate + radius filter as /scan.
# ═══════════════════════════════════════════════════════════
def _ai_extract_upcoming(area, text):
    if not OPENAI_API_KEY:
        return None
    prompt = (
        f"You scout FUTURE concrete work for contractors near {area}, before it "
        "becomes a formal bid.\n\n"
        "From the website text below (council agendas, budgets, capital improvement "
        "plans / CIPs, engineering department pages, planning documents), find ANY "
        "mention of PLANNED or PROPOSED sidewalk, ADA ramp, curb & gutter, or related "
        "concrete/flatwork projects that are not yet an open bid.\n\n"
        "Respond ONLY with a JSON array. Each item has keys: \"title\", \"scope\", "
        "\"timeframe\" (e.g. a fiscal year, quarter, or phrase like \"FY2027\" or "
        "\"pending council approval\" — use \"\" if unclear), \"contact\", \"email\", "
        "\"phone\", \"url\", \"city\". \"city\" is the US city where the work is "
        "planned, exactly as written; if unclear use \"\" and do NOT guess. "
        "Use \"\" for any missing field. If nothing planned is mentioned, return []. "
        "No markdown, no text outside the array.\n\n"
        f"WEBSITE TEXT:\n{text[:16000]}"
    )
    body = json.dumps({
        "model": OPENAI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = data["choices"][0]["message"]["content"].strip()
        s, e = out.find("["), out.rfind("]")
        if s != -1 and e != -1 and e > s:
            out = out[s:e + 1]
        items = json.loads(out)
        return items if isinstance(items, list) else []
    except Exception:
        return None


def _perform_upcoming(location, radius, force=False):
    """Core of /upcoming: resolve a location, search for planned/budgeted
    concrete work near it, extract and cache. Extracted out of the /upcoming
    route -- exactly as _perform_scan was -- so the weekly alert job can run
    the same pipeline instead of a second copy of it that drifts.

    Returns a dict of response fields, or None if the location can't be
    resolved. Callers are responsible for checking OPENAI_API_KEY first;
    without it this finds nothing, since every page here needs extraction."""
    center = _resolve_center(location)
    if not center:
        return None

    cdb = _cache()
    today = datetime.datetime.now().strftime("%Y%m%d")
    cache = cdb.setdefault("upcoming_cache", {})
    ckey = f"{center['state']}|{center['city'].lower()}|{int(radius)}|{today}"
    if ckey in cache and not force:
        c = cache[ckey]
        return {"location": f"{center['city']}, {center['state']}",
                "items": c["items"], "total": c["total"],
                "city_coords": c.get("city_coords", {}), "cached": True}

    c, s = center["city"], center["state"]
    queries = [
        f"{c} {s} capital improvement plan sidewalk ADA curb",
        f"{c} {s} council agenda sidewalk program budget",
        f"{c} {s} engineering department sidewalk ADA transition plan",
        f"{c} {s} public works budget sidewalk curb gutter fiscal year",
        f"{c} {s} CIP concrete infrastructure plan upcoming",
    ]
    seen, pages = set(), []
    for q in queries:
        results, used_ddg = _web_search(q, max_results=6)
        for r in results:
            if r["url"] not in seen:
                seen.add(r["url"])
                pages.append(r)
        if used_ddg:  # only the scraped backend needs pacing — see _run_local_queries
            time.sleep(DDG_QUERY_PAUSE)

    grouped, city_coords, drop_stats = {}, {}, {}
    lock = threading.Lock()
    center_coords = (center["lat"], center["lon"])

    # Pages were processed one after another here, unlike /scan. Each one is a
    # page fetch plus an OpenAI call, so a full budget ran well past the app's
    # own request timeout and the user just saw "that took too long". Same
    # prioritisation and fan-out as /scan, and the same recall handling: a
    # planned project naming a county or an authority is anchored to the town
    # we searched rather than dropped.
    def _process(it):
        text = it["content"] or _fetch_text(it["url"])
        if len(text) < 200:
            return
        items = _ai_extract_upcoming(f"{c}, {s}", text)
        if not items:
            return
        with lock:
            for b in items:
                if isinstance(b, dict):
                    b["url"] = _resolve_bid_url(b.get("url"), it["url"], text)
                    b["status"] = "Planned"
                    _place_bid(grouped, b, center, radius, cdb, default_city="",
                              city_coords=city_coords, default_state=s,
                              fallback_coords=center_coords, stats=drop_stats)

    with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as ex:
        list(ex.map(_process, _prioritize_pages(pages)[:MAX_PAGES]))

    total = sum(len(v) for v in grouped.values())
    funnel = ", ".join(f"{k}={v}" for k, v in sorted(drop_stats.items())) or "none"
    print(f"[upcoming] {int(radius)} mi from {c},{s} -> {total} planned "
          f"({funnel})", flush=True)
    cache[ckey] = {"ts": datetime.datetime.now().isoformat(), "items": grouped,
                  "total": total, "city_coords": city_coords}
    cdb["upcoming_cache"] = {k: v for k, v in cache.items() if k.endswith(today)}
    _save_cache(cdb)

    return {"location": f"{center['city']}, {center['state']}",
            "items": grouped, "total": total, "city_coords": city_coords,
            "debug": {"pages": len(pages), "kept": total, "funnel": drop_stats}}


@app.route("/upcoming", methods=["POST"])
def upcoming():
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key", "")
    device = data.get("device_id", "")
    supabase_token = data.get("supabase_token", "")
    location = (data.get("location") or "").strip()
    try:
        radius = float(data.get("radius") or 25)
    except (TypeError, ValueError):
        radius = 25.0

    if not _license_is_active(key, device, supabase_token):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    if not location:
        return jsonify({"ok": False, "reason": "no_location"})
    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "reason": "ai_unavailable"})

    outcome = _perform_upcoming(location, radius)
    if outcome is None:
        return jsonify({"ok": False, "reason": "location_not_found"})
    return jsonify(dict(outcome, ok=True))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
