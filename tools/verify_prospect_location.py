#!/usr/bin/env python3
"""Confirm a prospect is actually where the email is about to say they are.

A.C. Moate Industries was emailed "243 agency bid pages within 125 miles of
Toledo". They are in Auburn, Washington. They replied asking to be removed,
which is the correct response to mail about a market you do not work in.

The mistake was structural, not careless. Candidates come from searching
"<city> concrete contractor sidewalk curb", and a contractor with per-city
landing pages ranks for cities they merely advertise into. The search engine
answered the question it was asked; nothing then checked whether the company
had an address anywhere near the town whose number we were about to quote.

So this checks. For each prospect it reads the company's own site and looks
for evidence of the claimed state: a ZIP, a "City, ST", or the state named
outright. No evidence means the row is held, not sent.

Deliberately weak on the city and strict on the state. A contractor in Willow
Grove serving Skippack twenty miles away is fine -- same market, same
coverage number. A contractor in Washington being told about Ohio is not, and
that failure always crosses a state line.

    python3 tools/verify_prospect_location.py             # every ready row
    python3 tools/verify_prospect_location.py --all       # including sent
"""
import argparse
import csv
import html as H
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import state_fetch  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
PROSPECTS = os.path.join(os.path.dirname(_HERE), "data", "outreach_prospects.csv")
PATHS = ["", "contact", "contact-us", "about", "about-us", "locations",
         "service-areas"]

STATE_NAME = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

# A free-mail address tells you nothing about a website, so those rows carry
# an explicit `website` column instead.
FREE_MAIL = {"gmail.com", "comcast.net", "yahoo.com", "outlook.com",
             "hotmail.com", "aol.com", "att.net", "verizon.net", "msn.com"}


def site_for(row):
    site = (row.get("website") or "").strip()
    if site:
        return site if site.startswith("http") else "https://" + site
    domain = (row.get("email") or "").split("@")[-1].strip().lower()
    if not domain or domain in FREE_MAIL:
        return ""
    return "https://" + domain


def _text(h):
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h or "")
    return re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", h)))


def evidence(row):
    """(verdict, note). verdict is ok | no_state | no_site | unreachable."""
    site = site_for(row)
    if not site:
        return "no_site", "free-mail address and no website column"
    blob = ""
    for p in PATHS:
        st, h = state_fetch.fetch(site.rstrip("/") + ("/" + p if p else ""),
                                  timeout=18)
        if st == 200 and h:
            blob += " " + _text(h)
    if not blob.strip():
        return "unreachable", "nothing readable at " + site
    want = (row.get("state") or "").strip().upper()
    name = STATE_NAME.get(want, want)
    hits = []
    if re.search(r",\s*" + re.escape(want) + r"\b", blob):
        hits.append('"City, %s"' % want)
    if name and re.search(r"\b" + re.escape(name) + r"\b", blob, re.I):
        hits.append(name)
    if not hits:
        others = sorted({s for s in STATE_NAME
                         if re.search(r",\s*" + s + r"\b", blob)})
        return "no_state", ("no sign of %s; site names %s"
                            % (want, ", ".join(others[:6]) or "no state"))
    city = (row.get("city") or "").strip()
    if city and not re.search(r"\b" + re.escape(city) + r"\b", blob, re.I):
        return "ok", "state confirmed (%s); city %s not named, likely nearby" % (
            ", ".join(hits), city)
    return "ok", "confirmed " + ", ".join(hits)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true",
                    help="check every row, not just those ready to send")
    args = ap.parse_args()

    if not os.path.exists(PROSPECTS):
        print("no prospect list at " + PROSPECTS, file=sys.stderr)
        return 2
    with open(PROSPECTS, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    todo = rows if args.all else [r for r in rows
                                  if r.get("status") == "ready"]
    if not todo:
        print("nothing to check")
        return 0

    bad = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for row, (verdict, note) in zip(todo, ex.map(evidence, todo)):
            mark = "ok  " if verdict == "ok" else "HOLD"
            if verdict != "ok":
                bad += 1
            print("%s %-10s %-32s %s, %s -- %s"
                  % (mark, row["slug"], row["company"][:32],
                     row.get("city", ""), row.get("state", ""), note))
    print("\n%d checked, %d to look at before sending." % (len(todo), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
