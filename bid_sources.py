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
# Ordered most-likely first and it matters: the live scan path only probes
# the first couple (candidate_bid_urls(..., limit=2)) to protect the request
# budget, while the offline crawl walks the whole list.
#
# The original five found a bid page for 24.8% of the .gov registry. The
# homepage-link fallback catches sites that link to bids from the front page
# in obvious words -- but a county that files procurement three levels down
# under Departments links nothing obvious from its homepage and matched none
# of five guesses, which is why counties came out so badly (Arkansas: 1 of
# 89; Oklahoma: 2 of 64).
CANDIDATE_BID_PATHS = (
    "/Bids.aspx",          # CivicPlus — by far the most common
    "/bids",
    "/bids-and-rfps",
    "/purchasing",
    "/rfp",
    # ── added after measuring the first crawl's misses ──
    "/rfps",
    "/procurement",
    "/solicitations",
    "/bid-opportunities",
    "/bidopportunities",
    "/current-bids",
    "/open-bids",
    "/invitation-to-bid",
    "/notice-to-bidders",
    "/doing-business/bids",
    "/business/bids",
    "/departments/purchasing",
    "/departments/finance/purchasing",
    "/government/purchasing",
    "/purchasing/bids",
    "/finance/purchasing",
    "/RFP.aspx",
    "/Purchasing.aspx",
    "/bids.html",
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


# When none of CANDIDATE_BID_PATHS hit, the next best guess is an actual
# bid-shaped link off the homepage a real visitor would click — every
# platform's bid page lives at a different path, but nearly all of them
# link to it from the front page with obvious wording. This is what lets a
# city with no pre-crawled entry (tools/discover_bid_portals.py can't reach
# literally everywhere) and no CivicPlus-shaped URL still get a real shot at
# its own bid page during a live scan, instead of falling straight to a
# generic web search that has no idea which of its results are actually
# nearby -- a search-engine result being ABOUT a real, geocodable city is not
# the same as it being about the city that was searched for.
_HOMEPAGE_LINK_RE = re.compile(r'<a[^>]+href="([^"#][^"]*)"[^>]*>(.*?)</a>', re.I | re.S)
_HOMEPAGE_LINK_HINTS = ("bid", "rfp", "rfq", "solicitation", "procurement",
                        "purchasing", "vendor")
_HOMEPAGE_LINK_NOISE = ("facebook.com", "twitter.com", "x.com", "instagram.com",
                        "youtube.com", "linkedin.com", "mailto:", "tel:", "javascript:")


def _slug_text(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def link_for_title(html, base_url, title, min_len=12):
    """The posting link whose anchor text matches this bid's title.

    _fetch_text strips every tag before the page reaches the extraction
    model, so on a non-CivicPlus portal the model never sees an href and
    every bid ends up pointed at the listing page -- not a 404, but it drops
    the contractor on a list to hunt through, and it leaves the enricher
    nothing per-posting to read.

    Matching is deliberately strict: one side's slug must CONTAIN the
    other's, and short titles are refused outright. A wrong link is worse
    than the listing page, so anything ambiguous returns "".
    """
    want = _slug_text(title)
    if not html or not base_url or len(want) < min_len:
        return ""
    best, best_len = "", 0
    for m in _HOMEPAGE_LINK_RE.finditer(html):
        href, label = m.group(1), _slug_text(_clean(m.group(2)))
        if not label or any(n in href.lower() for n in _HOMEPAGE_LINK_NOISE):
            continue
        if want in label or (len(label) >= min_len and label in want):
            # Prefer the tightest match: a nav link whose text happens to
            # contain a short title should lose to the posting itself.
            if not best or len(label) < best_len:
                best, best_len = href, len(label)
    return urllib.parse.urljoin(base_url, _unescape(best)) if best else ""


def extract_bid_link_candidates(html, base_url, max_candidates=3):
    """Links off a homepage whose href or label suggest a bid page, best
    first. Pure text-in/URLs-out, same discipline as the rest of this file --
    the caller fetches the homepage and any candidate returned here."""
    if not html or not base_url:
        return []
    base = base_url.rstrip("/")
    seen, scored = set(), []
    for m in _HOMEPAGE_LINK_RE.finditer(html):
        href, label = m.group(1), _clean(m.group(2))
        blob = (href + " " + label).lower()
        if any(n in blob for n in _HOMEPAGE_LINK_NOISE):
            continue
        hits = sum(1 for term in _HOMEPAGE_LINK_HINTS if term in blob)
        if not hits:
            continue
        # Same reasoning as parse_civicplus_html: let urljoin resolve it
        # against the page it was found on rather than gluing strings.
        url = urllib.parse.urljoin(base_url, _unescape(href))
        if url in seen:
            continue
        seen.add(url)
        scored.append((hits, url))
    scored.sort(key=lambda t: -t[0])
    return [url for _, url in scored[:max_candidates]]


# ── Relevance prefilter ─────────────────────────────────────────────────────
# Deliberately generous. This runs BEFORE any AI call, so its job is only to
# throw out listings that obviously have nothing to do with the trade —
# janitorial, insurance, software. Anything that might involve concrete gets
# through and is judged properly later. A false negative here is a lost bid; a
# false positive costs a fraction of a cent.
# "ada" is deliberately NOT here: as a bare substring it matches Nevada,
# Canada, Adams and Palisades. _has_strong_term matches it with a word
# boundary instead, which is the only way it means the Act.
NICHE_TERMS = (
    "sidewalk", "side walk", "curb", "gutter", "concrete", "flatwork",
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


# The terms that name the trade itself. NICHE_TERMS is deliberately loose --
# it is a cheap gate before an AI call -- but on the known-portal path there is
# no AI call afterwards, so a weak contextual match is the ONLY thing standing
# between a listing and the customer's feed. "Specialized Legal Services for a
# Potential Large-Scale Digital Infrastructure Project" reached one, on
# "infrastructure".
STRONG_NICHE_TERMS = (
    "sidewalk", "side walk", "curb", "gutter", "concrete", "flatwork",
    "paving", "pavement", "crosswalk", "driveway", "apron", "slab",
    "walkway", "curb ramp", "ada ramp",
)

# "ada" as a bare substring matches Nevada, Canada, Adams and Palisades. It
# only means the Act when it stands alone.
_ADA_RE = re.compile(r"\bada\b", re.I)


def _has_strong_term(blob):
    return _ADA_RE.search(blob) is not None or \
        any(t in blob for t in STRONG_NICHE_TERMS)


def looks_relevant(*texts):
    """True if a listing is worth spending an extraction call on."""
    blob = " ".join(str(t or "") for t in texts).lower()
    if not blob.strip():
        return False
    strong = _has_strong_term(blob)
    # A listing that names an unrelated trade needs a real trade word to
    # survive: "sidewalk replacement and landscaping" is our work, "legal
    # services for an infrastructure project" is not.
    if not strong and any(t in blob for t in CLEARLY_UNRELATED):
        return False
    return strong or any(term in blob for term in NICHE_TERMS)


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
# CivicPlus lays the row out as LABELS then VALUES -- "Status: Closes: Closed
# 3/11/2025 4:00 PM" -- so the status word does not follow "Status:" directly.
# Requiring that it did meant every CivicPlus row parsed as status "", which
# _place_bid then defaults to Open: an awarded job shown as live work.
_STATUS_RE = re.compile(
    r"\bstatus\b\s*[:\-]?\s*(?:clos(?:e|es|ing)\s*(?:date)?\s*[:\-]?\s*)?"
    r"(open|closed|awarded|cancell?ed|withdrawn|pending|expired)\b", re.I)


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


_READ_ON_RE = re.compile(r"^\s*read\s*on\b", re.I)

# A CivicPlus bid listing renders each posting as a link to Bids.aspx?bidID=N.
_BID_LINK_RE = re.compile(
    r'<a[^>]+href="([^"]*Bids\.aspx\?bidID=\d+[^"]*)"[^>]*>(.*?)</a>',
    re.I | re.S)


# Most municipal bid pages are empty most of the time -- 21 of 30 sampled
# portals had nothing posted. An empty listing is not a parser failure, and
# treating it as one sent the whole page to the AI extractor to discover the
# same nothing, at real cost and, worse, real latency inside the scan's time
# budget. These two strings appear on essentially every CivicPlus bid page
# (20/21 and 19/21 of the empty sample); either is enough to say "this is the
# right page, there is simply nothing on it".
_CIVICPLUS_PAGE_MARKERS = ("bids.aspx", "bid postings")


def civicplus_page_is_empty(html):
    """True when this is recognisably a CivicPlus bid page with no postings.

    Deliberately requires a positive marker rather than inferring emptiness
    from the absence of bid links: a fetch that returned an error page, a
    login wall or a redirect also has no bid links, and those DO deserve the
    AI fallback.
    """
    text = str(html or "")
    if not text or _BID_LINK_RE.search(text):
        return False
    low = text.lower()
    return any(m in low for m in _CIVICPLUS_PAGE_MARKERS)


def parse_civicplus_html(html, base_url=""):
    """Rows from a CivicPlus Bids.aspx listing page.

    The listing is the authoritative set of what is currently open; the RSS
    feed only reaches back a fortnight.
    """
    rows, seen = [], set()
    text = str(html or "")
    # CivicPlus emits TWO links per posting: the title, then a "Read on:
    # <title>" link. The second is the same bid -- taking it produced a
    # duplicate row titled after the link text -- but the posting's Status and
    # Closes values sit AFTER it, so it cannot simply be skipped over: it marks
    # the middle of the posting, not the end.
    all_matches = list(_BID_LINK_RE.finditer(text))
    posts = [m for m in all_matches
             if not _READ_ON_RE.match(_clean(_unescape(m.group(2))))]
    for i, m in enumerate(posts):
        href, label = m.group(1), _clean(_unescape(m.group(2)))
        if not label or len(label) < 4:
            continue
        matches = posts
        # urljoin, not string concatenation. base_url is the LISTING page
        # ("https://x.gov/Bids.aspx"), not the site root, so appending to it
        # produced "https://x.gov/Bids.aspx/bids.aspx?bidID=415" -- a 404 for
        # every posting on every CivicPlus site. That single join is why bid
        # links 404'd, why contacts were missing and why half of all bids had
        # no deadline: the enricher that reads contact, deadline and scope off
        # a posting could never load one.
        url = urllib.parse.urljoin(base_url, _unescape(href)) if base_url \
            else _unescape(href)
        if url in seen:
            continue
        seen.add(url)
        # Closing dates and the posting status sit in the markup near the link
        # rather than inside it -- but AFTER the posting's summary text, which
        # can run to several hundred characters. A fixed 600-char window fell
        # short of them on any posting with a real description, so the row came
        # back with no status and no deadline. Read to the next posting's link
        # instead, which is exactly this posting's own markup and no more.
        stop = posts[i + 1].start() if i + 1 < len(posts) else len(text)
        window = _clean(_unescape(text[m.end():min(stop, m.end() + 4000)]))
        cm = _CLOSE_RE.search(window)
        rows.append({"title": label, "url": url, "scope": "",
                     "deadline": cm.group(1) if cm else "",
                     "status": _status_near(window),
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
# "Contact: Jane Doe", "Contact Person - Jane Doe", "Questions to Jane Doe"
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


def parse_contact(text):
    """Pull a name / email / phone out of a bid posting. Missing parts are "".

    Never returns a partially-parsed phone number: a match is either a full
    10-digit US number or nothing, because a half-number on a bid card is worse
    than a blank — the contractor dials it and loses the job to the wait.
    """
    blob = _clean(_unescape(text))
    if not blob:
        return {"contact": "", "email": "", "phone": ""}

    email = ""
    for candidate in _EMAIL_RE.findall(blob):
        low = candidate.lower()
        if any(junk in low for junk in _JUNK_EMAIL_PARTS):
            continue
        if low.endswith((".png", ".jpg", ".gif", ".css", ".js")):
            continue
        email = candidate
        break

    phone = ""
    pm = _PHONE_RE.search(blob)
    if pm:
        phone = f"({pm.group(1)}) {pm.group(2)}-{pm.group(3)}"

    contact = ""
    nm = _CONTACT_NAME_RE.search(blob)
    if nm:
        name = nm.group(1).strip()
        # "Contact The City Of" and friends are labels, not people.
        if not re.search(r"\b(the|city|county|department|office|clerk's|purchasing)\b",
                         name, re.I):
            contact = name

    return {"contact": contact, "email": email, "phone": phone}


_SCOPE_LABEL_RE = re.compile(
    r"(?:description|scope(?:\s+of\s+work)?|summary|project\s+description)\s*[:\-]\s*"
    r"(.{40,600}?)(?:\s*(?:contact|closing|publication|bid\s+opening|attachment)\b|$)",
    re.I | re.S)


def detail_deadline(html):
    """The closing date stated on a posting page, or "" if none is given.

    Worth as much as the contact details: a bid with no deadline gets no
    urgency ranking, and — more expensively — cannot be recognised as expired,
    so last year's listing shows as open indefinitely.
    """
    m = _CLOSE_RE.search(_clean(_unescape(html)))
    return m.group(1).strip() if m else ""


# Only a LABELLED figure counts. A bid page is full of dollar amounts that
# are not the job's value -- bid bonds, plan deposits, non-refundable fees,
# liquidated damages per day -- and presenting any of those as the project
# value would be worse than showing nothing, because a contractor would price
# against it. Measured on live postings: about 4% state a labelled estimate.
_VALUE_LABEL_RE = re.compile(
    r"(?:engineer'?s?\s+estimate|estimated\s+(?:cost|value|price|budget)|"
    r"project\s+estimate|opinion\s+of\s+probable\s+cost|"
    r"budget(?:ed)?\s+amount|estimated\s+project\s+cost|"
    r"estimated\s+construction\s+cost)"
    r"[^$\n]{0,60}?(\$\s?[\d,]{4,}(?:\.\d{2})?)", re.I)

# Amounts that sit near a value-ish word but are definitely not the job.
_NOT_A_VALUE_RE = re.compile(
    r"bid\s+bond|plan\s+deposit|non-?refundable|liquidated\s+damages|"
    r"per\s+day|filing\s+fee|application\s+fee", re.I)


def detail_value(html):
    """The project's stated value, or "" when the page doesn't give one."""
    text = _clean(_unescape(str(html or "")))
    if not text:
        return ""
    for m in _VALUE_LABEL_RE.finditer(text):
        window = text[max(0, m.start() - 60):m.end() + 40]
        if _NOT_A_VALUE_RE.search(window):
            continue
        return re.sub(r"\s+", "", m.group(1))
    return ""


# Fields a posting carries that a listing row never does. Measured on 25 live
# postings: publication date 92%, a linked packet 56%, bid number 40%, an
# addendum 36%, a pre-bid meeting 20%.
_PUBLISHED_RE = re.compile(
    rf"publication\s+date(?:/time)?\s*:?\s*({_DATE})", re.I)
_BID_NUMBER_RE = re.compile(
    r"bid\s*(?:number|no\.?|#)\s*:?\s*([A-Z0-9][A-Z0-9\-/]{2,24})", re.I)
_PREBID_RE = re.compile(r"pre-?bid\s+(?:meeting|conference)", re.I)
_MANDATORY_RE = re.compile(
    r"(mandatory|required|must\s+attend)[^.]{0,80}?pre-?bid|"
    r"pre-?bid[^.]{0,80}?(mandatory|is\s+required|must\s+attend)", re.I)
_ADDENDA_RE = re.compile(r"\baddend(?:um|a)\b", re.I)
# CivicPlus serves packets from /DocumentCenter, not as plain .pdf links --
# looking only for ".pdf" found none of them.
_DOC_LINK_RE = re.compile(
    r'href="([^"]*(?:DocumentCenter/View|ShowDocument|\.pdf|\.docx?|\.zip)[^"]*)"',
    re.I)


def detail_published(html):
    """The date the posting says it went up. "" if absent."""
    m = _PUBLISHED_RE.search(_clean(_unescape(str(html or ""))))
    return m.group(1).strip() if m else ""


def detail_bid_number(html):
    """The agency's own reference for this solicitation -- what a contractor
    reads out on the phone."""
    m = _BID_NUMBER_RE.search(_clean(_unescape(str(html or ""))))
    if not m:
        return ""
    num = m.group(1).strip(" -/")
    # "Bid Number: Bid" and similar label bleed.
    return "" if num.lower() in ("bid", "number", "no", "rfp", "rfq") else num


def detail_prebid(html):
    """"mandatory" / "yes" / "" for a pre-bid meeting.

    Worth surfacing out of proportion to how often it appears: a mandatory
    pre-bid meeting missed is not a late bid, it is an ineligible one.
    """
    text = _clean(_unescape(str(html or "")))
    if not _PREBID_RE.search(text):
        return ""
    return "mandatory" if _MANDATORY_RE.search(text) else "yes"


def detail_has_addenda(html):
    """True when the posting mentions an addendum. Same reasoning: bidding
    against a superseded scope is worse than not bidding."""
    return bool(_ADDENDA_RE.search(_clean(_unescape(str(html or "")))))


def detail_documents(html, base_url="", limit=5):
    """Links to the bid packet -- the drawings and specs, where the
    quantities a contractor prices from actually live."""
    out, seen = [], set()
    for href in _DOC_LINK_RE.findall(str(html or "")):
        url = urllib.parse.urljoin(base_url, _unescape(href)) if base_url \
            else _unescape(href)
        # A bare /DocumentCenter is the index, not a document.
        if url.rstrip("/").lower().endswith("documentcenter"):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= limit:
            break
    return out


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
