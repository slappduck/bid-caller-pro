#!/usr/bin/env python3
"""Build a prospect list of concrete contractors from their own websites.

Input is a seed file of `STATE|CITY|URL` lines (one business per line, from
search). For each one this fetches the homepage and the usual contact pages,
pulls the business email and phone that the business publishes about itself,
and writes a CSV the campaign sender can take.

Only public business contact details, and only from sites whose robots.txt
allows it -- the same standard applied to the state public-notice network,
which is why that one was not built. A site that disallows us is skipped and
recorded as `robots_disallow` rather than quietly dropped.

Deliberately NOT here: scraping Yelp/Angi/BBB/directory sites. Their terms
forbid it, their listings are frequently stale, and a lead-gen shell page is
not a contractor. Those domains are excluded at search time and again below.

  python3 tools/build_contractor_list.py seeds.txt --out data/prospects.csv
"""
import argparse
import csv
import html
import re
import sys
import urllib.parse
import urllib.request
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor

UA = "BidCallerPro/1.0 (+https://curbcallpro.com)"
CONTACT_PATHS = ("", "/contact", "/contact-us", "/contactus", "/about",
                 "/about-us", "/get-a-quote", "/estimate")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\(?\b(\d{3})\)?[-.\s]?(\d{3})[-.\s]?(\d{4})\b")

# Addresses that are never a business contact: platform noise, tracking, and
# the placeholder addresses theme templates ship with.
EMAIL_JUNK = (
    "example.com", "example.org", "yourdomain", "domain.com", "email.com",
    "sentry.io", "wixpress.com", "godaddy.com", "squarespace.com",
    "wordpress.com", "shopify.com", "cloudflare", "schema.org", "w3.org",
    "sentry-next", "@2x", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    "@sentry", "no-reply", "noreply", "donotreply",
)
# Aggregators and lead-gen networks. A generic "<service><city>.com" shell is
# not a contractor -- it is a form that resells the lead.
DOMAIN_JUNK = (
    "yelp.", "angi.", "bbb.org", "houzz.", "thumbtack.", "homeadvisor.",
    "yellowpages.", "porch.com", "buildzoom.", "procore.", "homeguide.",
    "downtobid.", "planhub.", "craigslist.", "facebook.", "instagram.",
    "linkedin.", "indeed.", "ziprecruiter.",
)


def _get(url, timeout=12, limit=250000):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype and "text" not in ctype:
                return None
            return resp.read(limit).decode("utf-8", "ignore")
    except Exception:
        return None


def _robots_ok(base):
    """Default to allowed when robots.txt is missing or unreadable -- that is
    what a missing robots.txt means. Only an explicit Disallow blocks us.

    We fetch it ourselves rather than calling RobotFileParser.read(), which
    sends urllib's default Python-urllib/3.x agent. Small business sites sit
    behind the same bot filters that broke DuckDuckGo and Resend for this
    codebase: they answer that agent with a captcha or challenge page, and
    RobotFileParser then parses the HTML as if it were rules. That produced a
    21-of-43 "disallowed" rate on sites whose robots.txt actually says
    `Disallow:` with an empty value -- which means allow everything.
    """
    text = _get(urllib.parse.urljoin(base, "/robots.txt"), timeout=10,
                limit=100000)
    if not text or "<html" in text[:400].lower():
        return True  # missing, or a challenge page -- not a directive
    rp = urllib.robotparser.RobotFileParser()
    try:
        rp.parse(text.splitlines())
        return rp.can_fetch(UA, base)
    except Exception:
        return True


def _title(page):
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", page or "")
    if not m:
        return ""
    t = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    # Titles are usually "Name | Concrete Contractor in Springfield MO".
    return re.split(r"\s*[|–—]\s*", t)[0].strip()[:120]


def _emails(page, host):
    out = []
    for e in EMAIL_RE.findall(page or ""):
        low = e.lower()
        if any(j in low for j in EMAIL_JUNK):
            continue
        if len(low) > 80:
            continue
        out.append(low)
    # An address on the business's own domain is far more likely to be theirs
    # than a gmail scraped out of a testimonial or a web-designer credit.
    root = host.lower().replace("www.", "")
    own = [e for e in out if root.split(".")[0] in e.split("@")[-1]]
    ordered = own + [e for e in out if e not in own]
    seen, uniq = set(), []
    for e in ordered:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    return uniq


def _phone(page):
    m = PHONE_RE.search(re.sub(r"(?is)<(script|style).*?</\1>", "", page or ""))
    return f"({m.group(1)}) {m.group(2)}-{m.group(3)}" if m else ""


def probe(seed):
    state, city, url = seed
    host = urllib.parse.urlparse(url).netloc
    if any(j in host.lower() for j in DOMAIN_JUNK):
        return dict(state=state, city=city, website=url, status="aggregator",
                    company="", email="", extra_emails="", phone="")
    base = f"{urllib.parse.urlparse(url).scheme}://{host}"
    if not _robots_ok(base):
        return dict(state=state, city=city, website=url,
                    status="robots_disallow", company="", email="",
                    extra_emails="", phone="")

    company, phone, found = "", "", []
    for path in CONTACT_PATHS:
        page = _get(base + path)
        if not page:
            continue
        if not company:
            company = _title(page)
        if not phone:
            phone = _phone(page)
        found.extend(_emails(page, host))
        if found and phone:
            break

    seen, uniq = set(), []
    for e in found:
        if e not in seen:
            seen.add(e)
            uniq.append(e)

    return dict(state=state, city=city, website=base,
                status="ok" if uniq else ("no_email" if company else "unreachable"),
                company=company, email=uniq[0] if uniq else "",
                extra_emails=";".join(uniq[1:4]), phone=phone)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("seeds")
    ap.add_argument("--out", default="data/prospects.csv")
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    seeds = []
    for line in open(args.seeds):
        parts = line.strip().split("|")
        if len(parts) == 3 and parts[2].startswith("http"):
            seeds.append(tuple(parts))
    print(f"probing {len(seeds)} sites", flush=True)

    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for r in ex.map(probe, seeds):
            rows.append(r)

    fields = ["company", "city", "state", "email", "phone", "website",
              "extra_emails", "status"]
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    tally = {}
    for r in rows:
        tally[r["status"]] = tally.get(r["status"], 0) + 1
    print(f"\n{args.out}")
    for k, v in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {k:18s} {v}")


if __name__ == "__main__":
    main()
