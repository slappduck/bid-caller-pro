#!/usr/bin/env python3
"""Polite HTTP for state-government sites.

City sites take a plain urllib request without complaint. State sites very
often do not: several sit behind Akamai or Cloudflare and reject anything
whose headers do not look like a real browser session. Arkansas turned out
to be rejecting *incomplete headers* rather than our identity -- it answers
happily once Accept-Language and the Sec-Fetch-* set are present alongside a
User-Agent that says who we are.

Five states (KS, MA, ME, NH, NV) reject the honest agent no matter how
complete the headers are: they are checking that the User-Agent string claims
to be a browser. We do not lie to them. A site that has deliberately turned
away non-browser agents has made a choice, and quietly working around it is
not ours to make -- those states are recorded as blocked and reported, and
their bids have to reach us another way (the state's own open-data feed, an
emailed notice list, or a human).
"""
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

UA = ("CurbCallBot/1.0 (+https://curbcallpro.com; "
      "concrete bid aggregator; contact support@curbcallpro.com)")

# A real browser sends all of these. Sending only User-Agent is what several
# state edge configurations actually object to.
HEADERS = {
    "User-Agent": UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

# One request per host at a time, and a gap between them. State sites serve a
# whole department off one host; the city crawl's fan-out is not appropriate.
MIN_GAP_SEC = 1.5
_host_lock = threading.Lock()
_last_hit = {}
_robots = {}
_robots_lock = threading.Lock()


def _host_of(url):
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _wait_turn(host):
    while True:
        with _host_lock:
            now = time.time()
            ready = now - _last_hit.get(host, 0.0)
            if ready >= MIN_GAP_SEC:
                _last_hit[host] = now
                return
            sleep_for = MIN_GAP_SEC - ready
        time.sleep(sleep_for)


def robots_allows(url):
    """False only when a reachable robots.txt actually disallows this path.

    A robots.txt we cannot read is not consent and not refusal. Treating an
    unreachable one as "disallowed" would drop the several states whose edge
    blocks /robots.txt itself while serving the bid pages fine; treating it as
    permission is the same default every crawler uses. We take the second, and
    the blocked-agent rule above is what actually keeps us out where a site
    has said no.
    """
    host = _host_of(url)
    if not host:
        return False
    with _robots_lock:
        rp = _robots.get(host)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        robots_url = "%s://%s/robots.txt" % (
            urllib.parse.urlparse(url).scheme or "https", host)
        try:
            _wait_turn(host)
            req = urllib.request.Request(robots_url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                rp.parse(r.read(200000).decode("utf-8", "replace").splitlines())
        except Exception:
            rp = None  # unreadable -- see docstring
        with _robots_lock:
            _robots[host] = rp
    if rp is None:
        return True
    try:
        return rp.can_fetch(UA, url)
    except Exception:
        return True


def fetch(url, timeout=25, max_bytes=600000, check_robots=True):
    """(status, text). status is an int, or a short string for a failure.

    "blocked" specifically means the site refused our honest agent -- kept
    distinct from a transport error so the report can say which states chose
    to turn us away rather than which ones were merely down.
    """
    if check_robots and not robots_allows(url):
        return "robots_disallow", ""
    host = _host_of(url)
    if not host:
        return "bad_url", ""
    _wait_turn(host)
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(max_bytes).decode("utf-8", "replace")
    except urllib.error.HTTPError as ex:
        if ex.code in (401, 403, 406, 429):
            return "blocked", ""
        return "http_%d" % ex.code, ""
    except Exception as ex:
        return type(ex).__name__, ""
