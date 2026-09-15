"""
license_server — License validation + bid scanning for Bid Caller Pro.

This package is a structural split of what used to be a single 8,500+ line
license_server.py module, done purely to make the code navigable. It is NOT
a redesign and changes no behavior: every name that used to live at module
scope in license_server.py (every route, every helper, every constant,
every private underscore-prefixed function) is still exactly one name in
exactly one shared namespace, `license_server.__dict__` -- `import
license_server as ls; ls.anything_that_worked_before` keeps working,
`ls.app` (the Flask instance gunicorn serves) included.

WHY THIS IS __init__.py *EXECUTING* EACH FILE RATHER THAN IMPORTING REAL
SUBMODULES:

The obvious way to split a module into a package is to give each file its
own real submodule namespace and have __init__.py `from . import core`,
`from . import scan`, etc., then copy every public name up with something
like `for name in dir(core): globals()[name] = getattr(core, name)`. That
was the first approach tried here, and it is wrong for this codebase: 55
test files reach into internals via `unittest.mock.patch.object(ls,
"_some_helper", ...)`. That works by setting `license_server.__dict__["_some
_helper"]` to a mock -- but if `_some_helper` actually lives in, say,
license_server/search_sources.py as ITS OWN separate module namespace, then
every OTHER function inside search_sources.py that calls `_some_helper(...)`
resolves that bare name against search_sources.py's own `__dict__`, not
license_server's. The patch would silently miss every internal call site,
and only the direct `ls._some_helper` reference (which nothing in production
uses) would see the mock. Confirmed empirically: an earlier version of this
split with real submodules and a copy-based re-export ran clean at the
`import license_server as ls; ls.<name>` level but took the test suite from
1724 passing to 254 failures + 153 errors, almost all of exactly this
shape (a mocked helper that a sibling function in the same section calls
directly, or a live-mutated module-level counter -- e.g. the DuckDuckGo
"consecutive empty searches" streak -- read stale because it was copied by
value at import time instead of read from the one place it changes).

So instead: each file below (core.py, licensing.py, ...) is a plain source
file, not imported as a submodule at all. __init__.py reads each one in the
same order its content appeared in the original license_server.py and
`exec`s it with THIS module's own `globals()` as both its globals and
locals -- i.e. every name any of these files defines lands directly in
`license_server.__dict__`, the same single namespace the original
monolithic file always had. This is provably behavior-preserving (it is the
same code, executed in the same order, in the same namespace -- splitting
across files is purely a source-layout change) and it means
`patch.object(ls, "_some_helper", ...)` really does redirect every call
site, exactly as it did against the one-file version, because there is
still only one `_some_helper` binding in existence.

The exec order below matters (top-level code earlier in the original file
may be referenced by top-level code later in it -- e.g. `_env_secret`, used
by module constants below it must exist first) and was checked
mechanically against the original file to confirm no section here depends,
at MODULE-EXECUTION time (as opposed to inside a function body, which
resolves names lazily at call time and is unaffected by ordering), on
something defined in a section listed after it.

Sections, roughly following the banner comments the original file marked
each concern with (see each file's own top for the fuller breakdown of what
it covers and, where relevant, why a given helper ended up there rather
than somewhere more obviously named for it):
  core                    - Flask app, CORS, secrets loading, persistence
                            (Upstash-backed license/scan-cache helpers), and
                            license-key HMAC signing/verification
  licensing               - /validate, /trial, /issue, /revoke, /
  business_intelligence   - engagement/wins/funnel/BI summaries, the
                            lifecycle log, trial identity + license-active
                            checks, admin-email allowlist, /visit
  outreach_tracking       - /click, /coverage, /health (the big status dump)
  email                   - Resend delivery + delivery-feedback webhook
  terms                   - Supabase auth-token verification, terms
                            acceptance/status/accept/purge, /account/delete
  scheduled_jobs          - Stripe webhook signature check, renewal
                            reminders, admin error alerts, the cron
                            watchdog, trial-ending reminders, weekly digest
  alerts                  - /run-saved-search-alerts and the
                            /run-upcoming-alerts job body (the ROUTE itself
                            ended up physically inside admin_diagnostics --
                            see that file's own note)
  admin_diagnostics       - data export, /diag, admin/whoami/reviews/export,
                            the feed-accuracy bid audit, the global Flask
                            error handler
  referrals               - referral bonus days, Stripe webhook, /mykey,
                            and the outreach-campaign send/approve/draft/
                            suppression machinery (all one banner section
                            in the original file)
  community_submissions   - agency-posted notices, /support, /claim,
                            /admin/list, US state-name normalization, IP
                            rate limiting
  search_sources          - geocoding/deadline-parsing/scoring helpers, plus
                            every search integration (Google, Tavily,
                            Brave, DuckDuckGo, robots.txt, BidNet, SAM.gov
                            federal opportunities) and the portal/query
                            scan-building helpers layered on top of them
  scan                    - live scan progress, scan history, /scan,
                            /residential-leads, /upcoming
"""

import os as _os

_FILES = (
    "core.py",
    "licensing.py",
    "business_intelligence.py",
    "outreach_tracking.py",
    "email.py",
    "terms.py",
    "scheduled_jobs.py",
    "alerts.py",
    "admin_diagnostics.py",
    "referrals.py",
    "community_submissions.py",
    "search_sources.py",
    "scan.py",
)

_pkg_dir = _os.path.dirname(_os.path.abspath(__file__))

# Each file is compiled and exec'd with ITS OWN real path as the code
# object's filename, so `inspect.getsource(ls._perform_scan)` (and every
# other single-function lookup the test suite does this way) resolves
# correctly off the real file on disk, at the real line number within it --
# nothing special needed there.
_sources = []
for _fname in _FILES:
    _path = _os.path.join(_pkg_dir, _fname)
    with open(_path, encoding="utf-8") as _f:
        _source = _f.read()
    _sources.append(_source)
    exec(compile(_source, _path, "exec"), globals())

# A handful of tests additionally read "the source of license_server" as one
# whole blob -- either `inspect.getsource(ls)` (source of the MODULE
# object, which only ever looks at one file: whatever `ls.__file__` is), or
# a plain `open(repo_root/"license_server.py")` because that is what the
# single-file version of this project always was. Both are pre-existing,
# unedited test behavior this split must not break, so this regenerates a
# repo-root license_server.py on every import: the exact concatenation, in
# original order, of the files above -- i.e. the same text the one-file
# license_server.py used to be, byte for byte, so `inspect.getsource(ls)` and
# a raw read of it stay identical to before the split, and the two can never
# quietly drift out of sync no matter how the package's real files change.
# `license_server/` (this package) always shadows it for `import
# license_server` -- see the module docstring above -- so it is never what
# actually runs; it exists purely for code that reads source as text.
_repo_root = _os.path.dirname(_pkg_dir)
_full_source_path = _os.path.join(_repo_root, "license_server.py")
try:
    with open(_full_source_path, "w", encoding="utf-8") as _f:
        _f.write("\n\n".join(_sources))
    __file__ = _full_source_path
except OSError:
    # Read-only filesystem or similar: production does not need this file
    # (gunicorn imports the package, never this path), only some tests do,
    # so this never blocks a real deployment from starting.
    pass

del _os, _pkg_dir, _fname, _path, _f, _source, _sources, _repo_root, _full_source_path
