#!/usr/bin/env python3
"""Read a prospect's own website and report what is actually on it.

The intro line is the whole pitch. `outreach_draft.py` deliberately refuses
to write one, because a generated opener is a merge field wearing a costume
and reads like one. But refusing to write it left the blank page, and a blank
page at five a day gets filled with whatever is easiest to find.

Whatever is easiest to find is the top of the homepage. Columbia Curb &
Gutter got an email opening on their 2001 SBA Regional Prime Contractor
award; that sentence is the third paragraph of their "Who We Are" page, and
it was twenty-five years old. It reads as scraped because it was. Two
paragraphs further down the same page: slip-formed barrier for roads and
bridges, cold milling, their own trucking fleet, work delivered for
contractors across Missouri and neighbouring states. A company with its own
fleet working regionally cares about a wide radius, which is the strongest
version of the argument we have -- and it was sitting there unread.

So this reads the site and hands back the material, sorted, with the sentence
each fact came from. It does not write the email and it never will. Quoting
the source sentence is the point: a fact you cannot see the origin of is a
fact you are about to get wrong in front of a stranger.

Dated claims are flagged with their age, so an award from 2001 announces
itself as an award from 2001 rather than arriving as a fresh detail.

Nothing here sends or drafts anything. It prints research for a person to
read before they type.

    python3 tools/outreach_research.py                # every ready row
    python3 tools/outreach_research.py --slug ccg     # one, ignoring status
    python3 tools/outreach_research.py --all          # including sent/held
    python3 tools/outreach_research.py --json         # machine-readable
"""
import argparse
import csv
import datetime
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import state_fetch  # noqa: E402
from tools.verify_prospect_location import (  # noqa: E402
    PROSPECTS, _text, site_for)

# The pages worth reading. Ordered by how often they carry something specific
# rather than something generic -- "about" beats the homepage, and a services
# page beats both, because the homepage is where the marketing copy lives.
PATHS = ["", "about", "about-us", "who-we-are", "services", "what-we-do",
         "projects", "our-work", "portfolio", "capabilities"]

THIS_YEAR = datetime.date.today().year

# Old enough that leading with it says "I read your homepage and stopped".
STALE_YEARS = 10

# A year before this is a typo or a date in body text, not a founding year.
OLDEST_PLAUSIBLE = 1850


# Splitting on ". " cuts "Columbia Curb & Gutter Co." in half, and a quote
# that stops mid-company-name reads as sloppy in the one place the reader is
# judging whether a person wrote this. Contractor copy is thick with these:
# Co., Inc., the U.S. Small Business Administration, St. Louis.
_ABBREV = (r"Co|Inc|Corp|Ltd|LLC|LLP|Bros|Mfg|Assn|No|Est|Dept|Div|"
           r"Jr|Sr|Mr|Mrs|Ms|Dr|St|Ave|Rd|Blvd|Hwy|Ste|approx|"
           r"U\.S|U\.S\.A|D\.B\.E|[A-Z]")
_DOT = "\x00"


def _sentences(blob):
    """Split into sentences without cutting company names in half."""
    safe = re.sub(r"\b(" + _ABBREV + r")\.", r"\1" + _DOT, blob)
    parts = re.split(r"(?<=[.!?])\s+", safe)
    out = []
    for p in parts:
        s = p.replace(_DOT, ".").strip()
        if 20 <= len(s) <= 400:
            out.append(s)
    return out


# Navigation, cookie banners and template credits carry no punctuation, so a
# whole menu collapses into one 300-character "sentence" and the useful clause
# ends up buried in the middle of it. Quote a window around the match instead
# of the whole run-on: the reader wants the fact, not the site chrome.
_WINDOW_BEFORE = 70
_LONG = 240


def _window(s, rx):
    m = rx.search(s)
    if not m or len(s) <= _LONG:
        return s
    start = max(0, m.start() - _WINDOW_BEFORE)
    if start:
        # Start on a word boundary so the quote does not open mid-word.
        space = s.find(" ", start)
        start = space + 1 if 0 <= space < m.start() else start
    clip = s[start:m.end() + _LONG - _WINDOW_BEFORE].strip()
    return ("..." if start else "") + clip


def _find(sentences, pattern):
    """Every sentence matching `pattern`, deduped, order preserved."""
    rx = re.compile(pattern, re.I)
    seen, out = set(), []
    for s in sentences:
        if rx.search(s):
            key = s.lower()
            if key not in seen:
                seen.add(key)
                out.append(_window(s, rx))
    return out


def founded(sentences):
    """(year, sentence) for the founding year, or (None, '')."""
    rx = re.compile(
        r"\b(?:founded|established|est\.?|serving|in business|since)\b[^.]{0,40}?"
        r"\b(1[89]\d{2}|20[0-2]\d)\b", re.I)
    for s in sentences:
        m = rx.search(s)
        if m:
            y = int(m.group(1))
            if OLDEST_PLAUSIBLE <= y <= THIS_YEAR:
                return y, _window(s, rx)
    return None, ""


def dated_claims(sentences, skip_year):
    """Sentences carrying a year, with age. These are the staleness traps."""
    out = []
    for s in sentences:
        for m in re.finditer(r"\b(19\d{2}|20[0-2]\d)\b", s):
            y = int(m.group(1))
            if y == skip_year or not (OLDEST_PLAUSIBLE <= y <= THIS_YEAR):
                continue
            out.append({"year": y, "age": THIS_YEAR - y, "sentence": s,
                        "stale": THIS_YEAR - y > STALE_YEARS})
            break
    return out


# What they actually build. Specific beats broad: "slip-formed barrier" is a
# usable opener, "concrete" is not, so the narrow terms are listed first and
# the generic ones are only reported when nothing narrower matched.
NARROW = (r"slip[- ]form\w*|cold milling|ADA ramp|curb ramp|barrier wall|"
          r"detectable warning|street sweeping|excavat\w+|grading|"
          r"stamped concrete|decorative concrete|concrete pumping|"
          r"tilt[- ]up|post[- ]tension\w*|rebar|shotcrete|bridge deck")
BROAD = (r"curb (?:and|&) gutter|sidewalk|flatwork|driveway|patio|"
         r"parking lot|foundation|paving|asphalt|concrete")

SIGNALS = [
    ("service_area",
     r"throughout|serving|service area|counties|surrounding|statewide|"
     r"tri[- ]state|within \d+ (?:miles|mi)\b"),
    ("scale",
     r"\bfleet\b|our own equipment|\d+\s*(?:employees|crews|trucks)|"
     r"in[- ]house|family[- ]owned|second[- ]generation|third[- ]generation"),
    ("public_works",
     r"\bDBE\b|\bMBE\b|\bWBE\b|\bSBE\b|prequalif\w+|\bMoDOT\b|\bDOT\b|"
     r"prevailing wage|municipal|city of |county of |public works|"
     r"school district|bonded|bid(?:ding|s)?\b"),
    ("credentials",
     r"licensed|insured|certified|OSHA|accredited|member of|association"),
]


def research(row):
    """Everything readable about one prospect. Facts only, with sources."""
    site = site_for(row)
    out = {"slug": row.get("slug", ""), "company": row.get("company", ""),
           "city": row.get("city", ""), "state": row.get("state", ""),
           "site": site, "problem": "", "founded": None, "founded_note": "",
           "dated": [], "specialties": [], "signals": {}}

    if not site:
        out["problem"] = "free-mail address and no website column"
        return out

    blob = ""
    for p in PATHS:
        st, h = state_fetch.fetch(site.rstrip("/") + ("/" + p if p else ""),
                                  timeout=18)
        if st == 200 and h:
            blob += " " + _text(h)
    if not blob.strip():
        out["problem"] = "nothing readable at " + site
        return out

    sents = _sentences(blob)
    year, note = founded(sents)
    out["founded"] = year
    out["founded_note"] = note
    out["dated"] = dated_claims(sents, year)[:4]

    narrow = _find(sents, NARROW)
    out["specialties"] = (narrow or _find(sents, BROAD))[:3]
    for name, pattern in SIGNALS:
        hits = _find(sents, pattern)[:2]
        if hits:
            out["signals"][name] = hits
    return out


LABEL = {"service_area": "service area", "scale": "scale",
         "public_works": "public works", "credentials": "credentials"}


def show(r):
    print("=" * 72)
    head = "%s   (%s, %s)" % (r["company"], r["city"], r["state"])
    print(head)
    print("  %s" % (r["site"] or "-"))
    if r["problem"]:
        print("  ** %s" % r["problem"])
        print()
        return
    if r["founded"]:
        print("\n  founded  %d  (%d years)" % (r["founded"],
                                               THIS_YEAR - r["founded"]))
        print("           \"%s\"" % r["founded_note"][:200])
    if r["specialties"]:
        print("\n  builds")
        for s in r["specialties"]:
            print("           \"%s\"" % s[:200])
    for name, hits in r["signals"].items():
        print("\n  %s" % LABEL.get(name, name))
        for s in hits:
            print("           \"%s\"" % s[:200])
    stale = [d for d in r["dated"] if d["stale"]]
    if stale:
        print("\n  dated -- do not lead with these")
        for d in stale:
            print("           %d (%d yrs old) \"%s\""
                  % (d["year"], d["age"], d["sentence"][:150]))
    print()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slug", action="append", default=None,
                    help="research only these, ignoring status")
    ap.add_argument("--all", action="store_true",
                    help="every row, not just those ready to send")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    args = ap.parse_args()

    if not os.path.exists(PROSPECTS):
        print("no prospect list at %s\n"
              "It is deliberately not in the repo -- business contact data, "
              "public repo.\nSee OUTREACH.md for the columns."
              % PROSPECTS, file=sys.stderr)
        return 2
    with open(PROSPECTS, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if args.slug:
        want = set(args.slug)
        todo = [r for r in rows if r.get("slug") in want]
    elif args.all:
        todo = rows
    else:
        todo = [r for r in rows if r.get("status") == "ready"]
    if not todo:
        print("nothing to research")
        return 0

    with ThreadPoolExecutor(max_workers=4) as ex:
        found = list(ex.map(research, todo))

    if args.json:
        print(json.dumps(found, indent=2))
        return 0
    for r in found:
        show(r)
    stuck = [r for r in found if r["problem"]]
    print("%d researched, %d with nothing to read." % (len(found), len(stuck)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
