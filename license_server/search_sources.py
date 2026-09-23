# ═══════════════════════════════════════════════════════════
# SHARED HELPERS (HTTP, geocoding, distance)
# ═══════════════════════════════════════════════════════════
def _get_json(url, headers=None, timeout=20):
    try:
        req = urllib.request.Request(url, headers=headers or {"User-Agent": "BidCallerPro/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _geo_from_zip(zip_code):
    data = _get_json(f"https://api.zippopotam.us/us/{zip_code}")
    p = (data or {}).get("places") or []
    if not p:
        return None
    try:
        return {"lat": float(p[0]["latitude"]), "lon": float(p[0]["longitude"]),
                "city": p[0].get("place name", ""),
                "state": (p[0].get("state abbreviation") or "").upper()}
    except (KeyError, ValueError, TypeError):
        return None


# Public bodies rarely call themselves by a bare city name. The AI is told to
# copy the location "exactly as written in the text", so it faithfully returns
# things like "City of O'Fallon", "Greene County", or "Aurora R-VIII School
# District" — none of which zippopotam can resolve, so every one of those bids
# used to be thrown away. Counties, townships and school districts are core
# buyers for sidewalk and ADA work, so that was a large hole in coverage.
# \b + \s* rather than \s+ so a truncated "City of" reduces to nothing at all,
# instead of leaving "City of" to be geocoded as if it were a place.
_PLACE_PREFIX_RE = re.compile(
    r"^(?:the\s+)?(?:city|town|village|township|borough|county|municipality)\s+of\b\s*", re.I)
_PLACE_SUFFIX_RE = re.compile(
    r"\s+(?:r-[ivxlc]+\s+)?(?:school\s+district|public\s+schools|community\s+schools|"
    r"unified\s+school\s+district|isd|usd|county\s+schools|"
    r"housing\s+authority|water\s+district|utility\s+district|"
    r"public\s+works|road\s+district|fire\s+district|park\s+district)\b.*$", re.I)
_PLACE_TRAILING_RE = re.compile(r"[\s,;:.\-]+$")


def _normalize_place(name):
    """Reduce an authority's name to the place it is named after.

    "City of O'Fallon" -> "O'Fallon", "Aurora R-VIII School District" ->
    "Aurora". Returns "" if nothing usable is left.
    """
    s = " ".join(str(name or "").split())
    if not s:
        return ""
    s = _PLACE_PREFIX_RE.sub("", s)
    s = _PLACE_SUFFIX_RE.sub("", s)
    s = _PLACE_TRAILING_RE.sub("", s)
    return s.strip()


def _zip_name_key(name):
    """A place name reduced to what makes two spellings the same place."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _largest_cluster(pts, radius_mi=25.0):
    """The biggest group of points within `radius_mi` of one of their number.

    Greedy and O(n^2), which is free at the handful of ZIPs a city has.
    """
    best = []
    for anchor in pts:
        near = [p for p in pts
                if _miles_between(anchor[0], anchor[1], p[0], p[1]) <= radius_mi]
        if len(near) > len(best):
            best = near
    return best or list(pts)


def _zippopotam_city(city, state):
    """Coordinates for a named place, averaged over its ZIP codes.

    Averaging every ZIP the API returns was wrong in a way that quietly
    destroyed the answer for ambiguous names. Asked for Frankfort, IL,
    zippopotam returns three places -- Frankfort itself at 41.5,-87.8, plus
    Frankfort Heights and West Frankfort, both around 38.0,-88.9. The mean of
    those three is a field near Effingham, 150 miles from any of them, and a
    contractor in Frankfort saw 8 nearby agencies instead of 113.

    Two filters, in order. Exact name first: "Frankfort Heights" is not
    Frankfort, and dropping the near-misses solves this case outright. Then,
    for names that really are duplicated within one state, keep the largest
    cluster -- more ZIP codes means the bigger place, which is what someone
    typing a bare city name almost always means.

    A city with many ZIPs still averages them, which is the behaviour worth
    keeping: Springfield MO spans 16 ZIPs across 10 miles and Peoria 36
    across 19, and their means are correct.
    """
    url = f"https://api.zippopotam.us/us/{state.upper()}/{urllib.parse.quote(city)}"
    data = _get_json(url)
    places = (data or {}).get("places") or []
    pts, exact = [], []
    want = _zip_name_key(city)
    for p in places:
        try:
            pt = (float(p["latitude"]), float(p["longitude"]))
        except (KeyError, ValueError, TypeError):
            continue
        pts.append(pt)
        if _zip_name_key(p.get("place name")) == want:
            exact.append(pt)
    chosen = exact or pts
    if not chosen:
        return None
    if len(chosen) > 1:
        chosen = _largest_cluster(chosen)
    return (sum(x for x, _ in chosen) / len(chosen),
            sum(y for _, y in chosen) / len(chosen))


# Nominatim asks for a descriptive User-Agent and no more than one request a
# second. Results are cached in the geo db, so real traffic here is low.
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_UA = "BidCallerPro/2.0 (bid search for concrete contractors)"
_nominatim_lock = threading.Lock()
_nominatim_last = [0.0]


def _nominatim_place(place, state):
    """Geocode anything zippopotam can't: counties, townships, unincorporated
    communities, and other named administrative areas."""
    with _nominatim_lock:
        wait = 1.0 - (time.time() - _nominatim_last[0])
        if wait > 0:
            time.sleep(wait)
        _nominatim_last[0] = time.time()
    qs = urllib.parse.urlencode({
        "q": f"{place}, {state}, USA", "format": "json", "limit": "1",
        "countrycodes": "us", "addressdetails": "0",
    })
    data = _get_json(f"{_NOMINATIM_URL}?{qs}",
                     headers={"User-Agent": _NOMINATIM_UA, "Accept": "application/json"})
    if not isinstance(data, list) or not data:
        return None
    try:
        return float(data[0]["lat"]), float(data[0]["lon"])
    except (KeyError, ValueError, TypeError):
        return None


def _geo_from_city(city, state):
    """Resolve a place name to coordinates, trying progressively looser forms.

    zippopotam only knows names that appear in ZIP-code data, which excludes
    most counties and townships, so it is tried first (fast, generous rate
    limit) and Nominatim picks up whatever it misses.
    """
    state = (state or "").upper()
    raw = " ".join(str(city or "").split())
    if not raw or not state:
        return None
    normalized = _normalize_place(raw)
    attempts = [a for a in dict.fromkeys([raw, normalized]) if a]
    for attempt in attempts:
        hit = _zippopotam_city(attempt, state)
        if hit:
            return {"lat": hit[0], "lon": hit[1], "city": attempt, "state": state}
    for attempt in attempts:
        hit = _nominatim_place(attempt, state)
        if hit:
            return {"lat": hit[0], "lon": hit[1], "city": attempt, "state": state}
    return None


def _miles_between(lat1, lon1, lat2, lon2):
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _destination_point(lat, lon, bearing_deg, distance_mi):
    """Point `distance_mi` miles from (lat,lon) along compass bearing `bearing_deg`."""
    R = 3958.8
    br = math.radians(bearing_deg)
    lat1, lon1 = math.radians(lat), math.radians(lon)
    d_r = distance_mi / R
    lat2 = math.asin(math.sin(lat1) * math.cos(d_r) + math.cos(lat1) * math.sin(d_r) * math.cos(br))
    lon2 = lon1 + math.atan2(
        math.sin(br) * math.sin(d_r) * math.cos(lat1),
        math.cos(d_r) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


# Civil divisions that are not places anyone lets a bid from. A rural point
# reverse-geocodes to one of these constantly, and BigDataCloud writes them
# with an "of" prefix ("Township of Rock Prairie").
_NON_PLACE_RE = re.compile(
    r"\b(township|twp|unincorporated|unorganized|census\s+designated|"
    r"CDP|precinct|ward|survey|reservation)\b", re.I)


def _reverse_geocode_city(lat, lon):
    """Free, keyless reverse geocode (same provider the app uses client-side
    for auto-fill) -- turns a lat/lon into a {city, state} so we can search
    towns scattered across a wide radius, not just the one the user typed.

    The name this returns is not cosmetic: it becomes the label the user sees,
    every search query the scan builds, and the key the .gov directory is asked
    for. A live scan came back centred on "Township of Rock Prairie, MO" — a
    rural civil township with no procurement office, no registered domain, and
    no meaning in a search query. The structured portal path never ran at all,
    because there is nothing to look up. So candidates are considered in order
    and the first one that names a real government is taken, falling back to
    the county — which does let road and curb work, and which the registry
    knows — rather than to a township.
    """
    url = (f"https://api.bigdatacloud.net/data/reverse-geocode-client"
           f"?latitude={lat}&longitude={lon}&localityLanguage=en")
    data = _get_json(url)
    if not data:
        return None
    sub = (data.get("principalSubdivisionCode") or "").split("-")[-1].upper()
    country = (data.get("countryCode") or "").upper()
    if sub not in STATE_ABBRS or country != "US":
        return None

    # Best first. adminLevel 8 is the municipality, 7 the civil township, 6 the
    # county; the top-level "city" is usually 8 but is empty in rural areas.
    admin = ((data.get("localityInfo") or {}).get("administrative") or [])

    def _named(level):
        return [str(e.get("name") or "").strip() for e in admin
                if isinstance(e, dict) and e.get("adminLevel") == level
                and str(e.get("name") or "").strip()]

    # Kept as (cleaned, original) pairs: _normalize_place strips the very
    # "Township of" prefix that marks a name as unusable, so the check below
    # has to run against what the provider actually said.
    candidates, seen = [], set()
    for raw in ([str(data.get("city") or "").strip()] + _named(8)
                + [str(data.get("locality") or "").strip()]
                + _named(7) + _named(6)):
        cleaned = _normalize_place(raw)
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            candidates.append((cleaned, raw))
    if not candidates:
        return None

    # A name the .gov registry recognises is one we can actually search — but
    # a township still doesn't qualify just because stripping "Township of"
    # leaves something that happens to be registered. "Township of Franklin"
    # became "Franklin", which Missouri does have a domain for, and beat the
    # actual city the point was in.
    for cleaned, raw in candidates:
        if not _NON_PLACE_RE.search(raw) and gov_directory.known_in_state(cleaned, sub):
            return cleaned, sub
    # Otherwise anything that isn't a bare civil division.
    for cleaned, raw in candidates:
        if not _NON_PLACE_RE.search(raw):
            return cleaned, sub
    return None


def _nearby_anchor_towns(center, radius, pdb=None):
    """Pick a handful of towns scattered around the search radius (not just
    the center city) so a wide-radius scan actually looks in more places
    instead of only searching near the one city the user typed. Skipped for
    tight radii where the center-only search already covers the area well."""
    if radius < 40:
        return []
    # Guessed points are worth rationing by area -- most of them land on
    # nothing, so more of them only helps when there is more empty ground to
    # cover. A verified town is not the same trade: every one is a real
    # procurement office, so the budget (MAX_ANCHOR_TOWNS, env-tunable) is
    # worth spending in full rather than scaling it down for a mid-size
    # radius. round(radius/20) gave a 50mi scan just two anchors, which is
    # what left Springfield -- the single most productive source in the area
    # -- out of an Aurora scan entirely.
    n_guessed = max(2, min(MAX_ANCHOR_TOWNS, round(radius / 20)))
    n = MAX_ANCHOR_TOWNS

    # Prefer towns we already know have a real bid page over points guessed
    # off a compass bearing. Anchors are the only towns besides the centre
    # that get SEARCH queries, and that is what actually finds work: a town's
    # own portal lists only its own solicitations, while the queries reach the
    # county road department, the school district and the state portal around
    # it. A 50mi scan from Aurora, MO reaches Springfield (28mi) -- but
    # Springfield only ever got its portal read, which today holds an ice
    # machine rental, a PA system and a skate-shop concession and nothing in
    # this trade, so it contributed nothing, while the query budget went to
    # reverse-geocoded guesses that can land on a township with no
    # procurement office at all (see _reverse_geocode_city's own notes).
    # Same budget, verified targets.
    if pdb is not None:
        try:
            known = bid_portals.towns_within_radius(
                pdb, center["lat"], center["lon"], radius,
                exclude={(center["city"].lower(), center["state"])})
        except Exception as ex:  # never let anchor selection break a scan
            print(f"[scan] known-town anchor lookup failed: {ex}", flush=True)
            known = []
        if known:
            # Nearest first: the closest verified towns are both the most
            # likely to be worth driving to and, for someone in a small town
            # next to a metro, the way the metro itself gets reached. The
            # farther known towns in the radius are not dropped -- they still
            # get their portal read by the known-towns pass in _perform_scan,
            # they just don't get search queries too.
            known.sort(key=lambda t: _miles_between(
                center["lat"], center["lon"], t[2], t[3]))
            return known[:n]
    # One ring of towns at a single distance leaves the ground between it and
    # the centre unsearched, which on a 125mi scan is most of the area. Wide
    # radii get two rings, with the outer ring's bearings offset so the towns
    # interleave rather than lining up along the same spokes.
    rings = [0.7] if radius < 80 else [0.5, 0.85]
    per_ring = max(1, n_guessed // len(rings))
    seen = {(center["city"].lower(), center["state"])}
    towns = []
    for ring_i, frac in enumerate(rings):
        dist = radius * frac
        for i in range(per_ring):
            bearing = (i * (360.0 / per_ring)) + (ring_i * (180.0 / per_ring))
            lat, lon = _destination_point(center["lat"], center["lon"], bearing, dist)
            found = _reverse_geocode_city(lat, lon)
            if not found:
                continue
            city, state = found
            key = (city.lower(), state)
            if key in seen:
                continue
            seen.add(key)
            # Coordinates come back too: a bid found by searching this town but
            # naming a place we can't geocode (a county, a school district) can
            # be anchored here instead of discarded. See _place_bid.
            towns.append((city, state, lat, lon))
    return towns


_DEADLINE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y",
    "%B %d %Y", "%b %d %Y", "%m/%d/%y",
)


# Date shapes to pull out of surrounding prose. Ordered longest-first within a
# family so "12/01/2026" isn't mistaken for a 2-digit year.
_MONTH_WORDS = ("January|February|March|April|May|June|July|August|September|"
                "October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec")
_DATE_PATTERNS = (
    re.compile(r"\d{4}-\d{2}-\d{2}"),
    re.compile(r"\d{1,2}/\d{1,2}/\d{4}"),
    re.compile(r"\d{1,2}-\d{1,2}-\d{4}"),
    re.compile(rf"(?:{_MONTH_WORDS})\.?\s+\d{{1,2}},?\s+\d{{4}}", re.I),
    re.compile(r"\d{1,2}/\d{1,2}/\d{2}(?!\d)"),
)


def _parse_deadline(text):
    """Best-effort parse of a free-text deadline into a date. None if unparseable.

    Deadlines on real bid pages are almost never a bare date -- they read
    "Due by 12/01/2026 at 2:00 PM", "Bids due December 1, 2026 at 2:00 p.m.",
    "Thursday, December 1, 2026". Parsing only the whole string missed every
    one of those, and the miss was expensive in two directions: the bid lost
    its entire deadline-urgency score (a job due in 3 days ranked like one with
    no deadline at all), and _apply_deadline_status could not tell that an
    expired listing had expired, so it kept showing as open. So find the date
    inside the text first, then parse that.
    """
    if not text:
        return None
    t = " ".join(str(text).split())
    candidates = []
    for pat in _DATE_PATTERNS:
        m = pat.search(t)
        if m:
            candidates.append(m.group(0))
    candidates.append(t)  # whole string, for anything the patterns don't cover
    for cand in candidates:
        # "Sept." -> "Sep" so %b matches; drop trailing punctuation.
        cand = re.sub(r"(?i)\bsept\b", "Sep", cand).replace(".", "").strip().strip(",")
        for fmt in _DEADLINE_FORMATS:
            try:
                return datetime.datetime.strptime(cand, fmt).date()
            except ValueError:
                continue
    return None


_NICHE_KEYWORDS = ("sidewalk", "ada ramp", "ada", "curb", "gutter", "concrete", "flatwork")


def _score_bid(bid):
    """Fit score used to sort each city's bids best-first, instead of
    leaving them in whatever order sources happened to return them. Combines
    deadline urgency, how strongly the text matches our niche (vs. a bid that
    only qualified through the broad construction NAICS codes), and whether
    there's enough info to actually act on it. Higher is better."""
    score = 0.0
    if not _is_open_bid(bid):
        score -= 100
    else:
        d = _parse_deadline(bid.get("deadline"))
        if d:
            days_left = (d - datetime.datetime.now().date()).days
            score -= 50 if days_left < 0 else 0
            score += max(0, 30 - min(days_left, 30)) if days_left >= 0 else 0
    text = f"{bid.get('title', '')} {bid.get('scope', '')}".lower()
    score += sum(2 for k in _NICHE_KEYWORDS if k in text)
    if bid.get("email") or bid.get("phone"):
        score += 3
    if bid.get("value"):
        score += 1
    # Distance, once the radius is wide enough for it to mean anything. Worth
    # real weight but never as much as a deadline: a job closing in three days
    # forty miles out still beats one closing in a month next door, because
    # the near one will still be there tomorrow. Capped so a 125-mile bid is
    # penalised, not buried.
    miles = bid.get("miles")
    if isinstance(miles, (int, float)):
        score -= min(float(miles), 125.0) / 10.0
    return score


# US timezone abbreviations as UTC offsets. A bid page states its deadline in
# local time ("2:00 PM CST"), the server runs UTC, and getting this wrong in
# the wrong direction closes a bid that is still live -- so the table only
# covers abbreviations that are unambiguous in a US bidding context.
_TZ_OFFSETS = {
    "UTC": 0, "GMT": 0,
    "EST": -5, "EDT": -4, "ET": -5,
    "CST": -6, "CDT": -5, "CT": -6,
    "MST": -7, "MDT": -6, "MT": -7,
    "PST": -8, "PDT": -7, "PT": -8,
    "AKST": -9, "AKDT": -8,
    "HST": -10, "HAST": -10,
}

# The latest zone any US bid could be in. Used when a deadline states a time
# but no zone: assuming Hawaii means we only ever call a bid closed once it
# has closed everywhere, which is the safe direction to be wrong in.
_LATEST_US_OFFSET = -10

_TIME_RE = re.compile(
    r"(?<!\d)(\d{1,2})[:.](\d{2})\s*([ap])\.?m\.?\s*([A-Z]{2,4})?", re.I)


def _parse_deadline_moment(text):
    """The exact instant a deadline falls, as an aware UTC datetime.

    None when the text has no time of day -- a date-only deadline is handled
    by the plain date comparison, which correctly leaves "due today" open all
    day because nobody stated an hour.
    """
    d = _parse_deadline(text)
    if not d:
        return None
    m = _TIME_RE.search(" ".join(str(text or "").split()))
    if not m:
        return None
    hour, minute, half, tz = int(m.group(1)), int(m.group(2)), m.group(3).lower(), m.group(4)
    if hour > 12 or minute > 59:
        return None
    if half == "p" and hour != 12:
        hour += 12
    elif half == "a" and hour == 12:
        hour = 0
    offset = _TZ_OFFSETS.get((tz or "").upper(), _LATEST_US_OFFSET)
    local = datetime.datetime(d.year, d.month, d.day, hour, minute,
                              tzinfo=datetime.timezone(
                                  datetime.timedelta(hours=offset)))
    return local.astimezone(datetime.timezone.utc)


def _apply_deadline_status(bid):
    """Force status to Closed if the stated deadline has already passed.

    A full date is checked first. If the deadline text doesn't match any
    known date format (e.g. "FY2024", a stale notice reused from a prior
    year, or other free-text the AI didn't clean up despite instructions),
    fall back to a bare 4-digit year: a deadline field naming a year strictly
    before the current one means this is almost certainly a stale/expired
    listing, not a genuinely open bid, and got missed by the earlier version
    of this check (its own docstring used to say unparseable deadlines were
    "left as-is" -- which is exactly how old bids were slipping through as
    apparently active)."""
    deadline_text = bid.get("deadline")
    d = _parse_deadline(deadline_text)
    if d:
        # A deadline that names an hour is checked to the minute. Without this
        # a bid due "08/19/2026 01:00 AM EDT" read as open for the whole of
        # the 19th, hours after it had shut.
        moment = _parse_deadline_moment(deadline_text)
        if moment is not None:
            if moment < datetime.datetime.now(datetime.timezone.utc):
                bid["status"] = "Closed"
        elif d < datetime.datetime.now().date():
            bid["status"] = "Closed"
        return bid
    m = re.search(r"(?<!\d)(20\d{2})(?!\d)", str(deadline_text or ""))
    if m and int(m.group(1)) < datetime.datetime.now().year:
        bid["status"] = "Closed"
    return bid


STATE_NAME_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
STATE_ABBRS = set(STATE_NAME_TO_ABBR.values())


_COORD_RE = re.compile(r"^(-?\d{1,3}(?:\.\d+)?),\s*(-?\d{1,3}(?:\.\d+)?)$")


def _resolve_center(location):
    """Return {lat, lon, city, state} for a ZIP, 'City, ST' / 'City, State',
    or a raw 'lat, lon' pair (the map-click auto-fill falls back to this
    format when reverse geocoding hasn't resolved a city name yet, or when
    it fails outright — resolving it here instead of failing outright means
    a map click always produces a usable location)."""
    loc = (location or "").strip()
    if not loc:
        return None
    mc = _COORD_RE.match(loc)
    if mc:
        lat, lon = float(mc.group(1)), float(mc.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            got = _reverse_geocode_city(lat, lon)
            city, state = got if got else ("", "")
            return {"lat": lat, "lon": lon, "city": city, "state": state}
    m = re.search(r"\b(\d{5})\b", loc)
    if m:
        g = _geo_from_zip(m.group(1))
        if g:
            return g
    m2 = re.search(r"^(.*?),\s*([A-Za-z]{2})\b", loc)
    if m2 and m2.group(2).upper() in STATE_ABBRS:
        g = _geo_from_city(m2.group(1).strip(), m2.group(2).upper())
        if g:
            return g
    m3 = re.search(r"^(.*?),\s*([A-Za-z][A-Za-z ]+)$", loc)
    if m3:
        st = STATE_NAME_TO_ABBR.get(m3.group(2).strip().lower())
        if st:
            g = _geo_from_city(m3.group(1).strip(), st)
            if g:
                return g
    return None


GEO_MISS_RETRY_HOURS = 24


def _cached_point(cache, key, fetch):
    """Look up a [lat, lon] in `cache`, calling `fetch()` on a miss.

    Failures are remembered only briefly, and a legacy bare None counts as an
    already-expired miss so previously poisoned caches heal by themselves.
    Caching a miss forever is how one transient geocoder outage used to change
    results permanently — for a city, bids there were dropped from every later
    scan; for a ZIP, leads there stopped being distance-checked at all. One
    helper for both so a third copy can't drift off on its own.
    """
    hit = cache.get(key)
    if isinstance(hit, list):
        return hit
    if isinstance(hit, dict):
        try:
            missed_at = datetime.datetime.fromisoformat(hit.get("missed_at", ""))
        except (TypeError, ValueError):
            missed_at = None
        if missed_at and (datetime.datetime.now() - missed_at).total_seconds() \
                < GEO_MISS_RETRY_HOURS * 3600:
            return None
    point = fetch()
    if not point:
        cache[key] = {"missed_at": datetime.datetime.now().isoformat()}
        return None
    cache[key] = [point[0], point[1]]
    return cache[key]


def _city_coords(city, state, db):
    """Geocode a (city, state) to [lat, lon], cached in the JSON db."""
    if not city or not state:
        return None

    def _fetch():
        g = _geo_from_city(city, state)
        return (g["lat"], g["lon"]) if g else None

    return _cached_point(db.setdefault("geo_cache", {}),
                         f"{city.lower()}|{state.upper()}", _fetch)


# ═══════════════════════════════════════════════════════════
# LOCAL SEARCH  (Tavily — AI search API, free 1k/mo, no card)
# ═══════════════════════════════════════════════════════════
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
TAVILY_URL = "https://api.tavily.com/search"

# Recall knobs. All three trade scan time and OpenAI spend for coverage, and
# the right values depend on how the live backend actually performs, so they
# are env-tunable — watch the "funnel" figures in /scan's debug and the wall
# clock, and dial from there. Fetch+extract is entirely I/O-bound, so raising
# PAGE_WORKERS buys pages without proportionally more wall clock.
MAX_PAGES = int(os.environ.get("SCAN_MAX_PAGES", "24"))
# Kept well under the OpenAI burst limits: several towns run at once, each
# with its own pool, so concurrent extractions is this times the town workers.
PAGE_WORKERS = int(os.environ.get("SCAN_PAGE_WORKERS", "6"))
# At most this many pages from any one domain, so a single aggregator can't
# consume the whole per-town budget.
MAX_PAGES_PER_DOMAIN = int(os.environ.get("SCAN_MAX_PAGES_PER_DOMAIN", "4"))
# Towns searched around the radius, on top of the centre. More towns means
# more of a wide radius is actually looked at, at the cost of scan time.
MAX_ANCHOR_TOWNS = int(os.environ.get("SCAN_MAX_ANCHORS", "6"))
# Known-portal towns within radius are a direct fetch each, not a search
# query, so this can be far more generous than MAX_ANCHOR_TOWNS without a
# proportional cost increase -- capped so a scan centered in a very
# densely-covered metro doesn't try to fetch every known town in the state.
# How many already-verified bid pages a scan may read. This is the
# GUARANTEED half of a scan -- no search engine involved, just fetching pages
# the directory already knows serve bids -- so a low cap throws away the most
# reliable coverage there is. 40 was set when the directory held ~750
# agencies; it now holds 4,428, and at 40 a 125-mile scan of Emporia read 40
# of 109 known towns and silently ignored the other 69.
MAX_KNOWN_TOWNS = int(os.environ.get("SCAN_MAX_KNOWN_TOWNS", "120"))
# Raising the cap without a clock is how a dense metro turns into a timeout.
# Towns are ordered closest-first, so stopping on time keeps the nearest ones
# and drops the furthest -- the right ones to lose.
KNOWN_TOWN_BUDGET_SEC = float(os.environ.get("SCAN_KNOWN_TOWN_BUDGET", "40"))
KNOWN_TOWN_WORKERS = int(os.environ.get("SCAN_KNOWN_TOWN_WORKERS", "16"))

# Primary sources: the agency actually letting the work, rather than a site
# re-listing it. Preferred when the budget is tight.
_GOV_DOMAIN_RE = re.compile(r"(?:^|\.)(?:gov|mil)$|(?:^|\.)[a-z]{2}\.us$", re.I)


def _page_domain(url):
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _prioritize_pages(items):
    """Order candidate pages so a fixed budget buys the widest, best coverage.

    BidNet entries keep their existing precedence (they arrive first and carry
    no content), then government domains, then everything else — with a per
    domain cap applied across the whole list so one site can't dominate.
    """
    def rank(pair):
        idx, it = pair
        dom = _page_domain(it.get("url", ""))
        bidnet = 0 if "bidnetdirect.com" in dom else 1
        gov = 0 if _GOV_DOMAIN_RE.search(dom) else 1
        return (bidnet, gov, idx)

    ordered = [it for _, it in sorted(enumerate(items), key=rank)]
    per_domain, head, tail = {}, [], []
    for it in ordered:
        dom = _page_domain(it.get("url", ""))
        per_domain[dom] = per_domain.get(dom, 0) + 1
        # Over-quota pages aren't discarded, just moved behind everything else,
        # so they're still used if the budget outlasts the diverse candidates.
        (head if per_domain[dom] <= MAX_PAGES_PER_DOMAIN else tail).append(it)
    return head + tail

_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


# Tavily bills per search and charges DOUBLE for "advanced" depth. A 50mi scan
# issues ~39 searches, so at advanced depth one scan cost ~78 credits and the
# free 1,000/month allowance was gone in about a dozen scans — after which
# every search silently returns nothing and the scan quietly falls back to
# scraping DuckDuckGo. Depth only affects how hard Tavily works at ranking;
# we use the results as a URL list and fetch the pages ourselves, so basic is
# the right trade and halves the bill.
TAVILY_DEPTH = os.environ.get("TAVILY_SEARCH_DEPTH", "basic")

# A quota or auth failure here is the single most damaging silent failure in
# the product: /scan still returns 200, just with almost nothing in it. Track
# it the same way the DuckDuckGo breaker does so /health can say so out loud.
_tavily_lock = threading.Lock()
_tavily_state = {"ok": 0, "failed": 0, "last_error": "", "last_status": 0}


def _tavily_note(ok, status=0, detail=""):
    with _tavily_lock:
        if ok:
            _tavily_state["ok"] += 1
        else:
            _tavily_state["failed"] += 1
            _tavily_state["last_status"] = status
            _tavily_state["last_error"] = (detail or "")[:200]


def _tavily_health():
    with _tavily_lock:
        st = dict(_tavily_state)
    total = st["ok"] + st["failed"]
    # 402/429/432 are the shapes a spent allowance arrives in.
    st["quota_or_auth_failure"] = st["last_status"] in (401, 402, 429, 432)
    st["failing"] = total > 0 and st["ok"] == 0 and st["failed"] > 0
    return st


# ── Google Programmable Search — the free-tier primary (no scraping) ──
# 100 queries/day free, permanently, off Google's own index. Chosen over
# Tavily as the default because a scan is 12-40 searches: any paid allowance
# disappears fast, and the free tier here is 3x Tavily's while being a
# documented API rather than a scrape.
#
# Two values, both from Google and both required:
#   BRAVE_API_KEY   api-dashboard.search.brave.com -> subscribe to the free
#                   plan (card required as anti-fraud, not charged) and copy
#                   turn ON "Search the entire web", copy the Search engine ID
BRAVE_API_KEY = _env_secret("BRAVE_API_KEY", "")
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
# Brave's free tier allows one request per second.
BRAVE_MIN_INTERVAL = float(os.environ.get("BRAVE_MIN_INTERVAL", "1.1"))

_brave_state = {"ok": 0, "failed": 0, "last_error": "", "last_status": 0}


def _brave_note(ok, status=0, detail=""):
    with _tavily_lock:  # same lock: these counters are read together in /health
        if ok:
            _brave_state["ok"] += 1
        else:
            _brave_state["failed"] += 1
            _brave_state["last_status"] = status
            _brave_state["last_error"] = (detail or "")[:200]


def _brave_health():
    with _tavily_lock:
        st = dict(_brave_state)
    # 429 = the per-second or monthly cap; 401/403 = a bad or unsubscribed key.
    st["quota_or_auth_failure"] = st["last_status"] in (401, 403, 429)
    st["failing"] = st["failed"] > 0 and st["ok"] == 0
    return st


# Brave's free tier is one request per second. Scans fan out across threads,
# so without pacing here several land in the same second and come back 429 --
# which looks exactly like the monthly quota being spent.
_brave_pace_lock = threading.Lock()
_brave_last_call = [0.0]


def _brave_wait_turn():
    with _brave_pace_lock:
        gap = time.time() - _brave_last_call[0]
        if gap < BRAVE_MIN_INTERVAL:
            time.sleep(BRAVE_MIN_INTERVAL - gap)
        _brave_last_call[0] = time.time()


def _brave_search(query, max_results=5):
    """Search via the Brave Search API; returns [{url, content}].

    Like Google's, this returns only the snippet as `content` -- Brave does
    not serve page bodies, so callers fall through to _fetch_text on the URL
    exactly as they already do for a thin result.
    """
    if not BRAVE_API_KEY:
        return []
    params = urllib.parse.urlencode({
        "q": query,
        # Brave caps count at 20.
        "count": max(1, min(int(max_results or 5), 20)),
        "country": "us",
    })
    req = urllib.request.Request(
        f"{BRAVE_URL}?{params}", method="GET",
        headers={"X-Subscription-Token": BRAVE_API_KEY,
                 "Accept": "application/json"})
    _brave_wait_turn()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        print(f"[scan] Brave HTTP {e.code}: {detail[:160]}", flush=True)
        _brave_note(False, e.code, detail)
        _provider_mark_down("brave", e.code)
        if e.code in (401, 403, 429):
            _alert_admin(
                f"Brave Search returning HTTP {e.code} — local bid search degraded",
                "Brave rejected a query. 429 means either the one-per-second "
                "limit or the monthly free credit is spent; 401/403 means the "
                "key is wrong or the subscription lapsed. Until it clears, "
                "scans fall back to Tavily (if configured) and then to "
                f"scraping DuckDuckGo.\n\nResponse: {detail}")
        return []
    except Exception as ex:
        print(f"[scan] Brave error: {ex}", flush=True)
        _brave_note(False, 0, str(ex))
        return []
    _brave_note(True)
    _provider_clear("brave")
    items = ((data.get("web") or {}).get("results")) or []
    print(f"[scan] Brave: {len(items)} results for {query!r}", flush=True)
    return [{"url": it.get("url") or "",
             "content": it.get("description") or it.get("title") or ""}
            for it in items if it.get("url")][:max_results]


def _tavily_search(query, max_results=5):
    """Search via Tavily; returns [{url, content}]."""
    if not TAVILY_API_KEY:
        print("[scan] no TAVILY_API_KEY set", flush=True)
        return []
    body = json.dumps({
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": TAVILY_DEPTH,
        "max_results": max_results,
        "include_raw_content": True,
    }).encode("utf-8")
    req = urllib.request.Request(TAVILY_URL, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TAVILY_API_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        print(f"[scan] Tavily HTTP {e.code}: {detail}", flush=True)
        _tavily_note(False, e.code, detail)
        # 402/432 are Tavily's "out of credit" codes, which no retry fixes.
        _provider_mark_down("tavily", 429 if e.code in (402, 432) else e.code)
        if e.code in (401, 402, 429, 432):
            _alert_admin(
                f"Tavily returning HTTP {e.code} — local bid search is degraded",
                "Tavily rejected a search. 402/429/432 normally means the monthly "
                "credit allowance is spent; 401 means the key is wrong. While this "
                "lasts, /scan falls back to scraping DuckDuckGo, which from a shared "
                "Render IP often returns nothing — so scans will look like the area "
                f"simply has no bids.\n\nResponse: {detail}",
            )
        return []
    except Exception as ex:
        print(f"[scan] Tavily error: {ex}", flush=True)
        _tavily_note(False, 0, str(ex))
        return []
    _tavily_note(True)
    _provider_clear("tavily")
    results = data.get("results") or []
    print(f"[scan] Tavily: {len(results)} results for {query!r}", flush=True)
    out = []
    for r in results:
        url = r.get("url") or ""
        if url:
            out.append({"url": url,
                        "content": r.get("raw_content") or r.get("content") or ""})
    return out


# A provider that has just answered 401/403/429 will answer the same way for
# every remaining query in this scan. Calling it twelve more times costs a
# round trip each -- and for Brave, 1.1s of pacing each -- before falling
# through to the same fallback every time. Once it has refused for a reason
# that will not change in the next minute, stop asking until the cooldown.
_PROVIDER_COOLDOWN_SEC = float(os.environ.get("SEARCH_PROVIDER_COOLDOWN", "300"))
_provider_down_until = {}
_provider_lock = threading.Lock()


def _provider_is_down(name):
    with _provider_lock:
        return time.time() < _provider_down_until.get(name, 0)


def _provider_mark_down(name, status):
    """Bench a provider after a refusal that a retry will not fix."""
    if status not in (401, 403, 429):
        return
    with _provider_lock:
        _provider_down_until[name] = time.time() + _PROVIDER_COOLDOWN_SEC
    print(f"[scan] {name} benched for {_PROVIDER_COOLDOWN_SEC:.0f}s "
          f"after HTTP {status}", flush=True)


def _provider_clear(name):
    with _provider_lock:
        _provider_down_until.pop(name, None)


def _web_search(query, max_results=6):
    """One search, whichever provider is available. Returns ([{url, content}],
    used_scraper).

    Order is cheapest-reliable first: Brave's free tier (a real API, ~1,000
    queries a month on the free credit), then Tavily if a key is configured
    and still has credit, then scraping DuckDuckGo as the last resort.
    Callers only need `used_scraper` so they can apply DDG's pacing delay and
    skip it entirely otherwise -- the old code inferred that from "did Tavily
    return nothing", which stopped being true the moment there was more than
    one keyed provider.

    Google Programmable Search used to lead this chain. Google is deprecating
    the Custom Search JSON API and no longer lets a project enable it, so the
    integration was removed rather than left to fail 403 on every query.
    """
    if BRAVE_API_KEY and not _provider_is_down("brave"):
        results = _brave_search(query, max_results=max_results)
        if results:
            return results, False
    if TAVILY_API_KEY and not _provider_is_down("tavily"):
        results = _tavily_search(query, max_results=max_results)
        if results:
            return results, False
    return _ddg_search(query), True


# ── DuckDuckGo with a browser "disguise" — last-resort local search (no key) ──
_DDG_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def _ddg_headers(ua):
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://duckduckgo.com/",
        "Origin": "https://duckduckgo.com",
        "Content-Type": "application/x-www-form-urlencoded",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Ch-Ua": '"Chromium";v="125", "Not.A/Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    }


def _parse_ddg(html):
    """Pull real result URLs out of a DuckDuckGo HTML results page."""
    out, seen = [], set()
    for m in re.finditer(r'uddg=([^&"\']+)', html):
        u = urllib.parse.unquote(m.group(1))
        if u.startswith("http") and u not in seen:
            seen.add(u)
            out.append(u)
    if out:
        return out
    for m in re.finditer(r'href="(https?://[^"]+)"', html):
        u = m.group(1)
        if "duckduckgo.com" in u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


# Circuit breaker: DuckDuckGo scraping is the one search path with no API
# key, so when TAVILY_API_KEY isn't set it's the *only* local search source.
# If it starts getting blocked (layout change, IP block on Render's shared
# dyno IP) every scan would silently return zero local bids with nothing in
# the logs pointing at why. This counter + the check in /scan turns that into
# a proactive admin email instead of a customer complaint.
_ddg_lock = threading.Lock()
_ddg_fail_streak = 0
DDG_TRIP_THRESHOLD = 8
# Pause between consecutive scraped searches, to stay unobtrusive.
DDG_QUERY_PAUSE = float(os.environ.get("SCAN_DDG_PAUSE", "0.5"))


def _ddg_note_result(found):
    global _ddg_fail_streak
    with _ddg_lock:
        _ddg_fail_streak = 0 if found else _ddg_fail_streak + 1


def _ddg_is_degraded():
    with _ddg_lock:
        return _ddg_fail_streak >= DDG_TRIP_THRESHOLD


def _ddg_search(query, count=6):
    """Scrape DuckDuckGo with rotating, browser-like headers. Returns [{url, content}]."""
    ua = random.choice(_DDG_UAS)
    for endpoint in ("https://html.duckduckgo.com/html/",
                     "https://lite.duckduckgo.com/lite/"):
        try:
            body = urllib.parse.urlencode({"q": query, "kl": "us-en"}).encode()
            req = urllib.request.Request(endpoint, data=body, headers=_ddg_headers(ua))
            with urllib.request.urlopen(req, timeout=20) as resp:
                html = resp.read().decode("utf-8", "ignore")
            found = _parse_ddg(html)
            if found:
                _ddg_note_result(True)
                return [{"url": u, "content": ""} for u in found[:count]]
            print(f"[scan] DDG no links from {endpoint} for {query!r}", flush=True)
        except Exception as ex:
            print(f"[scan] DDG error ({endpoint}): {ex}", flush=True)
    _ddg_note_result(False)
    return []


# A URL we know is real is worth waiting for. A guessed one is not: most
# speculative probes are 404s or dead hosts, and at the full timeout a handful
# of them will spend the entire request budget before the search path even
# starts. That is not a hypothetical — it is what made every scan return zero.
FETCH_TIMEOUT = int(os.environ.get("SCAN_FETCH_TIMEOUT", "18"))
PROBE_TIMEOUT = int(os.environ.get("SCAN_PROBE_TIMEOUT", "6"))
PORTAL_WORKERS = int(os.environ.get("SCAN_PORTAL_WORKERS", "6"))
PORTALS_PER_TOWN = int(os.environ.get("SCAN_PORTALS_PER_TOWN", "8"))
# Contact details live on each individual posting, so reading them costs one
# fetch per bid. Bounded per portal, and given the shorter probe timeout: a
# missing phone number degrades a lead, a blown request budget loses every one.
DETAIL_PAGES_PER_PORTAL = int(os.environ.get("SCAN_DETAIL_PAGES", "10"))
DETAIL_WORKERS = int(os.environ.get("SCAN_DETAIL_WORKERS", "6"))


# Who we say we are when reading a government bid page. This is the vast
# majority of the traffic this service generates, and it goes to public
# agencies, so it says plainly what it is.
CRAWLER_UA = ("CurbCallBot/1.0 (+https://curbcallpro.com; "
              "concrete bid aggregator; contact support@curbcallpro.com)")


def _page_headers():
    """Complete headers, honestly identified.

    This used to send a random real-browser User-Agent, on the reasoning that
    municipal sites sit behind bot filters that reject on header fingerprint.
    Measured on 160 live portals from the directory: 157 answer a browser
    agent and 158 answer this one. The impersonation was buying nothing --
    what actually gets through a filter is sending the COMPLETE header set
    below, not lying about the User-Agent string.

    Two other places still rotate browser agents and are deliberately left
    alone, because they are different questions rather than oversights:
    _ddg_search scrapes DuckDuckGo, and _bidnet_direct_urls queries BidNet
    Direct's public search after a documented 403 from a bare fetch. Both are
    third-party services rather than public agencies, and changing either
    would most likely just break it. Worth a deliberate decision, not a
    drive-by edit.
    """
    return {
        "User-Agent": CRAWLER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


# ── robots.txt ──────────────────────────────────────────────────────────────
# The scanner read every page it could reach and never asked. That is not a
# legal problem -- these are public bid notices -- but it is a norm we should
# not be quietly breaking on a paying product, and a site that catches us
# doing it blocks the IP for every customer, not just the one scan.
#
# Measured before switching on: of 150 live portals sampled from the
# directory, 147 allow the bid page and 3 do not. Two percent is a cheap
# price. RESPECT_ROBOTS=0 turns it off if it ever proves otherwise.
#
# Fail-open by design. An unreachable robots.txt is not a refusal, and several
# state sites serve the bid page fine while blocking /robots.txt itself --
# treating that as "disallowed" would drop working sources for no reason.
RESPECT_ROBOTS = os.environ.get("RESPECT_ROBOTS", "1") != "0"
ROBOTS_TIMEOUT = float(os.environ.get("ROBOTS_TIMEOUT", "6"))
_robots_cache = {}
_robots_lock = threading.Lock()


def _robots_allows(url):
    if not RESPECT_ROBOTS:
        return True
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return True
    if not host:
        return True
    with _robots_lock:
        rp = _robots_cache.get(host, "miss")
    if rp == "miss":
        rp = None
        try:
            robots_url = "%s://%s/robots.txt" % (parts.scheme or "https",
                                                 parts.netloc)
            req = urllib.request.Request(robots_url, headers=_page_headers())
            with urllib.request.urlopen(req, timeout=ROBOTS_TIMEOUT) as resp:
                body = resp.read(200000).decode("utf-8", "replace")
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(body.splitlines())
            rp = parser
        except Exception:
            rp = None      # unreadable -> not a refusal, see note above
        with _robots_lock:
            _robots_cache[host] = rp
    if rp is None:
        return True
    try:
        return rp.can_fetch(_page_headers().get("User-Agent", "*"), url)
    except Exception:
        return True


def _fetch_page(url, timeout=None):
    """Fetch a page. Returns (text, outcome) where outcome explains a failure.

    The outcome matters: a 403 and an empty page were previously
    indistinguishable, both arriving as "" — so a portal being actively blocked
    looked exactly like a town with no bids.
    """
    if not _robots_allows(url):
        return "", "robots_disallow"
    try:
        req = urllib.request.Request(url, headers=_page_headers())
        with urllib.request.urlopen(req, timeout=timeout or FETCH_TIMEOUT) as resp:
            return resp.read(800000).decode("utf-8", "ignore"), "ok"
    except urllib.error.HTTPError as e:
        return "", f"http_{e.code}"
    except Exception as ex:
        name = type(ex).__name__.lower()
        return "", "timeout" if "timeout" in name else "unreachable"


def _resolve_bid_url(ai_url, page_url, source_text=""):
    """Which link a bid card should actually open.

    Two things were sending contractors to 404s:

    1. `b.setdefault("url", page_url)` never fired. The extraction prompt says
       'Use "" for any missing field', so the model returns "url": "" -- the
       key EXISTS, so setdefault left the empty string in place and the card
       rendered no link at all.

    2. Worse, when the model did supply a URL it was usually invented.
       _fetch_text strips every tag before the text reaches the model, so it
       never sees an href -- it only sees visible words. Asked for a "url"
       anyway, it reconstructs a plausible-looking one from the domain and a
       guessed path. Plausible-looking and wrong is exactly a 404.

    So the model's URL is trusted only when it appears verbatim in the text
    the model was actually shown -- i.e. the page printed it as visible text.
    Anything else falls back to the page we fetched the bid from, which is
    known-reachable and at worst one click from the real notice.
    """
    page_url = (page_url or "").strip()
    s = (ai_url or "").strip()
    if not s:
        return page_url
    low = s.lower()
    if low.startswith(("http://", "https://")):
        # Verbatim in the source text means the page really published it.
        return s if (source_text and s in source_text) else page_url
    if s.startswith("/") and page_url:
        # A root-relative path is a real path the model read off the page, not
        # a fabricated absolute URL -- resolving it against the page is safe.
        return urllib.parse.urljoin(page_url, s)
    # mailto:, javascript:, bare words, anything else: not a bid link.
    return page_url


def _html_to_text(raw):
    """Visible text from html. Split out of _fetch_text so a caller that
    needs the markup as well can fetch once and derive both."""
    if not raw:
        return ""
    raw = _SCRIPT_RE.sub(" ", raw)
    raw = _TAG_RE.sub(" ", raw)
    raw = re.sub(r"&[a-z#0-9]+;", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _fetch_text(url, timeout=None):
    return _html_to_text(_fetch_page(url, timeout=timeout)[0])


# ── BidNet Direct: a real public source, queried directly (no key) ──
# Unlike `site:bidnetdirect.com` search-engine queries (which only surface
# whatever DDG/Tavily happened to index), this hits BidNet Direct's own
# public "Open Solicitations" search directly by state + keyword. Verified
# by hand: no login wall, no JS rendering required -- a plain scripted GET
# with realistic browser headers gets a normal 200 with real server-rendered
# results (the earlier 403 seen from a bare fetch was header-fingerprint
# bot-blocking, the same class of thing DDG scraping already works around
# below, not a real access restriction). `location` is BidNet's own numeric
# state code, scraped once from their filter dropdown.
BIDNET_LOCATION_CODES = {
    "AL": 19, "AK": 25, "AZ": 31, "AR": 37, "CA": 43, "CO": 49, "CT": 55,
    "DE": 61, "DC": 67, "FL": 73, "GA": 79, "HI": 85, "ID": 91, "IL": 97,
    "IN": 103, "IA": 109, "KS": 115, "KY": 121, "LA": 127, "ME": 133,
    "MD": 139, "MA": 145, "MI": 151, "MN": 157, "MS": 163, "MO": 169,
    "MT": 175, "NE": 181, "NV": 187, "NH": 193, "NJ": 199, "NM": 205,
    "NY": 211, "NC": 217, "ND": 223, "OH": 229, "OK": 235, "OR": 241,
    "PA": 247, "RI": 253, "SC": 259, "SD": 265, "TN": 271, "TX": 277,
    "UT": 283, "VT": 289, "VA": 295, "WA": 301, "WV": 307, "WI": 313,
    "WY": 319,
}
BIDNET_KEYWORDS = ("sidewalk", "curb ramp")
_BIDNET_HREF_RE = re.compile(r'href="(/[a-z0-9][a-z0-9-]*/solicitations/open-bids/[^"]+)"')


def _bidnet_direct_urls(keywords, state_abbr, max_results=5):
    """Return up to max_results detail-page URLs from BidNet Direct's public
    search for this state + keyword. Returned in the same {"url","content"}
    shape _ddg_search/_tavily_search use, so callers can merge them straight
    into the existing fetch+AI-extract+place pipeline instead of needing a
    separate code path."""
    code = BIDNET_LOCATION_CODES.get(state_abbr)
    if not code:
        return []
    try:
        qs = urllib.parse.urlencode({
            "keywords": keywords, "location": code,
            "solSearchStatus": "openSolicitationsTab",
        })
        req = urllib.request.Request(
            f"https://www.bidnetdirect.com/public/solicitations/open?{qs}",
            headers={
                "User-Agent": random.choice(_DDG_UAS),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
        with urllib.request.urlopen(req, timeout=20) as resp:
            html_text = resp.read().decode("utf-8", "ignore")
    except Exception as ex:
        print(f"[scan] BidNet Direct search error ({state_abbr}/{keywords!r}): {ex}", flush=True)
        return []
    out, seen = [], set()
    for m in _BIDNET_HREF_RE.finditer(html_text):
        href = m.group(1).replace("&amp;", "&")
        url = "https://www.bidnetdirect.com" + href
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "content": ""})
        if len(out) >= max_results:
            break
    return out


# ═══════════════════════════════════════════════════════════
# FEDERAL SEARCH  (SAM.gov)
# ═══════════════════════════════════════════════════════════
SAM_API_KEY = os.environ.get("SAM_API_KEY", "")
# api.data.gov, not api.sam.gov. The previous default,
# api.sam.gov/prod/opportunities/v2/search, answers 404 -- every path under
# that host does, including the ones the docs used to name. So federal bids
# could not have worked even with a key set, which is consistent with there
# never having been a funnel counter for them. api.data.gov/sam/... is live:
# it answers 429 OVER_RATE_LIMIT to the shared DEMO_KEY, which is a rate
# limit on a real endpoint rather than a wrong address.
SAM_SEARCH_URL = os.environ.get(
    "SAM_SEARCH_URL", "https://api.sam.gov/opportunities/v2/search")
SCAN_WINDOW_DAYS = int(os.environ.get("SCAN_WINDOW_DAYS", "60"))

# Title keywords, kept as the fallback for a notice with no NAICS code on it.
#
# The history matters: an earlier version OR'd this with "NAICS starts with
# 236/237/238", which is every construction trade there is -- electricians,
# roofers, painters -- and let in a flood. Dropping to keywords-only fixed
# that but overcorrected, because a federal title is written for a
# contracting file, not a search box. Three real jobs found in one probe --
# "Whiteman AFB - FY27 Airfield Pavement", "Ft Leavenworth Asphalt Pavement
# Rehabilitation", "NICO Interpretive Waysides and Walk Improvements" -- are
# all our trade and none of them match a single term below as it stood.
#
# The answer is not a longer keyword list. It is the six-digit NAICS code,
# which states the trade outright: see federal_bids.CONCRETE_NAICS. Keywords
# now only decide notices that arrive without one.
CONSTRUCTION_KEYWORDS = (
    "sidewalk", "ada ramp", "curb ramp", "curb and gutter", "curb & gutter",
    "concrete", "flatwork", "pedestrian ramp", "pavement", "paving",
    "resurfac", "walkway", "hardscape", "curb replacement",
)


def _opp_naics(opp):
    """The NAICS code on a notice, in either transport's spelling."""
    code = opp.get("naicsCode") or opp.get("naics") or ""
    if isinstance(code, list) and code:
        first = code[0]
        code = (first.get("code") if isinstance(first, dict) else first) or ""
        if isinstance(code, list) and code:
            code = code[0]
    return str(code or "").strip()


def _is_construction(opp):
    """True if this federal notice is our trade.

    NAICS first, because it is an assertion rather than an inference: 238110
    IS "poured concrete foundation and structure contractor". A title only
    hints. Keywords stay for notices posted without a code.
    """
    naics = _opp_naics(opp)
    if naics:
        return naics in federal_bids.CONCRETE_NAICS
    title = (opp.get("title") or "").lower()
    return any(k in title for k in CONSTRUCTION_KEYWORDS)


# How far back to ask SAM for postings. NOT SCAN_WINDOW_DAYS, which is 60:
# that window is about how fresh a municipal listing should be, and applying
# it here hid every solicitation posted more than two months ago no matter how
# far in the future its response date was. Federal construction work is
# routinely posted long before it closes -- "Little Rock AFB Base Pavements
# IDIQ FY25" is exactly that shape -- so the posting date is the wrong thing
# to filter on. SAM caps the range at one year, so this asks for all of it and
# lets _is_open_bid decide what is still biddable.
# 180, not 365. The year-wide window is what made SAM slow enough to time
# out: a Woodford County scan logged two failed keyed requests, and because
# they were timeouts rather than rejections they burned the full SAM_TIMEOUT
# each -- 30 seconds of a 33-second federal stage that produced two bids.
#
# 60 was the original and far too short: it hid every solicitation posted
# more than two months ago however far ahead its response date was. But the
# other direction has a cost too, and six months already covers the long
# IDIQs that motivated widening it. An open federal solicitation posted more
# than half a year ago is rare enough not to be worth a timeout on every
# scan.
SAM_WINDOW_DAYS = int(os.environ.get("SAM_WINDOW_DAYS", "180"))
# Per request, not per scan. With a NAICS filter applied server-side a state
# returns a handful of rows, so this is headroom rather than a page size to
# work through. It replaces limit=1000, which downloaded a year of every
# federal solicitation in the state and filtered locally.
SAM_PAGE_LIMIT = int(os.environ.get("SAM_PAGE_LIMIT", "100"))
# 60 seconds times five trade codes times four states is not a timeout, it is
# an outage. A scan has a time budget and SAM is one source inside it.
# Six, not fifteen. Two timeouts at fifteen seconds were thirty seconds of
# every scan spent failing. SAM answers in well under a second when it
# answers at all, so a long timeout does not buy patience -- it buys a longer
# wait for the same failure.
SAM_TIMEOUT = int(os.environ.get("SAM_TIMEOUT", "6"))


# What SAM said last time, so a failure can be diagnosed from /health instead
# of from a scan funnel that only says "failed". The API key is NEVER in here:
# it travels as a query parameter, so anything derived from the URL is scrubbed
# before it is stored.
# Why a SAM request failed, kept where a restart cannot erase it.
#
# This was a bare module dict. The failure COUNTER lives in the KV store and
# survives, so /health would report consecutive_failures: 9 beside
# last_status: None -- the alarm outliving the only field that explains it.
# Render restarts on every deploy and after idling, so in practice the
# diagnosis was gone before anyone looked, which is most of why federal bids
# were broken for days without anyone being able to say what was wrong.
_SAM_HEALTH_KEY = "bidcaller:sam_health"
_sam_health = {"last_status": None, "last_error": "", "at": ""}


def _sam_health_note(status, error=""):
    """Record the outcome of a SAM request, in memory and durably."""
    at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _sam_health["last_status"] = status
    _sam_health["last_error"] = error
    # Stamped in memory too. Without this the live path reported a status with
    # no time beside it -- the exact gap this whole change exists to close,
    # reintroduced one layer down and visible the moment a real scan ran.
    _sam_health["at"] = at
    try:
        kv_backend.set(_SAM_HEALTH_KEY, {
            "last_status": status, "last_error": error, "at": at})
    except Exception:
        pass   # In-memory is still better than nothing.


def _sam_health_read():
    """The last known outcome, preferring whatever survived a restart.

    In-memory wins when it has something: it is this process's own truth and
    cannot be staler than the store.
    """
    if _sam_health["last_status"] is not None:
        return {"last_status": _sam_health["last_status"],
                "last_error": _sam_health["last_error"],
                "at": _sam_health.get("at", "")}
    try:
        blob = kv_backend.get(_SAM_HEALTH_KEY, None) or {}
    except Exception:
        blob = {}
    return {"last_status": blob.get("last_status"),
            "last_error": blob.get("last_error", ""),
            "at": blob.get("at", "")}


def _sam_scrub(text):
    """Remove the api_key from anything about to be shown or logged."""
    return re.sub(r"(api_key=)[^&\s]+", r"\1<redacted>", str(text or ""))


def _sam_fetch(state, ncode=None, ccode=None, timeout=None, limit=None):
    """One page of opportunities for a state, narrowed server-side.

    Returns None if the request itself failed, [] if it genuinely matched
    nothing. That distinction is the point: this used to end with
    `(data or {}).get(...) or []`, so a rejected key, a timeout and an empty
    state were the same empty list and a broken source looked like a quiet
    one.

    ncode/ccode matter as much. This asked for limit=1000 with no trade
    filter over a year of postings, then filtered locally -- every federal
    solicitation of any kind, for each of the three or four states a
    125-mile scan touches. While the key was invalid that cost nothing
    because it 403'd instantly; the moment a working key was set, scans
    started timing out. SAM can filter by NAICS and PSC itself, so ask it to.
    """
    if not SAM_API_KEY:
        return None
    today = datetime.datetime.now()
    params = {
        "api_key": SAM_API_KEY,
        "postedFrom": (today - datetime.timedelta(days=SAM_WINDOW_DAYS)).strftime("%m/%d/%Y"),
        "postedTo": today.strftime("%m/%d/%Y"),
        "limit": str(limit or SAM_PAGE_LIMIT),
        "offset": "0",
    }
    # No state means the whole country in one request. Only the refresh job
    # asks for that: six national queries cover every state, where filtering
    # per state costs six requests PER state and is what made the live path
    # unaffordable inside a scan.
    if state:
        params["state"] = state
    if ncode:
        params["ncode"] = ncode
    if ccode:
        params["ccode"] = ccode
    url = SAM_SEARCH_URL + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/json",
                          "User-Agent": CRAWLER_UA})
        with urllib.request.urlopen(req, timeout=timeout or SAM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # The status is the whole diagnosis: 403 is a rejected key, 404 a
        # wrong endpoint, 429 a rate limit. "failed" alone sent us guessing.
        _sam_health_note(e.code, _sam_scrub(str(e)))
        return None
    except Exception as e:
        # NOT None. None is reserved for "no request has been made"; a
        # timeout that reported None was read as a key problem for a whole
        # round of diagnosis when the key was fine and SAM was just slow.
        _sam_health_note(
            "timeout" if isinstance(e, (socket.timeout, TimeoutError))
            or "timed out" in str(e).lower() else "error",
            _sam_scrub("%s: %s" % (type(e).__name__, e)))
        return None
    _sam_health_note(200, "")
    return data.get("opportunitiesData") or []


def _sam_notice_url(notice_id):
    """The public sam.gov page for a notice id, or "" if there isn't one."""
    nid = str(notice_id or "").strip()
    # Ids are hex; anything else is not something to build a URL from.
    return f"https://sam.gov/opp/{nid}/view" if re.fullmatch(r"[0-9a-fA-F]{8,}", nid) else ""


def _normalize_opp(opp):
    poc_list = opp.get("pointOfContact") or []
    poc = poc_list[0] if poc_list else {}
    pop = opp.get("placeOfPerformance") or {}
    city = ((pop.get("city") or {}).get("name")) or ""
    # SAM states the performance state outright; pass it along so the bid is
    # geocoded against that rather than assumed to be in the centre's state.
    perf_state = (((pop.get("state") or {}).get("code")) or "").upper()
    deadline = (opp.get("responseDeadLine") or "")[:10]
    is_open = (opp.get("active") or "").strip().lower() == "yes"
    agency = opp.get("fullParentPathName") or opp.get("organizationName") or ""
    scope = " · ".join([b for b in ("Federal", opp.get("type") or "", agency) if b])
    bid = {
        "title": opp.get("title") or "Untitled Opportunity",
        "scope": scope,
        "status": "Open" if is_open else "Closed",
        "deadline": deadline,
        "contact": poc.get("fullName") or "",
        "email": poc.get("email") or "",
        "phone": poc.get("phone") or "",
        "value": "",
        # uiLink is not always present, but every notice has a noticeId and
        # sam.gov's public URL for one is stable. Without this the card
        # renders no link at all -- all the detail and nowhere to go.
        "url": opp.get("uiLink") or _sam_notice_url(opp.get("noticeId")),
    }
    _apply_deadline_status(bid)
    return bid, city, perf_state


# ═══════════════════════════════════════════════════════════
# AI EXTRACTION (now also returns a "city" so we can radius-filter)
# ═══════════════════════════════════════════════════════════
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def _ai_extract(area, text):
    if not OPENAI_API_KEY:
        return None
    prompt = (
        f"You extract SIDEWALK, ADA RAMP, CURB & GUTTER, and CONCRETE FLATWORK/"
        f"PAVING bid leads for a niche concrete contractor near {area}.\n\n"
        "From the website text below (which may be a full bid listing page "
        "covering many unrelated trades), extract ONLY bids, RFPs, RFQs, or "
        "solicitations where sidewalk construction/repair/replacement, ADA curb "
        "ramps, curb-and-gutter work, or concrete flatwork/paving is clearly "
        "part of the stated scope. A bid with mixed scope items still counts if "
        "concrete/sidewalk/curb work is explicitly one of them.\n\n"
        "Do NOT extract bids that are only about roofing, HVAC, plumbing, "
        "electrical, general building construction, demolition, painting, "
        "landscaping, or other unrelated trades -- even if they appear on the "
        "same page as real matches, and even though they're all technically "
        "\"construction.\" When in doubt, leave it out.\n\n"
        "A contract that has ALREADY BEEN AWARDED is not a lead. News stories "
        "reporting that a council awarded a contract, named a winning bid, or "
        "selected a low bidder describe work that is gone -- mark any such item "
        "\"Awarded\", never \"Open\", however recent it is.\n\n"
        "Respond ONLY with a JSON array. Each item has keys: \"title\", \"scope\", "
        "\"status\" (\"Open\", \"Closed\" or \"Awarded\"), \"deadline\", \"contact\", \"email\", "
        "\"phone\", \"value\", \"url\", \"city\". \"deadline\" must be an absolute "
        "calendar date exactly as the page states it (e.g. \"July 24, 2026\" or "
        "\"12/01/2026\"); NEVER a countdown or relative phrase like \"in 8 days\" "
        "or \"next week\" -- use \"\" if the page only gives one of those. "
        "\"city\" is the US city where the work "
        "will be performed, exactly as written in the text; if the location is not clearly "
        "stated, use \"\" and do NOT guess. \"value\" is a dollar amount only if stated. "
        "Use \"\" for any missing field. If no real bids, return []. "
        "No markdown, no text outside the array.\n\n"
        f"WEBSITE TEXT:\n{text[:16000]}"
    )
    body = json.dumps({
        "model": OPENAI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = data["choices"][0]["message"]["content"].strip()
        s, e = out.find("["), out.rfind("]")
        if s != -1 and e != -1 and e > s:
            out = out[s:e + 1]
        bids = json.loads(out)
        if not isinstance(bids, list):
            return []
        # Belt and braces: the prompt now says an awarded contract is Closed,
        # but the model returning "Open" (or nothing) on an award story is
        # exactly the failure that shipped, so decide it here too.
        if _looks_awarded(text):
            for b in bids:
                if isinstance(b, dict) and _is_open_bid(b):
                    b["status"] = "Awarded"
        closed_page = _page_declares_closed(text, len(bids))
        for b in bids:
            if not isinstance(b, dict):
                continue
            b["deadline"] = _clean_deadline(b.get("deadline"))
            if closed_page and _is_open_bid(b):
                b["status"] = "Closed"
        return bids
    except Exception:
        return None


@app.route("/extract", methods=["POST"])
def extract():
    data = request.get_json(force=True, silent=True) or {}
    if not _license_is_active(data.get("key", ""), data.get("device_id", "")):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    text = data.get("text") or ""
    if not text.strip():
        return jsonify({"ok": True, "bids": []})
    bids = _ai_extract(data.get("city") or "Unknown", text)
    if bids is None:
        return jsonify({"ok": False, "reason": "ai_error"}), 500
    return jsonify({"ok": True, "bids": bids})


# ═══════════════════════════════════════════════════════════
# AI-DRAFTED BID PROPOSALS
# Turns a bid the user is looking at into a ready-to-edit cover-letter-style
# proposal draft, personalized with the contractor's own company info (sent
# from the client — nothing is stored server-side). Same license gate and
# same OpenAI key as /scan and /extract, no new secrets needed.
# ═══════════════════════════════════════════════════════════
def _ai_draft_proposal(bid, company):
    if not OPENAI_API_KEY:
        return None
    co_name = (company.get("name") or "").strip() or "[Your Company Name]"
    co_contact = (company.get("contact") or "").strip() or "[Your Name]"
    co_phone = (company.get("phone") or "").strip() or "[Your Phone]"
    co_email = (company.get("email") or "").strip() or "[Your Email]"
    co_specialty = (company.get("specialty") or "").strip() or \
        "sidewalk, ADA ramp, and curb & gutter concrete work"

    prompt = (
        "You are an experienced construction estimator writing a bid proposal "
        "cover letter for a small concrete contracting company responding to a "
        "public bid or RFP. Write a professional, concise, ready-to-send "
        "proposal letter body (no subject line, no markdown) that:\n"
        "- Opens by referencing the specific project by name\n"
        "- States the company's interest and relevant experience in "
        f"{co_specialty}\n"
        "- Briefly addresses the stated scope of work\n"
        "- Notes willingness to meet the stated deadline (if one is given)\n"
        "- Requests any plan documents, addenda, or walkthrough details needed "
        "to submit a formal quote\n"
        "- Closes with the contact information provided\n"
        "Keep it under 300 words. Do NOT invent specific dollar amounts, "
        "license numbers, bond amounts, or past project names — omit those "
        "details rather than making them up.\n\n"
        f"PROJECT TITLE: {bid.get('title', '')}\n"
        f"SCOPE: {bid.get('scope', '')}\n"
        f"DEADLINE: {bid.get('deadline', '')}\n"
        f"LOCATION: {bid.get('city', '')}\n\n"
        f"COMPANY NAME: {co_name}\n"
        f"CONTACT PERSON: {co_contact}\n"
        f"PHONE: {co_phone}\n"
        f"EMAIL: {co_email}\n"
    )
    body = json.dumps({
        "model": OPENAI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except Exception as ex:
        print(f"[draft-proposal] error: {ex}", flush=True)
        return None


@app.route("/draft-proposal", methods=["POST"])
def draft_proposal():
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key", "")
    device = data.get("device_id", "")
    supabase_token = data.get("supabase_token", "")
    if not _license_is_active(key, device, supabase_token):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "reason": "ai_unavailable"})

    bid = data.get("bid") or {}
    company = data.get("company") or {}
    if not (bid.get("title") or "").strip():
        return jsonify({"ok": False, "reason": "no_bid"})

    draft = _ai_draft_proposal(bid, company)
    if draft is None:
        return jsonify({"ok": False, "reason": "ai_error"}), 500
    return jsonify({"ok": True, "draft": draft})


# ═══════════════════════════════════════════════════════════
# /scan  —  LOCAL (Brave + AI) + FEDERAL (SAM), radius-filtered
# ═══════════════════════════════════════════════════════════
def _enrich_from_detail_pages(rows, stats=None, lock=None):
    """Fill in contact / email / phone by reading each posting's own page.

    A listing page carries titles and dates and nothing a contractor can act
    on. Every bid coming out of the structured path used to arrive with blank
    contact fields, so the app's Email and Call buttons had nothing to attach
    to and the only way to reach the buyer was to go find the posting by hand.

    Mutates `rows` in place. Failure is per-row and silent by design: a posting
    that won't load costs its own contact details, never the bid itself.
    """
    targets = [r for r in (rows or []) if r.get("url")]
    if not targets:
        return

    def _one(row):
        try:
            page, outcome = _fetch_page(row["url"], timeout=PROBE_TIMEOUT)
            if outcome != "ok" or not page:
                # Distinguished from "read it, found nothing new". A live scan
                # enriched 3 of 16 postings where a sandbox sample managed
                # 88%, and those are very different problems: one is the
                # posting pages being unreachable from the server, the other
                # is the extractors not matching what is on them. Guessing
                # between them wasted a round already.
                row["_fetch_failed"] = True
                return False
            found = bid_sources.parse_contact(page)
        except Exception:
            row["_fetch_failed"] = True
            return False
        got = False
        for field in ("contact", "email", "phone"):
            if found.get(field) and not row.get(field):
                row[field] = found[field]
                got = True
        # The posting also carries the closing date and the real scope, where
        # a listing row had only a title. The deadline matters most: without
        # one a bid gets no urgency ranking and cannot be recognised as
        # expired, so last year's programme shows as open indefinitely.
        if not str(row.get("deadline") or "").strip():
            due = bid_sources.detail_deadline(page)
            if due:
                row["deadline"] = due
                got = True
        if not row.get("scope"):
            body = bid_sources.detail_scope(page)
            if body:
                row["scope"] = body
        # The posting sometimes states an engineer's estimate. The structured
        # paths hardcoded value to "" and nothing ever filled it, so a page
        # that said "$220,000" reached the customer blank -- and the card's
        # Est. Value box invited them to guess a number the page had already
        # given them.
        if not str(row.get("value") or "").strip():
            amount = bid_sources.detail_value(page)
            if amount:
                row["value"] = amount
                got = True
        # Fields only the posting carries. The two flags are worth surfacing
        # out of proportion to how often they appear: a missed mandatory
        # pre-bid meeting is not a late bid, it is an ineligible one, and
        # pricing against a scope an addendum has superseded is worse than
        # not bidding at all.
        for field, fn in (("published", bid_sources.detail_published),
                          ("bid_number", bid_sources.detail_bid_number),
                          ("prebid", bid_sources.detail_prebid)):
            if not str(row.get(field) or "").strip():
                val = fn(page)
                if val:
                    row[field] = val
                    got = True
        if not row.get("addenda") and bid_sources.detail_has_addenda(page):
            row["addenda"] = True
            got = True
        if not row.get("documents"):
            docs = bid_sources.detail_documents(page, row["url"])
            if docs:
                row["documents"] = docs
                got = True
        return got

    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as ex:
        results = list(ex.map(_one, targets))

    if stats is not None:
        # These used to count "did the enricher fill ANY field", under names
        # that read as "did we get a contact". With the enricher now also
        # recovering deadline, value, publication date, bid number, pre-bid
        # and documents, that gap made the numbers unreadable: a posting that
        # yielded a deadline and no phone counted as a contact found.
        reachable = sum(1 for r in targets if r.get("email") or r.get("phone"))
        enriched = sum(1 for r in results if r)
        unreachable = sum(1 for r in targets if r.pop("_fetch_failed", False))
        def _bump():
            stats["contacts_found"] = stats.get("contacts_found", 0) + reachable
            missed = len(targets) - reachable
            if missed:
                stats["contacts_missing"] = stats.get("contacts_missing", 0) + missed
            stats["postings_enriched"] = \
                stats.get("postings_enriched", 0) + enriched
            stats["postings_read"] = stats.get("postings_read", 0) + len(targets)
            if unreachable:
                stats["postings_unreachable"] = \
                    stats.get("postings_unreachable", 0) + unreachable
        if lock is not None:
            with lock:
                _bump()
        else:
            _bump()


def _run_known_portals(city, state, ai_label, grouped, center, radius, cdb,
                        city_coords, lock, pdb, default_city="", town_coords=None,
                        stats=None):
    """Fetch URLs already known (from a prior scan or the seed list) to be
    this city's real bid page, directly — no search engine involved. This is
    the fast, deterministic path that /scan tries before falling back to
    live search: no per-query search-API cost, no dependency on that day's
    search rankings, and it can't be blocked the way scraping DuckDuckGo can.
    Entries age out (via bid_portals.MAX_FAIL) if they stop returning real
    content, so a site redesign doesn't silently keep failing forever."""
    with lock:
        # Eight, not six. Adding school districts pushed Cincinnati and
        # Houston to seven portals each, and a cap below the real maximum
        # does not save time -- it silently drops a verified bid page in the
        # two biggest cities in the directory. Only those two towns read more
        # than six, so the cost is two extra fetches on two scans.
        portals = list(bid_portals.get_portals(pdb, city, state))[:PORTALS_PER_TOWN]

    # Nothing learned for this town yet? Its official domain is already known —
    # CISA publishes the registry of every .gov, so there is no need to search
    # for it. Probe the handful of paths a municipal bid page actually takes;
    # a hit is recorded below and costs nothing on every later scan.
    #
    # This also finally reaches counties. They let a great deal of curb, road
    # and drainage work and were entirely absent before: none were seeded, and
    # a county name doesn't geocode, so any bid naming one was thrown away.
    if not portals:
        lookups = gov_directory.lookup(city, state)[:2]
        probed = []
        for entry in lookups:
            for candidate in bid_sources.candidate_bid_urls(entry["domain"], limit=2):
                probed.append({"url": candidate, "probe": True,
                               "platform": "civicplus"
                               if candidate.lower().endswith("bids.aspx") else "custom"})

        # The guessed common paths above only cover CivicPlus and a couple of
        # others -- most platforms put their bid page at a path nothing here
        # would guess. Before falling all the way through to a generic web
        # search -- which has no way to tell a result ABOUT this city apart
        # from one that merely mentions it, see _place_bid's out_of_radius
        # counter, exactly what a city with no known portal and no lucky
        # guess above degrades to -- try an actual bid-shaped link off each
        # entity's own homepage. Same extraction tools/discover_bid_portals.py
        # uses for the offline national crawl, just live and per-scan instead
        # of pre-computed, so a city outside that crawl's coverage still gets
        # a real shot at its own bid page. Fetched concurrently: this runs
        # before the parallel probe stage below, so sequential homepage
        # fetches would add their full latency on top of it for nothing.
        def _homepage_links(entry):
            home_url = f"https://{entry['domain']}"
            html, outcome = _fetch_page(home_url, timeout=PROBE_TIMEOUT)
            if outcome != "ok" or not html:
                return []
            return bid_sources.extract_bid_link_candidates(html, home_url, max_candidates=2)

        if lookups:
            with ThreadPoolExecutor(max_workers=len(lookups)) as ex:
                for links in ex.map(_homepage_links, lookups):
                    for link in links:
                        probed.append({"url": link, "probe": True, "platform": "custom"})

        portals = probed[:8]
    # Read every portal at once. This loop used to be sequential, which was
    # survivable when it only ever touched one or two known-good URLs. Adding
    # speculative .gov probes on top of it was not: a handful of dead guesses
    # at a full fetch timeout each consumed the entire request budget before
    # the search path ran, the app aborted the request, and every scan came
    # back empty. Fetching is pure I/O, so width costs nothing here.
    raw = [0]

    def _read_portal(entry):
        url = entry["url"]
        timeout = PROBE_TIMEOUT if entry.get("probe") else None

        # Structured reading is an OPTIMISATION over the AI path, never a
        # replacement. If the parser matches nothing we fall through to the AI
        # below rather than losing the portal entirely, and a live page counts
        # as a success even when our regex didn't understand it — a parser gap
        # is our problem, not a dead site.
        if bid_sources.identify_platform(url) == "civicplus":
            page, outcome = _fetch_page(url, timeout=timeout)
            if outcome != "ok" and stats is not None:
                with lock:
                    k = f"portal_fetch_{outcome}"
                    stats[k] = stats.get(k, 0) + 1
            rows = bid_sources.parse_civicplus_html(page, base_url=url)
            if rows:
                keep = []
                for row in rows:
                    if not bid_sources.looks_relevant(row["title"], row.get("scope")):
                        if stats is not None:
                            with lock:
                                stats["filtered_not_niche"] = stats.get("filtered_not_niche", 0) + 1
                        continue
                    keep.append(row)
                # The listing page has no contact details — they live on each
                # individual posting, which we already hold the URL for. A bid
                # with nobody to call is barely a lead, so read them. Capped and
                # concurrent: a sequential pass over a dozen postings is exactly
                # what blew the request budget the last time.
                ordered = _enrichment_order(keep)
                _note_enrich_budget(ordered, DETAIL_PAGES_PER_PORTAL, stats, lock)
                _enrich_from_detail_pages(
                    ordered[:DETAIL_PAGES_PER_PORTAL], stats, lock)
                with lock:
                    bid_portals.record_result(pdb, city, state, url, True)
                    for row in keep:
                        raw[0] += 1
                        # Pass the enriched row THROUGH rather than copying a
                        # fixed list of fields out of it. The old allowlist
                        # named nine keys and hardcoded value to "", so every
                        # field the detail-page enricher had just recovered --
                        # value, published, bid_number, prebid, addenda,
                        # documents -- was read off the posting and then
                        # dropped on the floor one line later. CivicPlus is
                        # the platform behind ~2,400 of the portals in the
                        # directory, so that silently emptied those six rows
                        # of the bid card for most of the board.
                        payload = dict(row)
                        # The listing states its own status where it has one;
                        # trust that over assuming everything on the page is
                        # live.
                        payload["status"] = row.get("status") or "Open"
                        payload["city"] = default_city or city
                        _place_bid(grouped, payload,
                            center, radius, cdb, default_city=default_city or city,
                            city_coords=city_coords, default_state=state,
                            fallback_coords=town_coords, stats=stats,
                            origin="portal")
                return
            if bid_sources.civicplus_page_is_empty(page):
                # The right page, with nothing posted. Sending it to the AI
                # would spend a call and seconds of the scan's town budget to
                # discover the same nothing -- and that budget is what decides
                # how many other towns get read at all.
                with lock:
                    bid_portals.record_result(pdb, city, state, url, True)
                    if stats is not None:
                        stats["civicplus_no_open_bids"] = \
                            stats.get("civicplus_no_open_bids", 0) + 1
                return
            if bid_sources.page_is_missing(page):
                # Checked before the two below because it is the most
                # specific answer: this is not a bid page with a layout we
                # cannot read, it is a 404 or a lapsed domain. Same outcome,
                # but the funnel should say which.
                with lock:
                    bid_portals.record_result(pdb, city, state, url, False)
                    if stats is not None:
                        stats["portal_page_missing"] = \
                            stats.get("portal_page_missing", 0) + 1
                return
            # Before writing this off as a parser gap: the commonest reason a
            # CivicPlus Bids page holds no bids is that the city has MOVED its
            # solicitations to a hosted platform and left this page behind as
            # a signpost -- "View Open Solicitations" pointing at BeaconBid,
            # OpenGov, BidNet. Handing the signpost to the AI reads a page
            # with no bids on it, every scan, forever. Follow it instead, and
            # write the real address into the directory so the next scan goes
            # straight there.
            moved = bid_sources.hosted_portal_link(page, url)
            if moved:
                with lock:
                    bid_portals.record_result(pdb, city, state, url, True)
                    bid_portals.learn_portal(pdb, city, state, moved,
                                             platform="custom",
                                             allow_hosted=True)
                    if stats is not None:
                        stats["portal_moved_to_hosted"] = \
                            stats.get("portal_moved_to_hosted", 0) + 1
                url = moved
                timeout = None
            elif bid_sources.page_is_wrong_module(page):
                # A /Bids.aspx URL serving "Home - Lake County, Ohio" or
                # "Sitka Police Department". The bid module is gone and the
                # site is answering with something else, so there is nothing
                # here to parse and nothing worth an AI call. Let it fail
                # towards MAX_FAIL like any other dead entry.
                with lock:
                    bid_portals.record_result(pdb, city, state, url, False)
                    if stats is not None:
                        stats["portal_wrong_module"] = \
                            stats.get("portal_wrong_module", 0) + 1
                return
            elif stats is not None:
                with lock:
                    stats["civicplus_parse_miss"] = stats.get("civicplus_parse_miss", 0) + 1

        # Fetch once and KEEP the html. _fetch_text discards it, which is
        # why a non-CivicPlus portal's bids could never be given their own
        # posting link: the model is shown text only and never sees an href.
        page_html = _fetch_page(url, timeout=timeout)[0] or ""
        text = _html_to_text(page_html)
        # "200 OK" is not proof the URL is right. Municipal sites overwhelmingly
        # serve their not-found page with a 200, and a parked or lapsed domain
        # serves a sales page the same way -- both are long enough to clear the
        # length check below, so the entry was recorded as a SUCCESS on every
        # scan and could never age out via bid_portals.MAX_FAIL. Sampling 400
        # CivicPlus entries found 21 in that state, one of them a lapsed domain
        # now serving an online-casino page to our customers.
        if bid_sources.page_is_missing(page_html):
            with lock:
                bid_portals.record_result(pdb, city, state, url, False)
                if stats is not None:
                    stats["portal_page_missing"] = \
                        stats.get("portal_page_missing", 0) + 1
            return
        ok = len(text) >= 200
        with lock:
            bid_portals.record_result(pdb, city, state, url, ok)
        if not ok:
            return
        # Skip the extraction call on a portal with nothing of ours anywhere
        # on it. 21 of 33 sampled agency portals are in that state at any
        # moment -- each one an AI call, and seconds of the scan's town
        # budget, spent to find nothing. The portal is still recorded above as
        # a working source; there is simply nothing for us on it today.
        #
        # page_may_hold_work, NOT looks_relevant. This used looks_relevant,
        # which is the per-posting test and vetoes on words like "professional
        # services" before it ever looks for a trade term. On a whole page
        # that made one unrelated RFP enough to discard every real bid beside
        # it: 44 of 166 town portals were skipped this way while their text
        # said concrete, sidewalk, curb or paving. It also contradicted the
        # contract _run_local_queries documents for this function -- that a
        # known portal is trusted and a gap here is our problem, not evidence
        # the page is irrelevant. Which postings are actually ours is still
        # decided per posting, below and in the CivicPlus branch above.
        if not bid_sources.page_may_hold_work(text):
            if stats is not None:
                with lock:
                    stats["portal_no_niche_content"] = \
                        stats.get("portal_no_niche_content", 0) + 1
            return
        bids = _ai_extract(ai_label, text)
        if not bids:
            return
        # Bids that ended up with their own posting URL can be enriched the
        # same way the CivicPlus path already is -- that recovers a deadline
        # on 95% of postings and a phone on 81%. Ones still pointing at the
        # listing page are skipped: re-reading the page we just read adds
        # nothing.
        own_pages = [b for b in bids if isinstance(b, dict)
                     and bid_sources.link_for_title(page_html, url, b.get("title"))]
        for b in own_pages:
            b["url"] = bid_sources.link_for_title(page_html, url, b.get("title"))
        if own_pages:
            ordered = _enrichment_order(own_pages)
            _note_enrich_budget(ordered, DETAIL_PAGES_PER_PORTAL, stats, lock)
            _enrich_from_detail_pages(ordered[:DETAIL_PAGES_PER_PORTAL],
                                      stats, lock)
        with lock:
            raw[0] += len(bids)
            for b in bids:
                if isinstance(b, dict):
                    # Prefer this bid's own posting link, matched from the
                    # page's anchors by title. Falling back to the listing
                    # page is correct but lands the contractor on a list, and
                    # leaves the enricher nothing per-posting to read.
                    own = bid_sources.link_for_title(page_html, url, b.get("title"))
                    b["url"] = own or _resolve_bid_url(b.get("url"), url, text)
                    # `or city`, matching the CivicPlus branch above. This is
                    # a known portal -- this town's OWN bid page -- so a bid
                    # on it that doesn't restate the town is still that
                    # town's. Without the fallback _place_bid dropped it as
                    # no_location, losing bids from the most reliable source
                    # in the pipeline. The fix was applied to the CivicPlus
                    # branch and missed here, which is where every non-
                    # CivicPlus portal is read -- 1,260 of them.
                    _place_bid(grouped, b, center, radius, cdb, pdb=pdb,
                              default_city=default_city or city,
                              city_coords=city_coords, default_state=state,
                              fallback_coords=town_coords, stats=stats,
                              origin="portal")

    if portals:
        with ThreadPoolExecutor(max_workers=min(PORTAL_WORKERS, len(portals))) as ex:
            list(ex.map(_read_portal, portals))
    return raw[0]


# Aggregator platforms a city's own bid page structurally cannot show. Packed
# into two OR-ed site: queries rather than one per domain: every engine we use
# supports OR-ed site: filters, and eight separate searches for the same
# question was the single biggest line item in a scan's search budget.
_AGG_SITES = (
    ("bidnetdirect.com", "demandstar.com", "planetbids.com", "publicpurchase.com"),
    ("questcdn.com", "opengov.com", "bonfirehub.com", "bidexpress.com",
     "bidsearch.com"),
)

# State procurement portals, folded into the packed query for that state
# rather than costing a search of their own.
_STATE_PORTALS = {"MO": "missouribuys.mo.gov"}


def _agg_sites(group, state=""):
    """An OR-ed site: filter for one group of aggregator domains."""
    sites = list(_AGG_SITES[group])
    extra = _STATE_PORTALS.get((state or "").upper())
    if extra and group == len(_AGG_SITES) - 1:
        sites.append(extra)
    return " OR ".join("site:" + d for d in sites)


def _run_local_queries(queries, ai_label, max_pages, grouped, center, radius, cdb,
                        city_coords, seen_urls, lock, pdb, default_city="", state="",
                        town_coords=None, stats=None):
    """Run one town's worth of search queries and extract bids from the top
    pages. Each town gets its own max_pages slice so a big scan with several
    anchor towns doesn't let the first town's results crowd out the rest.

    `lock` guards every read/write to the structures shared across towns
    (seen_urls, grouped, city_coords, cdb's geo_cache, pdb the portal
    directory) since towns are now run concurrently from the /scan route.
    The searches themselves stay sequential per-town (with the existing
    throttle) to avoid hammering DuckDuckGo; only the independent per-page
    fetch+AI-extract step below is fanned out.

    Any page that turns out to be a real per-agency bid page (not a generic
    aggregator listing) gets recorded into the portal directory, so future
    scans of this city can skip straight to it via _run_known_portals
    instead of re-searching — coverage improves scan over scan instead of
    resetting every time.

    BidNet Direct results (a real public source, queried directly by state --
    see _bidnet_direct_urls) are put FIRST in the item list, so they win the
    max_pages budget over speculative search-engine hits: a guaranteed real
    government solicitation is worth more than an uncertain DDG/Tavily
    result, and this doesn't raise the per-scan AI-extraction cost since the
    slice below is still capped at the same max_pages either way."""
    items = []
    for kw in BIDNET_KEYWORDS:
        for r in _bidnet_direct_urls(kw, state):
            with lock:
                if r["url"] in seen_urls:
                    continue
                seen_urls.add(r["url"])
            items.append(r)

    for q in queries:
        results, used_ddg = _web_search(q, max_results=6)
        for r in results:
            with lock:
                if r["url"] in seen_urls:
                    continue
                seen_urls.add(r["url"])
            # Place the result before paying for it. A search for "Aurora MO
            # sidewalk bid" reliably returns auroragov.org -- Aurora,
            # COLORADO -- and the old order fetched the page and spent an AI
            # extraction before _place_bid worked out it was 700 miles away.
            # 21 of 38 extractions on a live Aurora scan died that way. Only
            # acts on domains the directory can actually place; an unknown
            # domain is still fetched, since absence proves nothing.
            known = bid_portals.town_for_url(r["url"])
            if known:
                pt = bid_portals.coords_for_town(*known)
                if pt and _miles_between(center["lat"], center["lon"],
                                         pt[0], pt[1]) > radius:
                    if stats is not None:
                        with lock:
                            stats["search_hit_out_of_area"] = \
                                stats.get("search_hit_out_of_area", 0) + 1
                    continue
            items.append(r)
        # The pause exists to avoid hammering DuckDuckGo, which is scraped and
        # will start blocking. It used to run after every query regardless, so
        # with Tavily configured — where DDG is never even called — it was
        # several seconds of pure dead time per town, on an endpoint already
        # pushing against the client's timeout.
        if used_ddg:
            time.sleep(DDG_QUERY_PAUSE)

    # How the page budget gets spent decides recall as much as its size does.
    # The queries deliberately include several site: searches, so left in
    # discovery order one aggregator can swallow the whole allowance while the
    # agency's own posting further down the list is never opened. Ordering
    # keeps BidNet's verified solicitations first, then favours government
    # domains (a .gov page is the primary source, not a re-listing), and caps
    # how many pages any single domain may take.
    items = _prioritize_pages(items)

    raw = [0]

    def _process(it):
        text = it["content"] or _fetch_text(it["url"])
        if len(text) < 200:
            return
        # These pages are unverified search hits, unlike a known portal's own
        # listing (see _run_known_portals, which deliberately does NOT gate
        # on content -- a parser gap there is our problem, not evidence the
        # page is irrelevant). Here there's no such trust to lean on, so a
        # page with none of the niche terms anywhere in it is essentially
        # never going to yield a bid -- skip the OpenAI call rather than pay
        # for a page about janitorial services or a council-meeting agenda
        # that happened to rank for the search query.
        if not bid_sources.looks_relevant(text):
            if stats is not None:
                with lock:
                    stats["filtered_not_niche"] = stats.get("filtered_not_niche", 0) + 1
            return
        bids = _ai_extract(ai_label, text)
        if not bids:
            return
        with lock:
            raw[0] += len(bids)
            for b in bids:
                if isinstance(b, dict):
                    b["url"] = _resolve_bid_url(b.get("url"), it["url"], text)
                    bid_city = (b.get("city") or default_city or "").split(",")[0].strip()
                    _place_bid(grouped, b, center, radius, cdb, pdb=pdb,
                               default_city=default_city,
                              city_coords=city_coords, default_state=state,
                              fallback_coords=town_coords, stats=stats,
                              origin="search")
                    if bid_city and state:
                        bid_portals.learn_portal(pdb, bid_city, state, it["url"])

    with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as ex:
        list(ex.map(_process, items[:max_pages]))

    return raw[0]


# Words that mean a solicitation can't be bid on right now — either it is over,
# or (in /upcoming's case) it hasn't been let yet. Anything else counts as open.
# Local news coverage of a council awarding a contract reads almost exactly
# like a bid notice to the extraction model: same project, same agency, same
# dollar figure. It is the opposite of a lead -- the work is already gone. A
# real example that reached a customer: "The City Council awarded the contract
# for a new sidewalk to be installed at Westwood Drive... The winning bid was
# $748,908 from Cardenas Concrete". These phrases only exist once a winner does.
_AWARD_PHRASES = (
    "awarded the contract", "award the contract", "contract was awarded",
    "contract awarded", "was awarded to", "awarded to ", "winning bid",
    "winning bidder", "successful bidder", "apparent low bidder",
    "low bidder was", "bid was accepted", "council awarded",
    "commission awarded", "notice of award", "has been awarded",
)

# A live bid listing page routinely carries past awards next to current
# solicitations, so award language on its own must never close a page that is
# still plainly asking for bids.
_OPEN_SOLICITATION_PHRASES = (
    "bids due", "bid due", "proposals due", "due date", "accepting bids",
    "now accepting", "sealed bids will be received", "will be received until",
    "bid opening", "submit bids", "submittal deadline", "closes on",
    "responses due", "questions due", "request for proposals due",
    "bids will be accepted", "deadline for bids",
)


# A countdown is not a deadline. "In 8 days" was rendered by somebody's page
# at some unknown moment; kept as-is it displays as fact, scores as urgent,
# and -- because it carries no date and no year -- _apply_deadline_status
# cannot tell that it expired. A real listing showed "In 8 days" for a
# solicitation that had closed 26 days earlier.
_RELATIVE_DEADLINE_RE = re.compile(
    r"^(in\s+(a|an|\d+)\s+(day|week|month|hour)s?|tomorrow|today|tonight|"
    r"next\s+(week|month|year)|this\s+(week|month)|\d+\s+days?\s+(left|remaining)|"
    r"closing\s+soon|due\s+soon|asap|open\s+now|ongoing|tbd|n/?a)\.?$", re.I)


def _clean_deadline(text):
    """Deadline text with unverifiable countdowns removed.

    Anything naming a date or even a bare year is kept -- "FY2027" and
    "December 1, 2026" are both checkable. A pure countdown is not, and
    showing "Not listed" beats showing a number we cannot stand behind.
    """
    s = " ".join(str(text or "").split())
    if not s or _parse_deadline(s):
        return s
    return "" if _RELATIVE_DEADLINE_RE.match(s) else s


# Phrases where a page states, about itself, that the thing is over.
_EXPLICIT_CLOSED_MARKERS = (
    "status: closed", "status:closed", "past due", "bidding is closed",
    "bidding has closed", "this solicitation is closed",
    "solicitation is closed", "no longer accepting", "submissions are closed",
    "closed to bidding", "bid is closed", "closed for bidding",
)


def _page_declares_closed(text, bid_count):
    """True when the page itself says this solicitation is closed.

    Only trusted on a single-solicitation detail page. A listing page carries
    a status per row and one row reading "Closed" says nothing about the
    others -- closing all of them would throw away real work, which is a
    worse failure than showing one stale bid. On a page describing exactly
    one solicitation there is no such ambiguity.
    """
    if bid_count != 1:
        return False
    low = (text or "").lower()
    return any(m in low for m in _EXPLICIT_CLOSED_MARKERS)


def _looks_awarded(text):
    """True when this page is reporting a finished award, not soliciting one."""
    low = (text or "").lower()
    if not any(p in low for p in _AWARD_PHRASES):
        return False
    return not any(p in low for p in _OPEN_SOLICITATION_PHRASES)


_CLOSED_STATUS_WORDS = ("closed", "close date", "awarded", "award to",
                        "cancel", "expired", "withdrawn", "archived",
                        "no longer", "not accepting", "complete",
                        "planned", "upcoming", "anticipated")


def _is_open_bid(bid):
    """A bid the client will actually display. Mirrors isOpen() in app.html.

    This used to require the status to be exactly "open", and dropped anything
    else. That is not how the real world writes it: agencies and the extraction
    model both produce "Accepting Bids", "Active", "Advertised", "Open - Bids
    Due 12/1", "Currently Open". Every one of those was placed into the result
    by _place_bid, counted as kept in the funnel, and then reported as zero and
    hidden by the app — a scan could find seven genuine local bids and still
    tell the contractor there was nothing out there. So the test is inverted:
    a bid is open unless it says it isn't.
    """
    status = str((bid or {}).get("status") or "").strip().lower()
    if not status:
        return True  # unstated status is not evidence of a closed bid
    return not any(word in status for word in _CLOSED_STATUS_WORDS)


def _closed_on_arrival(row):
    """True if the listing row already says this bid is shut.

    Deliberately built out of the two functions that decide this everywhere
    else, on a throwaway copy, so it can never drift from them: a stated
    Closed/Awarded status, or a deadline already past. An undated row is
    never closed on arrival — reading its posting is the only thing that can
    date it, so it is the last row we would want to skip.
    """
    probe = {"status": (row or {}).get("status"),
             "deadline": (row or {}).get("deadline")}
    _apply_deadline_status(probe)
    return not _is_open_bid(probe)


def _enrichment_order(rows):
    """Rows ordered by how much reading each posting is worth.

    Enrichment is rationed twice: ten postings per portal, and fourteen across
    the whole result. The second cap is where this matters. A scan keeps every
    bid it finds and lets the client hide the closed ones, so the pile handed
    to _enrich_placed_bids is mostly dead: a 125-mile scan from New Salem, MA
    kept 93 bids of which 83 had already closed. Sorted only by whether a
    deadline was present, roughly nine of the fourteen reads went to bids
    nobody can bid on, and the handful of live ones arrived with no contact,
    no scope and no engineer's estimate.

    The per-portal cap turns out not to have this problem — 38 live CivicPlus
    portals were checked and not one had enough closed relevant rows to push
    an open bid out of its ten. The ordering is applied there anyway because
    it costs nothing and the pathological page is only a matter of time, but
    the measured win is at the whole-result stage.

    Undated first — a missing deadline is the one thing only the posting can
    supply, and until it is supplied the bid cannot even be recognised as
    expired. Then open dated rows. Closed rows last: they are still kept and
    still shown with their Closed badge, they just stop taking a slot from a
    bid somebody could actually still bid on. Stable, so listing order is
    preserved within each group.
    """
    def rank(row):
        if not str((row or {}).get("deadline") or "").strip():
            return 0
        return 2 if _closed_on_arrival(row) else 1
    return sorted(rows or [], key=rank)


def _note_enrich_budget(rows, budget, stats, lock=None):
    """Count postings the budget could not reach, split by whether it mattered.

    Without this the reordering above is unfalsifiable. `enrich_budget_spared`
    is the number of already-closed postings that fell outside the budget —
    slots the old order would have spent on a dead bid. `enrich_budget_short`
    is the number of open or undated ones that did not fit, which is the
    honest cost of a cap of this size and the number to watch if it grows.
    """
    if stats is None or len(rows) <= budget:
        return
    missed = rows[budget:]
    spared = sum(1 for r in missed if _closed_on_arrival(r))
    short = len(missed) - spared

    def _bump():
        if spared:
            stats["enrich_budget_spared"] = \
                stats.get("enrich_budget_spared", 0) + spared
        if short:
            stats["enrich_budget_short"] = \
                stats.get("enrich_budget_short", 0) + short
    if lock is not None:
        with lock:
            _bump()
    else:
        _bump()


def _status_breakdown(grouped):
    """How many bids in a result carry each status, e.g. {"Open": 2, "Closed": 7}."""
    out = {}
    for bids in (grouped or {}).values():
        for b in bids:
            label = str((b or {}).get("status") or "(none)").strip() or "(none)"
            out[label] = out.get(label, 0) + 1
    return out


def _scan_sample(grouped, limit=8):
    """A handful of what the last scan actually placed, for /health.

    Only fields that already appear on a public bid notice — no contact
    details, nothing about who ran the scan.
    """
    out = []
    for city, bids in sorted((grouped or {}).items()):
        for b in bids:
            out.append({"city": city,
                        "title": str((b or {}).get("title") or "")[:120],
                        "deadline": str((b or {}).get("deadline") or ""),
                        "status": str((b or {}).get("status") or ""),
                        "open": _is_open_bid(b)})
            if len(out) >= limit:
                return out
    return out


def _bid_dupe_key(bid):
    """Identity of a solicitation for de-duplication: title + deadline.

    Both halves are normalised, because the same job routinely arrives twice
    -- once off the agency's own page and once via search or an aggregator --
    written slightly differently each time. Comparing the deadline as raw text
    meant "9/3/2026" and "09/03/2026" were different bids, as were
    "09/03/2026" and "09/03/2026 02:00 PM EDT", so the contractor got two
    cards for one job and starring one did nothing to the other.
    """
    raw = (bid or {}).get("title") or ""
    title = re.sub(r"\s+", " ", str(raw)).strip().lower().strip(" .,-\u2013\u2014:;")
    due = str((bid or {}).get("deadline") or "").strip()
    parsed = _parse_deadline(due)
    # An unparseable deadline keeps its text, so two genuinely different
    # free-text dates still separate two genuinely different bids.
    return title, (parsed.isoformat() if parsed else due.lower())


# Bodies that let concrete work but are not places a gazetteer can find:
# counties, road districts, school districts, transit and housing authorities.
_AUTHORITY_RE = re.compile(
    r"\b(count(?:y|ies)|parish|borough|township|twp|district|authority|"
    r"commission|department|board|schools?|university|college|port|"
    r"utilit(?:y|ies)|water|sewer|drainage|levee|transit|housing|airport|"
    r"regional|council|association|consolidated|R-[IVX]+)\b", re.I)


_COUNTY_SHAPED_RE = re.compile(r"\bcount(?:y|ies)\b|\bparish\b|\bborough\b", re.I)


def _looks_like_authority(name):
    """True if a name is an organisation rather than a town.

    This is the dividing line for the search-town fallback in _place_bid. The
    fallback exists because "Greene County" and "Ozark R-VI School District"
    don't geocode and their bids were being thrown away. It must NOT catch an
    ordinary city name that failed to resolve — a plain town that doesn't
    exist in the state we searched is evidence the bid is somewhere else
    entirely, and pinning it here presents a job a thousand miles away as
    local. That is worse than missing it: the contractor drives, or bids, on a
    job that was never theirs.
    """
    return bool(_AUTHORITY_RE.search(str(name or "")))


def _split_city_state(raw):
    """'Bentonville, AR' -> ('Bentonville', 'AR'). Bare city -> ('Bentonville', '')."""
    parts = [p.strip() for p in str(raw or "").split(",")]
    city = parts[0]
    state = ""
    if len(parts) > 1 and parts[1]:
        cand = parts[1]
        state = cand.upper() if cand.upper() in STATE_ABBRS \
            else STATE_NAME_TO_ABBR.get(cand.lower(), "")
    return city, state


# A bid with no stated deadline cannot be aged by _apply_deadline_status --
# there is nothing to compare against today -- so it sits in the feed forever.
# The nightly audit measured this at half of everything shown. Recording when
# a dateless bid was first seen gives the only clock available.
UNDATED_MAX_DAYS = int(os.environ.get("SCAN_UNDATED_MAX_DAYS", "60"))
# Typical solicitations run two to four weeks, so 60 days is deliberately
# generous: retiring a job that is genuinely still open costs a customer real
# work, while showing a dead one costs them a wasted phone call.
_UNDATED_STORE_MAX = 5000


def _age_out_undated(bid, city, db, stats=None):
    """Retire a dateless bid once it has been in the feed too long."""
    if not _is_open_bid(bid) or _parse_deadline(bid.get("deadline")):
        return
    today = datetime.datetime.now().date()
    # If the posting states when it went up, that is a real age rather than
    # an inferred one -- and it works on the first sighting instead of
    # starting a clock we then have to wait out. 88% of postings carry it.
    posted = _parse_deadline(bid.get("published"))
    if posted:
        if (today - posted).days >= UNDATED_MAX_DAYS:
            bid["status"] = "Closed"
            if stats is not None:
                stats["aged_out_undated"] = stats.get("aged_out_undated", 0) + 1
        return

    store = db.setdefault("undated_first_seen", {})
    sig = _bid_sig(city, bid)
    first = store.get(sig)
    if not first:
        store[sig] = today.isoformat()
        # Unbounded growth would eventually be the whole cache. Evict oldest
        # first; a re-seen bid simply restarts its clock, which errs towards
        # showing work rather than hiding it.
        if len(store) > _UNDATED_STORE_MAX:
            for old in sorted(store, key=store.get)[:len(store) - _UNDATED_STORE_MAX]:
                store.pop(old, None)
        return
    try:
        age = (today - datetime.date.fromisoformat(first)).days
    except (ValueError, TypeError):
        store[sig] = today.isoformat()
        return
    if age >= UNDATED_MAX_DAYS:
        bid["status"] = "Closed"
        if stats is not None:
            stats["aged_out_undated"] = stats.get("aged_out_undated", 0) + 1


def _place_bid(grouped, bid, center, radius, db, default_city="", city_coords=None,
               default_state="", fallback_coords=None, stats=None, pdb=None,
               origin=None):
    """Keep a bid ONLY if its real city geocodes within the radius.

    The city is resolved against the most specific state we have: the one the
    AI actually stated, else the town whose search produced this bid, else the
    search centre's. Previously every bid was geocoded against the CENTRE
    state alone, and the state the AI returned was thrown away by splitting on
    the comma. Any radius wide enough to cross a state line — which is most of
    the 75mi and 125mi range, and the whole point of those options — then
    looked up a real "Bentonville, AR" bid as "Bentonville, MO", found
    nothing, and dropped it. Out-of-state bids were invisible on exactly the
    wide scans meant to surface them.
    """
    def _count(reason):
        if stats is not None:
            stats[reason] = stats.get(reason, 0) + 1

    if not isinstance(bid, dict):
        _count("malformed")
        return
    # Whether the bid named its OWN city, or we are about to lend it the town
    # whose scan turned it up. That fallback exists because a town's own bid
    # page is that town's by definition -- a posting there that doesn't
    # restate the city is still local.
    #
    # An aggregator page is the opposite case. PlanetBids, BidNet, DemandStar
    # and the rest host every agency in the country behind one domain, so the
    # search town says nothing about where the work is. A live scan of
    # Rollingwood, CA surfaced a City of DUARTE job -- 358 miles away, on the
    # far side of the state -- and lending it Rollingwood's name and
    # coordinates put it on the board as local, past the radius check, under
    # the wrong town's heading.
    stated_city = str(bid.get("city") or "").strip()
    from_aggregator = bid_portals.is_aggregator_url(bid.get("url") or "")
    if from_aggregator and not stated_city:
        _count("aggregator_no_location")
        return
    # Same failure, on an ordinary municipal CMS rather than a bid platform.
    # A bid found while searching Charlestown, IN and read off
    # cms3.revize.com/revize/fairfield/... is not Charlestown's work. With no
    # city of its own it was lent the search town's name, geocoded cleanly
    # because "Charlestown" is a real place, and reached a live board reading
    # "Charlestown - 16 mi" for a job in a Fairfield hundreds of miles away.
    # Note this is checked BEFORE the coordinate lookup, not in the
    # unresolvable-place fallback further down: that fallback only runs when
    # the borrowed name fails to geocode, and a borrowed name that geocodes
    # perfectly is precisely the dangerous case.
    if not stated_city and bid_portals.url_names_other_place(
            bid.get("url"), default_city, pdb):
        _count("url_names_another_town")
        return
    city, stated_state = _split_city_state(stated_city or default_city or "")
    if not city:
        _count("no_location")
        return  # no stated location -> can't verify it's local -> drop
    # A stated state is taken as final: if the text says Springfield, IL, that
    # is the Illinois one, and failing to geocode it must drop the bid rather
    # than fall through to a same-named city in the centre's state. Guessing
    # there would quietly relocate a bid hundreds of miles and present it as
    # local, which is worse than missing it. The fallback chain only applies
    # when no state was stated at all.
    if stated_state:
        candidates = [stated_state]
    else:
        candidates = [s for s in (default_state, center["state"]) if s]
    # The model reads city names off page text and sometimes drops a
    # character -- a real scan filed a Missouri bid under "Ashlan". That town
    # does not exist, so it never geocodes, radius search never sees it, and
    # it never groups with the rest of Ashland's work. Correct it against the
    # towns we actually know, but only on an unambiguous single-edit match.
    for st in candidates:
        snapped = bid_portals.snap_city_name(city, st.upper())
        if snapped != city:
            city = snapped
            _count("city_name_corrected")
            break

    coords, used_state = None, ""
    tried = []
    for st in candidates:
        st = st.upper()
        if st in tried:
            continue
        tried.append(st)
        coords = _city_coords(city, st, db)
        if coords:
            used_state = st
            break
    if not coords:
        # Last resort: anchor the bid to the town whose search turned it up.
        # Plenty of real buyers name themselves in ways no gazetteer resolves
        # ("Greene County", a road district, a regional authority), and this
        # page was found by searching a specific town, so the work is around
        # there. Only safe when the text didn't name a different state — a bid
        # explicitly in another state must not be pinned to this one.
        search_state = (default_state or center["state"]).upper()
        # ...but only for names that are plausibly unmappable in the first
        # place: an authority, or the very town we searched. A live 125mi scan
        # of Russellville MO returned bids in Binghamton NY, Barrington IL and
        # Aledo IL — ordinary cities that simply don't exist in Missouri, so
        # they failed to geocode, got stamped with a Missouri anchor town's
        # coordinates, and passed the radius check on borrowed location.
        # The town this search was run against is local by definition, and must
        # never be second-guessed below: the .gov registry only covers bodies
        # that own a domain (194 of Missouri's ~950 incorporated places), so a
        # small town's absence from it means nothing at all.
        is_search_town = (city.strip().lower()
                          == str(default_city or "").strip().lower()
                          and bool(str(default_city or "").strip()))
        anchorable = _looks_like_authority(city) or is_search_town
        # An authority we can place elsewhere is not local, whatever page it
        # was found on. A Missouri scan returned "DuPage County" sidewalk work
        # — DuPage is in Illinois, 350 miles away — and it passed the check
        # above because "County" makes a name look unmappable.
        #
        # Only acted on when the registry knows the name in exactly ONE state
        # and it isn't ours. Coverage is far too thin to read absence as
        # evidence: Kansas is missing 40% of its own counties, so "not
        # registered here" would throw away real local work. A name in several
        # states is ambiguous and left alone; a name in none is the road
        # district / drainage board case this fallback exists to catch.
        # Counties only. They are the one tier the registry covers densely
        # (3,137 entries against ~3,143 real counties), so "known in exactly
        # one state" is meaningful for them. For townships and districts it is
        # not — Ohio's Wayne Township owning the only registered domain of that
        # name says nothing about whether Missouri has one.
        if anchorable and not stated_state and not is_search_town \
                and _COUNTY_SHAPED_RE.search(city):
            elsewhere = gov_directory.states_for_org(city)
            if len(elsewhere) == 1 and search_state not in elsewhere:
                _count("authority_in_another_state")
                return
        # Same reasoning as above: an aggregator page's buyer may be anywhere,
        # so a name we could not geocode must not be pinned to the search
        # town's coordinates just because it looks like an authority.
        if fallback_coords and anchorable and not from_aggregator \
                and (not stated_state or stated_state == search_state):
            coords, used_state = fallback_coords, search_state
            _count("placed_by_search_town")
        else:
            _count("unresolvable_place")
            return
    miles = _miles_between(center["lat"], center["lon"], coords[0], coords[1])
    if miles > radius:
        _count("out_of_radius")
        return  # outside the chosen radius
    # Keep it. The radius check has always computed this and thrown it away,
    # which was survivable while the app defaulted to 25 miles and everything
    # on the board was near. At 125 it is the first thing a contractor needs:
    # a job 8 miles out and one 120 miles out are different propositions and
    # the card had no way to tell them apart.
    bid["miles"] = int(round(miles))
    _apply_deadline_status(bid)
    bid.pop("city", None)
    # Out-of-state towns keep their state in the label, both to disambiguate
    # same-named cities and because "this one is across the line" is something
    # a contractor wants to see before driving to look at it.
    label = city if used_state == center["state"] else f"{city}, {used_state}"
    # The same solicitation routinely turns up on two different pages -- an
    # aggregator and the agency's own site -- and both used to be kept. The
    # client derives a bid's id from its city + title + scope, so the copies
    # came out as duplicate cards sharing one id: starring one appeared to
    # star the other. Same title and deadline in the same town is the same job.
    # Before it can be shown: if it carries no date, has it been sitting in
    # the feed since before anyone would still want it?
    _age_out_undated(bid, label, db, stats)

    bucket = grouped.setdefault(label, [])
    key = _bid_dupe_key(bid)
    if key[0] and any(_bid_dupe_key(existing) == key for existing in bucket):
        _count("duplicate")
        return
    bucket.append(bid)
    _count("kept")
    # Which half of the pipeline paid for this bid.
    #
    # Search is now the most expensive stage in a scan -- 23.7 of 39.4
    # seconds on a Grant County run -- and nothing recorded whether it was
    # earning that. "kept" alone cannot answer it, so "should a scan keep
    # spending twenty seconds on search queries" was unanswerable from the
    # data. That is the position the stage timings were added to get out of.
    if origin:
        _count("kept_from_" + origin)
    # "kept" counts bids that made it into the result; the reported total counts
    # only the OPEN ones. That gap used to be invisible, and a scan that placed
    # seven bids and reported zero looked like a bug in the geography rather
    # than what it was — everything found had been ruled expired. The closed
    # count is taken at the end of the scan, once enrichment has had its say.
    if city_coords is not None:
        city_coords[label] = {"lat": coords[0], "lon": coords[1]}


# ═══════════════════════════════════════════════════════════
# State DOT lettings
#
# Every source above is a city or county: the page belongs to one place, so
# the place is known before a single row is read. A state letting page is the
# opposite -- one table carrying work from every corner of the state, where
# the only location is a county named inside the row. That is why these get
# their own reader and their own placement, and why counties.py exists.
#
# The yield is worth the separate path. A random live sample of 90 CivicPlus
# city portals produced 8 concrete-relevant bids between them; Florida's
# letting page alone produces 75, and a 50-mile scan from Tampa picks up 23 of
# them. Missouri adds 6 statewide, 4 of them inside 125 miles of Springfield.
#
# Only two states are wired today, and that is a supply fact rather than a
# missing feature -- see SEARCH_PLAN.md Phase 6 for what the other 48 are
# blocked on, and for the four false positives that make the strictness here
# non-negotiable.
# ═══════════════════════════════════════════════════════════

# NOTE: this file now lives one directory deeper than the original
# license_server.py did (repo_root/license_server.py -> repo_root/
# license_server/<this file>.py), and its __file__ reflects that. The
# extra os.path.dirname(...) below is only to keep this path resolving
# to the exact same repo_root-relative location as before the split --
# not a behavior change.
STATE_SOURCES_CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "data", "state_bid_sources.csv")
STATE_SOURCE_MIN_USABLE = int(os.environ.get("STATE_SOURCE_MIN_USABLE", "2"))
STATE_SOURCE_TIMEOUT = float(os.environ.get("STATE_SOURCE_TIMEOUT", "20"))
# One worker per state a wide radius can touch. Four is the observed
# maximum for 125 miles; the cap exists so a pathological radius cannot
# open a dozen sockets at once.
STATE_WORKERS = int(os.environ.get("SCAN_STATE_WORKERS", "4"))
_state_sources_cache = {"at": 0.0, "rows": None}


def _resolve_state_listing(url, kind, page):
    """(listing_url, listing_html) for a source, following an index if needed.

    A "listing" source is read directly. An "index" source is a page of dated
    letting links, and the listing is whichever is current -- Alabama's lives
    at .../NTC_August_28_2026.html and the address changes every letting, so
    storing the dated URL means the source dies silently when it rotates.
    """
    if kind != "index":
        return url, page
    link = bid_sources.newest_letting_link(
        page, url, today=datetime.date.today().timetuple()[:3])
    if not link:
        return url, page
    body, outcome = _fetch_page(link, timeout=STATE_SOURCE_TIMEOUT)
    if outcome != "ok" or not body:
        return url, page
    return link, body


def _state_sources():
    """{state: (url, kind)} for states VERIFIED to yield usable rows.

    Gated on the measured `usable` column, not on whether a URL was found. The
    discovery crawl reported convincing listings in 22 states; running the real
    parser over them, 2 produce placeable concrete-relevant rows. Shipping the
    other 20 would mean fetching South Dakota's fuel price index on every scan
    of the region and showing a contractor nothing for it.
    """
    now = time.time()
    if _state_sources_cache["rows"] is not None and \
            now - _state_sources_cache["at"] < 600:
        return _state_sources_cache["rows"]
    out = {}
    try:
        with open(STATE_SOURCES_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    usable = int(row.get("usable") or 0)
                except (TypeError, ValueError):
                    usable = 0
                url = (row.get("url") or "").strip()
                st = (row.get("state") or "").strip().upper()
                if st and url and usable >= STATE_SOURCE_MIN_USABLE:
                    out[st] = (url, (row.get("kind") or "listing").strip())
    except OSError:
        out = {}
    _state_sources_cache.update({"at": now, "rows": out})
    return out


def _place_state_bid(grouped, row, center, radius, city_coords=None, stats=None):
    """Place a state letting row, which already knows exactly where it is.

    _place_bid resolves a city name against a gazetteer because that is all a
    municipal posting gives you. A state row has been matched to a county
    centroid already, so running it through name resolution could only lose
    it -- "Cole" is not a city.

    The bucket is labelled "<County> County, ST" rather than a town, because
    that is the truth about the work: a resurfacing job spanning eleven miles
    of Route 163 is not in any one town, and inventing one would put it on the
    map in the wrong spot.
    """
    def _count(reason):
        if stats is not None:
            stats[reason] = stats.get(reason, 0) + 1

    # A row can name several counties, because a state "call" bundles several
    # jobs. Place it at the one NEAREST the contractor: the work really is in
    # all of them, so the nearest is both true and the only useful answer to
    # "how far is this". Picking any other way is arbitrary -- an earlier
    # version took the most populous and labelled a Henry County job as Polk.
    places = row.get("places") or []
    if not places and row.get("lat") is not None:
        places = [(row.get("county") or "", row["lat"], row["lon"])]
    if not places:
        _count("state_row_unplaceable")
        return
    county, lat, lon = min(
        places,
        key=lambda p: _miles_between(center["lat"], center["lon"], p[1], p[2]))
    miles = _miles_between(center["lat"], center["lon"], lat, lon)
    if miles > radius:
        _count("out_of_radius")
        return
    # This is an allowlist, so anything not named here is silently dropped --
    # which is exactly how "call" got lost once already, leaving every state
    # bid at the plan-holder fetcher with nothing to look up. "documents"
    # carries the job's own Bid Book and Plans links; without it the card
    # would send the contractor to the letting index instead.
    bid = {k: row[k] for k in
           ("title", "scope", "url", "deadline", "status", "source", "call",
            "documents")
           if k in row}
    bid["miles"] = int(round(miles))
    bid["county"] = county
    others = [c for c in (row.get("all_counties") or []) if c != county]
    if others:
        bid["also_in"] = others
    _apply_deadline_status(bid)
    # A letting that has already happened cannot be bid, and unlike a city
    # posting somebody may have saved, a state row is re-read from scratch on
    # every scan -- so a closed one has no purpose at all. Florida publishes
    # every letting from January onward on one page, which put 43 dead jobs
    # on a Tampa board against 8 live ones.
    if not _is_open_bid(bid):
        _count("state_letting_already_held")
        return None
    label = "%s County, %s" % (str(county).title(),
                               row.get("state") or center["state"])
    bucket = grouped.setdefault(label, [])
    key = _bid_dupe_key(bid)
    if key[0] and any(_bid_dupe_key(x) == key for x in bucket):
        _count("duplicate")
        return
    bucket.append(bid)
    _count("kept")
    _count("state_dot_kept")
    if city_coords is not None:
        city_coords[label] = {"lat": lat, "lon": lon}
    return bid


PLAN_HOLDER_MAX = int(os.environ.get("SCAN_PLAN_HOLDER_MAX", "12"))
PLAN_HOLDER_WORKERS = int(os.environ.get("SCAN_PLAN_HOLDER_WORKERS", "4"))
PLAN_HOLDER_BUDGET_SEC = float(
    os.environ.get("SCAN_PLAN_HOLDER_BUDGET_SEC", "8"))


def _attach_plan_holders(bids, letting_html, letting_url, stats=None):
    """Name the contractors bidding each state job, so a sub knows who to call.

    This is the answer to "why show me a highway contract I cannot win as
    prime". The prime bidders on that job need somebody to price the ramps and
    the sidewalk, and the letting publishes exactly who they are. Two of the
    eight holders on one MoDOT call were themselves concrete companies, which
    is the clearest evidence that subs already work this list.

    Only runs for bids that survived the radius, and only up to
    PLAN_HOLDER_MAX of them: it is one extra fetch per job, so it is spent on
    work the contractor can actually reach. Failure is per-bid and silent.

    NOT exported. These are named individuals' business contacts on a
    government page, shown in the context of the job they are bidding. The
    CSV export deliberately omits them -- see exportCSV in app.html.
    """
    index_url = bid_sources.plan_holder_index(letting_html, letting_url)
    if not index_url:
        return 0
    # Deadline first, then the count cap. A Branson scan spent 19 of its 83
    # seconds in this stage -- the MoDOT letting page itself took 2, and the
    # rest was twelve plan-holder fetches at four workers. Plan holders are a
    # per-job extra, not a bid: a scan should never spend a fifth of itself on
    # them. Raising the worker count would fix the clock by leaning harder on
    # one agency's server, which is the wrong trade for somebody else's
    # infrastructure, so this bounds the time instead.
    deadline = time.time() + PLAN_HOLDER_BUDGET_SEC
    targets = [b for b in bids if b.get("call")][:PLAN_HOLDER_MAX]
    if not targets:
        return 0

    def _one(bid):
        url = bid_sources.plan_holder_url_for_call(index_url, bid["call"])
        if not url:
            return 0
        page, outcome = _fetch_page(url, timeout=PROBE_TIMEOUT)
        if outcome != "ok" or not page:
            return 0
        holders = bid_sources.parse_plan_holders(page)
        if holders:
            bid["plan_holders"] = holders
            bid["plan_holder_url"] = url
        return len(holders)

    def _one_in_time(bid):
        # Checked per job rather than per batch: the pool runs several at
        # once, so whoever starts after the budget is gone simply does not.
        if time.time() >= deadline:
            return 0
        return _one(bid)

    with ThreadPoolExecutor(max_workers=PLAN_HOLDER_WORKERS) as ex:
        found = sum(ex.map(_one_in_time, targets))
    if time.time() >= deadline and stats is not None:
        stats["plan_holder_budget_spent"] = 1
    if stats is not None and found:
        stats["plan_holders_found"] = stats.get("plan_holders_found", 0) + found
        stats["plan_holder_jobs"] = stats.get("plan_holder_jobs", 0) + sum(
            1 for b in targets if b.get("plan_holders"))
    return found


def _run_state_sources(center, radius, grouped, city_coords=None, stats=None):
    """Read the letting page of every verified state the radius touches.

    Cheap by construction: one fetch per state, and a 125-mile scan touches at
    most four. Failure is per-state and silent -- a state page that is down
    costs its own rows, never the scan.
    """
    sources = _state_sources()
    if not sources:
        return 0
    try:
        states = counties.states_within(center["lat"], center["lon"], radius)
    except Exception:
        states = [center.get("state", "").upper()]
    todo = [(st,) + sources[st] for st in states if st in sources]
    if not todo:
        return 0

    # Fetch every state at once, then process them one at a time.
    #
    # This loop used to do both serially: up to four states, each with a
    # 20-second timeout, one after the other. That is 80 seconds of a scan's
    # budget spent waiting on sockets while nothing else happens, and it was
    # the only stage still doing it -- towns, portals, detail pages and the
    # federal source are all already concurrent.
    #
    # Only the FETCH is parallel. Parsing and placement stay on this thread
    # because _place_state_bid mutates `grouped` and `city_coords`, and the
    # win here is entirely in the waiting: making the mutation concurrent
    # would add a class of bug for no measurable gain.
    def _load(item):
        st, url, kind = item
        try:
            page, outcome = _fetch_page(url, timeout=STATE_SOURCE_TIMEOUT)
            if outcome != "ok" or not page:
                return st, url, None, outcome
            url, page = _resolve_state_listing(url, kind, page)
            return st, url, page, "ok"
        except Exception as ex:
            return st, url, None, "error:%s" % type(ex).__name__

    with ThreadPoolExecutor(max_workers=min(STATE_WORKERS, len(todo))) as ex:
        fetched = list(ex.map(_load, todo))

    placed = 0
    for st, url, page, outcome in fetched:
        try:
            if page is None:
                if stats is not None:
                    k = "state_fetch_%s" % outcome
                    stats[k] = stats.get(k, 0) + 1
                continue
            rows = bid_sources.parse_state_letting(
                page, st, url, counties.counties_named)
            if stats is not None:
                stats["state_rows_read"] = \
                    stats.get("state_rows_read", 0) + len(rows)
            landed = []
            for row in rows:
                before = sum(len(v) for v in grouped.values())
                kept = _place_state_bid(grouped, row, center, radius,
                                        city_coords, stats)
                if sum(len(v) for v in grouped.values()) > before:
                    placed += 1
                    if kept is not None:
                        landed.append(kept)
            # Only for jobs that made the board -- see _attach_plan_holders.
            if landed:
                _attach_plan_holders(landed, page, url, stats)
        except Exception as ex:      # never let a state page break a scan
            print("[scan] state source %s failed: %s" % (st, ex), flush=True)
    print("[scan] %d bids from %d state letting page(s)"
          % (placed, len(todo)), flush=True)
    return placed


# One scan's ceiling on detail reads on the keyless transport. Its search
# response carries no location and no contact, so a detail fetch is the only
# way to learn either -- one request per candidate. Measured over MO/KS/AR the
# real number was twelve, so this is headroom, not a constraint that bites.
# The keyed transport needs none of this: its search payload is already full.
FEDERAL_DETAIL_MAX = int(os.environ.get("SCAN_FEDERAL_MAX", "30"))
FEDERAL_WORKERS = int(os.environ.get("SCAN_FEDERAL_WORKERS", "6"))
FEDERAL_TIMEOUT = int(os.environ.get("SCAN_FEDERAL_TIMEOUT", "30"))
# A whole-source ceiling. Individual request timeouts multiply -- five trade
# codes across four states is twenty requests -- so the only thing that
# actually bounds a scan is a wall clock on the source as a whole.
FEDERAL_BUDGET_SEC = float(os.environ.get("SCAN_FEDERAL_BUDGET_SEC", "12"))


# A source that fails every scan should stop being asked.
#
# Federal cost 30.4 seconds of a Casey County scan and produced no bids at
# all: two keyed requests, both timing out at the full SAM_TIMEOUT, then the
# fallback. Capping the budget bounded that but did not stop it -- every
# scan paid it again, and a cap on a source that never succeeds is the wrong
# remedy. It should stop asking and check back later.
#
# Deliberately simple: consecutive failures, and a cooldown that only ends
# with a probe. No half-open state machine, because the cost of being wrong
# is one skipped federal source on one scan, and federal contributed 2 bids
# across the last ten scans.
_FEDERAL_TRIP_AFTER = int(os.environ.get("SCAN_FEDERAL_TRIP_AFTER", "3"))
_FEDERAL_COOLDOWN_SEC = float(os.environ.get("SCAN_FEDERAL_COOLDOWN", "900"))
_federal_breaker = {"fails": 0, "open_until": 0.0}
# A SECOND breaker, on the keyed transport alone.
#
# The first version of this rested the whole federal source, which was wrong
# and wasteful: it is the KEYED transport that times out from Render, while
# the public one answers fine and is what produced the only federal bids
# these scans have ever returned. Resting both meant a broken credential
# switched off a working source.
#
# So the keyed transport gets its own breaker. Once it has failed enough
# times the scan goes STRAIGHT to public -- not as a fallback after burning
# the budget on timeouts, but as the first and only call.
_keyed_breaker = {"fails": 0, "open_until": 0.0}


# Breaker state lives in the durable store, not in this process.
#
# In memory it did not work, and the way it failed was quiet: a Branson scan
# logged two keyed failures and /health reported keyed_failures 0 across six
# consecutive reads. Whether that is a second gunicorn worker or a restart
# does not matter -- a breaker whose memory is per-process cannot count
# consecutive failures across scans, which is the only thing it does.
#
# Falls back to the in-process dicts when no durable store is configured, so
# a local run still behaves sanely; it just cannot outlive the process.
_BREAKER_KEY = "bidcaller:federal_breakers"


def _breakers():
    if not kv_backend.is_durable():
        return {"federal": _federal_breaker, "keyed": _keyed_breaker}
    got = kv_backend.get(_BREAKER_KEY, None)
    if not isinstance(got, dict):
        got = {}
    for name, mem in (("federal", _federal_breaker), ("keyed", _keyed_breaker)):
        cur = got.get(name)
        if not isinstance(cur, dict):
            got[name] = dict(mem)
    return got


def _save_breakers(state):
    if kv_backend.is_durable():
        try:
            kv_backend.set(_BREAKER_KEY, state)
        except Exception:
            pass          # a breaker that cannot persist must not break a scan
    else:
        _federal_breaker.update(state.get("federal") or {})
        _keyed_breaker.update(state.get("keyed") or {})


def _breaker_open(name):
    try:
        return time.time() < float((_breakers().get(name) or {}).get("open_until") or 0)
    except Exception:
        return False


def _federal_breaker_open():
    """True while the whole federal source is being rested."""
    return _breaker_open("federal")


def _keyed_breaker_open():
    """True while the documented API is being skipped in favour of public."""
    return _breaker_open("keyed")


def _note_breaker_named(name, ok):
    st = _breakers()
    cur = dict(st.get(name) or {"fails": 0, "open_until": 0.0})
    if ok:
        cur = {"fails": 0, "open_until": 0.0}
    else:
        cur["fails"] = int(cur.get("fails") or 0) + 1
        if cur["fails"] >= _FEDERAL_TRIP_AFTER:
            cur["open_until"] = time.time() + _FEDERAL_COOLDOWN_SEC
    st[name] = cur
    _save_breakers(st)


def _note_breaker(state, ok):
    """Back-compat shim: route the two known dicts to their durable names."""
    _note_breaker_named("keyed" if state is _keyed_breaker else "federal", ok)


def _federal_note(ok):
    """Record whether the federal source answered at all this scan."""
    _note_breaker(_federal_breaker, ok)


def _federal_states(center, radius):
    """Every state the radius touches, not just the one under the pin.

    The old federal block asked SAM for center["state"] alone. That is the
    same bug _place_bid documents for cities: a 125-mile circle is usually
    several states wide, and the whole reason somebody picks that radius is
    to see across the line.
    """
    try:
        states = counties.states_within(center["lat"], center["lon"], radius)
    except Exception:
        states = [str(center.get("state") or "").upper()]
    return [s for s in states if s]


# ── Federal opportunities, fetched out of band ──────────────────────────────
#
# SAM's authenticated search does not answer inside a scan. Measured against
# the live service: a REJECTED request comes back in 0.6s, while an
# authenticated one -- even a two-day window asking for a single row -- does
# not return in forty seconds. Production agreed: nine consecutive failures,
# and exactly two timeouts per scan at six seconds each, which is the whole
# twelve-second federal budget spent to learn nothing.
#
# No timeout value fixes that. SAM_TIMEOUT sits inside FEDERAL_BUDGET_SEC
# inside a scan somebody is watching; raising either only makes the scan
# slower while still failing. The answer is to stop asking during the scan.
#
# So a scheduled job asks instead, where a slow API costs nobody anything, and
# scans read what it left behind. Two things fall out of that:
#
#   * Federal bids start appearing at all, for the first time.
#   * Twelve and a half seconds comes off the critical path of every scan.
#
# The refresh queries NATIONALLY, once per trade code -- six requests for the
# whole country. The live path had to filter by state server-side, which costs
# six requests per state and is exactly what made it unaffordable.
FEDERAL_CACHE_KEY = "bidcaller:federal_cache"
FEDERAL_CACHE_MAX_AGE_H = float(os.environ.get("FEDERAL_CACHE_MAX_AGE_H", "36"))
SAM_REFRESH_TIMEOUT = int(os.environ.get("SAM_REFRESH_TIMEOUT", "120"))

# The refresh is allowed to be slow, but not unbounded. Six trade codes at a
# 120s per-request timeout is 720 seconds of rope, and the worker is killed
# long before that: every one of the first ten scheduled runs died at exactly
# 240 seconds with an HTTP 500, because a killed worker cannot return the
# clean 503 this endpoint was written to send. It also cannot run Flask's
# error handler, so _alert_admin never fired and the failures were silent.
#
# So the whole job gets one wall clock, set under the platform's limit, and
# no single query may spend what is left of it.
FEDERAL_REFRESH_BUDGET_SEC = int(
    os.environ.get("FEDERAL_REFRESH_BUDGET_SEC", "200"))

# SAM being down does not get better on query four. Twelve consecutive
# failures were logged against the live service while each run still worked
# patiently through all six codes; stopping early turns a four-minute death
# into a ten-second report of what is actually wrong.
REFRESH_GIVE_UP_AFTER = int(os.environ.get("REFRESH_GIVE_UP_AFTER", "3"))
SAM_REFRESH_LIMIT = int(os.environ.get("SAM_REFRESH_LIMIT", "1000"))


def _federal_refresh():
    """Pull every concrete-trade federal notice in the country into the KV.

    Slow on purpose. Nothing is waiting on it.
    """
    if not SAM_API_KEY:
        return {"ok": False, "reason": "no_key"}
    queries = ([({"ncode": n}, True) for n in federal_bids.CONCRETE_NAICS] +
               [({"ccode": c}, False) for c in federal_bids.CONCRETE_PSC])
    rows, seen, failed, attempted = [], set(), 0, 0
    deadline = time.time() + FEDERAL_REFRESH_BUDGET_SEC
    consecutive, ran_dry = 0, ""
    for params, trusted in queries:
        left = deadline - time.time()
        if left <= 5:
            ran_dry = "budget_exhausted"
            break
        attempted += 1
        try:
            opps = _sam_fetch(None, timeout=min(SAM_REFRESH_TIMEOUT, int(left)),
                              limit=SAM_REFRESH_LIMIT, **params)
        except Exception:
            opps = None
        if opps is None:
            failed += 1
            consecutive += 1
            if consecutive >= REFRESH_GIVE_UP_AFTER:
                ran_dry = "sam_unavailable"
                break
            continue
        consecutive = 0
        for opp in opps:
            # SAM's documented schema is a list of objects, but this loop is
            # the only thing standing between one malformed entry and an
            # AttributeError that would kill the whole refresh -- every
            # remaining NAICS/PSC query for the run, not just this one row.
            if not isinstance(opp, dict):
                continue
            if not _is_construction(opp):
                continue
            if not trusted and not bid_sources.looks_relevant(opp.get("title")):
                continue
            # Amendments repeat a solicitation under a new notice id.
            key = (opp.get("solicitationNumber") or opp.get("noticeId") or "").lower()
            if key and key in seen:
                continue
            seen.add(key)
            try:
                bid, city, perf_state = _normalize_opp(opp)
            except Exception:
                continue
            if not bid:
                continue
            bid["city"], bid["state"] = city, perf_state
            rows.append(bid)
    # Never replace a good cache with the wreckage of a failed run. A scan
    # reading yesterday's federal bids is fine; reading none because the
    # refresh had a bad night is the outage this exists to prevent.
    if failed == attempted or not attempted:
        return {"ok": False, "reason": ran_dry or "all_queries_failed",
                "attempted": attempted,
                "sam_status": _sam_health_read().get("last_status")}
    payload = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "rows": rows, "queries": attempted, "failed": failed}
    try:
        kv_backend.set(FEDERAL_CACHE_KEY, payload)
    except Exception as ex:
        return {"ok": False, "reason": type(ex).__name__}
    return {"ok": True, "rows": len(rows), "queries": attempted,
            "failed": failed, "partial": ran_dry or ""}


def _federal_cache_age_h():
    """Hours since the last successful refresh, or None if there is none."""
    try:
        blob = kv_backend.get(FEDERAL_CACHE_KEY, None) or {}
        at = datetime.datetime.fromisoformat(blob.get("at"))
    except Exception:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc)
            - at).total_seconds() / 3600.0


def _federal_cached(states, stats):
    """Federal notices for these states, straight out of the KV. No network.

    Returns None when there is nothing usable to read, so the caller can fall
    back to the live path rather than reporting a quiet radius.
    """
    age = _federal_cache_age_h()
    if age is None:
        _bump(stats, "federal_cache_missing")
        return None
    if age > FEDERAL_CACHE_MAX_AGE_H:
        # Stale enough that a closed solicitation could be shown as open.
        _bump(stats, "federal_cache_stale")
        return None
    try:
        rows = (kv_backend.get(FEDERAL_CACHE_KEY, None) or {}).get("rows") or []
    except Exception:
        _bump(stats, "federal_cache_missing")
        return None
    want = {str(s).upper() for s in states}
    hits = [b for b in rows if str(b.get("state") or "").upper() in want]
    if stats is not None:
        stats["federal_from_cache"] = (stats.get("federal_from_cache", 0)
                                       + len(hits))
    return hits


@app.route("/run-federal-refresh", methods=["POST"])
def run_federal_refresh():
    """Scheduled. Gated by CRON_SECRET, like the other cron jobs."""
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("token") or request.headers.get("X-Cron-Secret", "")
    if not CRON_SECRET or not hmac.compare_digest(token, CRON_SECRET):
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    result = _federal_refresh()
    if result.get("ok"):
        _cron_beat("federal-refresh")
    return jsonify(result), (200 if result.get("ok") else 503)


def _federal_keyed(states, stats, deadline=None):
    """Candidates from the documented, keyed API.

    Its payload is complete -- place of performance and point of contact
    arrive with the search row -- so this needs no second request per notice.

    Queries are narrowed server-side, one per trade code per state, rather
    than pulling the state's whole year and filtering here. NAICS hits are
    trusted outright; PSC is broader than the trade, so those go through the
    normal relevance filter, same rule as the public transport.
    """
    out, failed, attempted = [], 0, 0
    seen = set()
    queries = ([({"ncode": n}, True) for n in federal_bids.CONCRETE_NAICS] +
               [({"ccode": c}, False) for c in federal_bids.CONCRETE_PSC])
    # One wall clock for the whole source. Per-request timeouts multiply --
    # six trade codes across four states is twenty-four requests -- so a
    # request timeout alone does not bound a scan. Checked as a flag rather
    # than a bare break, because break only leaves the inner loop and the
    # outer one would re-enter and re-count it once per remaining state.
    if deadline is None:
        deadline = time.time() + FEDERAL_BUDGET_SEC
    out_of_time = False
    for st in states:
        if out_of_time:
            break
        for params, trusted in queries:
            if time.time() > deadline:
                _bump(stats, "federal_budget_exhausted")
                out_of_time = True
                break
            attempted += 1
            try:
                opps = _sam_fetch(st, **params)
            except Exception:
                opps = None
            if opps is None:
                failed += 1
                _bump(stats, "federal_search_failed")
                continue
            if not opps:
                # Normal, and worth counting: most (state, trade code) pairs
                # match nothing on any given day. Without this, "the source
                # ran and found nothing" is indistinguishable from "every
                # request failed", which is the confusion this whole area
                # already cost two rounds of live scans to resolve.
                _bump(stats, "federal_search_empty")
                continue
            _bump(stats, "federal_search_ok")
            for opp in opps:
                # See the matching guard in the keyed path above: one
                # malformed entry must not take down every remaining
                # state/trade pair in this run.
                if not isinstance(opp, dict):
                    continue
                if not _is_construction(opp):
                    continue
                if not trusted and not bid_sources.looks_relevant(
                        opp.get("title")):
                    _bump(stats, "federal_psc_off_trade")
                    continue
                # Amendments repeat a solicitation; the notice id changes but
                # the solicitation number does not.
                key = (opp.get("solicitationNumber")
                       or opp.get("noticeId") or "").lower()
                if key and key in seen:
                    _bump(stats, "federal_amendment_collapsed")
                    continue
                seen.add(key)
                try:
                    bid, city, perf_state = _normalize_opp(opp)
                except Exception:
                    continue
                bid["city"], bid["state"] = city, perf_state
                out.append(bid)
    # A rejected key must not mean no federal bids at all. The public
    # transport reads the same public-domain data and needs no credentials,
    # so fall back rather than going quiet -- which is what happened once:
    # the keyed path returned nothing, bumped nothing, and the public one
    # could see eight active Texas notices the whole time.
    # Judge the KEYED transport on its own record, separately from whether
    # federal as a whole produced anything.
    if attempted:
        _note_breaker(_keyed_breaker, failed < attempted)
    if attempted and failed == attempted:
        # Inherit the remaining budget rather than starting a fresh one. The
        # keyed path failing is not a reason to spend the federal allowance
        # twice: a Woodford County scan burned 33 seconds this way -- two
        # 15-second timeouts, then the whole public path again -- for two
        # bids, which was 94% of all measured stage time.
        if time.time() >= deadline:
            _bump(stats, "federal_budget_exhausted")
            return out
        _bump(stats, "federal_fell_back_to_public")
        return _federal_public(states, stats, deadline)
    return out


def _federal_public(states, stats, deadline=None):
    """Candidates from sam.gov's own unauthenticated search.

    Exists so federal bids work before anybody has registered a key. Same
    public-domain data, one extra fetch per notice because this transport's
    search index omits location and contact.
    """
    seen, candidates = set(), []
    # NAICS queries are trusted outright; PSC queries are not, because the
    # code is broader than the trade. See federal_bids.CONCRETE_PSC.
    queries = ([({"naics": n}, True) for n in federal_bids.CONCRETE_NAICS] +
               [({"psc": c}, False) for c in federal_bids.CONCRETE_PSC])
    # This path also has to watch the clock. It is reached either directly or
    # as the keyed path's fallback, and in the fallback case most of the
    # federal allowance may already be gone.
    if deadline is None:
        deadline = time.time() + FEDERAL_BUDGET_SEC
    out_of_time = False
    for st in states:
        if out_of_time:
            break
        for params, trusted in queries:
            if time.time() >= deadline:
                _bump(stats, "federal_budget_exhausted")
                out_of_time = True
                break
            try:
                page, outcome = _fetch_page(
                    federal_bids.search_url(state=st, **params),
                    timeout=FEDERAL_TIMEOUT)
            except Exception:
                _bump(stats, "federal_search_error")
                continue
            if outcome != "ok" or not page:
                _bump(stats, "federal_search_%s" % outcome)
                continue
            try:
                rows = federal_bids.parse_search(page)
            except Exception:
                _bump(stats, "federal_search_unparsed")
                continue
            for row in rows:
                if not row.get("active") or not row.get("id"):
                    continue
                if not trusted and not bid_sources.looks_relevant(
                        row.get("title")):
                    _bump(stats, "federal_psc_off_trade")
                    continue
                # Collapse amendments BEFORE spending a detail fetch. A
                # solicitation reappears with every amendment and each is the
                # same job -- Fort Leavenworth's asphalt contract showed up
                # three times in one probe. The notice id changes; the
                # solicitation number does not, so key on that.
                key = (row.get("solicitation") or row["id"]).lower()
                if key in seen:
                    _bump(stats, "federal_amendment_collapsed")
                    continue
                seen.add(key)
                candidates.append(row)
    if len(candidates) > FEDERAL_DETAIL_MAX:
        _bump(stats, "federal_over_budget")
        candidates = candidates[:FEDERAL_DETAIL_MAX]

    def _detail(row):
        try:
            raw, outcome = _fetch_page(federal_bids.detail_url(row["id"]),
                                       timeout=FEDERAL_TIMEOUT)
        except Exception:
            return None
        if outcome != "ok" or not raw:
            return None
        try:
            return federal_bids.to_bid(row, federal_bids.parse_detail(raw))
        except Exception:
            return None

    if not candidates:
        return []
    with ThreadPoolExecutor(max_workers=FEDERAL_WORKERS) as ex:
        return [b for b in ex.map(_detail, candidates) if b]


def _bump(stats, reason):
    if stats is not None:
        stats[reason] = stats.get(reason, 0) + 1


def _run_federal_sources(center, radius, grouped, cdb, city_coords=None,
                         stats=None, pdb=None):
    """Add federal solicitations whose work falls inside the radius.

    Federal work was already wired up but could never have produced a bid:
    the default endpoint 404s, the relevance test was title-keywords that
    real federal titles do not use, and only the centre state was asked.
    Roughly 1,200 notices in the concrete NAICS codes are open nationwide at
    any moment and the app saw none of them.

    Worth more than its count suggests, because of what arrives with each
    one. Of nine placeable jobs in a MO/KS/AR probe, nine had a named
    contracting officer with a phone or an email, and all nine were
    small-business set-asides. Contacts are the app's second-biggest quality
    gap everywhere else; here they are simply part of the record, and no
    extraction call is spent to get them.
    """
    states = _federal_states(center, radius)
    if not states:
        return 0
    # Only the LIVE path is worth resting. A breaker opened by SAM timing out
    # must not also switch off a cache read that costs a millisecond and
    # cannot fail in the same way -- that would keep federal bids hidden for
    # exactly as long as the thing that made them work.
    if _federal_breaker_open() and _federal_cache_age_h() is None:
        # Resting. Costs one skipped source on one scan; the alternative is
        # paying thirty seconds a scan to fail at the same thing.
        _bump(stats, "federal_resting")
        return 0
    # One clock for the stage, handed to whichever transport runs -- and to
    # the fallback if the keyed one gives out, so a failing key cannot spend
    # the federal allowance twice.
    # The cache first, and usually only the cache. It is filled by
    # /run-federal-refresh on a schedule, where SAM being slow costs nobody
    # anything; reading it is a dictionary lookup. The live paths below stay
    # as the fallback for a project that has never run the job, or whose
    # refresh has been failing long enough for the data to go stale.
    cached = _federal_cached(states, stats)
    if cached is not None:
        bids = cached
        deadline = time.time() + FEDERAL_BUDGET_SEC   # unused; kept for shape
    else:
        deadline = time.time() + FEDERAL_BUDGET_SEC
        use_keyed = bool(SAM_API_KEY) and not _keyed_breaker_open()
        if SAM_API_KEY and not use_keyed:
            # The documented API is resting; the public one reads the same
            # public-domain data and is currently the only one that answers.
            _bump(stats, "federal_keyed_resting")
        bids = (_federal_keyed(states, stats, deadline) if use_keyed
                else _federal_public(states, stats, deadline))
    # "Answered" means the transport worked, not that it found anything --
    # a genuinely quiet radius must not trip the breaker.
    answered = bool(bids) or bool(stats and (stats.get("federal_search_ok")
                                             or stats.get("federal_search_empty")))
    _federal_note(answered)

    placed = 0
    for bid in bids:
        if not bid:
            _bump(stats, "federal_unplaceable")
            continue
        _apply_deadline_status(bid)
        # Same rule the state reader applies. "Active" at SAM means the
        # notice is live, not that its response date is still ahead: three
        # of twelve probe hits were already past theirs.
        if not _is_open_bid(bid):
            _bump(stats, "federal_already_closed")
            continue
        before = sum(len(v) for v in grouped.values())
        _place_bid(grouped, bid, center, radius, cdb,
                   default_city=bid.get("city", ""),
                   city_coords=city_coords,
                   default_state=bid.get("state", ""),
                   stats=stats, pdb=pdb)
        if sum(len(v) for v in grouped.values()) > before:
            placed += 1
            _bump(stats, "federal_kept")
    print("[scan] %d federal bids from %d candidate(s) in %s (%s transport)"
          % (placed, len(bids), ",".join(states),
             "keyed" if use_keyed else "public"), flush=True)
    return placed


ENRICH_MAX = int(os.environ.get("SCAN_ENRICH_MAX", "14"))

# One wall clock for the WHOLE scan.
#
# Every stage had its own budget and they simply added: 40s of known-town
# reads, then unbudgeted search and extraction, then state pages, then 25s of
# federal, then enrichment, then plan holders. Nothing capped the sum, so a
# 125-mile scan from Republic, MO ran past the client's 150-second timeout --
# it finished and banked 32 bids, but the phone had already given up.
#
# A RUNAWAY GUARD, not a routine trimmer. It sits just under the client's
# 150-second timeout, so a normal scan finishes well inside it and loses
# nothing; it only bites on the pathological run that would otherwise grow
# without limit.
#
# It was 95s first, which was too aggressive and set before the app learned
# to collect a scan that overruns. With that in place an overrunning scan is
# no longer a failure -- the client re-requests and is served the full
# cached result -- so cutting stages early costs bids for no benefit.
#
# What the ordering protects matters as much as the number. The guarded
# stages run state -> federal -> agency -> enrich, so time runs out at the
# END and enrichment is sacrificed first. Enrichment adds no bids: it fills
# contacts and deadlines on bids already found. So the first thing a slow
# scan loses is phone numbers, not listings, and the bid-producing stages
# have to be well past the budget before they are touched at all.
SCAN_BUDGET_SEC = float(os.environ.get("SCAN_BUDGET_SEC", "135"))

