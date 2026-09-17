#!/usr/bin/env python3
"""One-off diagnostic for the federal-refresh 404s.

_federal_refresh has failed every scheduled run since roughly Sep 12,
always the same shape: sam_status 404 from SAM_SEARCH_URL (the official,
keyed transport). 404 means the endpoint itself is wrong, not the key --
see the comment on _sam_fetch. Run this with your real SAM_API_KEY to find
out what actually answers right now, without guessing at production.

Usage:
    SAM_API_KEY=your-real-key python3 tools/diagnose_sam_endpoint.py

Never logs the key. Only prints HTTP status and a short, scrubbed snippet
of each response body, plus the count of rows found for a candidate that
answers 200.
"""
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_KEY = os.environ.get("SAM_API_KEY", "")

CANDIDATES = (
    # Today's default -- this is the one 404ing in production.
    "https://api.data.gov/sam/opportunities/v2/search",
    "https://api.data.gov/sam/opportunities/v2/search/",
    "https://api.data.gov/sam/opportunities/v3/search",
    "https://api.data.gov/sam/opportunities/v1/search",
    "https://api.sam.gov/opportunities/v2/search",
    "https://api.sam.gov/prod/opportunities/v2/search",
)


def _scrub(text):
    """Never let the real key reach a log or a screen."""
    return re.sub(re.escape(API_KEY), "<redacted>", text) if API_KEY else text


def _probe(base_url, naics="238110"):
    today = datetime.datetime.now()
    params = {
        "api_key": API_KEY,
        "postedFrom": (today - datetime.timedelta(days=30)).strftime("%m/%d/%Y"),
        "postedTo": today.strftime("%m/%d/%Y"),
        "limit": "5",
        "offset": "0",
        "ncode": naics,
    }
    url = base_url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, _scrub(e.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001 - this is a diagnostic, report everything
        return None, _scrub("%s: %s" % (type(e).__name__, e))


def main():
    if not API_KEY:
        print("Set SAM_API_KEY (your real one) and re-run:\n"
              "  SAM_API_KEY=... python3 tools/diagnose_sam_endpoint.py")
        return 2

    print("Probing %d candidate endpoints with your real key.\n"
          "Pausing between requests to avoid a false 429.\n" % len(CANDIDATES))

    results = []
    for i, base in enumerate(CANDIDATES):
        if i:
            time.sleep(4)
        status, body = _probe(base)
        snippet = body[:200].replace("\n", " ")
        print("%-55s -> %s\n  %s\n" % (base, status, snippet))
        rows = None
        if status == 200:
            try:
                data = json.loads(body)
                rows = len(data.get("opportunitiesData")
                           or data.get("_embedded", {}).get("results") or [])
            except Exception:
                pass
        results.append((base, status, rows))

    print("=" * 70)
    good = [r for r in results if r[1] == 200]
    if good:
        print("Endpoint(s) that answered 200:")
        for base, status, rows in good:
            print("  %s (rows in this small window: %s)" % (base, rows))
        print("\nUpdate SAM_SEARCH_URL to the working one (Render env var, no code "
              "change needed) or tell me which one worked and I'll fix the default.")
    else:
        print("None of these answered 200. Statuses seen:",
              {r[1] for r in results})
        print("Worth checking your api.data.gov account/open.gsa.gov docs directly "
              "for a deprecation notice -- this key may need re-registering "
              "against whatever replaced this API.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
