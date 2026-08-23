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

import html
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


# Third-party procurement platforms a city migrates its bids TO. Sampling the
# directory found a recurring shape: the CivicPlus Bids module is left in
# place as a signpost -- "View Open Solicitations" -- and every actual
# solicitation now lives on one of these. The old page parses to zero rows and
# is not empty, so it was counted as a parse miss and handed to the AI, which
# read a page with no bids on it. 25 of ~180 portal reads in the benchmark
# landed here.
#
# beaconbid.com is on the list because two sampled cities had moved there and
# nothing in the codebase knew the platform existed.
HOSTED_PORTAL_DOMAINS = (
    "beaconbid.com", "procurement.opengov.com", "opengov.com",
    "bidnetdirect.com", "bonfirehub.com", "planetbids.com", "questcdn.com",
    "demandstar.com", "publicpurchase.com", "bidexpress.com",
    "vendorregistry.com", "ionwave.net", "bidsandtenders.com",
    "periscopeholdings.com", "bidbuy.illinois.gov",
)
_HOSTED_HREF_RE = re.compile(r'<a[^>]+href="(https?://[^"#]+)"[^>]*>(.{0,120}?)</a>',
                             re.I | re.S)

# These platforms put a sign-up page right next to the bid list, and the
# sign-up link usually comes first in the markup. Chicopee's page offers
# "Register for Alerts" (.../register) above "View Open Solicitations"
# (.../open); taking the first hosted link on the page learned the
# registration form as the city's bid portal.
_NOT_A_LISTING_RE = re.compile(
    r"\b(?:register|registration|signup|sign-up|sign\s*up|login|log-?in|"
    r"account|subscribe|alerts?|notif\w*|help|support|faq|terms|privacy|"
    r"contact|about|training|tutorial)\b", re.I)
_IS_A_LISTING_RE = re.compile(
    r"\b(?:open|current|active|solicitations?|bids?|opportunit\w*|"
    r"proposals?|rfps?|projects?|portal|browse|view)\b", re.I)


_CANONICAL_RE = re.compile(
    r'<(?:link[^>]+rel=["\']canonical["\'][^>]+href|'
    r'meta[^>]+(?:property|name)=["\']og:url["\'][^>]+content)'
    r'=["\']([^"\']+)["\']', re.I)


def _is_hosted_agency_url(url):
    """A hosted-platform URL that belongs to ONE agency, not a search page."""
    try:
        parts = urllib.parse.urlparse(str(url or ""))
    except ValueError:
        return False
    host = parts.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    if not any(host == d or host.endswith("." + d)
               for d in HOSTED_PORTAL_DOMAINS):
        return False
    if parts.query:
        return False
    return len([s for s in parts.path.split("/") if s]) >= 2


def hosted_portal_link(html, base_url=""):
    """A link off this page to the city's OWN bid list on a hosted
    procurement platform. "" if there is none.

    Only city-scoped URLs qualify. "bidnetdirect.com" on its own is a search
    engine for bids and means nothing; "bidnetdirect.com/mississippi/city-of-x"
    is a stable page belonging to one agency, which is exactly what the portal
    directory is for. The test is two or more path segments and no query
    string -- a query is how every one of these platforms expresses a search.

    Candidates are ranked rather than taken in document order, because the
    registration link reliably comes first.
    Checked before the anchors: a page that has been taken over by a hosted
    platform usually declares its real address in <link rel="canonical">. That
    is the site telling us where it actually lives, which beats any link on
    it. Canon City's /Bids.aspx serves BidNet Direct's page and names
    bidnetdirect.com/colorado/cityofcanoncity in its canonical tag while
    carrying no anchor to it at all, so the anchor-only search found nothing
    and the portal counted as a parse miss on every scan forever.
    """
    text = str(html or "")
    for m in _CANONICAL_RE.finditer(text):
        url = _unescape(m.group(1))
        if _is_hosted_agency_url(url):
            return url
    best, best_score = "", -99
    for m in _HOSTED_HREF_RE.finditer(text):
        url = _unescape(m.group(1))
        label = _clean(_unescape(m.group(2)))
        try:
            parts = urllib.parse.urlparse(url)
        except ValueError:
            continue
        host = parts.netloc.lower()
        host = host[4:] if host.startswith("www.") else host
        if not any(host == d or host.endswith("." + d)
                   for d in HOSTED_PORTAL_DOMAINS):
            continue
        if parts.query:
            continue
        segs = [s for s in parts.path.split("/") if s]
        if len(segs) < 2:
            continue
        tail = segs[-1].replace("-", " ").replace("_", " ")
        score = 0
        if _NOT_A_LISTING_RE.search(tail) or _NOT_A_LISTING_RE.search(label):
            score -= 10
        if _IS_A_LISTING_RE.search(tail):
            score += 3
        if _IS_A_LISTING_RE.search(label):
            score += 2
        if score > best_score:
            best, best_score = url, score
    # Every candidate looked like a sign-up page: better to leave the entry
    # alone than to learn a registration form as the city's bid portal.
    return best if best_score > -10 else ""


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
    # A street overlay programme is let with curb, gutter and ADA ramp repair
    # as pay items, so it is squarely this trade's work -- and "2026 Asphalt
    # Overlay Program" matched none of the terms above. "overlay" is not
    # listed on its own: a zoning overlay district is not a paving job.
    "asphalt", "mill and overlay", "milling and overlay", "microsurfacing",
    # Site work a concrete crew self-performs, or where the concrete is the
    # substantial part of the job. Auditing what the gate REJECTED found
    # these being thrown away: storm sewer work (the inlets, manholes and
    # curb restoration are all flatwork), demolition (which is concrete
    # removal), retaining walls, culverts, and trench restoration behind a
    # utility line.
    #
    # Kept out of STRONG_NICHE_TERMS deliberately -- these describe jobs
    # this trade can bid, not jobs that are definitionally theirs, so they
    # must not override the CLEARLY_UNRELATED and professional-services
    # checks the way "sidewalk" does.
    "demolition", "excavation", "earthwork", "grading", "site work",
    "sitework", "storm sewer", "stormsewer", "stormwater", "storm water",
    "drainage", "culvert", "retaining wall", "manhole", "catch basin",
    "footing", "bollard", "dumpster enclosure", "full depth reclamation",
    "sewer lateral", "trench restoration",
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


# Consultant work, not construction work. This needs its own rule rather than
# a CLEARLY_UNRELATED entry because these titles carry a STRONG term and so
# skip that check entirely: "Pavement Management Program Engineering Services"
# matches "pavement", "Cochituate Rail Trail Design" matches "trail". A
# concrete contractor cannot bid any of them.
#
# Only phrases that name the profession qualify. A bare "Services" must not:
# "Crack Sealing Services" and "On-Call Public Works Repair" are real work.
_PRO_SERVICES_RE = re.compile(
    r"\b(?:engineering|design|consulting|consultant|architectural|appraisal|"
    r"surveying|inspection|planning)\s+(?:and\s+\w+\s+)?services\b"
    r"|\bprofessional\s+services\b"
    r"|\bfeasibility\s+study\b|\bmaster\s+plan\b"
    r"|\bland\s+survey(?:ing)?\b|\bright[\s-]of[\s-]way\s+appraisal\b"
    r"|\brfq?\s+for\s+(?:engineering|design|consulting)\b"
    # Construction Engineering & Inspection. The agency hires a firm to watch
    # somebody else build it -- it is the opposite end of the job from the
    # crew pouring the concrete, and it reads as concrete work because the
    # scope describes concrete work. Written every possible way, and the
    # parenthetical in "Inspection (CEI) Services" defeats the adjacency the
    # rule above needs, which is how it reached a live Nashville board.
    r"|\bce\s?&\s?i\b|\(\s*ce\s?&?\s?i\s*\)"
    r"|\bconstruction\s+engineering\s+(?:and|&)\s+inspection\b"
    r"|\bconstruction\s+inspection\s+services\b"
    r"|\bmaterials?\s+testing\s+services\b"
    r"|\binspection\s*\([^)]{1,12}\)\s*services\b", re.I)


# Buying the stuff is not doing the work. "Emulsified Asphalt for the Wilson
# County Road Commission" and "Metal Culverts for the Wilson County Road
# Commission" both reached a live Nashville board: they name this trade's
# materials, so every keyword fires, but the contract is a commodity order and
# a concrete crew has nothing to bid.
#
# The test is deliberately two-sided. A supply shape alone is not enough --
# "furnish and install 400 LF of curb" is exactly our work -- so a real
# construction verb anywhere in the text rescues it.
_COMMODITY = (r"emulsified\s+asphalt|asphalt\s+(?:emulsion|cement|binder)|"
              r"bituminous\s+material|cold\s+mix|hot\s+mix|"
              r"ready[\s-]?mix(?:ed)?\s+concrete|"
              r"(?:metal|plastic|hdpe|corrugated|concrete)\s+(?:culvert|pipe)s?|"
              r"culvert\s+pipes?|aggregate|crushed\s+(?:stone|rock)|"
              r"sand\s+and\s+gravel|road\s+salt|de[\s-]?icing|rip[\s-]?rap|"
              r"reinforcing\s+steel|rebar|guardrail\s+material|"
              r"traffic\s+(?:paint|sign)s?|fuel|lubricants?")
_SUPPLY_SHAPE_RE = re.compile(
    r"\b(?:purchase|procurement|acquisition|supply|supplying)\s+of\b"
    r"|\bannual\s+(?:supply|materials?|purchase)\b"
    r"|\bmaterials?\s+(?:bid|contract|purchase|supply)\b"
    r"|\b(?:%s)\s+for\s+(?:the\s+)?\w" % _COMMODITY
    + r"|\bbid\s+for\s+(?:the\s+)?(?:purchase|supply)\b", re.I)
# Verbs that mean somebody is building something, not shipping it.
_BUILD_VERB_RE = re.compile(
    r"\b(?:construct|install|replace|repair|rehabilitat|reconstruct|resurfac|"
    r"mill(?:ing)?|pave|paving|overlay|remove|demolish|excavat|grade|grading|"
    r"widen|realign|build|erect|pour)\w*", re.I)


# "<road name> ... Improvements" is one of the commonest ways a municipality
# titles street work, and the exact substrings "street improvement" and
# "road improvement" in NICHE_TERMS above cannot see it: any word in between
# defeats them. Sampling the live board across six metros, this alone was
# throwing away "Bear Creek Road Safety Improvements", "Lonedell Road Safety
# Improvements", "Saline Road Safety Improvements", "Canton Ave Improvement
# Project - Phase 2" and "Commercial Street (8th Ave to 10th Ave) Stormsewer
# Improvements" -- five real jobs in one sweep, lost on word order.
# The first line is how a city writes an address. The second is how a state
# DOT writes one, and it was missing entirely -- which quietly cost us real
# work: "Coldmill and resurface on Route 32" failed the road-work test while
# the otherwise identical "on Ohio Street" passed. State lettings are written
# in route numbers almost exclusively.
#
# "route" alone is the one risky word here, since a bus route is not this
# trade, so it is admitted only as a numbered designation ("Route 32",
# "Route K", "State Route 45") and the transit senses are excluded outright.
_ROADWAY = (r"street|st\.|road|rd\.|ave|avenue|drive|blvd|boulevard|"
            r"highway|hwy|lane|parkway|pkwy|court|alley|corridor|intersection|"
            r"interstate|freeway|expressway|"
            r"(?:state\s+|county\s+|us\s+|sr\s+|fm\s+)?route\s+[A-Z0-9]+|"
            r"i-\d+|us-?\s?\d+|sr-?\s?\d+|mile\s+marker")
# A bus route being "improved" is a transit study, not concrete.
_TRANSIT_ROUTE_RE = re.compile(
    r"\b(?:bus|transit|shuttle|delivery|snow|mail|paratransit|bike)\s+route\b",
    re.I)
_ROAD_WORK_RE = re.compile(
    rf"\b(?:{_ROADWAY})\b[^.;:]{{0,45}}?\b(?:improvement|reconstruct|"
    rf"rehabilitat|resurfac|widening|realign)"
    rf"|\b(?:improvement|reconstruct|rehabilitat|resurfac|widening|realign)"
    rf"\w*\s+(?:of\s+|to\s+)?[^.;:]{{0,45}}?\b(?:{_ROADWAY})\b", re.I)

# Bare "parking" earns its place -- a parking lot is flatwork -- but only
# where it means the surface. The meters and the permit software are not
# this trade.
_PARKING_RE = re.compile(
    r"\bparking\b(?!\s+(?:meter|enforcement|permit|citation|ticket|"
    r"management\s+(?:system|software)|study|garage\s+(?:audit|study)))", re.I)


# "Public works" and "cdbg" are loose terms that earn their place when they
# describe the WORK ("On-Call Public Works Services", a CDBG sidewalk
# programme) and mislead when they name the buyer or the funding pot. Live
# board sampling surfaced "BID - 2026 Roof Replacement at Public Works" and
# "RFP-26-27-010-Public Works Office Addition" -- a roof and a building --
# alongside "CDBG MAP Grantee Training Services", which is a training course.
_DEPT_NOT_WORK_RE = re.compile(
    r"\b(?:at|for|the)\s+(?:the\s+)?public\s+works\b"
    r"|\bpublic\s+works\s+(?:office|building|facility|garage|shop|yard|"
    r"department|director|complex|admin\w*)\b"
    r"|\bcdbg\b[^.;:]{0,25}\b(?:training|administration|grantee|"
    r"consultant|planning|program\s+management)\b", re.I)

# A listing row that is really the page's own navigation. "Public Works Bids"
# is a menu item, not a solicitation, and it reached the board as one.
_NAV_TITLE_RE = re.compile(
    r"^\s*(?:view\s+)?(?:all\s+)?(?:current\s+|open\s+|closed\s+)?"
    r"(?:public\s+works\s+|purchasing\s+|engineering\s+)?"
    r"(?:bids?|rfps?|rfqs?|solicitations?|bid\s+postings?|"
    r"bids?\s*(?:&|and)\s*rfps?|opportunities|proposals?)"
    r"(?:\s+(?:page|list|home|archive|and\s+rfps?))?\s*$", re.I)


# Page furniture that gets scraped as a title. _NAV_TITLE_RE above only fires
# when a title is ENTIRELY navigation, so "Concrete Bid Information" -- the
# heading of a page, not a job -- sails through on the word "Concrete".
#
# The test is what is LEFT once the furniture is removed. "Concrete Bid
# Information" leaves "Concrete", which names a trade and no project. "Union
# 2026 Street Repair Program Concrete Bid Information" leaves the whole
# programme name and is a real solicitation.
_PAGE_LABEL_TAIL_RE = re.compile(
    r"\s*[-\u2013\u2014:|]?\s*(?:"
    r"bid(?:ding)?\s+information|bid\s+opportunit(?:y|ies)|"
    r"solicitation\s+information|contract\s+information|"
    r"click\s+here(?:\s+for\s+(?:more\s+)?(?:information|details?))?|"
    r"(?:for\s+)?more\s+information|view\s+(?:details?|more)|"
    r"read\s+more|learn\s+more|details?"
    r")\s*$", re.I)
# Long enough to name a project, not just a trade. "Concrete" is 8.
_MIN_TITLE_CORE = 12


def _title_minus_page_furniture(title):
    text = str(title or "").strip()
    prev = None
    while prev != text:
        prev = text
        text = _PAGE_LABEL_TAIL_RE.sub("", text).strip(" -\u2013\u2014:|")
    return text


def _has_strong_term(blob):
    return _ADA_RE.search(blob) is not None or \
        any(t in blob for t in STRONG_NICHE_TERMS)


def looks_relevant(*texts):
    """True if a listing is worth spending an extraction call on."""
    parts = [str(t or "") for t in texts]
    blob = " ".join(parts).lower()
    if not blob.strip():
        return False
    # A menu item is not a solicitation. Tested against the title alone --
    # a real bid whose SCOPE happens to read "bids and rfps" is still real.
    if parts and _NAV_TITLE_RE.match(parts[0].strip()):
        return False
    # A trade word plus page furniture is a heading, not a job.
    if parts:
        head = parts[0].strip()
        core = _title_minus_page_furniture(head)
        if core != head and len(core) < _MIN_TITLE_CORE:
            return False
    # Checked before the strong term, deliberately: these titles all contain
    # one and would otherwise pass unexamined.
    if _PRO_SERVICES_RE.search(blob):
        return False
    # Buying materials is not doing the work -- unless the text also says
    # somebody is building with them.
    if _SUPPLY_SHAPE_RE.search(blob) and not _BUILD_VERB_RE.search(blob):
        return False
    # Same reasoning: these name the buyer or the funding source, not the job,
    # and would sail past on "public works" or "cdbg" alone.
    if _DEPT_NOT_WORK_RE.search(blob) and not _has_strong_term(blob):
        return False
    strong = _has_strong_term(blob)
    # A listing that names an unrelated trade needs a real trade word to
    # survive: "sidewalk replacement and landscaping" is our work, "legal
    # services for an infrastructure project" is not.
    if not strong and any(t in blob for t in CLEARLY_UNRELATED):
        return False
    road_work = _ROAD_WORK_RE.search(blob) is not None
    if road_work and _TRANSIT_ROUTE_RE.search(blob) and not strong:
        road_work = False
    return (strong or any(term in blob for term in NICHE_TERMS)
            or road_work
            or _PARKING_RE.search(blob) is not None)


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
    """Decode HTML entities. The stdlib table, not a hand-picked six.

    The six that used to be listed here did not include &rsquo; or &apos;,
    which is how procurement pages overwhelmingly write the apostrophe in
    "Engineer's Estimate". The raw entity survived into the cleaned text and
    defeated the regex looking for it, so a page printing
    "Engineer&rsquo;s Estimate: $1,000,000" reported no value at all. The same
    gap silently damaged every title, contact name and date carrying a curly
    quote, an em dash or a numeric reference.
    """
    return html.unescape(str(text or ""))


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
# Two tiers, tried in order. A posting page states a lot of dates and only
# one of them is the bid's: a live sample had "no later than 08/19/2026"
# meaning the day the DOCUMENTS became available, with the real "Closing
# Date/Time: 9/2/2026" further down, and another had "Questions Due:
# September 11" ahead of "Submission Deadline: September 29". Reading either
# page with one flat pattern picked the wrong date, and a deadline that is
# too early retires a live bid.
#
# Tier 1 is the labelled field every procurement platform prints. Tier 2 is
# the prose a legal notice uses when there is no labelled field at all.
_CLOSE_LABEL_RE = re.compile(
    r"(?:clos(?:e|es|ing)|bid\s+opening|submission\s+deadline|"
    r"bids?\s+due|proposals?\s+due|responses?\s+due|submittals?\s+due|"
    r"due)\s*"
    # "Information" is CivicPlus's own filler: the field is printed as "Bid
    # Opening Information: 8/25/26" as often as "Bid Opening Date/Time:".
    r"(?:date|information|info)?(?:\s*/\s*time|\s+and\s+time|\s*&\s*time)?\s*[:\-]\s*"
    r"(?:\d{1,2}:\d{2}\s*(?:[ap]\.?m\.?)?\s*(?:on\s+)?)?"
    rf"({_DATE})", re.I)
_CLOSE_PROSE_RE = re.compile(
    r"(?:no\s+later\s+than|must\s+be\s+received\s+by|received\s+until|"
    r"accepted\s+until|open\s+until|deadline|clos(?:e|es|ed|ing)|"
    # A bid opening is the effective deadline -- bids are due before it. Kept
    # out of tier 1 without a label, because "Bid Opening Information: 19
    # Moore Street" is an address, and only reached here when no labelled
    # closing field exists anywhere on the page.
    r"bid\s+opening|due)"
    r"(?:[^\d<>]{0,20}?\d{1,2}:\d{2}\s*(?:[ap]\.?m\.?)?)?"
    r"[^\d<>]{0,20}?"
    rf"({_DATE})", re.I)

# Dates that sit behind a deadline-ish word but belong to something else on
# the timetable. Every one of these was observed on a real posting;
# "Questions Due: September 11" ahead of "Submission Deadline: September 29"
# is the case that made this necessary.
#
# Anchored to the END of the lead-in, so it only fires when the disqualifying
# word is what the label is actually attached to. An unanchored search over a
# wider window read the previous field instead: "Publication Date/Time:
# 7/1/2026 ... Closing Date/Time: 12/1/2026" threw away the real closing date
# because "Publication" appeared 40 characters earlier. Only spaces and word
# characters may sit between -- a slash, colon or digit means a different
# field has started.
_NOT_THE_DEADLINE_RE = re.compile(
    r"(?:questions?|inquir\w*|clarificat\w*|rfi|addend\w*|"
    r"pre-?bid|site\s+visit|walk-?(?:through|thru)|job\s+walk|"
    r"documents?\s+available|plans?\s+available|publicat\w*|"
    r"advertis\w*|award\w*|notice\s+to\s+proceed|substantial\s+completion|"
    r"registrat\w*)[\s\w]{0,15}$", re.I)


def _first_real_deadline(text, pattern):
    """First match of `pattern` whose lead-in isn't about something else."""
    for m in pattern.finditer(text):
        if _NOT_THE_DEADLINE_RE.search(text[max(0, m.start() - 45):m.start()]):
            continue
        return m.group(1).strip()
    return ""


def _deadline_in(text):
    """The bid's own closing date somewhere in this text. "" if none."""
    text = str(text or "")
    return (_first_real_deadline(text, _CLOSE_LABEL_RE)
            or _first_real_deadline(text, _CLOSE_PROSE_RE))


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


# A listing row's TITLE routinely announces that the row is not a solicitation
# at all. CivicPlus sites keep their whole archive on the same page and simply
# retitle the entry when the job is let: "Roadway Improvements 2019" becomes
# "Award - Roadway Improvements 2019". Nothing else on the row changes -- no
# Closes: date, and the Status chip is often still absent -- so a bid let in
# 2019 arrived with status "Open" and was shown to the customer as live work.
#
# Measured on 400 real portals: of 88 niche rows that reached the feed, 88
# were displayed as open, and the majority were awards, published bid results
# or cancellations. This is exactly the "awarded jobs shown as open" the app
# was reported for.
# Both a leading label ("Award - X") and a trailing one ("X Award", "X
# Results"). Sites use whichever reads better and the trailing form was
# invisible: "Catch Basin Cleaning Award" and "CATCH BASIN CLEANING RESULTS"
# both reached the live board as open work.
_TITLE_AWARDED_RE = re.compile(
    r"^\s*(?:notice\s+of\s+)?award(?:ed|s)?\b\s*[:\-–—]"      # "Award - X"
    r"|^\s*award(?:ed)?\s*[:/]"                                  # "Award: X"
    r"|\bnotice\s+of\s+award\b"
    r"|\bhas\s+been\s+awarded\b"
    r"|\b(?:bid\s+)?award(?:ed)?\s*$", re.I)                     # "X Award"
_TITLE_RESULTS_RE = re.compile(
    r"\bbid\s+results\b|^\s*results\s*[:\-–—]"
    r"|\bbid\s+tab(?:ulation)?s?\b"
    r"|^\s*registry\s+of\s+proposals\b"
    r"|\bresults\s*$", re.I)                                      # "X Results"
_TITLE_CANCELLED_RE = re.compile(
    r"^\s*cancell?(?:ation|ed|ation\s+of)\b|\bcancell?ed\b"
    r"|^\s*cancellation\s+of\s+(?:bids?|procurement)\b", re.I)


# A wrong portal URL is very often served with HTTP 200 and a not-found page
# in the body, so _fetch_page reports "ok" and nothing downstream notices. The
# entry is then RECORDED AS A SUCCESS on every scan, which defeats
# bid_portals.MAX_FAIL entirely: the URL can never age out of the directory.
#
# Measured on 400 sampled CivicPlus entries, 21 were pages like this -- "404 |
# City of Drayton", "Page not found - City of Sheffield Lake Ohio",
# "CityOfPawnee.com is for sale | HugeDomains", and one lapsed domain now
# serving an online-casino page. Roughly one entry in twenty, held forever.
_MISSING_PAGE_RE = re.compile(
    r"<title[^>]*>\s*404\b"
    r"|<title[^>]*>[^<]{0,80}\b(?:page\s+not\s+found|404\s+(?:error|not\s+found)"
    r"|status\s+code\s+404|error\s+404)"
    r"|<h1[^>]*>\s*404\b"
    r"|\bthe\s+page\s+you\s+(?:requested|are\s+looking\s+for)\s+"
    r"(?:could\s+not\s+be\s+found|cannot\s+be\s+found|does\s+not\s+exist)"
    # Lapsed domains. Two flavours, both seen on real directory entries: a
    # broker's parking page, and a municipal domain someone re-registered and
    # pointed at an offshore gambling site. The second matters more than a
    # dead link -- forestparkga.org and lewistonmn.org are both in the
    # directory and both now serve casino pages to our customers.
    r"|<title[^>]*>[^<]{0,60}\bhugedomains(?:\.com)?\b"
    r"|\bis\s+for\s+sale\s*\|\s*hugedomains"
    r"|\bthis\s+domain\s+(?:name\s+)?is\s+for\s+sale\b"
    r"|\b(?:buy|purchase)\s+this\s+domain\b"
    r"|\bdomain\s+(?:is\s+)?(?:parked|for\s+sale)\b"
    r"|\bsitus\s+(?:togel|slot|judi)\b|\bbandar\s+(?:togel|toto|slot)\b"
    r"|\bslot\s+gacor\b|\bjudi\s+online\b", re.I)

# Words a page about bids has in its title. A CivicPlus Bids.aspx URL that
# serves something with none of them is not the bid page: sampling 500
# directory entries turned up "Home - Lake County, Ohio", "News & Events |
# City of Arlington, TX", "Sitka Police Department" and "Ethics Review Board
# - City of New Orleans", all reached at /Bids.aspx, all recorded as healthy
# on every scan and handed to the AI to read for bids they do not contain.
_BID_TITLE_RE = re.compile(
    r"\bbids?\b|\brf[pqi]s?\b|\bsolicitat\w*|\bprocure\w*|\bpurchas\w*"
    r"|\bopportunit\w*|\bcontract\w*|\bvendor\w*|\btender\w*"
    r"|\bquote\w*|\bproposal\w*", re.I)
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def page_title(html):
    """The page's <title>, cleaned. "" if it has none."""
    m = _TITLE_TAG_RE.search(str(html or "")[:20000])
    return _clean(_unescape(m.group(1))) if m else ""


def page_is_wrong_module(html):
    """True if this page is plainly not about bids at all.

    Narrower than it sounds, and deliberately so. The test is the page's own
    <title>: a real bid page names bids, procurement, purchasing,
    solicitations or opportunities in it -- "Purchasing | Taos County, NM",
    "Portal - Open Opportunities - City of Norwich, CT" -- even when our
    parser cannot read the body. A page titled "Home" or "News Archive" is
    the site's homepage being served for a URL that no longer exists.

    Only meaningful for a URL we expected to be a bid page, and only ever a
    reason to let the entry fail towards MAX_FAIL, never to delete it.
    """
    title = page_title(html)
    if not title:
        return False            # no title is not evidence either way
    if _BID_TITLE_RE.search(title):
        return False
    low = str(html or "").lower()
    return "bids.aspx" not in low and "bid postings" not in low


def page_is_missing(html):
    """True if this page is a not-found or parked-domain page served with 200.

    Distinct from civicplus_page_is_empty, which means "the right page, with
    nothing posted today" -- that entry is healthy and must keep its place in
    the directory. This one means the URL is wrong and should be allowed to
    fail its way out.
    """
    return bool(_MISSING_PAGE_RE.search(str(html or "")[:20000]))


def status_from_title(title):
    """A non-solicitation status the row's own title declares. "" if none.

    Deliberately anchored on label-shaped matches rather than the bare words:
    "Award" appearing anywhere would catch "Award Winning Streetscape Design",
    while "Award - ", "Award:" and "Notice of Award" are how an archived row
    is actually retitled.
    """
    text = _clean(_unescape(str(title or "")))
    if _TITLE_AWARDED_RE.search(text):
        return "Awarded"
    if _TITLE_CANCELLED_RE.search(text):
        return "Cancelled"
    if _TITLE_RESULTS_RE.search(text):
        # Results are published once bidding has closed, by definition.
        return "Closed"
    return ""


def _status_near(text):
    """The posting status stated in a listing row, normalised. "" if absent."""
    m = _STATUS_RE.search(str(text or ""))
    return m.group(1).strip().capitalize() if m else ""


# NOT WIRED INTO THE SCAN, deliberately. An RSS feed looks like the ideal
# source -- cheap, and it would give "posted since last check" for free --
# and this parser works. It is unused because the feeds are not there:
# sampled across 115 live CivicPlus portals, exactly ONE exposed a bids feed
# at /rss.aspx, and its items were already on the HTML listing. Modern
# CivicPlus does not publish the module feed.
#
# Kept rather than deleted because the parser is correct and cheap to hold,
# and a platform that does publish feeds can reuse it. Do not wire it into
# _run_known_portals expecting more rows -- that measurement has been done.
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
        closes = _deadline_in(desc) or _deadline_in(title)
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
        closes = _deadline_in(window)
        # The title wins over the row's Status chip. An archived CivicPlus
        # entry keeps whatever chip it had -- frequently none, which reads as
        # open -- while the title is edited to say it was awarded.
        rows.append({"title": label, "url": url, "scope": "",
                     "deadline": closes,
                     "status": status_from_title(label) or _status_near(window),
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


# ── State DOT letting listings ──────────────────────────────────────────────
# A state letting page is not shaped like a city bid page. A town's page lists
# that town's solicitations and the location is implied by whose site it is.
# A state page carries work from every corner of the state on one table, and
# says where each job is only inside the description ("Route K VERNON County.
# Resurface from I-49 near Nevada..."). So the row matters more than the page,
# and the county name inside the row is the only thing that can place it.
#
# The hazard here is navigation. A state site's main menu is dozens of <li>
# elements, and a generous row extractor happily reports Arkansas's nav as 633
# "rows" of which 32 pass the relevance filter -- "ADA", "Asphalt Binder Price
# Index", "Historic Structures Bridge Demolition Movie Clips". Every one is a
# menu entry. A row therefore has to look like a *record*, not merely like text
# containing a trade word.

_LETTING_ID_RE = re.compile(r"\b[A-Z]{0,3}[-\s]?\d{3,}[A-Z0-9\-]*\b")
_ROW_DATE_RE = re.compile(
    r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|"
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b")
# Menu entries that keep slipping through on trade words alone.
_NAV_ROW_RE = re.compile(
    r"^(?:home|about|contact|search|menu|news|events|careers|employment|"
    r"login|sign\s*in|espa\w*ol|skip to|read more|learn more|view all|"
    r"privacy|accessibility|site\s*map|faq)\b", re.I)


def _row_is_a_record(text, cells):
    """True if this row reads like one solicitation, not a menu entry.

    Three ways to qualify, any one of which is enough: it carries a date, it
    carries something shaped like a project/call number, or it is a multi-cell
    row with a real sentence of description in it. A bare trade word in a
    two-word <li> qualifies as none of them.
    """
    blob = str(text or "").strip()
    if len(blob) < 12 or _NAV_ROW_RE.match(blob):
        return False
    if _ROW_DATE_RE.search(blob):
        return True
    if len(cells) >= 2 and _LETTING_ID_RE.search(cells[0] or ""):
        return True
    # A description long enough to be a scope, in a row that has structure.
    return len(cells) >= 2 and max((len(c) for c in cells), default=0) >= 40


_COUNTY_HEADER_RE = re.compile(r"^\s*(?:county|parish|borough)\s*$", re.I)


def _county_column(header_cells):
    """Index of the column a table's own header says holds counties, or None.

    This is the only thing that makes a bare "Duval" in a cell trustworthy.
    Without it there is no way to tell Florida's County column from TxDOT's
    District column, whose values are also county names.
    """
    for i, cell in enumerate(header_cells or ()):
        if _COUNTY_HEADER_RE.match(str(cell or "")):
            return i
    return None


_PAGE_LETTING_DATE_RES = (
    # MoDOT: "Bid Opening Date 09/18/2026"
    re.compile(r"(?i)bid\w*\s+(?:open\w*|due)[^.]{0,40}?"
               r"(\d{1,2}/\d{1,2}/\d{2,4})"),
    # "September 18, 2026 Letting" / "Letting: September 18, 2026"
    re.compile(r"(?i)letting\s*(?:date)?\s*[:\-]?\s*"
               r"([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})"),
    re.compile(r"(?i)([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})\s+letting"),
)


def page_letting_date(html, url=""):
    """The one letting date a whole page is about, as text, or "".

    Used for pages that hold a single letting -- Missouri states it as a bid
    opening date in the body, Alabama only in the URL. Pages that hold many
    lettings (Florida) must not use this: see letting_rows, which pairs each
    table with the date heading above it.
    """
    text = _clean(_unescape(re.sub(
        r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", str(html or ""))))
    for pattern in _PAGE_LETTING_DATE_RES:
        m = pattern.search(text)
        if m:
            return m.group(1)
    when = _date_in(url)
    return "%d/%d/%d" % (when[1], when[2], when[0]) if when else ""


# A date heading standing on its own above a table -- how Florida separates
# one letting from the next. Deliberately anchored to a short standalone
# string so a date buried in a sentence is not mistaken for a section break.
_DATE_HEADING_RE = re.compile(
    r"(?i)(?:^|>)\s*([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})\s*(?:letting)?\s*(?:<|$)")


def _heading_date_before(body, pos, floor=0):
    """The last standalone date heading before `pos`, or ""."""
    best = ""
    for m in _DATE_HEADING_RE.finditer(body, floor, pos):
        best = m.group(1)
    return best


def letting_rows(html):
    """Record-shaped rows from a state letting page.

    Returns [(joined_text, cells, county_column, letting_date)] -- both the
    column index and the date travel with the row, because a page can carry
    several tables and only one has a County header, while each may belong to
    a different letting.

    Tables first, because that is what every state that works uses. List items
    are a fallback for the handful that render cards, and they are held to the
    same record test -- which is precisely what keeps a nav menu out.
    """
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ",
                  str(html or ""))
    out = []
    matches = list(re.finditer(r"(?is)<table[^>]*>(.*?)</table>", body))
    spans = [(m.group(1), m.start(), (matches[i - 1].end() if i else 0))
             for i, m in enumerate(matches)] or [(body, 0, 0)]
    current_date = ""
    for table, start, prev_end in spans:
        # Florida puts one letting per section and the whole page spans
        # January to September, so a table-only reader treats a February
        # letting as current work. The date is a heading, and it does not sit
        # directly above the projects table: each section runs
        #   <heading date> -> a one-row "Important Letting Documents" table
        #   -> the projects table.
        # So the date is carried forward from the last heading seen rather
        # than looked for immediately above each table.
        seen_here = _heading_date_before(body, start, prev_end)
        if seen_here:
            current_date = seen_here
        table_date = current_date
        chunks = re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", table)
        col = None
        for chunk in chunks:
            cells = [_clean(_unescape(c)) for c in
                     re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", chunk)]
            cells = [c for c in cells if c]
            if len(cells) < 2:
                continue
            if col is None:
                col = _county_column(cells)
                if col is not None:
                    continue          # that row was the header, not a record
            text = " | ".join(cells)
            if _row_is_a_record(text, cells):
                out.append((text, cells, col, table_date))
    if out:
        return out
    # Only when the page has no record-shaped table at all. An earlier version
    # tried the list fallback whenever the table pass returned fewer than
    # three rows AND reset the accumulator to do it, which silently threw away
    # a one- or two-row table -- exactly the shape a small state's letting has.
    for chunk in re.findall(r"(?is)<li[^>]*>(.{40,700}?)</li>", body):
        text = _clean(_unescape(chunk))
        if _row_is_a_record(text, [text]):
            out.append((text, [text], None, ""))
    return out


# A state "call" is a procurement unit, not a job. MoDOT bundles several jobs
# into one and numbers them inside a single cell: "(1): Job JSR0028 Route 18
# HENRY County. Coldmill... (2): Job JSR0033 Route 54 CEDAR, ST CLAIR County."
# Read whole, that row names four counties and gets placed at whichever is
# nearest -- so a Springfield contractor saw a card headed "Polk County, 28mi"
# whose description was about work in Henry. Each numbered job is its own
# piece of work in its own place and has to be split out.
_BUNDLED_JOB_RE = re.compile(r"\(\s*\d+\s*\)\s*:\s*(?=Job\b)", re.I)


def split_bundled_jobs(desc):
    """One description per job. Unbundled text comes back as a single item."""
    text = str(desc or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _BUNDLED_JOB_RE.split(text) if p.strip()]
    if len(parts) > 1:
        return parts
    # A single "(1): Job ..." is still a bundle marker -- a call that happens
    # to hold one job. The numbering is MoDOT's bookkeeping, not something a
    # contractor should read on a card, so drop a leading marker either way.
    return [_BUNDLED_JOB_RE.sub("", text, count=1).strip() or text]


# Some states do not publish a stable listing URL at all. Alabama's letting
# lives at .../NTC/2026/NTC_August_28_2026.html -- a new address every
# letting, and the old one 404s. Kentucky and Tennessee are the same shape: an
# index page whose rows are dates linking to that date's letting. For those,
# the source we store is the INDEX, and the current listing is resolved from
# it on each scan. Storing the dated URL instead means the source silently
# dies the moment the letting rotates.
_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")
_MONTH_NUM = {m: i + 1 for i, m in enumerate(_MONTHS)}
_LETTING_LINK_RE = re.compile(
    r"letting|notice\s*to\s*contractors|\bntc\b|bid\s*opening", re.I)
_SKIP_LINK_RE = re.compile(
    r"prior|previous|past|archive|histor|result|tabulat|award|"
    r"wage|prequalif|help|form", re.I)


def _date_in(text):
    """(y, m, d) from "August 28, 2026", "NTC_August_28_2026" or "8/28/2026"."""
    blob = str(text or "").replace("_", " ").replace("-", " ")
    m = re.search(r"(%s)[a-z]*\s+(\d{1,2})\s*,?\s+(\d{4})" % "|".join(_MONTHS),
                  blob, re.I)
    if m:
        return (int(m.group(3)), _MONTH_NUM[m.group(1).lower()], int(m.group(2)))
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", blob)
    if m:
        return (int(m.group(3)), int(m.group(1)), int(m.group(2)))
    return None


def newest_letting_link(html, base_url="", today=None):
    """The most recent dated letting link on an index page, or "".

    Prefers the newest letting that is not in the future by more than a year
    (a guard against a stray 2031 planning date), and skips anything the label
    marks as prior/archived/results -- those pages parse perfectly well and
    contain nothing biddable.
    """
    best, best_key = "", None
    for m in re.finditer(
            r'(?is)<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', str(html or "")):
        href = _unescape(m.group(1))
        label = _clean(_unescape(m.group(2)))
        blob = label + " " + href
        if not _LETTING_LINK_RE.search(blob) or _SKIP_LINK_RE.search(blob):
            continue
        when = _date_in(label) or _date_in(href)
        if not when:
            continue
        if today and when[0] > today[0] + 1:
            continue
        # A page beats a document. Alabama's index offers both the notice as
        # HTML and the same letting as a PDF, and the PDF sorts first by
        # accident of link order -- we cannot read it, so it would look like a
        # dead source.
        is_doc = bool(re.search(r"\.(pdf|docx?|xlsx?|zip)$", href, re.I))
        key = (when, 0 if is_doc else 1)
        if best_key is None or key > best_key:
            best_key = key
            best = urllib.parse.urljoin(base_url, href) if base_url else href
    return best


# A third row shape, after tables and list items: the "Notice to Contractors"
# prose page. Alabama publishes one per letting -- no table at all, just
# numbered blocks:
#
#   1. DEMOF-RPF-NHF-PRF-A210(943), TUSCALOOSA COUNTY  Contract Time: 620
#   Working Days for constructing the Bridge Replacement and Approaches
#   (Grading, Drainage, Pavement and Traffic Stripe)...
#
# Everything needed is there -- project number, county, scope -- and a
# table-only reader sees none of it.
_NTC_ITEM_RE = re.compile(
    r"(?<![\d.])(\d{1,3})\.\s+"                    # "1. "
    r"([A-Z][A-Z0-9]*(?:[-/][A-Z0-9]+)*\([0-9]+\)|"  # STPSU-3525(253)
    r"[A-Z]{2,}[A-Z0-9]*-[0-9][-0-9A-Z]*)"           # ATRP2-52-2024-263
    r"\s*,\s*")


# How far past the project number to look for the job's own county. Long
# enough for "PROJNO , TUSCALOOSA COUNTY", short enough to stop before the
# scope starts naming neighbours.
HEAD_FOR_COUNTY = 90


def notice_to_contractors_items(text, min_len=60):
    """Numbered project blocks from a Notice to Contractors page.

    Each block runs from its own number to the next one. The header of such a
    page repeats the same list as a bare index ("1. X, TUSCALOOSA 4. Y,
    SUMTER") before the detailed entries, so blocks shorter than min_len are
    dropped -- those are the index, not the notice.
    """
    blob = re.sub(r"\s+", " ", str(text or ""))
    marks = list(_NTC_ITEM_RE.finditer(blob))
    if len(marks) < 2:
        return []
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(blob)
        body = blob[m.start():end].strip()
        if len(body) >= min_len:
            out.append((m.group(2), body))
    return out


def parse_notice_to_contractors(html, state, base_url="", county_finder=None):
    """Rows from a Notice to Contractors prose page (no table involved)."""
    text = _clean(_unescape(re.sub(
        r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", str(html or ""))))
    notice_date = page_letting_date(html, base_url)
    rows, seen = [], set()
    for ident, body in notice_to_contractors_items(text):
        if not looks_relevant(body):
            continue
        # The county stated immediately after the project number is where the
        # work is. The rest of the block routinely names others -- a route
        # "from the Shelby County line", an adjoining district -- and picking
        # by population instead put a Chilton County job in Shelby.
        head = body[:HEAD_FOR_COUNTY]
        places = county_finder([head], state) if county_finder else []
        if not places and county_finder:
            places = county_finder([body], state)
        if not places:
            continue
        if ident in seen:
            continue
        seen.add(ident)
        # "21. IM-HSIP-IMGR-I022(327) , WALKER COUNTY ..." -- the item number
        # is the notice's own ordering and means nothing on a card, and the
        # project id is already carried as the title prefix.
        shown = re.sub(r"^\d{1,3}\.\s*", "", body).strip()
        rows.append(_state_row(ident, shown[:1200], body, places, state,
                               base_url, call=ident,
                               letting_date=notice_date))
    return rows


def parse_state_letting(html, state, base_url="", county_finder=None):
    """Concrete-relevant, placeable rows from a state DOT letting page.

    `county_finder` is injected rather than imported so this module stays pure
    text-in/rows-out and testable without the county table on disk. Production
    passes counties.counties_named, which takes the row's CELLS and demands
    explicit evidence -- a dedicated county column, or a name followed by the
    word County. A loose search over the joined row text is not good enough
    here: TxDOT's district column is full of county names that have nothing to
    do with where the job is.

    A row has to clear both bars to come back: it must pass the same
    looks_relevant() filter every city bid goes through, and it must name a
    county we can put on a map. A statewide row we cannot place is worse than
    no row -- it would be shown to every contractor in the state regardless of
    distance.
    """
    rows = []
    seen = set()
    page_date = page_letting_date(html, base_url)
    for text, cells, county_column, table_date in letting_rows(html):
        if not looks_relevant(text):
            continue
        if not county_finder:
            continue
        # The longest cell is the description on every state layout checked;
        # the first short one is the call/project number.
        full_desc = max(cells, key=len) if cells else text
        ident = ""
        for c in cells:
            if c is not full_desc and len(c) <= 24 and _LETTING_ID_RE.search(c):
                ident = c
                break
        jobs = split_bundled_jobs(full_desc)
        for desc in jobs:
            if len(jobs) > 1 and not looks_relevant(desc):
                # One bundle can mix a sidewalk job with a signal upgrade.
                continue
            if county_column is not None:
                # A dedicated county column describes the whole row, so it
                # stays authoritative however the description is split.
                places = county_finder(cells, state, county_column)
            else:
                places = county_finder([desc], state)
            if not places:
                continue
            key = (ident, desc[:120])
            if key in seen:
                continue
            seen.add(key)
            rows.append(_state_row(
                ident, desc, text, places, state, base_url,
                call=(cells[0] if cells else ""),
                letting_date=(table_date or page_date)))
    if not rows:
        # No table and no list gave us anything placeable. Some states publish
        # the letting as prose instead -- see parse_notice_to_contractors.
        rows = parse_notice_to_contractors(html, state, base_url, county_finder)
    return rows


def _compose_state_title(ident, desc):
    """"CODE — description", without repeating the code when the description
    already opens with it."""
    body = str(desc or "").strip()
    code = str(ident or "").strip()
    if code and body.upper().startswith(code.upper()):
        body = body[len(code):].lstrip(" ,-\u2013\u2014:")
    if not body:
        return code[:300]
    return (("%s \u2014 %s" % (code, body)) if code else body)[:300]


def _state_row(ident, desc, text, places, state, base_url, call="",
               letting_date=""):
    county, lat, lon = places[0]
    return {
        # The description on a notice page opens with the same project code we
        # use as the identifier, so composing "CODE — CODE , COUNTY ..." said
        # it twice. Drop the leading copy.
        "title": _compose_state_title(ident, desc),
        "scope": desc[:1200],
        "url": base_url,
        # A state row rarely states its own due date -- the letting date IS
        # the deadline, and without it every state bid arrived undated, could
        # never be recognised as expired, and sat on the board forever.
        "deadline": _deadline_in(text) or letting_date or "",
        "status": status_from_title(desc) or _status_near(text) or "Open",
        "county": county,
        "lat": lat,
        "lon": lon,
        "state": state,
        "source": "state_dot",
        # The letting's own reference for this row -- MoDOT calls it a "call
        # number" (G02, D07). It is what addresses the plan-holder list for
        # this job, and nothing else needs it.
        "call": str(call or "").strip()[:16],
        "all_counties": [p[0] for p in places],
        # Every county this JOB names, with coordinates. Placement picks the
        # one nearest the scan centre -- the work really is in all of them, so
        # nearest is both true and the only useful answer to "how far is it".
        "places": list(places),
    }


# ── Plan holders ────────────────────────────────────────────────────────────
# A state highway job is not something a three-truck concrete crew wins as
# prime. The way in is as a sub, and the letting page names exactly who to
# call: the contractors who pulled plans on that job. Two of the eight holders
# on one MoDOT call were concrete companies, so subs already work this list.
#
# These are named individuals' business contact details on a government page.
# They belong on the card for the job they are bidding, and nowhere else -- in
# particular they must never reach the CSV export or a campaign list. That is
# a product rule, enforced at the export, not a parsing concern; it is written
# here because this is where somebody would come looking.

# "vendor" is deliberately NOT a company word. MoDOT's header is
# "Name - Vendor #", where Vendor is the state's ID for the PERSON, so
# matching on it handed back "McFail, Jacob 0013043" as the company and left
# the actual Organization column unread.
_PH_HEADERS = {
    "company": ("organization", "company", "firm", "bidder", "contractor",
                "business"),
    "contact": ("name", "contact", "attention"),
    "phone": ("phone", "telephone", "tel"),
    "email": ("email", "e-mail"),
    "address": ("address", "location", "city"),
}
# "Rhea, Don 0010907" -- the state's vendor number is bookkeeping, not a name.
_VENDOR_NUM_RE = re.compile(r"\s*\b\d{5,}\b\s*$")


def _ph_column_map(header_cells):
    out = {}
    for i, cell in enumerate(header_cells or ()):
        low = str(cell or "").strip().lower()
        if not low:
            continue
        for field, words in _PH_HEADERS.items():
            if field in out:
                continue
            if any(w in low for w in words):
                out[field] = i
                break
    return out


def _flip_name(name):
    """"Rhea, Don" -> "Don Rhea". Left alone if it is not that shape."""
    text = _VENDOR_NUM_RE.sub("", str(name or "")).strip()
    if text.count(",") == 1:
        last, first = (p.strip() for p in text.split(","))
        if last and first and " " not in first.strip():
            return "%s %s" % (first, last)
    return text


def parse_plan_holders(html, limit=25):
    """Contractors who pulled plans on a job: company, contact, phone, email.

    Header-driven rather than positional -- the column order is not the same
    on every state and guessing it wrong would attach a phone number to the
    wrong company. A row needs a company name and at least one way to reach
    somebody, or it is not a lead and is dropped.
    """
    out, seen = [], set()
    for table in re.findall(r"(?is)<table[^>]*>(.*?)</table>", str(html or "")):
        cols = None
        for chunk in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", table):
            cells = [_clean(_unescape(c)) for c in
                     re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", chunk)]
            if not any(cells):
                continue
            if cols is None:
                found = _ph_column_map(cells)
                if "company" in found:
                    cols = found
                continue
            def at(field):
                i = cols.get(field)
                return cells[i].strip() if i is not None and i < len(cells) else ""
            company = at("company")
            email = at("email")
            phone = at("phone")
            if not company or not (email or phone):
                continue
            if email and any(j in email.lower() for j in _JUNK_EMAIL_PARTS):
                email = ""
            key = company.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({"company": company,
                        "contact": _flip_name(at("contact")),
                        "phone": phone, "email": email,
                        "address": at("address")})
            if len(out) >= limit:
                return out
    return out


def plan_holder_index(html, base_url=""):
    """The letting page's link to its plan-holder index, or ""."""
    for m in re.finditer(r'(?is)<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                         str(html or "")):
        label = _clean(_unescape(m.group(2)))
        href = _unescape(m.group(1))
        if re.search(r"plan\s*holder", label + " " + href, re.I):
            return urllib.parse.urljoin(base_url, href) if base_url else href
    return ""


def plan_holder_url_for_call(index_url, call_no):
    """MoDOT shape: .../PlanHolder/Index/6128 -> .../PlanHolder/Call/6128?call=G02

    Named for what it is. This is one state's URL convention, not a standard,
    and anything else needs its own mapping rather than a wider guess here.
    """
    call = str(call_no or "").strip()
    if not call or not index_url:
        return ""
    m = re.search(r"/PlanHolder/Index/(\d+)", str(index_url), re.I)
    if not m:
        return ""
    base = str(index_url)[:m.start()]
    return "%s/PlanHolder/Call/%s?call=%s" % (
        base, m.group(1), urllib.parse.quote(call))


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
    text = _clean(_unescape(html))
    return (_first_real_deadline(text, _CLOSE_LABEL_RE)
            or _first_real_deadline(text, _CLOSE_PROSE_RE))


# Only a LABELLED figure counts. A bid page is full of dollar amounts that
# are not the job's value -- bid bonds, plan deposits, non-refundable fees,
# liquidated damages per day -- and presenting any of those as the project
# value would be worse than showing nothing, because a contractor would price
# against it. Measured on live postings: about 4% state a labelled estimate.
# The apostrophe class covers the straight quote, the curly one and the
# backtick: pages write "Engineer's", "Engineer’s" and "ENGINEER`S", and only
# the first was matched.
_VALUE_LABEL_RE = re.compile(
    r"(?:engineer(?:ing)?['’ʼ`]?s?\s+estimate|"
    # One optional word between "estimated" and the noun. PlanetBids labels
    # the field "Estimated Bid Value" and the exact-adjacency pattern could
    # not see it, so a posting stating $130,000.00 reached the customer with
    # an empty Est. Value box. Same shape covers "Estimated Project Cost" and
    # "Estimated Contract Value".
    r"estimated\s+(?:\w+\s+)?(?:cost|value|price|budget|amount)|"
    r"(?:bid|contract|project|construction)\s+value\s*(?:is|:)?|"
    r"project\s+estimate|opinion\s+of\s+probable\s+cost|"
    r"budget(?:ed)?\s+amount|estimated\s+project\s+cost|"
    r"estimated\s+construction\s+cost|construction\s+estimate)"
    r"[^$\n]{0,60}?(\$\s?[\d,]{4,}(?:\.\d{2})?)", re.I)

# Amounts that sit near a value-ish word but are definitely not the job.
# Amounts that sit near a value-ish word but are definitely not the job.
# The insurance limits are the commonest false positive by far: nearly every
# construction solicitation carries "$1,000,000 each occurrence / $2,000,000
# general aggregate", and reporting that as the project's value would have a
# contractor pricing against a number the page never claimed.
_NOT_A_VALUE = (r"bid\s+bond|plan\s+deposit|non-?refundable|"
                r"liquidated\s+damages|filing\s+fee|application\s+fee|"
                r"bid\s+security|performance\s+bond|payment\s+bond|"
                r"each\s+occurrence|general\s+aggregate|"
                r"combined\s+single\s+limit|liability\s+insurance|"
                r"umbrella\s+(?:policy|coverage)")
# Immediately BEFORE the label. Only spaces and word characters may sit
# between -- a "$" or a digit means a different field has already started.
_NOT_A_VALUE_LEAD_RE = re.compile(rf"(?:{_NOT_A_VALUE})[\s\w]{{0,15}}$", re.I)
# Immediately AFTER the figure, which is where the rate qualifiers live:
# "$1,000 per calendar day", "$500 per occurrence".
_NOT_A_VALUE_TRAIL_RE = re.compile(
    rf"\s*(?:per\s+(?:calendar\s+|working\s+|business\s+)?day"
    rf"|per\s+(?:occurrence|unit|each|ton|sf|square\s+foot|linear\s+foot|lf)"
    rf"|{_NOT_A_VALUE})", re.I)


def detail_value(html):
    """The project's stated value, or "" when the page doesn't give one."""
    text = _clean(_unescape(str(html or "")))
    if not text:
        return ""
    for m in _VALUE_LABEL_RE.finditer(text):
        # Anchored either side of the match rather than searched over a
        # 100-character window. The window version read the PREVIOUS field:
        # a PlanetBids posting printing "Liquidated Damages $1,000 per
        # calendar day  Estimated Bid Value $130,000.00" threw away the
        # $130,000 because "Liquidated Damages" sat 40 characters earlier.
        # A disqualifier only counts when it is what the amount is attached
        # to -- immediately before the label, or immediately after the figure.
        lead = text[max(0, m.start() - 45):m.start()]
        trail = text[m.end():m.end() + 30]
        if _NOT_A_VALUE_LEAD_RE.search(lead) or _NOT_A_VALUE_TRAIL_RE.match(trail):
            continue
        return re.sub(r"\s+", "", m.group(1))
    return ""


# Fields a posting carries that a listing row never does. Measured on 25 live
# postings: publication date 92%, a linked packet 56%, bid number 40%, an
# addendum 36%, a pre-bid meeting 20%.
# "Publication Date" is CivicPlus's own label and was the only one matched.
# Agencies on every other platform write the same fact a dozen other ways, so
# a posting that plainly said "Posted: 11/03/2026" read as undated -- and an
# undated bid waits out a 60-day first-seen clock instead of being aged on the
# date it is printed with. The bare one-word labels require the colon; the
# multi-word ones are specific enough without it.
_PUBLISHED_RE = re.compile(
    rf"(?:(?:publication|posting|issue|issued|release)\s+date(?:/time)?"
    rf"|date\s+(?:published|posted|issued|released)"
    rf"|(?:published|posted|issued|released)\s+on"
    rf"|published|posted|issued)\s*:\s*({_DATE})"
    rf"|(?:(?:publication|posting|issue|issued|release)\s+date(?:/time)?"
    rf"|date\s+(?:published|posted|issued|released)"
    rf"|(?:published|posted|issued|released)\s+on)\s+({_DATE})", re.I)
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
    if not m:
        return ""
    return (m.group(1) or m.group(2) or "").strip()


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
