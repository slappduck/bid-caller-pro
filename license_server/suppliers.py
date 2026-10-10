# ===========================================================================
# READY-MIX SUPPLIERS AND CONCRETE PRICES
#
# Two things a contractor needs before pricing flatwork: who can deliver
# concrete to this job, and roughly what it costs. Nobody publishes local
# ready-mix prices -- each plant quotes its own -- so prices here come from
# what CurbCall users log after getting quotes, shown only as an area range
# and only once there are enough of them that no one quote can be picked out.
#
#   POST /suppliers/nearby  {city, state}         plants near a town, + area quotes
#   POST /suppliers/quote   {city, state, price_per_cy, yards, ...}   log a quote
#
# Plants come from OpenStreetMap (concrete works, and businesses named ready
# mix or redi-mix); where it knows fewer than three, a web search for ready
# mix in that town fills in links. Cached per area for a month: plants don't
# move, and Overpass and Brave both ask callers not to hammer them.
# ===========================================================================
import statistics

SUPPLIER_RADIUS_MI = 50          # about as far as a ready-mix plant delivers
SUPPLIER_MAX = 25
SUPPLIER_CACHE_DAYS = 30
_SUPPLIER_CACHE_PREFIX = "bidcaller:suppliers:"
_SUPPLIER_RATE_KEY = "bidcaller:suppliers_rate"
SUPPLIER_MAX_PER_KEY_PER_DAY = 60
_QUOTE_RATE_KEY = "bidcaller:concrete_quote_rate"
QUOTE_MAX_PER_KEY_PER_DAY = 20
# Area prices: quotes this close and this recent, and only when there are at
# least this many from at least this many different contractors.
QUOTE_RADIUS_MI = 75
QUOTE_MAX_AGE_DAYS = 365
QUOTE_MIN_COUNT = 3
QUOTE_MIN_USERS = 2
# Anything outside these is a typo (a total typed as a per-yard price), not a quote.
QUOTE_PRICE_RANGE = (40.0, 600.0)
QUOTE_YARDS_RANGE = (0.25, 20000.0)

_OVERPASS_URLS = ("https://overpass-api.de/api/interpreter",
                  "https://overpass.kumi.systems/api/interpreter")
_READY_MIX_NAME = re.compile(
    r"ready[\s-]?mix|redi[\s-]?mix|readi[\s-]?mix|concrete\s+(?:plant|batch|supply|products|materials)"
    r"|batch\s+plant", re.I)
# Named like a concrete contractor, not a plant: never listed off its name alone.
_CONTRACTOR_NAME = re.compile(r"construction|contracting|contractors?\b|flatwork|finishing|pumping"
                              r"|repair|leveling|lifting|coatings?|staining|polish", re.I)


def _supplier_query(lat, lon, radius_m):
    a = f"around:{int(radius_m)},{lat:.5f},{lon:.5f}"
    return ("[out:json][timeout:40];("
            f'nwr["man_made"="works"]["product"~"concrete",i]({a});'
            f'nwr["industrial"~"concrete",i]({a});'
            f'nwr["name"~"ready.?mix|redi.?mix|readi.?mix|concrete (plant|batch|supply|products|materials)|batch plant",i]({a});'
            ");out tags center;")


def _overpass(query):
    body = urllib.parse.urlencode({"data": query}).encode()
    for url in _OVERPASS_URLS:
        try:
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"User-Agent": _NOMINATIM_UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8")).get("elements") or []
        except Exception as ex:
            print(f"[suppliers] overpass {url}: {ex}", flush=True)
    return None


def _supplier_address(t):
    street = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x)
    town = t.get("addr:city") or ""
    st = t.get("addr:state") or ""
    return ", ".join(x for x in (street, town, " ".join(x for x in (st, t.get("addr:postcode")) if x)) if x)


def _supplier_rows(elements, lat, lon):
    """Overpass elements -> one row per plant, nearest first. A works or
    industrial feature counts on its tags; anything else only when its name
    says ready mix, and never when the name says contractor."""
    rows, seen = [], set()
    for e in elements or []:
        t = e.get("tags") or {}
        name = " ".join(str(t.get("name") or t.get("operator") or t.get("brand") or "").split())
        if not name:
            continue
        tagged = (t.get("man_made") == "works" and "concrete" in str(t.get("product", "")).lower()) \
            or "concrete" in str(t.get("industrial", "")).lower()
        if not tagged and (not _READY_MIX_NAME.search(name) or _CONTRACTOR_NAME.search(name)):
            continue
        plat = e.get("lat", (e.get("center") or {}).get("lat"))
        plon = e.get("lon", (e.get("center") or {}).get("lon"))
        if plat is None or plon is None:
            continue
        dist = _miles_between(lat, lon, float(plat), float(plon))
        key = (name.lower(), round(float(plat), 2), round(float(plon), 2))
        if key in seen or dist > SUPPLIER_RADIUS_MI:
            continue
        seen.add(key)
        site = t.get("website") or t.get("contact:website") or t.get("url") or ""
        rows.append({
            "name": name[:120],
            "miles": round(dist, 1),
            "lat": round(float(plat), 5), "lon": round(float(plon), 5),
            "phone": str(t.get("phone") or t.get("contact:phone") or "")[:40],
            "website": site if re.match(r"^https?://", site, re.I) else (f"https://{site}" if site and "." in site else ""),
            "email": str(t.get("email") or t.get("contact:email") or "")[:120],
            "address": _supplier_address(t)[:160],
            "source": "OpenStreetMap",
        })
    rows.sort(key=lambda r: r["miles"])
    return rows[:SUPPLIER_MAX]


def _supplier_web(city, state):
    """Links from a web search, for an area the map knows little about.
    Directories and social pages are dropped: the point is the plant's own site."""
    out, seen = [], set()
    for r in _brave_search(f"ready mix concrete {city} {state}", max_results=10):
        url = r.get("url") or ""
        host = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
        if not host or host in seen or re.search(
                r"yelp|facebook|yellowpages|mapquest|bbb\.org|angi|homeadvisor|thumbtack|houzz|"
                r"linkedin|indeed|manta|nextdoor|instagram|reddit|wikipedia|youtube|google\.", host):
            continue
        seen.add(host)
        out.append({"site": host, "url": url, "snippet": str(r.get("content") or "")[:200]})
    return out[:6]


def _suppliers_near(city, state):
    geo = _geo_from_city(city, state)
    if not geo:
        return None
    cell = f"{round(geo['lat'] * 4) / 4:.2f},{round(geo['lon'] * 4) / 4:.2f}"
    cache_key = _SUPPLIER_CACHE_PREFIX + cell
    try:
        hit = kv_backend.get(cache_key, None)
    except Exception:
        hit = None
    if isinstance(hit, dict) and time.time() - float(hit.get("at") or 0) < SUPPLIER_CACHE_DAYS * 86400:
        plants, web = hit.get("plants") or [], hit.get("web") or []
    else:
        elements = _overpass(_supplier_query(geo["lat"], geo["lon"], SUPPLIER_RADIUS_MI * 1609.34))
        plants = _supplier_rows(elements, geo["lat"], geo["lon"]) if elements is not None else []
        web = _supplier_web(geo["city"], state) if len(plants) < 3 else []
        if elements is not None:     # a failed map read is retried next time, not cached
            try:
                kv_backend.set(cache_key, {"at": time.time(), "plants": plants, "web": web})
            except Exception:
                pass
    # Distances from this town, not the cell's center the cache was filed under.
    for p in plants:
        p["miles"] = round(_miles_between(geo["lat"], geo["lon"], p["lat"], p["lon"]), 1)
    plants = sorted((p for p in plants if p["miles"] <= SUPPLIER_RADIUS_MI), key=lambda p: p["miles"])
    return {"lat": geo["lat"], "lon": geo["lon"], "plants": plants, "web": web}


def _area_quotes(lat, lon):
    """Logged quotes near here, as a range, or None while there are too few
    to show without pointing at one contractor's price."""
    since = (datetime.datetime.utcnow() - datetime.timedelta(days=QUOTE_MAX_AGE_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    dlat, dlon = QUOTE_RADIUS_MI / 69.0, QUOTE_RADIUS_MI / max(1.0, 69.0 * math.cos(math.radians(lat)))
    rows = _supabase_admin_request(
        "/rest/v1/concrete_quotes?select=user_id,lat,lon,price_per_cy"
        f"&lat=gte.{lat - dlat:.3f}&lat=lte.{lat + dlat:.3f}"
        f"&lon=gte.{lon - dlon:.3f}&lon=lte.{lon + dlon:.3f}"
        f"&created_at=gte.{since}&limit=2000")
    if not isinstance(rows, list):
        return None
    near = [r for r in rows if _miles_between(lat, lon, float(r["lat"]), float(r["lon"])) <= QUOTE_RADIUS_MI]
    if len(near) < QUOTE_MIN_COUNT or len({r["user_id"] for r in near}) < QUOTE_MIN_USERS:
        return {"enough": False, "count": len(near)}
    prices = sorted(float(r["price_per_cy"]) for r in near)
    return {"enough": True, "count": len(prices), "low": round(prices[0], 2), "high": round(prices[-1], 2),
            "median": round(statistics.median(prices), 2), "radius_mi": QUOTE_RADIUS_MI}


def _supplier_place(data):
    city = " ".join(str(data.get("city") or "").split())[:80]
    state = str(data.get("state") or "").strip().upper()[:2]
    return (city, state) if city and re.fullmatch(r"[A-Z]{2}", state) else (None, None)


@app.route("/suppliers/nearby", methods=["POST"])
def suppliers_nearby():
    data = request.get_json(force=True, silent=True) or {}
    key, device = data.get("key", ""), data.get("device_id", "")
    if not _license_is_active(key, device, data.get("supabase_token", "")):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    city, state = _supplier_place(data)
    if not city:
        return jsonify({"ok": False, "reason": "bad_place"}), 400
    if not _ip_rate_ok(_SUPPLIER_RATE_KEY, key or device or _client_ip(), SUPPLIER_MAX_PER_KEY_PER_DAY):
        return jsonify({"ok": False, "reason": "rate_limited"}), 429
    found = _suppliers_near(city, state)
    if not found:
        return jsonify({"ok": False, "reason": "place_not_found"})
    return jsonify({"ok": True, "city": city, "state": state, "radius_mi": SUPPLIER_RADIUS_MI,
                    "plants": found["plants"], "web": found["web"],
                    "quotes": _area_quotes(found["lat"], found["lon"])})


def _quote_number(v, lo, hi):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if lo <= x <= hi and math.isfinite(x) else None


@app.route("/suppliers/quote", methods=["POST"])
def suppliers_quote():
    """A quote the contractor got, kept with their account and a location
    rounded to about seven miles. Never shown on its own: it only ever
    counts toward an area's range."""
    data = request.get_json(force=True, silent=True) or {}
    user = _supabase_user(data.get("supabase_token", ""))
    if not user or not user.get("id"):
        return jsonify({"ok": False, "reason": "sign_in"}), 403
    city, state = _supplier_place(data)
    price = _quote_number(data.get("price_per_cy"), *QUOTE_PRICE_RANGE)
    yards = _quote_number(data.get("yards"), *QUOTE_YARDS_RANGE)
    if not city or price is None or yards is None:
        return jsonify({"ok": False, "reason": "bad_quote"}), 400
    if not _ip_rate_ok(_QUOTE_RATE_KEY, user["id"], QUOTE_MAX_PER_KEY_PER_DAY):
        return jsonify({"ok": False, "reason": "rate_limited"}), 429
    geo = _geo_from_city(city, state)
    if not geo:
        return jsonify({"ok": False, "reason": "place_not_found"})
    row = {"user_id": user["id"], "lat": round(geo["lat"], 1), "lon": round(geo["lon"], 1), "state": state,
           "price_per_cy": round(price, 2), "yards": round(yards, 2),
           "delivery_fee": _quote_number(data.get("delivery_fee"), 0, 2000),
           "short_load_fee": _quote_number(data.get("short_load_fee"), 0, 5000),
           "psi": int(_quote_number(data.get("psi"), 2000, 10000) or 0) or None}
    if _supabase_admin_request("/rest/v1/concrete_quotes", method="POST", data=row) is None:
        return jsonify({"ok": False, "reason": "not_saved"}), 503
    return jsonify({"ok": True})
