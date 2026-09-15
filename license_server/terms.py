# Saved-search email alerts (/run-saved-search-alerts, see below). Both are
# OPTIONAL and the feature is inert (returns "not_configured") until both are
# set -- nothing changes for existing users until you deliberately turn this
# on. SUPABASE_SERVICE_ROLE_KEY is a HIGH-PRIVILEGE secret (bypasses every
# row-level-security policy in the project) -- only ever set it as a Render
# env var, never ship it to a client. CRON_SECRET is a password only your
# scheduler (e.g. a GitHub Actions workflow) knows, so this endpoint can't be
# triggered by randoms hammering the URL.
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")


def _verify_supabase_token(token):
    """Ask Supabase if this access token belongs to a real signed-in user.
    Returns the user's email on success, None on failure.
    No external packages needed — just a plain HTTPS call."""
    if not (SUPABASE_URL and SUPABASE_ANON_KEY and token):
        return None
    try:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": SUPABASE_ANON_KEY,
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            user = json.loads(resp.read().decode("utf-8"))
            return (user.get("email") or "").lower() or None
    except Exception:
        return None


def _supabase_user(token):
    """The whole signed-in user record, not just the email.

    Deleting an account needs the user's id: Supabase's admin delete is
    addressed by id, and the id is the only field that cannot change under
    us while someone is mid-flow.
    """
    if not (SUPABASE_URL and SUPABASE_ANON_KEY and token):
        return None
    try:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"Authorization": f"Bearer {token}", "apikey": SUPABASE_ANON_KEY})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _supabase_delete_user(user_id):
    """Delete an auth user. Needs the service-role key; returns True on success."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and user_id):
        return False
    try:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/auth/v1/admin/users/{urllib.parse.quote(user_id)}",
            method="DELETE",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                     "apikey": SUPABASE_SERVICE_ROLE_KEY})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status in (200, 204)
    except Exception:
        return False


# ── Terms acceptance ────────────────────────────────────────────────────────
#
# The signup checkbox blocked a button in the browser and nothing else. The
# Terms carry the disclaimer of warranties, the liability cap and the choice
# of Missouri law, and every one of those binds only somebody who accepted
# them -- with no record, there was no way to show anyone had.
#
# The version is recorded, not just the fact. "They agreed to the Terms" is
# weak when the Terms change; "they accepted 2026-06-17" is evidence. These
# constants must match the dates published on terms.html and privacy.html,
# and a test fails if they drift.
TERMS_VERSION = os.environ.get("TERMS_VERSION", "2026-09-10")
PRIVACY_VERSION = os.environ.get("PRIVACY_VERSION", "2026-09-10")
# "reaccept" is an existing user agreeing to a version published after
# they signed up. Recorded distinctly so the record shows which
# acceptances were made at signup and which on a re-prompt.
_ACCEPT_METHODS = {"signup_form", "google", "magic_link", "reaccept"}


def _is_duplicate_row(err):
    """True when PostgREST refused a write because the row already exists.

    Read from the body rather than assumed from the status: 409 also covers
    foreign-key conflicts, and treating one of those as success would report a
    consent record that was never stored.
    """
    try:
        return json.loads(err.read().decode("utf-8", "replace")).get("code") == "23505"
    except Exception:
        return False


def _record_terms_acceptance(user, method):
    """Append one consent row. Returns True when it is safely stored.

    Written server-side with the service-role key rather than by the browser.
    A client-reported "yes I agreed" is worth exactly what the checkbox it
    replaces is worth; the point of the record is that the server saw it.
    """
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return False
    uid = (user or {}).get("id")
    if not uid:
        return False
    row = {"user_id": uid,
           "email": (user.get("email") or "").strip().lower(),
           "terms_version": TERMS_VERSION,
           "privacy_version": PRIVACY_VERSION,
           "method": method if method in _ACCEPT_METHODS else ""}
    try:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/terms_acceptances",
            data=json.dumps(row).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                     "apikey": SUPABASE_SERVICE_ROLE_KEY,
                     "Content-Type": "application/json",
                     "Prefer": "return=minimal"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status in (200, 201, 204)
    except urllib.error.HTTPError as ex:
        # A unique violation means this account already accepted this exact
        # pair of versions, which is the outcome the caller wanted. Reporting
        # it as a failure would leave the browser's pending flag set and make
        # it retry the same rejected write at every sign-in, forever.
        if ex.code == 409 and _is_duplicate_row(ex):
            return True
        print(f"[terms] could not record acceptance: {ex}", flush=True)
        return False
    except Exception as ex:
        # Loud, because a consent record that quietly fails to save is worse
        # than not having the feature: it looks like evidence exists.
        print(f"[terms] could not record acceptance: {ex}", flush=True)
        return False


def _has_accepted_current(user_id):
    """Has this account accepted the versions being published right now?

    Returns None when the question cannot be answered -- no configuration, a
    failed query -- and the caller treats that as "do not prompt". Nagging a
    paying customer because a database call failed is worse than a late
    re-acceptance, and the Terms already provide that continued use after a
    posted change counts.
    """
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and user_id):
        return None
    try:
        url = (f"{SUPABASE_URL}/rest/v1/terms_acceptances"
               f"?select=id&limit=1"
               f"&user_id=eq.{urllib.parse.quote(str(user_id))}"
               f"&terms_version=eq.{urllib.parse.quote(TERMS_VERSION)}"
               f"&privacy_version=eq.{urllib.parse.quote(PRIVACY_VERSION)}")
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                          "apikey": SUPABASE_SERVICE_ROLE_KEY})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return bool(json.loads(resp.read().decode("utf-8")))
    except Exception as ex:
        print(f"[terms] could not check acceptance: {type(ex).__name__}",
              flush=True)
        return None


@app.route("/terms/status", methods=["POST"])
def terms_status():
    """Whether this signed-in account still needs to accept what is published.

    The Terms and the Privacy Policy change. Somebody who agreed in June has
    not agreed to a September rewrite, and "continued use means you accept"
    is a weaker thing to rely on than a record of them clicking. This is what
    lets the app ask.
    """
    data = request.get_json(force=True, silent=True) or {}
    user = _supabase_user(data.get("supabase_token", ""))
    if not user or not user.get("id"):
        return jsonify({"ok": False, "reason": "not_signed_in"}), 401
    accepted = _has_accepted_current(user["id"])
    return jsonify({
        "ok": True,
        # Unknown is reported as "no prompt". Never block or nag on a failed
        # lookup -- see _has_accepted_current.
        "needs_acceptance": (accepted is False),
        "known": accepted is not None,
        "terms_version": TERMS_VERSION,
        "privacy_version": PRIVACY_VERSION,
    })


@app.route("/terms/accept", methods=["POST"])
def terms_accept():
    """Record that the signed-in account accepted the current Terms.

    Takes a Supabase token, not a user id: the caller says who they claim to
    be and the server checks it, so a record cannot be written on somebody
    else's behalf.
    """
    data = request.get_json(force=True, silent=True) or {}
    user = _supabase_user(data.get("supabase_token", ""))
    if not user or not user.get("id"):
        return jsonify({"ok": False, "reason": "not_signed_in"}), 401
    if not SUPABASE_SERVICE_ROLE_KEY:
        return jsonify({"ok": False, "reason": "not_configured"}), 503
    saved = _record_terms_acceptance(user, str(data.get("method") or ""))
    if not saved:
        return jsonify({"ok": False, "reason": "not_recorded"}), 502
    return jsonify({"ok": True, "terms_version": TERMS_VERSION,
                    "privacy_version": PRIVACY_VERSION})


# How long a consent record outlives the account it belongs to.
#
# Not Missouri's period, despite the Terms choosing Missouri law. Choice of
# law is not choice of forum: there is no forum-selection clause, so a
# customer in another state can sue where they live, and limitations periods
# are generally treated as procedural -- the court applies its OWN, not the
# one the contract picked. The relevant number is therefore the longest period
# among the states customers are actually in, not the one in the clause.
#
# Ten covers it. Missouri, Illinois, Iowa and Kentucky sit at ten for written
# contracts and are the longest in the sales region; the states outside it we
# have approached are shorter. Missouri itself offers ten on a writing for the
# payment of money (RSMo 516.110) and five on other written contracts
# (516.120), and which one a subscription falls under is exactly the question
# this is not the place to settle -- another reason to take the longer figure.
#
# Keeping evidence a few years past the point it could be needed costs
# nothing. Destroying it three years early cannot be undone. Confirm the
# period with the attorney, along with whether to add a forum-selection
# clause, and change the number here.
TERMS_RETENTION_YEARS = int(os.environ.get("TERMS_RETENTION_YEARS", "10"))


def _mark_terms_account_deleted(user_id):
    """Stamp the consent record instead of hashing or removing it.

    An earlier version replaced the address with a salted hash. Two things
    were wrong with that. The salt was the service-role key, so rotating a
    credential -- routine, and mandatory after any leak -- would have made
    every retained record permanently unverifiable while still looking
    intact. And a hash is worse evidence in practice: a plain address is a
    screenshot, a hash is a scheme that has to be explained and reproduced.

    So the address stays and the row gets a deletion date, which is what makes
    the retention bounded and answerable rather than indefinite.

    Best effort. A failure here must never block a deletion somebody asked
    for -- their account still goes.
    """
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and user_id):
        return False
    try:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/terms_acceptances"
            f"?user_id=eq.{urllib.parse.quote(str(user_id))}"
            f"&account_deleted_at=is.null",
            data=json.dumps({
                "account_deleted_at":
                    datetime.datetime.now(datetime.timezone.utc).isoformat()}).encode("utf-8"),
            method="PATCH",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                     "apikey": SUPABASE_SERVICE_ROLE_KEY,
                     "Content-Type": "application/json",
                     "Prefer": "return=minimal"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status in (200, 204)
    except Exception as ex:
        # Type only: the URL carries the project ref.
        print(f"[terms] could not stamp deletion for {user_id}: "
              f"{type(ex).__name__}", flush=True)
        return False


def _purge_expired_terms_acceptances():
    """Delete consent records whose retention period has run.

    Only rows belonging to accounts that are already gone -- a live customer
    is still bound by what they accepted, so account_deleted_at is null for
    them and they are never touched.

    This is the one place in the codebase permitted to delete from this
    table, and it is why the policy can promise a bounded retention rather
    than "indefinitely".
    """
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return {"ok": False, "reason": "not_configured"}
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=365 * TERMS_RETENTION_YEARS)).isoformat()
    try:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/terms_acceptances"
            f"?account_deleted_at=not.is.null"
            f"&account_deleted_at=lt.{urllib.parse.quote(cutoff)}",
            method="DELETE",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                     "apikey": SUPABASE_SERVICE_ROLE_KEY,
                     "Prefer": "return=representation,count=exact"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                purged = len(json.loads(body))
            except Exception:
                purged = 0
            return {"ok": True, "purged": purged, "cutoff": cutoff,
                    "retention_years": TERMS_RETENTION_YEARS}
    except Exception as ex:
        print(f"[terms] purge failed: {type(ex).__name__}", flush=True)
        return {"ok": False, "reason": type(ex).__name__}


@app.route("/terms/purge", methods=["POST"])
def terms_purge():
    """Scheduled cleanup. Gated by CRON_SECRET, like the other cron jobs."""
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("token") or request.headers.get("X-Cron-Secret", "")
    if not CRON_SECRET or not hmac.compare_digest(token, CRON_SECRET):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    result = _purge_expired_terms_acceptances()
    if result.get("ok"):
        _cron_beat("purge-expired-consent")
    return jsonify(result), (200 if result.get("ok") else 503)


@app.route("/account/delete", methods=["POST"])
def account_delete():
    """Delete the signed-in user's account and everything keyed to them.

    Refuses while a paid subscription is live. This server holds Stripe's
    webhook secret but no API key, so it cannot cancel a subscription -- and
    deleting the account anyway would leave a card being charged with no
    account left to log in and stop it. Better to send them to the billing
    portal first and have them come back.

    The trial record goes too, filed under the plus-stripped identity that
    _trial_identity() produces rather than the address as typed. That does
    mean deleting an account frees up a fresh trial on the same email, which
    is a real trade: the alternative is keeping a record of someone who asked
    to be forgotten, and a seven-day trial is not worth that. The trade is the
    same one for a tagged address -- it just was not honoured for those, which
    is the bug this note exists to stop coming back.
    """
    data = request.get_json(force=True, silent=True) or {}
    user = _supabase_user(data.get("supabase_token", ""))
    if not user or not user.get("id"):
        return jsonify({"ok": False, "reason": "not_signed_in"}), 401
    email = (user.get("email") or "").strip().lower()
    device = (data.get("device_id") or "").strip()

    db = _db()
    key = (db.get("emails", {}) or {}).get(email)
    if key and _license_is_active(key, device):
        return jsonify({"ok": False, "reason": "active_subscription",
                        "portal": "https://billing.stripe.com/p/login/"
                                  "3cIcN4an28420Yad2fejK00"}), 409

    if not SUPABASE_SERVICE_ROLE_KEY:
        return jsonify({"ok": False, "reason": "not_configured"}), 500
    # Before the auth user goes. The consent record is deliberately NOT
    # deleted -- see _mark_terms_account_deleted -- it is dated, so the
    # retention period has something to run from.
    _mark_terms_account_deleted(user["id"])

    if not _supabase_delete_user(user["id"]):
        return jsonify({"ok": False, "reason": "delete_failed"}), 502

    # Only now purge local rows: if the auth delete failed the account still
    # exists, and stripping its trial and licence out from under it would
    # leave a working login with no entitlements.
    trials = db.setdefault("trials", {})
    # Pop the key the trial was FILED under, which is the plus-stripped
    # identity -- not the raw address. These are the same string for an
    # ordinary email and different for a tagged one, so josh+test@gmail.com
    # deleted its account and left its trial row behind, with the full tagged
    # address still stored inside it. The function whose whole purpose is to
    # forget someone was keeping their email.
    trials.pop(f"email:{_trial_identity(email)}", None)
    # The raw form too: rows written before this was fixed are filed that way.
    trials.pop(f"email:{email}", None)
    if device:
        trials.pop(device, None)
    db.setdefault("emails", {}).pop(email, None)
    for table in ("referral_owner", "referral_codes"):
        rows = db.get(table) or {}
        for k in [k for k, v in rows.items() if v == email or k == email]:
            rows.pop(k, None)
    _save_db(db)
    # Deliberately not logged. The one action whose entire purpose is to
    # remove someone should not write their address into a log line on the
    # way out.
    return jsonify({"ok": True})

