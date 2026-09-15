def _terms_acceptance_stats():
    """Counts only -- never the rows themselves.

    Enough to answer "did that signup record consent?" without this endpoint
    becoming a way to read who agreed to what. The rows carry email addresses;
    a count does not, and a count is the whole question being asked.

    `current` uses the versions this server stamps, so a signup recorded
    against a stale version shows up as a gap between the two numbers rather
    than looking identical to a healthy one.
    """
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return {"configured": False}

    def count(query):
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/terms_acceptances?select=id&limit=1{query}",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                     "apikey": SUPABASE_SERVICE_ROLE_KEY,
                     "Prefer": "count=exact"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            # PostgREST reports the total in Content-Range as "0-0/N".
            rng = resp.headers.get("Content-Range") or ""
            return int(rng.rsplit("/", 1)[-1])

    try:
        return {
            "configured": True,
            "total": count(""),
            "current": count(f"&terms_version=eq.{TERMS_VERSION}"
                             f"&privacy_version=eq.{PRIVACY_VERSION}"),
            "stamping": {"terms": TERMS_VERSION, "privacy": PRIVACY_VERSION},
        }
    except Exception as ex:
        # Type only. The URL carries the project ref and the message can echo
        # request detail, and this whole payload is a debugging surface.
        return {"configured": True, "error": type(ex).__name__}


_PIPELINE_STATES = ("submitted", "won", "lost", "passed")


def _wins_summary():
    """Does the product actually get contractors work?

    The most valuable question the system could not answer. Customers mark a
    bid submitted, won, lost or passed, and that sits per-user in Supabase
    where nothing ever looked at it in aggregate -- so "is this product
    working" had no number behind it, only opinion.

    It matters twice over. A contractor who wins a job does not cancel, so
    this predicts retention better than any usage metric. And one real win is
    the first testimonial, which outweighs every line of copy on the landing
    page.

    Counts only. Row contents never leave Supabase; the one thing derived
    from identities is how many DISTINCT customers have won something, and
    only the number is returned.
    """
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return {"configured": False}

    def head_count(query):
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/saved_bids?select=bid_id&limit=1{query}",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                     "apikey": SUPABASE_SERVICE_ROLE_KEY,
                     "Prefer": "count=exact"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            rng = resp.headers.get("Content-Range") or ""
            return int(rng.rsplit("/", 1)[-1])

    try:
        out = {"configured": True, "saved_total": head_count("")}
        for state in _PIPELINE_STATES:
            out[state] = head_count(f"&pipeline=eq.{state}")
        decided = out["won"] + out["lost"]
        # Only meaningful once a few jobs have been decided; the raw counts
        # sit beside it so a 100% win rate on one job reads as what it is.
        out["win_rate_pct"] = (round(100.0 * out["won"] / decided, 1)
                               if decided else None)
        # How many people, not how many jobs. One customer winning five is a
        # very different business from five customers winning one each.
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/saved_bids"
            f"?select=user_id&pipeline=eq.won&limit=1000",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                     "apikey": SUPABASE_SERVICE_ROLE_KEY})
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
        out["customers_who_have_won"] = len({r.get("user_id") for r in rows
                                             if r.get("user_id")})
        return out
    except Exception as ex:
        return {"configured": True, "error": type(ex).__name__}


ENGAGEMENT_WINDOW_DAYS = int(os.environ.get("ENGAGEMENT_WINDOW_DAYS", "28"))


_VISIT_KEY = "bidcaller:site_visits"
_VISIT_DAYS = int(os.environ.get("SITE_VISIT_DAYS", "60"))
_visit_lock = threading.Lock()


@app.route("/visit", methods=["POST"])
def visit():
    """Count one visit to the marketing site, by day.

    /click counts outreach links only, so the one number that says whether
    the landing page works -- visitors who become trials -- could not be
    computed at all. This is the numerator's other half.

    Public and unauthenticated, like /coverage and /click: the page doing the
    reporting is public, so the worst case is a wrong count on a private
    dashboard. No addresses, no identifiers, no per-visitor row -- a date and
    a tally, which is all the ratio needs.
    """
    day = datetime.datetime.now().strftime("%Y-%m-%d")
    with _visit_lock:
        try:
            store = kv_backend.get(_VISIT_KEY, None)
            if not isinstance(store, dict):
                store = {}
            store[day] = int(store.get(day, 0)) + 1
            # Bounded: keep the recent window, drop the rest. A counter that
            # grows forever is a slow outage.
            for old in sorted(store)[:-_VISIT_DAYS]:
                store.pop(old, None)
            kv_backend.set(_VISIT_KEY, store)
        except Exception:
            return jsonify({"ok": True, "recorded": False})
    return jsonify({"ok": True, "recorded": True})


def _funnel_summary(db=None):
    """Visitors to the marketing site, and how many became trials.

    Both sides come from counts already being kept: site visits by day, and
    trial_started events in the lifecycle log. Deliberately a RATIO of two
    tallies rather than a per-visitor journey -- following individuals across
    the site would mean identifying them, and the question does not require
    it.
    """
    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=_VISIT_DAYS))
    try:
        store = kv_backend.get(_VISIT_KEY, None) or {}
    except Exception:
        store = {}
    visits = 0
    for day, n in (store.items() if isinstance(store, dict) else []):
        try:
            if datetime.datetime.strptime(day, "%Y-%m-%d").replace(
                    tzinfo=datetime.timezone.utc) >= since:
                visits += int(n)
        except Exception:
            continue
    try:
        log = (db if db is not None else _db()).get(LIFECYCLE_KEY) or []
    except Exception:
        log = []
    trials = 0
    for row in log:
        if row.get("event") != "trial_started":
            continue
        try:
            t = datetime.datetime.fromisoformat(row["at"])
            if t.tzinfo is None:
                t = t.replace(tzinfo=datetime.timezone.utc)
            if t >= since:
                trials += 1
        except Exception:
            continue
    return {
        "window_days": _VISIT_DAYS,
        "site_visits": visits,
        "signups_started": trials,
        # Nothing rather than zero when nobody has visited: zero would read
        # as "the page converts nobody" when it means "nobody came".
        "visit_to_trial_pct": (round(100.0 * trials / visits, 2)
                               if visits else None),
    }


def _engagement(db=None):
    """How often the people paying for this actually use it.

    The earliest churn signal there is. Somebody stops opening a tool weeks
    before they cancel it, so cancellations tell you about a decision already
    made while this tells you about one being made.

    Counted per person per week over a rolling window, from the same lifecycle
    log as everything else -- no new storage, and no addresses, because a
    scan event carries the same salted handle the rest of the log uses.
    """
    try:
        log = (db if db is not None else _db()).get(LIFECYCLE_KEY) or []
    except Exception:
        return {"events": 0}
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=ENGAGEMENT_WINDOW_DAYS)
    weeks = max(1.0, ENGAGEMENT_WINDOW_DAYS / 7.0)

    per_person, last_seen = {}, {}
    for row in log:
        if row.get("event") != "scanned":
            continue
        who = row.get("id")
        if not who:
            continue
        try:
            t = datetime.datetime.fromisoformat(row["at"])
            if t.tzinfo is None:
                t = t.replace(tzinfo=datetime.timezone.utc)
        except Exception:
            continue
        if t > last_seen.get(who, cutoff - datetime.timedelta(days=9999)):
            last_seen[who] = t
        if t >= cutoff:
            per_person[who] = per_person.get(who, 0) + 1

    rates = sorted(v / weeks for v in per_person.values())

    def median(xs):
        if not xs:
            return None
        mid = len(xs) // 2
        return round(xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2, 2)

    # Silent for a fortnight is the number worth acting on: it is a customer
    # deciding to leave, and unlike a cancellation it is still reversible.
    quiet = sum(1 for t in last_seen.values() if (now - t).days >= 14)
    return {
        "window_days": ENGAGEMENT_WINDOW_DAYS,
        "active_people": len(per_person),
        "scans_in_window": sum(per_person.values()),
        "median_scans_per_week": median(rates),
        "busiest_scans_per_week": round(rates[-1], 2) if rates else None,
        "silent_14d_or_more": quiet,
    }


def _bi_summary(db=None):
    """Trial-to-paid, churn and lifetime, computed from the lifecycle log.

    Counts and medians only. Nothing here names anybody, which is what makes
    it safe to read from a diagnostics endpoint.
    """
    try:
        log = (db if db is not None else _db()).get(LIFECYCLE_KEY) or []
    except Exception:
        return {"events": 0}

    def when(row):
        try:
            t = datetime.datetime.fromisoformat(row["at"])
            return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)
        except Exception:
            return None

    first = {}          # id -> {event: earliest time}
    counts = {}
    plans = {}
    for row in log:
        ev = row.get("event", "")
        counts[ev] = counts.get(ev, 0) + 1
        if ev == "subscribed" and row.get("plan"):
            plans[row["plan"]] = plans.get(row["plan"], 0) + 1
        who, t = row.get("id"), when(row)
        if not who or t is None:
            continue
        seen = first.setdefault(who, {})
        if ev not in seen or t < seen[ev]:
            seen[ev] = t

    def median(xs):
        xs = sorted(xs)
        if not xs:
            return None
        mid = len(xs) // 2
        return round(xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2, 1)

    trials = {w for w, e in first.items() if "trial_started" in e}
    paid = {w for w, e in first.items() if "subscribed" in e}
    churned = {w for w, e in first.items() if "churned" in e}

    to_paid = [(first[w]["subscribed"] - first[w]["trial_started"]).days
               for w in trials & paid]
    lifetime = [(first[w]["churned"] - first[w]["subscribed"]).days
                for w in paid & churned]

    return {
        "events": len(log),
        "counts": counts,
        "plans_sold": plans,
        "people": {"trialled": len(trials), "paid": len(paid),
                   "churned": len(churned),
                   "paying_now": len(paid - churned)},
        # The number that decides whether the funnel works at all.
        "trial_to_paid_pct": (round(100.0 * len(trials & paid) / len(trials), 1)
                              if trials else None),
        "median_days_trial_to_paid": median(to_paid),
        "median_days_subscribed_before_churn": median(lifetime),
        # Churn as a share of everyone who ever paid. Meaningless below a
        # handful of customers, which is exactly why the raw counts are here
        # beside it rather than the rate alone.
        "churn_pct_of_paid": (round(100.0 * len(churned) / len(paid), 1)
                              if paid else None),
    }


def _recent_scans(limit=None):
    """The scan history, newest first. Never raises — /health must answer even
    when the storage backend is the thing that is broken.

    `limit` defaults to SCAN_HISTORY_SHOW, not to everything retained. The
    store deliberately holds far more rows than a diagnostic page should
    print; an explicit limit is how you read the rest.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = SCAN_HISTORY_SHOW
    limit = max(1, min(limit, SCAN_HISTORY_MAX))
    try:
        history = kv_backend.get(SCAN_HISTORY_KEY, None) or []
        if not isinstance(history, list):
            return []
        return list(reversed(history))[:limit]
    except Exception:
        return []




# ═══════════════════════════════════════════════════════════
# LICENSE / TRIAL GATE
# ═══════════════════════════════════════════════════════════
# Accounts that never trial-expire and never need a subscription -- for
# testing the live product with a real signed-in account instead of resetting
# a device trial. A comma-separated env var, same pattern as MAILING_ADDRESS
# and FROM_EMAIL: nobody's email address belongs in a public repo.
ADMIN_EMAILS = os.environ.get("ADMIN_EMAILS", "")


def _admin_email_set():
    return {e.strip().lower() for e in ADMIN_EMAILS.split(",") if e.strip()}


def _is_admin_email(email):
    return bool(email) and email.strip().lower() in _admin_email_set()


# ── Business intelligence: the lifecycle log ────────────────────────────────
#
# What the system knew before this: that a licence key is dead. Not when, not
# after how long, not what the person was paying. Cancellations appended a key
# to a flat list -- db["revoked"] -- with no date and no reason, so "how long
# does a customer last" and "did churn move after a price change" were
# unanswerable, and unanswerable permanently: a date not written down at the
# moment it happened cannot be recovered afterwards.
#
# That is the argument for instrumenting now rather than when there is enough
# data to be interesting. The first ten customers are the ones whose behaviour
# decides whether this business works, and they are also the ones most easily
# lost.
#
# No addresses. Each entry carries a short salted hash of the trial identity,
# which is enough to follow ONE person's trial -> paid -> churn arc and to
# compute a lifetime, and useless for reading off who the customers are. An
# aggregate is the only thing anybody needs here.
LIFECYCLE_KEY = "lifecycle"
LIFECYCLE_MAX = int(os.environ.get("LIFECYCLE_MAX", "20000"))


def _bi_id(email):
    """A stable, non-reversible handle for one customer."""
    ident = _trial_identity(email or "")
    if not ident:
        return ""
    salt = (SUPABASE_SERVICE_ROLE_KEY or ADMIN_TOKEN or "curbcall")[:32]
    return hashlib.sha256((salt + "|bi|" + ident).encode("utf-8")).hexdigest()[:16]


def _bi_note(db, event, email="", plan="", extra=None):
    """Append one lifecycle event. Never raises -- BI must not break billing."""
    try:
        log = db.setdefault(LIFECYCLE_KEY, [])
        row = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "event": event, "id": _bi_id(email)}
        if plan:
            row["plan"] = plan
        if extra:
            row.update(extra)
        log.append(row)
        if len(log) > LIFECYCLE_MAX:
            del log[:len(log) - LIFECYCLE_MAX]
    except Exception:
        pass


def _trial_identity(email):
    """Normalize an email for TRIAL-ELIGIBILITY purposes only -- never for
    license-key lookups, which must stay exact. Strips a +tag from the local
    part: josh+1@gmail.com and josh+2@gmail.com deliver to the same inbox on
    Gmail, Outlook, Fastmail and most other providers, so without this, a free
    7-day trial (no card required, and every scan spends real OpenAI/search
    budget) could be farmed indefinitely from one real inbox."""
    email = (email or "").strip().lower()
    local, sep, domain = email.partition("@")
    if not sep:
        return email
    return f"{local.split('+', 1)[0]}@{domain}"


def _license_is_active(key, device, supabase_token=None):
    """Return True if this request has a valid license, active trial,
    OR a signed-in Supabase account (email-based trial counts too)."""
    key = (key or "").strip().upper()
    db = _db()

    # 1. Valid license key
    if key and key not in db.get("revoked", []):
        valid, _, _, _ = verify_key(key)
        if valid:
            return True

    # 2. Supabase account — check if their email has a key, or give them a trial
    if supabase_token:
        email = _verify_supabase_token(supabase_token)
        if email:
            if _is_admin_email(email):
                return True
            # email has an active issued key?
            ekey = db.get("emails", {}).get(email)
            if ekey and ekey not in db.get("revoked", []):
                ev, _, _, _ = verify_key(ekey)
                if ev:
                    return True
            # email-based trial -- normalized, so josh+1@ and josh+2@ can't
            # each claim their own free trial off one real inbox
            trials = db.setdefault("trials", {})
            trial_key = f"email:{_trial_identity(email)}"
            if trial_key in trials:
                started = datetime.datetime.fromisoformat(trials[trial_key]["started"])
                if datetime.datetime.now() <= started + datetime.timedelta(days=TRIAL_DAYS):
                    return True
            else:
                # First time this account scans — start their trial
                trials[trial_key] = {"started": datetime.datetime.now().isoformat(),
                                     "email": email}
                _save_db(db)
                return True

    # 3. Anonymous device-based trial (legacy / no account)
    trials = db.get("trials", {})
    if device in trials:
        started = datetime.datetime.fromisoformat(trials[device]["started"])
        if datetime.datetime.now() <= started + datetime.timedelta(days=TRIAL_DAYS):
            return True

    return False


