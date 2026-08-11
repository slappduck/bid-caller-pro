"""
bid_sources.py — direct, structured bid sources (no search engine involved)
═══════════════════════════════════════════════════════════════════════════

Why this module exists
──────────────────────
/scan's local search pays a search API to *guess* which pages might hold bids,
then pays OpenAI to read whatever comes back. That is expensive per query,
capped by whatever budget is left, dependent on that day's search rankings, and
it fails silently: when the search backend is rate-limited the scan still
returns 200 with almost nothing in it.

But public bids do not live in arbitrary corners of the web. They live in a
small number of procurement platforms with predictable, stable URLs. Springfield
MO is a CivicPlus site: every one of its open solicitations is on
springfieldmo.gov/Bids.aspx, and has been the whole time, for free.

So the model here is the opposite way round:

    search engine  →  used ONCE to work out which platform a town uses
    this module    →  hits that platform directly, every scan, forever

`bid_portals.py` already stores learned URLs per city; this module is what
knows how to *read* them, and how to recognise a platform from its URL.

Design rules
────────────
* Every parser takes text and returns rows. No network calls inside a parser,
  so all of them are testable against saved fixtures with no live site.
* Parsers never raise on malformed input — a site redesign degrades that one
  source to zero rows, it does not break a scan.
* Nothing here calls OpenAI. Listings arrive already structured (title, link,
  closing date), which is most of a bid record. AI extraction stays for the
  detail pages that genuinely need prose read, and `looks_relevant()` exists so
  obviously-unrelated listings ("Janitorial Services") are dropped before
  anything is spent on them.
"""

import re
import urllib.parse
import xml.etree.ElementTree as ET

# ── Platform recognition ────────────────────────────────────────────────────
# Matched against a URL's host (and path, for the hosted-subdomain shapes).
PLATFORM_SIGNATURES = (
    ("civicplus", re.compile(r"civicplus\.com$|/bids\.aspx", re.I)),
    ("demandstar", re.compile(r"demandstar\.com$", re.I)),
    ("planetbids", re.compile(r"planetbids\.com$", re.I)),
    ("bonfire", re.compile(r"bonfirehub\.com$", re.I)),
    ("opengov", re.compile(r"opengov\.com$|procurement\.opengov\.com", re.I)),
    ("bidnetdirect", re.compile(r"bidnetdirect\.com$", re.I)),
    ("questcdn", re.compile(r"questcdn\.com$", re.I)),
    ("publicpurchase", re.compile(r"publicpurchase\.com$", re.I)),
    ("bidexpress", re.compile(r"bidexpress\.com$", re.I)),
)


def identify_platform(url):
    """Name the procurement platform behind a URL, or "" if unrecognised.

    Recognising the platform is what lets a one-off discovery become a
    permanent, free source: once we know a town is on CivicPlus we can build
    its bid URLs ourselves instead of searching for them again.
    """
    try:
        parsed = urllib.parse.urlparse(url if "//" in str(url) else "//" + str(url))
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    for name, pattern in PLATFORM_SIGNATURES:
        if pattern.search(host) or pattern.search(path):
            return name
    # A .gov/.us host with a bids-shaped path is a real agency page even when
    # the platform underneath isn't one we recognise — worth keeping as a
    # source, just without a specialised reader.
    if re.search(r"\.(gov|mil)$|\.[a-z]{2}\.us$", host) and \
       re.search(r"bid|solicitation|procure|purchasing|rfp", path):
        return "agency"
    return ""


def civicplus_endpoints(domain):
    """The URLs worth reading on a CivicPlus site, best first.

    Bids.aspx carries every currently-open solicitation. The RSS feed carries
    only about two weeks, which is shorter than a typical bid stays open, so it
    is a freshness signal rather than a replacement — both are read.
    """
    host = str(domain or "").strip().strip("/")
    if not host:
        return []
    if "//" not in host:
        host = "https://" + host
    base = host.rstrip("/")
    return [
        f"{base}/Bids.aspx",
        f"{base}/Bids.aspx?CatID=&txtSort=Date&showAllBids=on&Status=",
        f"{base}/rss.aspx",
    ]


# Given only a hostname, these are the paths a municipal bid page actually
# takes, most likely first. Deliberately short: each one is a live fetch, so
# this is a probe, not a crawl. A hit gets recorded in the portal directory by
# the caller and is free on every later scan.
CANDIDATE_BID_PATHS = (
    "/Bids.aspx",          # CivicPlus — by far the most common
    "/bids",
    "/bids-and-rfps",
    "/purchasing",
    "/rfp",
)


def candidate_bid_urls(domain, limit=None):
    """Bid-page URLs worth probing for a bare government hostname."""
    host = str(domain or "").strip().strip("/").lower()
    if not host:
        return []
    if "//" not in host:
        host = "https://" + host
    base = host.rstrip("/")
    paths = CANDIDATE_BID_PATHS if limit is None else CANDIDATE_BID_PATHS[:limit]
    return [base + p for p in paths]


# ── Relevance prefilter ─────────────────────────────────────────────────────
# Deliberately generous. This runs BEFORE any AI call, so its job is only to
# throw out listings that obviously have nothing to do with the trade —
# janitorial, insurance, software. Anything that might involve concrete gets
# through and is judged properly later. A false negative here is a lost bid; a
# false positive costs a fraction of a cent.
NICHE_TERMS = (
    "sidewalk", "side walk", "ada", "curb", "gutter", "concrete", "flatwork",
    "paving", "pavement", "ramp", "driveway", "apron", "trail", "greenway",
    "street improvement", "streetscape", "roadway", "road improvement",
    "intersection", "safe routes", "pedestrian", "walkway", "crosswalk",
    "parking lot", "slab", "curb ramp", "transition plan", "cdbg",
    "public works", "infrastructure", "reconstruction", "resurfacing",
)
# Only used to explain a skip in the logs; never the sole reason to drop.
CLEARLY_UNRELATED = (
    "janitorial", "insurance", "audit", "software", "banking", "uniform",
    "food service", "landscap", "mowing", "tree trimming", "fuel", "towing",
    "copier", "printing services", "legal services", "staffing", "security guard",
)


def looks_relevant(*texts):
    """True if a listing is worth spending an extraction call on."""
    blob = " ".join(str(t or "") for t in texts).lower()
    if not blob.strip():
        return False
    return any(term in blob for term in NICHE_TERMS)


def rejection_reason(*texts):
    """Why a listing was skipped — for the scan funnel, not for logic."""
    blob = " ".join(str(t or "") for t in texts).lower()
    for term in CLEARLY_UNRELATED:
        if term in blob:
            return f"unrelated:{term}"
    return "no_niche_keyword"


# ── Parsers (pure text in, rows out) ────────────────────────────────────────
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(text):
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", str(text or ""))).strip()


def _unescape(text):
    out = str(text or "")
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")):
        out = out.replace(a, b)
    return out


_MONTHS = ("January|February|March|April|May|June|July|August|September|October|"
           "November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec")
_DATE = (rf"(?:{_MONTHS})\.?\s+\d{{1,2}},?\s+\d{{4}}"
         r"|\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}")

# A closing date is almost never written as "Closes: <date>". CivicPlus labels
# its columns "Closing Date/Time", "Bid Opening Date/Time", "Due Date and
# Time" — so the keyword and the date are separated by a few words of label.
# Requiring them to be adjacent (which the first version of this did) meant
# every bid read off a real CivicPlus listing came back with NO deadline. That
# costs twice: the bid loses its whole urgency score, and an expired listing
# can't be recognised as expired, so it shows as open.
#
# The gap is capped and may not contain a digit, so the pattern can never skip
# over one label to grab a neighbouring column's date.
_CLOSE_RE = re.compile(
    r"(?:bid\s+opening|bids?\s+due|clos(?:e|es|ed|ing)|due|deadline|"
    r"submittals?|responses?\s+due|proposals?\s+due|open\s+until|"
    r"accepted\s+until|received\s+until)"
    r"[^\d<>]{0,20}?"
    rf"({_DATE})",
    re.I)

# CivicPlus prints the posting's own status on the listing row. It is the
# authoritative answer to "is this still live?" and it is free — far better
# than inferring it from a date we may have failed to parse.
_STATUS_RE = re.compile(
    r"\bstatus\b\s*[:\-]?\s*"
    r"(open|closed|awarded|cancell?ed|withdrawn|pending|expired)\b", re.I)

# A real Bids.aspx row (confirmed against springfieldmo.gov, Aug 2026) doesn't
# put the value right after its own label — it lists both labels first,
# THEN both values: "Status: Closes: <status> <date>". _STATUS_RE never
# matches that shape because "Status:" is followed by "Closes:", not a status
# word. This is the authoritative field for both status and deadline, so it
# is tried before any free-text guess.
_STRUCTURED_STATUS_RE = re.compile(
    r"status\s*:?\s*(?:closes|bid\s+opening|due|deadline)\s*:?\s*"
    r"(open|closed|awarded|cancell?ed|withdrawn|pending|expired)\b"
    r"[^\d<>]{0,20}?"
    rf"({_DATE})", re.I)

# Free text on a posting ("the revised due date is...") uses the word "due"
# constantly and is not reliably the current deadline — an addendum notice
# about a past change can still say "due date" in prose. "Closing" and "bid
# opening" are only ever used as the actual field label, never as filler, so
# a match on those keywords is trusted over one found via the full, looser
# _CLOSE_RE.
_CLOSE_RE_STRICT = re.compile(
    r"(?:bid\s+opening|clos(?:e|es|ed|ing))"
    r"[^\d<>]{0,20}?"
    rf"({_DATE})", re.I)


def _status_near(text):
    """The posting status stated in a listing row, normalised. "" if absent."""
    m = _STATUS_RE.search(str(text or ""))
    return m.group(1).strip().capitalize() if m else ""


def parse_civicplus_rss(xml_text):
    """Rows from a CivicPlus module RSS feed.

    Returns [] rather than raising on anything malformed: a feed that changes
    shape should cost this one source, not the scan.
    """
    rows = []
    try:
        root = ET.fromstring(str(xml_text or "").strip())
    except ET.ParseError:
        return rows
    for item in root.iter():
        if not item.tag.lower().endswith("item"):
            continue
        get = lambda n: next(  # noqa: E731 - local shorthand, kept tight
            (c.text for c in item if c.tag.lower().endswith(n) and c.text), "")
        title = _clean(_unescape(get("title")))
        link = (get("link") or "").strip()
        desc = _clean(_unescape(get("description")))
        if not title:
            continue
        closes = ""
        m = _CLOSE_RE.search(desc) or _CLOSE_RE.search(title)
        if m:
            closes = m.group(1)
        rows.append({"title": title, "url": link, "scope": desc,
                     "deadline": closes, "status": _status_near(desc),
                     "source": "civicplus-rss"})
    return rows


# A CivicPlus bid listing renders each posting as a link to Bids.aspx?bidID=N.
_BID_LINK_RE = re.compile(
    r'<a[^>]+href="([^"]*Bids\.aspx\?bidID=\d+[^"]*)"[^>]*>(.*?)</a>',
    re.I | re.S)

# A real row's own teaser text (bid number, a truncated description, a "Read
# on" link that repeats the same href) routinely runs past 600 raw characters
# before the status/closing block starts — confirmed against a live fetch,
# where a plain 600-char window missed the deadline entirely on 2 of 3 open
# bids. This is the ceiling used when no row boundary is found first.
_ROW_WINDOW_CAP = 2000


def parse_civicplus_html(html, base_url=""):
    """Rows from a CivicPlus Bids.aspx listing page.

    The listing is the authoritative set of what is currently open; the RSS
    feed only reaches back a fortnight.
    """
    rows, seen = [], set()
    text = str(html or "")
    lower_text = text.lower()
    for m in _BID_LINK_RE.finditer(text):
        href, label = m.group(1), _clean(_unescape(m.group(2)))
        if not label or len(label) < 4:
            continue
        url = _unescape(href)
        if url.startswith("/") and base_url:
            url = base_url.rstrip("/") + url
        elif not url.lower().startswith("http") and base_url:
            url = base_url.rstrip("/") + "/" + url.lstrip("/")
        if url in seen:
            continue
        seen.add(url)
        # Closing dates and the posting status sit in the markup near the
        # link rather than inside it. The window is capped at the next row so
        # a bid with no stated status or deadline can never inherit its
        # neighbour's — a wide-open cap alone isn't safe for that.
        boundary = lower_text.find("listitemsrow", m.end())
        end = boundary if boundary != -1 else len(text)
        end = min(end, m.end() + _ROW_WINDOW_CAP)
        window = _clean(_unescape(text[m.end():end]))
        sm = _STRUCTURED_STATUS_RE.search(window)
        if sm:
            status, deadline = sm.group(1).strip().capitalize(), sm.group(2)
        else:
            cm = _CLOSE_RE_STRICT.search(window) or _CLOSE_RE.search(window)
            deadline = cm.group(1) if cm else ""
            status = _status_near(window)
        rows.append({"title": label, "url": url, "scope": "",
                     "deadline": deadline, "status": status,
                     "source": "civicplus"})
    return rows


# ── Contact details ─────────────────────────────────────────────────────────
# A bid with nobody to call is barely a lead. The listing page never carries
# this — it lives on the individual posting — so these run over a fetched
# detail page. Plain regex on purpose: no AI call, no cost, and contact blocks
# on procurement pages are formulaic.

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?<![\d\-])(?:\+?1[\s.\-]*)?\(?([2-9]\d{2})\)?[\s.\-]*(\d{3})[\s.\-]*(\d{4})(?![\d\-])")

# Cloudflare's email-obfuscation plugin — extremely common on .gov sites that
# sit behind Cloudflare (confirmed live on springfieldmo.gov) — replaces a
# mailto entirely with this attribute and a "[email protected]" placeholder as
# the visible text. _EMAIL_RE finds nothing on a page like that even though
# the buyer's real address is sitting right there, XOR'd against its own
# first byte.
_CF_EMAIL_RE = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')


def _decode_cfemail(hexstr):
    try:
        data = bytes.fromhex(hexstr)
    except ValueError:
        return ""
    if len(data) < 2:
        return ""
    key = data[0]
    decoded = bytes(b ^ key for b in data[1:])
    try:
        out = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    return out if "@" in out else ""


# The strict label is tried first: "Contact Person:", "Contact Name:",
# "Attention:", "Attn:" are only ever used as an actual field label. Confirmed
# live: a posting's prose routinely reads "please contact Euna OpenBids... at
# (866) 273-1863" well above the real "Contact Person:" field further down —
# bare "contact"/"questions to"/"submit(ted) to", matched with .search() in
# document order, would grab that vendor mention instead of the buyer's own
# name. Those looser phrasings are kept as a fallback for pages that never
# use a formal label at all.
_CONTACT_NAME_STRICT_RE = re.compile(
    r"(?i:contact\s+person|contact\s+name|attention|attn)\s*[:\-]?\s*"
    r"((?:[A-Z][A-Za-z.'\-]+\s+){1,2}[A-Z][A-Za-z.'\-]+)(?![A-Za-z.'\-])(?!\s*:)")
# "Contact: Jane Doe", "Direct questions to Jane Doe", "Submitted to Jane Doe"
# The label is matched case-insensitively; the name deliberately is NOT, so the
# capital letters still have to be there. Without that, "contact us for details"
# reads as a person called "Us For".
_CONTACT_NAME_RE = re.compile(
    r"(?i:contact(?:\s+person|\s+name)?|direct\s+questions\s+to|questions\s+to|"
    r"submit(?:ted)?\s+to|attention|attn)\s*[:\-]?\s*"
    # The trailing lookahead stops the name from swallowing the next field's
    # label: "Contact: Marla Whitfield Email: ..." is a person and a label, not
    # a three-word name.
    # The first lookahead forces the last word to be whole — without it the
    # regex just gives back letters ("...Emai") to satisfy the second. The
    # second stops the name swallowing the next field's label: "Contact: Marla
    # Whitfield Email: ..." is a person and a label, not a three-word name.
    r"((?:[A-Z][A-Za-z.'\-]+\s+){1,2}[A-Z][A-Za-z.'\-]+)(?![A-Za-z.'\-])(?!\s*:)")

# Addresses that belong to the website, not to a person who answers questions.
_JUNK_EMAIL_PARTS = ("webmaster", "postmaster", "no-reply", "noreply", "donotreply",
                     "example.com", "sentry.io", "@2x", "civicplus.com")

# How far past a found "Contact Person:"-style label to look for a phone
# number before falling back to the first one anywhere on the page. Keeps a
# nearby number ("Contact Person: Jane Doe ... Phone: 555-1234") from losing
# to an unrelated one mentioned earlier in the page's prose.
_PHONE_NEAR_LABEL = 400


def _valid_name(name):
    # "Contact The City Of" and friends are labels, not people.
    return not re.search(r"\b(the|city|county|department|office|clerk's|purchasing)\b",
                         name, re.I)


def parse_contact(text):
    """Pull a name / email / phone out of a bid posting. Missing parts are "".

    Never returns a partially-parsed phone number: a match is either a full
    10-digit US number or nothing, because a half-number on a bid card is worse
    than a blank — the contractor dials it and loses the job to the wait.
    """
    raw = str(text or "")
    blob = _clean(_unescape(raw))
    if not blob:
        return {"contact": "", "email": "", "phone": ""}

    email = ""
    for hexstr in _CF_EMAIL_RE.findall(raw):
        candidate = _decode_cfemail(hexstr)
        low = candidate.lower()
        if candidate and not any(junk in low for junk in _JUNK_EMAIL_PARTS):
            email = candidate
            break
    if not email:
        for candidate in _EMAIL_RE.findall(blob):
            low = candidate.lower()
            if any(junk in low for junk in _JUNK_EMAIL_PARTS):
                continue
            if low.endswith((".png", ".jpg", ".gif", ".css", ".js")):
                continue
            email = candidate
            break

    contact = ""
    label_end = None
    nm = _CONTACT_NAME_STRICT_RE.search(blob) or _CONTACT_NAME_RE.search(blob)
    if nm:
        name = nm.group(1).strip()
        if _valid_name(name):
            contact = name
            label_end = nm.end()

    phone = ""
    if label_end is not None:
        pm = _PHONE_RE.search(blob, label_end, label_end + _PHONE_NEAR_LABEL)
        if pm:
            phone = f"({pm.group(1)}) {pm.group(2)}-{pm.group(3)}"
    if not phone:
        pm = _PHONE_RE.search(blob)
        if pm:
            phone = f"({pm.group(1)}) {pm.group(2)}-{pm.group(3)}"

    return {"contact": contact, "email": email, "phone": phone}


# Confirmed live: a real posting's "Description:" field routinely runs several
# thousand characters of legal boilerplate before the next labelled field
# appears — a 600-char cap meant the capture group could never reach a stop
# word and the whole match failed, returning "" on every real description this
# was tested against.
_SCOPE_LABEL_RE = re.compile(
    r"(?:description|scope(?:\s+of\s+work)?|summary|project\s+description)\s*[:\-]\s*"
    r"(.{40,4000}?)(?:\s*(?:contact|closing|publication|bid\s+opening|attachment)\b|$)",
    re.I | re.S)


def detail_deadline(html):
    """The closing date stated on a posting page, or "" if none is given.

    Worth as much as the contact details: a bid with no deadline gets no
    urgency ranking, and — more expensively — cannot be recognised as expired,
    so last year's listing shows as open indefinitely.

    A real posting page (confirmed live) routinely mentions "due date" in
    prose above the actual "Closing Date/Time:" field — an addendum notice
    reading "the revised due date is August 20" that _CLOSE_RE, searching in
    document order, would return instead of the labelled field further down.
    The strict pattern is tried first because "closing" and "bid opening" are
    never used as filler the way "due" is.
    """
    blob = _clean(_unescape(html))
    m = _CLOSE_RE_STRICT.search(blob) or _CLOSE_RE.search(blob)
    return m.group(1).strip() if m else ""


def detail_scope(html):
    """The project description from a posting page, or "" if none is labelled."""
    blob = _clean(_unescape(html))
    m = _SCOPE_LABEL_RE.search(blob)
    return m.group(1).strip() if m else ""


_RSS_LINK_RE = re.compile(
    r'href="([^"]*(?:RSSFeed\.aspx|rss\.aspx)[^"]*)"[^>]*>([^<]*)', re.I)


def find_bid_feed(rss_index_html, base_url=""):
    """Pick the Bids module's feed out of a CivicPlus /rss.aspx index."""
    best = ""
    for m in _RSS_LINK_RE.finditer(str(rss_index_html or "")):
        href, label = _unescape(m.group(1)), _clean(m.group(2)).lower()
        blob = (href + " " + label).lower()
        if "bid" in blob:
            if href.startswith("/") and base_url:
                href = base_url.rstrip("/") + href
            return href
    return best
