#!/usr/bin/env python3
"""Record an opt-out in the prospect list, correctly, the day it arrives.

OUTREACH.md promises that an opt-out is honoured immediately and permanently.
Until now that promise was a person remembering to open a CSV and type the
right word in the right column while doing something else. A.C. Moate replied
"Please remove me from your email list" the morning after the first send;
that one was honoured, and it was honoured by hand.

Hand-editing is fine until the week there are four of them. The failure is
not dramatic -- a status typed as "unsubscribe" instead of "unsubscribed"
reads as an unrecognised value, the row stays eligible, and the tool that is
supposed to refuse to draft it drafts it. The person who asked to be left
alone gets a second email, which is the one outcome the whole guard exists to
prevent, and it is also a CAN-SPAM violation.

So this writes the exact value outreach_draft.py refuses on, writes it
atomically, and keeps a backup of the file it replaced.

It also looks for other rows at the same company. A person saying "take us
off your list" is speaking for the company, not for one mailbox, and a second
address at the same domain is the obvious way to break the promise while
believing it was kept. Those are reported, never changed automatically: which
addresses a request covers is a judgement, not a string match.

    python3 tools/outreach_optout.py someone@example.com
    python3 tools/outreach_optout.py --reason bounced a@x.com b@y.com
    python3 tools/outreach_optout.py --dry-run someone@example.com
"""
import argparse
import csv
import datetime
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.verify_prospect_location import FREE_MAIL, PROSPECTS  # noqa: E402

# The statuses outreach_draft.py treats as "never again". Spelling one of
# these wrong is the whole reason this script exists, so they are imported
# from the same place the guard reads them rather than retyped here.
from tools.outreach_draft import DO_NOT_CONTACT  # noqa: E402

DEFAULT_REASON = "unsubscribed"


def _norm(addr):
    return (addr or "").strip().lower()


def _domain(addr):
    return _norm(addr).split("@")[-1]


def apply_optouts(rows, wanted, reason, today):
    """(changed, missing, siblings). Pure -- no file access, so it is testable
    and so a dry run and a real run cannot disagree about what would happen."""
    wanted = {_norm(a) for a in wanted if _norm(a)}
    changed, seen = [], set()
    for row in rows:
        addr = _norm(row.get("email"))
        if addr not in wanted:
            continue
        seen.add(addr)
        if row.get("status") in DO_NOT_CONTACT:
            continue          # already honoured; never downgrade it
        row["status"] = reason
        if "sent_date" in row and not row.get("sent_date"):
            row["sent_date"] = today
        changed.append(row)

    missing = sorted(wanted - seen)

    # Other live rows at the same company. Reported only.
    domains = {_domain(a) for a in wanted} - FREE_MAIL - {""}
    siblings = [r for r in rows
                if _domain(r.get("email")) in domains
                and _norm(r.get("email")) not in wanted
                and r.get("status") not in DO_NOT_CONTACT]
    return changed, missing, siblings


def write_rows(path, fieldnames, rows):
    """Replace the list atomically, keeping the file it replaced.

    The prospect list exists in exactly one place -- it is gitignored on
    purpose, business contact data in a public repo -- so a half-written file
    here is not recoverable from anywhere.
    """
    shutil.copy2(path, path + ".bak")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("emails", nargs="+", help="addresses that asked to stop")
    ap.add_argument("--reason", default=DEFAULT_REASON,
                    choices=sorted(DO_NOT_CONTACT),
                    help="why (default: %s)" % DEFAULT_REASON)
    ap.add_argument("--dry-run", action="store_true",
                    help="say what would change, write nothing")
    args = ap.parse_args()

    if not os.path.exists(PROSPECTS):
        print("no prospect list at %s" % PROSPECTS, file=sys.stderr)
        return 2
    with open(PROSPECTS, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows, fields = list(reader), reader.fieldnames

    today = datetime.date.today().isoformat()
    changed, missing, siblings = apply_optouts(rows, args.emails,
                                               args.reason, today)

    for row in changed:
        print("%-10s %-32s -> %s" % (row.get("slug", ""),
                                     row.get("company", "")[:32], args.reason))
    for addr in missing:
        print("not in the list: %s" % addr, file=sys.stderr)
    if siblings:
        print("\nStill live at the same company -- decide, do not assume:",
              file=sys.stderr)
        for row in siblings:
            print("  %-10s %-28s %s" % (row.get("slug", ""),
                                        row.get("company", "")[:28],
                                        row.get("email", "")), file=sys.stderr)

    if not changed:
        print("nothing to change")
        return 1 if missing else 0
    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0
    write_rows(PROSPECTS, fields, rows)
    print("\n%d row(s) updated. Previous file kept at %s.bak"
          % (len(changed), os.path.basename(PROSPECTS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
