#!/usr/bin/env python3
"""AI-drafted outreach email, grounded in verified facts, never auto-sent.

outreach_draft.py refuses to write the email on purpose -- an earlier
template-based version read as machine-written, and two of five went to
contractors in the same city, so it was one forwarded email away from being
obvious. That lesson does not go away just because a model is writing the
prose instead of a merge field: a batch of AI drafts that all sound like the
same voice reproduces the exact failure.

So this stays inside the guards that already exist -- the same location
check, the same live agency count, the same do-not-contact enforcement, the
same MIN_AGENCIES floor -- and adds exactly one thing: a full draft, written
by Claude, grounded only in what outreach_research.py can actually find on
the prospect's own site. A prospect with nothing readable is held, not
drafted from nothing.

It writes files. It sends nothing, and it does not touch the CSV. A person
reads every draft, edits it, and sends it themselves -- same as always.

    python3 tools/outreach_ai_draft.py            # next 5 ready prospects
    python3 tools/outreach_ai_draft.py --limit 1
    python3 tools/outreach_ai_draft.py --slug procon

Needs ANTHROPIC_API_KEY, either exported or added as a line in
data/sender.env (already gitignored -- see OUTREACH.md).
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.outreach_draft import (  # noqa: E402
    DAILY, DO_NOT_CONTACT, MIN_AGENCIES, PROSPECTS, RADIUS, SITE,
    _looks_like_the_right_town, _sender, build_signature, coverage,
    signature_problem,
)
from tools.outreach_research import research  # noqa: E402
from tools.verify_prospect_location import evidence as _location_evidence  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
DRAFTS_DIR = os.path.join(_ROOT, "data", "outreach_drafts")
VOICE_FILE = os.path.join(_HERE, "outreach_voice.md")
MODEL = "claude-opus-5"


def voice():
    with open(VOICE_FILE, encoding="utf-8") as f:
        return f.read()


def facts_block(row, agencies, nearest_mi, researched):
    """The only material the model is allowed to draw specifics from."""
    lines = [f"- {agencies} bid-posting agencies within {RADIUS} miles of "
             f"{row['city']}, {row['state']} (verified live, not from the "
             "prospect's site)"]
    if nearest_mi is not None:
        lines.append(f"- nearest one is {nearest_mi} miles away")
    if researched.get("founded"):
        lines.append(f"- founded {researched['founded']}: "
                     f"\"{researched['founded_note']}\"")
    for s in researched.get("specialties", []):
        lines.append(f"- builds: \"{s}\"")
    for hits in researched.get("signals", {}).values():
        for s in hits:
            lines.append(f"- \"{s}\"")
    return "\n".join(lines)


def parse_response(text):
    """Split a model response into (subject, body). Defensive: a response
    that does not follow the asked-for shape still comes back as something
    reviewable rather than raising, since a person reads every draft anyway."""
    lines = text.strip().splitlines()
    if lines and lines[0].lower().startswith("subject:"):
        subject = lines[0].split(":", 1)[1].strip()
        body = "\n".join(lines[1:]).strip()
        return subject, body
    return "(no subject line returned -- write one)", text.strip()


def draft_email(client, row, agencies, nearest_mi, researched):
    prompt = (
        f"Recipient: {row['greeting']} at {row['company']}, "
        f"{row['city']}, {row['state']}.\n\n"
        "Facts you may use (do not use anything beyond these):\n"
        f"{facts_block(row, agencies, nearest_mi, researched)}\n\n"
        f"Tracking link to include once: {SITE}/go/{row['slug']}\n\n"
        "Respond with a line starting 'Subject: ', then a blank line, then "
        "the email body only. No signature, no sign-off name.")
    response = client.messages.create(
        model=MODEL, max_tokens=1024, system=voice(),
        messages=[{"role": "user", "content": prompt}])
    text = next(b.text for b in response.content if b.type == "text")
    return parse_response(text)


def _client():
    import anthropic
    key = _sender("ANTHROPIC_API_KEY")
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=DAILY)
    ap.add_argument("--slug", action="append", default=None,
                    help="draft only these prospects, ignoring status")
    ap.add_argument("--dest", default=DRAFTS_DIR,
                    help="where to write draft files (default data/outreach_drafts)")
    args = ap.parse_args()

    problem = signature_problem()
    if problem:
        print(problem, file=sys.stderr)
        return 2

    client = _client()
    if client is None:
        print("Not drafting: ANTHROPIC_API_KEY not set.\n"
              "Export it, or add a line to data/sender.env (gitignored):\n\n"
              "  ANTHROPIC_API_KEY=sk-ant-...\n", file=sys.stderr)
        return 2

    if not os.path.exists(PROSPECTS):
        print(f"no prospect list at {PROSPECTS}", file=sys.stderr)
        return 2
    with open(PROSPECTS, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    rows = [r for r in rows if r.get("status") not in DO_NOT_CONTACT]
    if args.slug:
        want = set(args.slug)
        queue = [r for r in rows if r["slug"] in want]
    else:
        queue = [r for r in rows if r.get("status") == "ready"][:args.limit]

    sig = build_signature()
    os.makedirs(args.dest, exist_ok=True)
    written, held = [], []
    for row in queue:
        verdict, why = _location_evidence(row)
        if verdict != "ok":
            held.append((row, f"location unconfirmed ({why})"))
            continue
        data = coverage(row["city"], row["state"])
        if not data or not data.get("ok"):
            held.append((row, "coverage lookup failed"))
            continue
        n = int(data.get("agencies") or 0)
        if not _looks_like_the_right_town(data, row["city"]):
            held.append((row, "location may be misresolved"))
            continue
        if n < MIN_AGENCIES:
            held.append((row, f"only {n} agencies — too thin to lead with"))
            continue
        researched = research(row)
        if researched.get("problem"):
            held.append((row, f"no material to draft from ({researched['problem']})"))
            continue
        subject, body = draft_email(client, row, n, data.get("nearest_mi"), researched)
        out = os.path.join(args.dest, f"{row['slug']}.txt")
        with open(out, "w", encoding="utf-8") as f:
            f.write(f"To: {row['email']}\nSubject: {subject}\n\n{body}\n\n{sig}\n")
        written.append(out)

    for path in written:
        print(f"drafted {path}")
    for r, why in held:
        print(f"held {r['slug']}: {why}", file=sys.stderr)
    print(f"\n{len(written)} draft(s) written to {args.dest}, {len(held)} held.\n"
          "Read every one before sending -- these are grounded but unreviewed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
