#!/usr/bin/env python3
"""Which SAM.gov opportunities endpoint actually answers, with a real key.

/health on the live server has been reporting sam_gov.last_status = 404 for
13 consecutive keyed requests against the currently configured endpoint,
https://api.data.gov/sam/opportunities/v2/search -- see license_server.py's
own comment on that status code: "403 a rejected key, 404 a wrong endpoint,
429 a rate limit." A 404 with a real, configured key is the wrong-endpoint
case, not an auth problem.

That endpoint was not picked at random -- tests/test_federal_scan.py has a
test locking it in, with a comment explaining that api.sam.gov/prod/... was
tried first and answers 404 on every path, so api.data.gov/sam/... was the
one confirmed live. That was true when it was written. GSA's own current
docs (open.gsa.gov/api/get-opportunities-public-api/) are self-contradictory
about the production host -- one section says api.sam.gov, the worked
example says api.sam.gov/prod -- and don't mention api.data.gov as a proxy
at all any more. So the working theory is that api.data.gov's SAM proxy has
since been retired, and the previously-ruled-out api.sam.gov guess was only
ever tested with the /prod/ stage prefix, never without it.

This script does not guess further -- it asks each candidate with a real
key and a real, validly-shaped request (same params license_server.py
sends: postedFrom/postedTo as a date window, limit, offset) and reports
what actually comes back. DEMO_KEY cannot answer this: SAM's opportunities
API 404s DEMO_KEY on every host tried during this investigation, so the
result is silent either way. Run this with the real SAM_API_KEY -- it lives
only in Render's environment, not in this repo -- from wherever that key is
available (Render's shell, or export it locally for one run).

Nothing here writes anything or changes which endpoint the app uses. If a
candidate comes back with real opportunity data, update OFFICIAL_BASE in
federal_bids.py and the SAM_SEARCH_URL default in license_server.py and
license_server/search_sources.py, then update the two lines in
tests/test_federal_scan.py::EndpointTests that pin the current answer --
they exist to catch exactly this class of regression, so a fix here isn't
done until that test asserts the new URL instead of the old one.

    SAM_API_KEY=xxxxx python3 tools/diagnose_sam_endpoint.py
    python3 tools/diagnose_sam_endpoint.py --key xxxxx
"""
import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

CANDIDATES = [
    ("current default (federal_bids.py / license_server.py)",
     "https://api.data.gov/sam/opportunities/v2/search"),
    ("api.sam.gov, no stage prefix -- untested by the prior investigation",
     "https://api.sam.gov/opportunities/v2/search"),
    ("api.sam.gov/prod -- already ruled out, kept here for the record",
     "https://api.sam.gov/prod/opportunities/v2/search"),
]

TIMEOUT = 20


def build_url(base, api_key, window_days=30):
    today = datetime.datetime.now()
    params = {
        "api_key": api_key,
        "postedFrom": (today - datetime.timedelta(days=window_days)).strftime("%m/%d/%Y"),
        "postedTo": today.strftime("%m/%d/%Y"),
        "limit": "1",
        "offset": "0",
    }
    return base + "?" + urllib.parse.urlencode(params)


def probe(label, base, api_key):
    url = build_url(base, api_key)
    print("=" * 72)
    print(label)
    print("  " + base)
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/json",
                          "User-Agent": "CurbCallBot/1.0 (+https://curbcallpro.com; "
                                        "concrete bid aggregator; contact support@curbcallpro.com)"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
                total = data.get("totalRecords")
                if total is not None:
                    print(f"  HTTP {resp.status} -- real JSON, totalRecords={total}")
                    print("  VERDICT: this endpoint works.")
                else:
                    print(f"  HTTP {resp.status} -- JSON but no totalRecords field; "
                          "shape may have changed:")
                    print("  " + body[:300])
                    print("  VERDICT: reachable, but response shape is unexpected -- look closer.")
            except json.JSONDecodeError:
                print(f"  HTTP {resp.status} -- not JSON:")
                print("  " + body[:300])
                print("  VERDICT: reachable but did not return JSON -- probably the wrong path.")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  HTTP {e.code}")
        print("  " + body[:300])
        if e.code == 403:
            print("  VERDICT: endpoint exists, key rejected (wrong/expired key, "
                  "or a sam.gov-profile key instead of an api.data.gov one).")
        elif e.code == 404:
            print("  VERDICT: wrong endpoint -- nothing is routed here.")
        elif e.code == 429:
            print("  VERDICT: endpoint exists, rate-limited -- try again later, "
                  "this is not the bug.")
        else:
            print("  VERDICT: unexpected status, read the body above.")
    except urllib.error.URLError as e:
        print(f"  Could not connect: {e.reason}")
        print("  VERDICT: host itself is not resolving/reachable.")
    except Exception as e:
        print(f"  Unexpected error: {e}")
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", help="SAM_API_KEY; falls back to the environment variable")
    args = ap.parse_args()

    api_key = args.key or os.environ.get("SAM_API_KEY")
    if not api_key:
        print("No SAM_API_KEY given (--key or the SAM_API_KEY env var) -- "
              "this is a Render-only secret, not something in this repo.\n"
              "DEMO_KEY cannot substitute: every host tried during this "
              "investigation 404s DEMO_KEY regardless of whether the path "
              "is right, so a DEMO_KEY run would prove nothing.",
              file=sys.stderr)
        return 2

    print(f"Probing {len(CANDIDATES)} candidate endpoints with the real key "
          f"({len(api_key)} chars, not printed)...\n")
    for label, base in CANDIDATES:
        probe(label, base, api_key)

    print("=" * 72)
    print("Whichever candidate above says 'this endpoint works' is the one to "
          "put in federal_bids.py's OFFICIAL_BASE and the SAM_SEARCH_URL "
          "defaults in license_server.py / license_server/search_sources.py. "
          "Update tests/test_federal_scan.py::EndpointTests to match before "
          "calling it fixed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
