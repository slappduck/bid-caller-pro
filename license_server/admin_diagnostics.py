# ── Full data export (admin-only) ──
# Everything a customer owns lives in one Supabase project on a free plan with
# limited backups. If that project is wiped, suspended or lapses there is no
# second copy anywhere: saved bids, pipeline notes and company profiles are
# user-authored and exist nowhere else.
#
# Deliberately pull-only and manual. The obvious "just back it up
# automatically" answer is a scheduled job committing a dump or uploading an
# Actions artifact, and BOTH leak customer data -- this repository is public.
# So the export is served once, to an authenticated admin, and whoever runs it
# decides where it lands.
_EXPORT_TABLES = ("company_profiles", "saved_bids", "saved_searches",
                  "user_feeds", "reviews")


def _export_table(name):
    """A whole table via the service-role key. Returns (rows, error)."""
    rows = _supabase_admin_request(f"/rest/v1/{name}?select=*")
    if rows is None:
        return [], "unreachable_or_missing"
    if not isinstance(rows, list):
        return [], "unexpected_shape"
    return rows, ""


# ── Read-only diagnostics ────────────────────────────────────────────────
# A second, deliberately weak credential. ADMIN_TOKEN can issue licences,
# send campaigns and export the user table, so it is the wrong thing to hand
# to anyone helping debug a scan -- the blast radius of a leak is the whole
# business. This one reads scan telemetry and nothing else: no user data, no
# licence keys, no email addresses, and no route that changes anything.
DIAG_TOKEN = _env_secret("DIAG_TOKEN", "")


def _diag_ok(supplied):
    if not DIAG_TOKEN:
        return False
    # Refusing the admin token here is deliberate. If the two were
    # interchangeable, "just use the admin one" would quietly become the
    # habit and the separation would buy nothing.
    if _admin_configured() and hmac.compare_digest(supplied or "", ADMIN_TOKEN):
        return False
    return hmac.compare_digest(supplied or "", DIAG_TOKEN)


@app.route("/diag", methods=["GET"])
def diag():
    """Everything needed to debug a scan, and nothing else.

    Read-only by construction: GET, no side effects, and the payload is
    assembled field by field rather than by filtering something larger, so a
    field added elsewhere cannot leak in here by default.
    """
    if not DIAG_TOKEN:
        return jsonify({"ok": False, "reason": "diag_not_configured"}), 503
    if not _diag_ok(request.headers.get("X-Diag-Token")):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403

    brave, tav = _brave_health(), _tavily_health()
    with _ddg_lock:
        ddg_streak = _ddg_fail_streak
    return jsonify({
        "ok": True,
        "version": _env_secret("RENDER_GIT_COMMIT", "")[:7],
        "providers": {
            "brave": {k: brave[k] for k in
                      ("ok", "failed", "last_status", "last_error")},
            "tavily": {k: tav[k] for k in
                       ("ok", "failed", "last_status", "last_error")},
            "ddg": {"consecutive_empty_searches": ddg_streak,
                    "degraded": ddg_streak >= DDG_TRIP_THRESHOLD},
            "benched_until": {k: round(v - time.time(), 1)
                              for k, v in _provider_down_until.items()},
        },
        # Delivery feedback. A campaign is only as good as the list it
        # leaves behind, and bounces are invisible without this.
        "email_events": kv_backend.get(_EMAIL_EVENTS_KEY, None),
        "suppressed_count": len(_suppression()),
        "webhook_configured": bool(RESEND_WEBHOOK_SECRET),
        # Outreach link opens, per recipient slug. Counted first-party, so
        # unlike the Cloudflare beacon it is not silently reduced by ad
        # blockers. Still a floor: a security scanner that runs JavaScript
        # counts as an open, and a forwarded link spreads one slug over
        # several readers.
        #
        # Behind the diag token on purpose. This says which named contractors
        # opened a cold email -- their behaviour, not ours -- and it first
        # went on /health, which is public and unauthenticated. A test caught
        # it. Do not move it back.
        "go_clicks": kv_backend.get(_CLICK_KEY, None),
        # Consent recording, as counts. A signup that silently failed to
        # record is otherwise invisible until somebody thinks to run SQL.
        "terms_acceptances": _terms_acceptance_stats(),
        # Trial -> paid -> churn, aggregated. Counts and medians, no names.
        "business": _bi_summary(),
        # Whether the product produces work. Counts only.
        "outcomes": _wins_summary(),
        # How often paying customers actually use it -- the earliest churn
        # signal, and unlike a cancellation it is still reversible.
        "engagement": _engagement(),
        # Does the marketing page work: visitors in, trials out.
        "funnel": _funnel_summary(),
        "last_scan": kv_backend.get("bidcaller:last_scan", None),
        "recent_scans": _recent_scans(request.args.get("scans")),
        "feed_audit": kv_backend.get(BID_AUDIT_KEY, None),
        "scan_config": {
            "max_pages_per_town": MAX_PAGES,
            "max_anchor_towns": MAX_ANCHOR_TOWNS,
            "max_known_towns": MAX_KNOWN_TOWNS,
            "known_town_budget_sec": KNOWN_TOWN_BUDGET_SEC,
            "detail_pages_per_portal": DETAIL_PAGES_PER_PORTAL,
            "probe_timeout": PROBE_TIMEOUT,
            "fetch_timeout": FETCH_TIMEOUT,
            "undated_max_days": UNDATED_MAX_DAYS,
            "model": OPENAI_MODEL,
        },
        "directory": {
            "portals": sum(len(v) for v in bid_portals._national_seeds().values()),
            "wikidata_portals": sum(len(v) for v in bid_portals._wikidata_seeds().values()),
            "geocoded_towns": len(bid_portals._coords()),
        },
    })


@app.route("/admin/whoami", methods=["POST"])
def admin_whoami():
    """Tells the caller, and only the caller, whether their signed-in account
    is an admin. Public and unauthenticated on purpose -- it never reveals
    the allowlist, only a yes/no about the one Supabase token presented, so
    the app can decide whether to show an Admin entry point without needing
    the admin token just to ask the question."""
    data = request.get_json(force=True, silent=True) or {}
    email = _verify_supabase_token(data.get("supabase_token", ""))
    return jsonify({"ok": True, "is_admin": _is_admin_email(email)})


@app.route("/admin/reviews", methods=["POST"])
def admin_reviews():
    """Admin: list every review, or approve/reject one. Admin token required.

    Moderation used to mean opening Supabase's table editor by hand -- fine
    when it was the only queue, awkward now that agency notices and campaign
    drafts already have a page. approve/reject take the review's numeric id;
    both are handled the same way (a boolean flip of `approved`), listed
    separately only so the caller's intent reads clearly in the request.
    """
    data = request.get_json(force=True, silent=True) or {}
    if not _admin_configured():
        return jsonify({"ok": False, "reason": "admin_not_configured"}), 503
    if not _admin_ok(data.get("admin_token")):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return jsonify({"ok": False, "reason": "supabase_not_configured"}), 503

    raw_id = data.get("approve") or data.get("reject")
    if raw_id:
        try:
            review_id = int(raw_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "invalid_review_id"}), 400
        _supabase_admin_request(
            f"/rest/v1/reviews?id=eq.{review_id}", method="PATCH",
            data={"approved": bool(data.get("approve"))})

    rows = _supabase_admin_request(
        "/rest/v1/reviews?select=id,rating,quote,display_name,company,"
        "approved,created_at&order=created_at.desc")
    return jsonify({"ok": True, "reviews": rows if isinstance(rows, list) else []})


@app.route("/admin/export", methods=["POST"])
def admin_export():
    """Full JSON dump of every user-owned table. Admin token required.

    This contains personal data by definition -- names, emails, phone numbers,
    and a contractor's private pipeline notes. Treat the response like a
    password: somewhere private, never a public repo or a shared drive.
    """
    data = request.get_json(force=True, silent=True) or {}
    if not _admin_configured():
        return jsonify({"ok": False, "reason": "admin_not_configured"}), 503
    if not _admin_ok(data.get("admin_token")):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return jsonify({"ok": False, "reason": "supabase_not_configured"}), 503

    tables, errors, total = {}, {}, 0
    for name in _EXPORT_TABLES:
        rows, err = _export_table(name)
        if err:
            # A table that was never created is a real finding, not a crash:
            # report it per-table and still return everything else.
            errors[name] = err
        tables[name] = rows
        total += len(rows)

    print(f"[export] {total} rows across {len(_EXPORT_TABLES)} tables"
          + (f", errors: {errors}" if errors else ""), flush=True)
    return jsonify({"ok": True,
                    "exported_at": datetime.datetime.now(
                        datetime.timezone.utc).isoformat(),
                    "tables": tables,
                    "row_counts": {k: len(v) for k, v in tables.items()},
                    "total_rows": total,
                    "errors": errors})


# ── Feed accuracy audit ──────────────────────────────────────────────────
# Every stale bid a customer has seen was found by the customer. An awarded
# job presented as live work costs trust in a way a missing bid does not, and
# the CivicPlus status bug that caused most of them sat there across ~2,400
# portals until someone happened to recognise a job they had already won.
#
# This samples real portals the way a scan does and measures what the parser
# would hand a customer. It changes nothing and stores a number, so a
# regression shows up as the number moving instead of as a complaint.
BID_AUDIT_KEY = "bidcaller:last_audit"
BID_AUDIT_SAMPLE = int(os.environ.get("BID_AUDIT_SAMPLE", "40"))


def _url_is_alive(url, timeout=12):
    """True if a bid link actually opens. None when we cannot tell.

    HEAD first because it is free, but plenty of government stacks answer 405
    or 404 to HEAD and 200 to GET, so a HEAD failure is retried as a GET
    before anything is called dead. Calling a live link dead would be a worse
    bug than the one this is measuring.
    """
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(
                url, method=method,
                headers={"User-Agent": "BidCallerPro/2.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status < 400:
                    return True
        except urllib.error.HTTPError as e:
            if e.code in (403, 405, 501) and method == "HEAD":
                continue          # server dislikes HEAD, not the URL
            if e.code == 404:
                return False
            return None           # 5xx, rate limit: unknown, not dead
        except Exception:
            continue
    return None


def _audit_portal(entry, link_checks=2):
    """Parse one portal the way a scan does; return per-row verdicts.

    Measures what a customer would be handed, not what the page contains:
    only rows the app would actually SHOW are judged on deadline, contact
    reachability and whether the link opens.
    """
    out = {"rows": 0, "niche_rows": 0, "shown_open": 0, "no_status": 0,
           "open_but_expired": 0, "awarded_shown_open": 0,
           "shown_no_deadline": 0, "links_checked": 0, "links_dead": 0}
    html = _fetch_raw_html(entry["url"])
    if not html:
        out["unreachable"] = 1
        return out
    try:
        # entry["url"], not entry["base"]. The scanner passes the LISTING url
        # here, and passing the site origin instead exercised a code path
        # production never takes -- which is how this audit reported 0 dead
        # links while every CivicPlus posting link was a 404. A monitor that
        # does not call the code the way production calls it will confirm
        # whatever you already believe.
        rows = bid_sources.parse_civicplus_html(html, entry["url"])
    except Exception:
        return out
    today = datetime.datetime.now().date()
    to_check = []
    for r in rows:
        out["rows"] += 1
        status = (r.get("status") or "").strip()
        bid = {"status": status, "deadline": r.get("deadline") or ""}
        if not status:
            out["no_status"] += 1
        # A listing page carries every trade the agency buys. The scan drops
        # anything off-niche BEFORE it reaches a customer, so counting those
        # as "shown" measured the wrong layer entirely -- the first live run
        # reported 100% off-niche, which was this bug, not the feed.
        if not bid_sources.looks_relevant(r.get("title"), r.get("scope")):
            continue
        out["niche_rows"] += 1
        # Collect the link before the open/closed split. Liveness tests how
        # the URL was BUILT, not whether the bid is current -- a closed
        # posting's page still exists. Sampling only open rows gave two links
        # a night, far too thin to catch the construction regression that
        # made every one of them a 404.
        if r.get("url"):
            to_check.append(r["url"])
        if not _is_open_bid(bid):
            continue
        out["shown_open"] += 1
        d = _parse_deadline(bid["deadline"])
        if d and d < today:
            # Shown as open with a deadline already past: the exact failure
            # this audit exists to catch.
            out["open_but_expired"] += 1
        elif not d:
            # No date at all. Nothing can ever age this out, so it would sit
            # in the feed indefinitely -- the remaining staleness hole.
            out["shown_no_deadline"] += 1
        if status.lower().startswith("award"):
            out["awarded_shown_open"] += 1

    for url in to_check[:link_checks]:
        alive = _url_is_alive(url)
        if alive is None:
            continue              # unknown is not evidence of a dead link
        out["links_checked"] += 1
        if not alive:
            out["links_dead"] += 1
    return out


def _fetch_raw_html(url, limit=250000, timeout=15):
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "BidCallerPro/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return resp.read(limit).decode("utf-8", "ignore")
    except Exception:
        return None


def _run_bid_audit(sample_size=None):
    """Sample CivicPlus portals and report what a customer would be shown."""
    size = int(sample_size or BID_AUDIT_SAMPLE)
    portals = []
    for (city, state), entries in bid_portals._national_seeds().items():
        for e in entries:
            if e.get("platform") == "civicplus" and e.get("url"):
                base = e["url"].split("/Bids.aspx")[0]
                # Audit the show-everything view, not the default one. A scan
                # reads both (see bid_sources.civicplus_endpoints), and the
                # default page lists only open bids -- so auditing it would
                # sample almost nothing and, worse, could never see the
                # awarded-shown-as-open failure this exists to catch.
                portals.append({
                    "url": base + "/Bids.aspx?catID=All&txtSort=Category"
                                  "&showAllBids=on",
                    "base": base, "city": city, "state": state})
    if not portals:
        return {"ok": False, "reason": "no_portals"}
    random.shuffle(portals)
    portals = portals[:size]

    totals = {"portals": 0, "unreachable": 0, "rows": 0, "niche_rows": 0,
              "shown_open": 0, "no_status": 0, "open_but_expired": 0,
              "awarded_shown_open": 0, "shown_no_deadline": 0,
              "links_checked": 0, "links_dead": 0}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for res in ex.map(_audit_portal, portals):
            totals["portals"] += 1
            for k, v in res.items():
                totals[k] = totals.get(k, 0) + v

    shown = max(totals["shown_open"], 1)
    bad = totals["open_but_expired"] + totals["awarded_shown_open"]
    totals["stale_rate_pct"] = round(100.0 * bad / shown, 2)
    # Undated rows are not stale today, but nothing can ever age them out.
    totals["undated_pct"] = round(100.0 * totals["shown_no_deadline"] / shown, 2)
    totals["dead_link_pct"] = round(
        100.0 * totals["links_dead"] / max(totals["links_checked"], 1), 2)
    totals["at"] = datetime.datetime.now().isoformat(timespec="seconds")
    totals["ok"] = True
    kv_backend.set(BID_AUDIT_KEY, totals)

    # A feed that is a few percent wrong is worth knowing about; a feed that
    # is badly wrong is worth being woken for.
    # Each threshold has a floor as well as a rate, so a tiny sample with one
    # bad row cannot page anyone.
    faults = []
    if totals["stale_rate_pct"] >= 5 and bad >= 3:
        faults.append(f"{bad} of {totals['shown_open']} shown rows are already "
                      f"expired or awarded ({totals['stale_rate_pct']}%)")
    if totals["dead_link_pct"] >= 10 and totals["links_dead"] >= 3:
        faults.append(f"{totals['links_dead']} of {totals['links_checked']} "
                      f"checked links are dead ({totals['dead_link_pct']}%)")
    if faults:
        _alert_admin(
            "Bid feed quality dropped: " + faults[0].split(" (")[0],
            "The nightly feed audit found:\n  - " + "\n  - ".join(faults) +
            f"\n\n{json.dumps(totals, indent=2)}")
    print(f"[audit] {json.dumps(totals)}", flush=True)
    return totals


@app.route("/run-bid-audit", methods=["POST"])
def run_bid_audit():
    """Nightly feed-accuracy check. Same CRON_SECRET gate as the alert jobs."""
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("token") or request.headers.get("X-Cron-Secret", "")
    if not CRON_SECRET or not hmac.compare_digest(token, CRON_SECRET):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    result = _run_bid_audit(data.get("sample"))
    if result.get("ok"):
        _cron_beat("bid-audit")
    return jsonify(result), (200 if result.get("ok") else 500)


@app.route("/run-upcoming-alerts", methods=["POST"])
def run_upcoming_alerts():
    """Weekly counterpart to /run-saved-search-alerts, same CRON_SECRET gate
    and same external-scheduler arrangement (see .github/workflows)."""
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("token") or request.headers.get("X-Cron-Secret", "")
    if not CRON_SECRET or not hmac.compare_digest(token, CRON_SECRET):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    result = _run_upcoming_alerts()
    if result.get("ok"):
        _cron_beat("upcoming-alerts")
    return jsonify(result), (200 if result.get("ok") else 500)


@app.errorhandler(Exception)
def _handle_unexpected_error(err):
    """Safety net: any unhandled exception in any route lands here instead
    of a bare 500 the customer can't do anything about, and we get an email
    about it instead of finding out when a customer complains. Routine HTTP
    errors (404 on a bad path, 405, etc.) are left to Flask's normal
    handling -- only genuine unexpected exceptions get alerted."""
    if isinstance(err, HTTPException):
        return err
    import traceback
    tb = traceback.format_exc()
    print(f"[error] unhandled exception on {request.path}: {err}\n{tb}", flush=True)
    _alert_admin(
        f"Unhandled error on {request.path}",
        f"{request.method} {request.path}\n\n{tb}",
    )
    return jsonify({"ok": False, "reason": "server_error"}), 500


def _issue_for(db, email, device, plan):
    """Create a key and index it by both email and device for later lookup."""
    months = 12 if plan == "annual" else 1
    key, exp = make_key(plan, months)
    db.setdefault("issued", {})[key] = {
        "plan": plan, "expires": exp[:10], "email": email, "device": device,
        "updated": datetime.datetime.now().isoformat()[:10],
    }
    if email:
        db.setdefault("emails", {})[email.lower()] = key
    if device:
        db.setdefault("devices", {})[device] = key
    return key

