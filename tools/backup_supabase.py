#!/usr/bin/env python3
"""Copy the customer data that only lives in Supabase, dated, verified.

tools/backup_local_data.py protects the two files that exist on one disk.
This protects something with the same shape of risk but higher stakes: on
Supabase's Free tier, nothing is backed up automatically -- no daily
snapshot, no point-in-time recovery. Company profiles, terms acceptances,
saved bids and searches, reviews, feeds, and the account list itself have
exactly as many copies as this script makes. A bad migration, an accidental
delete, or a Supabase-side incident has no recovery path otherwise.

This is not a substitute for Supabase's own paid backup tiers -- it is a DIY
floor under "we currently have nothing." It pulls every row from each table
this project's schema creates (supabase_sync_schema.sql /
supabase_kv_schema.sql), plus the account list via the Auth admin API, and
writes each as dated JSON -- same shape as backup_local_data.py: verified
against what the server actually reported, pruned after a retention window,
"saved ..." lines so a scheduled run is quiet unless something is wrong.

A snapshot from whenever this last ran, not a continuous log of every
change -- restoring from it loses anything written since. Better than
nothing is still the honest ceiling here, not "as good as a managed backup."

    python3 tools/backup_supabase.py "C:/Users/Josh/OneDrive/curbcall-backup/supabase"
    python3 tools/backup_supabase.py ~/Backups/supabase --keep 30
    python3 tools/backup_supabase.py ~/Backups/supabase --list

Needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY -- the same two values
license_server already reads from data/sender.env or a real environment
variable (see docs/environment_variables.md). The service role key bypasses
row-level security, same as it does for the backend, so treat it with the
same care: never in this repo, never in a client.
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.outreach_draft import _sender  # noqa: E402 -- reuses data/sender.env

SUPABASE_URL = (_sender("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = _sender("SUPABASE_SERVICE_ROLE_KEY")

# Every table this project's own schema files create -- not auth.users
# itself, which Supabase manages directly and which is fetched separately,
# through the Auth admin API, in fetch_users() below.
TABLES = ["company_profiles", "kv_store", "reviews", "saved_bids",
          "saved_searches", "terms_acceptances", "user_feeds"]

PAGE_SIZE = 1000
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})__")
DEFAULT_KEEP = 60


def _get(url, headers, timeout=30):
    """One HTTPS GET. Returns (status, body_bytes, response_headers).
    Never raises on a 4xx/5xx -- the caller decides what a bad status means,
    the same way outreach_draft.py's coverage() treats a failure as data."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def _table_headers(extra=None):
    h = {"Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
         "apikey": SUPABASE_SERVICE_ROLE_KEY}
    h.update(extra or {})
    return h


def fetch_table(name, get=_get):
    """Every row in one table, paginated via PostgREST's Range header.
    Raises IOError on anything but a clean 200/206 -- a partial or failed
    fetch must never be written to disk labelled as a complete backup."""
    rows = []
    offset = 0
    while True:
        status, body, _ = get(
            f"{SUPABASE_URL}/rest/v1/{name}?select=*",
            _table_headers({"Range-Unit": "items",
                            "Range": f"{offset}-{offset + PAGE_SIZE - 1}"}))
        if status not in (200, 206):
            raise IOError(f"{name}: HTTP {status} fetching rows at offset {offset}")
        page = json.loads(body.decode("utf-8"))
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def fetch_users(get=_get):
    """The account list via Supabase's Auth admin API, paginated by page
    number (that API's own convention, distinct from PostgREST's Range)."""
    users = []
    page = 1
    while True:
        status, body, _ = get(
            f"{SUPABASE_URL}/auth/v1/admin/users?page={page}&per_page={PAGE_SIZE}",
            _table_headers())
        if status != 200:
            raise IOError(f"users: HTTP {status} fetching page {page}")
        data = json.loads(body.decode("utf-8"))
        got = data.get("users", [])
        users.extend(got)
        if len(got) < PAGE_SIZE:
            return users
        page += 1


def backup(dest, today=None, get=_get):
    """(saved, failed). Verifies each write by re-reading it back and
    comparing row counts, rather than trusting the write succeeded."""
    today = today or datetime.date.today().isoformat()
    os.makedirs(dest, exist_ok=True)
    saved, failed = [], []
    for name, fetcher in [(n, lambda g, n=n: fetch_table(n, g)) for n in TABLES] + \
                         [("auth_users", fetch_users)]:
        try:
            rows = fetcher(get)
        except Exception as e:
            failed.append((name, str(e)))
            continue
        out = os.path.join(dest, f"{today}__{name}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, default=str)
        with open(out, encoding="utf-8") as f:
            verified = len(json.load(f)) == len(rows)
        # Unlink AFTER the handle closes. Deleting inside the `with` is legal
        # on POSIX and raises WinError 32 on Windows, which turned "drop the
        # copy that did not verify" into "crash the run and leave it behind"
        # -- the exact opposite of the promise above, on the platform the
        # scheduled backup actually runs from.
        if not verified:
            os.unlink(out)
            failed.append((name, "written copy did not verify"))
            continue
        saved.append((out, len(rows)))
    return saved, failed


def prune(dest, keep_days, today=None):
    """Delete dated copies older than keep_days. Never touches a file it
    did not name itself -- the stamp prefix is the whole permission,
    same rule as backup_local_data.py's prune()."""
    today = today or datetime.date.today()
    if isinstance(today, str):
        today = datetime.date.fromisoformat(today)
    removed = []
    for name in sorted(os.listdir(dest)):
        m = STAMP.match(name)
        if not m:
            continue
        try:
            when = datetime.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if (today - when).days > keep_days:
            os.unlink(os.path.join(dest, name))
            removed.append(name)
    return removed


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dest", help="folder to copy into; use one that syncs off this machine")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                    help="days of history to retain (default %d)" % DEFAULT_KEEP)
    ap.add_argument("--list", action="store_true",
                    help="show what is already backed up and stop")
    args = ap.parse_args()

    if args.list:
        if not os.path.isdir(args.dest):
            print("no backups at %s" % args.dest, file=sys.stderr)
            return 1
        names = [n for n in sorted(os.listdir(args.dest)) if STAMP.match(n)]
        for n in names:
            print(n)
        print("\n%d file(s). Newest: %s" % (len(names), names[-1] if names else "none"))
        return 0

    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        print("Not backing up: SUPABASE_URL and/or SUPABASE_SERVICE_ROLE_KEY not set.\n"
              "Add them to data/sender.env (gitignored) or export as real "
              "environment variables -- see docs/environment_variables.md.",
              file=sys.stderr)
        return 2

    saved, failed = backup(args.dest)
    for path, n in saved:
        print("saved %s (%d row(s))" % (path, n))
    for name, why in failed:
        print("FAILED %s: %s" % (name, why), file=sys.stderr)
    removed = prune(args.dest, args.keep)
    if removed:
        print("pruned %d copy(ies) older than %d days" % (len(removed), args.keep))
    if not saved:
        print("nothing was backed up", file=sys.stderr)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
