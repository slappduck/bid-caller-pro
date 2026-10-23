# ===========================================================================
# READY-MIX SUPPLIERS AND CONCRETE PRICES
#
# Two things a contractor needs before pricing flatwork: who can deliver
# concrete to this job, and roughly what it costs. Nobody publishes local
# ready-mix prices -- each plant quotes its own -- so prices here come from
# what CurbCall users log after getting quotes, shown only as an area range
# and only once there are enough of them that no one quote can be picked out.
#
#   POST /suppliers/nearby  {city, state, near?}  plants near the job (or town), + area quotes
#   POST /suppliers/quote   {city, state, price_per_cy, yards, ...}   log a quote
#
# Plants come from three places, asked at once and given a fixed time, so
# one slow source can't sink the search: OpenStreetMap's map (concrete works
# and businesses named ready mix), OpenStreetMap's place search (lighter,
# answers when the map is overloaded), and -- where those know fewer than
# four -- a web search that names plants with their sites and phones. The
# public map servers are often slow or refuse busy-hour queries; the search
# used to wait on them for up to two minutes and the app gave up first.
# Cached per area for a month when the map answered, a few hours when it didn't.
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
                  "https://overpass.private.coffee/api/interpreter")
# Seconds the whole search may take, inside the app's own wait.
SUPPLIER_TIME_BUDGET = 40
SUPPLIER_PARTIAL_CACHE_HOURS = 6
_READY_MIX_NAME = re.compile(
    r"ready[\s-]?mix|redi[\s-]?mix|readi[\s-]?mix|concrete\s+(?:plant|batch|supply|products|materials)"
    r"|batch\s+plant", re.I)
# Named like a concrete contractor, not a plant: never listed off its name alone.
_CONTRACTOR_NAME = re.compile(r"construction|contracting|contractors?\b|flatwork|finishing|pumping"
                              r"|repair|leveling|lifting|coatings?|staining|polish", re.I)
# Concrete things that aren't delivered ready mix: never a plant, even when
# mapped as a concrete works.
_NOT_READY_MIX = re.compile(r"siding|\bblocks?\b|pipe|pre-?cast|vault|septic|statuary|countertop|cutting", re.I)


def _supplier_query(lat, lon, radius_m):
    a = f"around:{int(radius_m)},{lat:.5f},{lon:.5f}"
    return ("[out:json][timeout:20];("
            f'nwr["man_made"="works"]["product"~"concrete",i]({a});'
            f'nwr["industrial"~"concrete",i]({a});'
            f'nwr["name"~"ready.?mix|redi.?mix|readi.?mix|concrete (plant|batch|supply|products|materials)|batch plant",i]({a});'
            ");out tags center;")


def _overpass(query, deadline=None):
    """Elements, or None when no server answered in time."""
    body = urllib.parse.urlencode({"data": query}).encode()
    for url in _OVERPASS_URLS:
        left = (deadline - time.time()) if deadline else 25
        if left < 5:
            break
        try:
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"User-Agent": _NOMINATIM_UA})
            with urllib.request.urlopen(req, timeout=min(25, left)) as resp:
                return json.loads(resp.read().decode("utf-8")).get("elements") or []
        except Exception as ex:
            print(f"[suppliers] overpass {url}: {ex}", flush=True)
    return None


def _nominatim_plants(lat, lon):
    """Plants from OpenStreetMap's place search: named ready mix (or a
    concrete works), inside the delivery radius. Never raises."""
    dlat = SUPPLIER_RADIUS_MI / 69.0
    dlon = SUPPLIER_RADIUS_MI / max(1.0, 69.0 * math.cos(math.radians(lat)))
    box = f"{lon - dlon:.4f},{lat + dlat:.4f},{lon + dlon:.4f},{lat - dlat:.4f}"
    rows = []

    def ask(q):
        with _nominatim_lock:
            wait = 1.0 - (time.time() - _nominatim_last[0])
            if wait > 0:
                time.sleep(wait)
            _nominatim_last[0] = time.time()
        qs = urllib.parse.urlencode({"q": q, "format": "jsonv2", "limit": "40", "extratags": "1",
                                     "addressdetails": "1", "viewbox": box, "bounded": "1",
                                     "countrycodes": "us"})
        return _get_json(f"{_NOMINATIM_URL}?{qs}", headers={"User-Agent": _NOMINATIM_UA,
                                                             "Accept": "application/json"}, timeout=12)
    for q in ("ready mix", "redi mix", "concrete"):
        data = ask(q)
        if data is None:          # turned away for asking too fast: once more, slower
            time.sleep(1.5)
            data = ask(q)
        for d in data if isinstance(data, list) else []:
            name = " ".join(str(d.get("name") or "").split())
            works = d.get("category") == "man_made" and d.get("type") == "works"
            if not name or _CONTRACTOR_NAME.search(name) or _NOT_READY_MIX.search(name):
                continue
            if not (_READY_MIX_NAME.search(name) or (works and re.search(r"concrete", name, re.I))
                    or (d.get("category") == "landuse" and re.search(r"concrete", name, re.I))):
                continue
            try:
                plat, plon = float(d["lat"]), float(d["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            t = d.get("extratags") or {}
            a = d.get("address") or {}
            street = " ".join(x for x in (a.get("house_number"), a.get("road")) if x)
            town = a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or ""
            site = t.get("website") or t.get("contact:website") or ""
            rows.append({"name": name[:120], "lat": round(plat, 5), "lon": round(plon, 5),
                         "miles": round(_miles_between(lat, lon, plat, plon), 1),
                         "phone": str(t.get("phone") or t.get("contact:phone") or "")[:40],
                         "website": site if re.match(r"^https?://", site, re.I) else "",
                         "email": str(t.get("email") or t.get("contact:email") or "")[:120],
                         "address": ", ".join(x for x in (street, town, a.get("state") or "") if x)[:160],
                         "source": "OpenStreetMap"})
    return rows


# A web search for the plants the map doesn't know. The model may only
# report businesses its search found, each with its own site or phone; the
# rest is checked here (a real http link, a town that geocodes inside the
# delivery radius) before anything is shown.
_AI_PLANTS_PROMPT = (
    "Use web search to find ready-mix concrete plants (companies that batch and deliver "
    "ready-mix concrete) within about {radius} miles of {place}. Not concrete contractors "
    "who only pour or finish. Return ONLY a JSON array of at most 10 objects with keys "
    "\"name\", \"address\", \"city\", \"state\" (2 letters), \"phone\", \"website\". "
    "Only businesses you found in the search results; \"\" for anything not found.")


def _ai_plants(place, state, lat, lon, deadline):
    if not OPENAI_API_KEY:
        return []
    prompt = _AI_PLANTS_PROMPT.format(radius=SUPPLIER_RADIUS_MI, place=place)
    text = ""
    for tool in ("web_search", "web_search_preview"):
        left = deadline - time.time()
        if left < 8:
            return []
        body = json.dumps({"model": OPENAI_MODEL, "tools": [{"type": tool}], "input": prompt}).encode()
        req = urllib.request.Request("https://api.openai.com/v1/responses", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=min(35, left)) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(f"[suppliers] web search ({tool}) HTTP {e.code}", flush=True)
            if e.code == 400:
                continue
            return []
        except Exception as ex:
            print(f"[suppliers] web search: {ex}", flush=True)
            return []
        for item in data.get("output") or []:
            if item.get("type") == "message":
                for c in item.get("content") or []:
                    if c.get("type") == "output_text":
                        text += c.get("text") or ""
        break
    m = re.search(r"\[.*\]", text, re.S)
    try:
        found = json.loads(m.group(0)) if m else []
    except ValueError:
        found = []
    rows = []
    for f in found[:10] if isinstance(found, list) else []:
        if not isinstance(f, dict):
            continue
        name = " ".join(str(f.get("name") or "").split())[:120]
        site = str(f.get("website") or "").strip()
        site = site if re.match(r"^https?://[^\s/]+\.[a-z]{2,}", site, re.I) else ""
        phone = re.sub(r"[^\d()+\- .]", "", str(f.get("phone") or ""))[:40].strip()
        if not name or _CONTRACTOR_NAME.search(name) or _NOT_READY_MIX.search(name) \
                or not (site or len(re.sub(r"\D", "", phone)) >= 10):
            continue
        town = " ".join(str(f.get("city") or "").split())[:60]
        st = str(f.get("state") or state).strip().upper()[:2]
        hit = _zippopotam_city(town, st) if town and re.fullmatch(r"[A-Z]{2}", st) else None
        miles = round(_miles_between(lat, lon, hit[0], hit[1]), 1) if hit else None
        if miles is not None and miles > SUPPLIER_RADIUS_MI + 10:
            continue
        addr = " ".join(str(f.get("address") or "").split())[:120]
        rows.append({"name": name, "lat": hit[0] if hit else None, "lon": hit[1] if hit else None,
                     "miles": miles, "phone": phone, "website": site, "email": "",
                     "address": ", ".join(x for x in (addr, town, st) if x and x not in addr)[:160] or addr,
                     "source": "web"})
    return rows


def _merge_plants(*lists):
    """One row per plant: the same name close by (or with no place) is one."""
    out = []
    for rows in lists:
        for r in rows or []:
            key = re.sub(r"[^a-z0-9]", "", r["name"].lower())
            dup = next((o for o in out if re.sub(r"[^a-z0-9]", "", o["name"].lower()) == key and (
                o.get("lat") is None or r.get("lat") is None
                or _miles_between(o["lat"], o["lon"], r["lat"], r["lon"]) < 5)), None)
            if dup:
                for k in ("phone", "website", "email", "address"):
                    if not dup.get(k) and r.get(k):
                        dup[k] = r[k]
                continue
            out.append(dict(r))
    return out


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
        if _NOT_READY_MIX.search(name):
            continue
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


# ── Where the job is ──
# A bid in the feed knows only the place its search was for, and for a
# statewide search that's the whole state ("Oklahoma"). The job itself is
# usually named in the bid: "Kickingbird Golf Course Parking Lot" is in
# Edmond, the agency's web address says edmondok.gov. So the job is placed
# from, in order: an address the contractor or the documents gave; a site
# the model picks out of the bid's own title, description and web address
# (it may only copy what's there, never guess); the bid's town.
BID_LOCATE_CACHE_DAYS = 30
_BID_LOCATE_PREFIX = "bidcaller:bid_locate:"
_LOCATE_PROMPT = (
    "Where is the work for this public construction bid? Use ONLY the text and web address "
    "below. Return ONE JSON object: {\"place\": the street address, intersection, or named "
    "site (park, school, golf course, building) where the work is, as written, else \"\"; "
    "\"city\": the city or town the work is in, if the text or the agency's web address "
    "makes it clear, else \"\"; \"state\": 2-letter code if clear, else \"\"}. "
    "Never guess. No markdown.\n\n")


def _ai_bid_place(title, scope, url):
    """{place, city, state} the bid itself names, cached. {} when unknown."""
    text = f"TITLE: {title}\nDESCRIPTION: {scope}\nWEB ADDRESS: {url}"[:3000]
    key = _BID_LOCATE_PREFIX + hashlib.sha256(text.encode()).hexdigest()[:32]
    try:
        hit = kv_backend.get(key, None)
    except Exception:
        hit = None
    if isinstance(hit, dict) and time.time() - float(hit.get("at") or 0) < BID_LOCATE_CACHE_DAYS * 86400:
        return hit.get("found") or {}
    if not OPENAI_API_KEY or not (title or scope):
        return {}
    body = json.dumps({"model": OPENAI_MODEL, "temperature": 0, "response_format": {"type": "json_object"},
                       "messages": [{"role": "user", "content": _LOCATE_PROMPT + text}]}).encode()
    req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            got = json.loads(json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"])
    except Exception as ex:
        print(f"[locate] model: {ex}", flush=True)
        return {}
    found = {}
    if isinstance(got, dict):
        for k, n in (("place", 160), ("city", 80), ("state", 20)):
            v = got.get(k)
            if isinstance(v, str) and v.strip():
                found[k] = " ".join(v.split())[:n]
    try:
        kv_backend.set(key, {"at": time.time(), "found": found})
    except Exception:
        pass
    return found


_CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"


def _census_address(address, state):
    """(lat, lon) for a US street address, from the Census Bureau's
    geocoder, or None. It knows house numbers OpenStreetMap often lacks, and
    "1017 S Ash, Ottawa" without the "St" that OpenStreetMap needs."""
    qs = urllib.parse.urlencode({"address": f"{address}, {state}", "benchmark": "Public_AR_Current",
                                 "format": "json"})
    data = _get_json(f"{_CENSUS_URL}?{qs}", timeout=12)
    try:
        m = data["result"]["addressMatches"][0]["coordinates"]
        return float(m["y"]), float(m["x"])
    except (TypeError, KeyError, IndexError, ValueError):
        return None


def _geocode_place(text, state):
    """A street address goes to the Census geocoder first; a named place
    (a park, a golf course) to OpenStreetMap."""
    if re.match(r"\s*\d+[A-Za-z]?\s+\S", text):
        hit = _census_address(text, state)
        if hit:
            return hit
    return _nominatim_place(text, state)


def _job_location(city, state, near="", title="", scope="", url=""):
    """{lat, lon, label, how, approx} -- how is "job" (an address given),
    "bid" (a site the bid names) or "town" -- or None when nothing places.

    The words of an address are kept even when no map can place them
    exactly: they're what a maps app, an email and a form need. The point is
    then the town's, marked approx, good enough for finding plants."""
    geo = _geo_from_city(city, state) if city else None

    def close(hit):
        # A place miles from the bid's own town is another one of the same name.
        return hit and (not geo or _miles_between(geo["lat"], geo["lon"], hit[0], hit[1]) < 60)

    def label(*parts):
        out = []
        for x in parts:
            if x and not any(x.lower() in o.lower() for o in out):
                out.append(x)
        return ", ".join(out)
    if near:
        hit = _geocode_place(f"{near}, {city}" if city else near, state)
        if close(hit):
            return {"lat": hit[0], "lon": hit[1], "label": label(near, city), "how": "job", "approx": False}
        if geo:
            return {"lat": geo["lat"], "lon": geo["lon"], "label": label(near, city), "how": "job", "approx": True}
    if title or scope:
        found = _ai_bid_place(title, scope, url)
        st = _normalize_state(found.get("state", "")) or state
        if st == state:
            town = found.get("city") or city
            place = found.get("place", "")
            tg = geo if not town or town.lower() == str(city).lower() else _geo_from_city(town, state)
            tg = tg if tg and close((tg["lat"], tg["lon"])) else None
            if place:
                hit = _geocode_place(f"{place}, {town}" if town else place, state)
                if close(hit):
                    return {"lat": hit[0], "lon": hit[1], "how": "bid", "approx": False,
                            "label": label(place, town, state)}
                base = tg or geo
                if base:
                    return {"lat": base["lat"], "lon": base["lon"], "how": "bid", "approx": True,
                            "label": label(place, town or city, state)}
            if tg and tg is not geo:
                return {"lat": tg["lat"], "lon": tg["lon"], "label": f"{tg['city']}, {state}", "how": "bid",
                        "approx": True}
    if geo:
        return {"lat": geo["lat"], "lon": geo["lon"], "label": f"{geo['city']}, {state}", "how": "town",
                "approx": True}
    return None


def _supplier_where(data):
    """(city, state) for a search: the city may be blank, or a whole state
    ("Oklahoma") from a statewide search, as long as the state is known."""
    city = " ".join(str(data.get("city") or "").split())[:80]
    state = _normalize_state(data.get("state", "")) or _normalize_state(city)
    if _normalize_state(city):
        city = ""
    return city, state


@app.route("/bid/locate", methods=["POST"])
def bid_locate():
    data = request.get_json(force=True, silent=True) or {}
    if not _license_is_active(data.get("key", ""), data.get("device_id", ""), data.get("supabase_token", "")):
        return jsonify({"ok": False, "reason": "not_licensed"}), 403
    city, state = _supplier_where(data)
    if not state:
        return jsonify({"ok": False, "reason": "bad_place"}), 400
    if not _ip_rate_ok(_SUPPLIER_RATE_KEY, data.get("key") or data.get("device_id") or _client_ip(),
                       SUPPLIER_MAX_PER_KEY_PER_DAY):
        return jsonify({"ok": False, "reason": "rate_limited"}), 429
    t = lambda k, n: " ".join(str(data.get(k) or "").split())[:n]
    where = _job_location(city, state, t("near", 160), t("title", 300), t("scope", 1500), t("url", 300))
    if not where:
        return jsonify({"ok": False, "reason": "place_not_found"})
    return jsonify({"ok": True, **where})


def _suppliers_near(city, state, near="", title="", scope="", url=""):
    """Plants around the job: its address, else the site the bid names, else
    the bid's town."""
    geo = _geo_from_city(city, state) if city else None
    loc = _job_location(city, state, near, title, scope, url)
    if not loc:
        return None
    center = loc
    cell = f"{round(center['lat'] * 4) / 4:.2f},{round(center['lon'] * 4) / 4:.2f}"
    cache_key = _SUPPLIER_CACHE_PREFIX + cell
    try:
        hit = kv_backend.get(cache_key, None)
    except Exception:
        hit = None
    fresh_for = (SUPPLIER_PARTIAL_CACHE_HOURS * 3600 if isinstance(hit, dict) and hit.get("partial")
                 else SUPPLIER_CACHE_DAYS * 86400)
    if isinstance(hit, dict) and time.time() - float(hit.get("at") or 0) < fresh_for:
        plants, web = hit.get("plants") or [], hit.get("web") or []
    else:
        deadline = time.time() + SUPPLIER_TIME_BUDGET
        place = loc["label"] if loc["how"] != "town" else f"{(geo or {}).get('city') or city}, {state}"
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_map = pool.submit(lambda: _overpass(
                _supplier_query(center["lat"], center["lon"], SUPPLIER_RADIUS_MI * 1609.34), deadline))
            f_osm = pool.submit(_nominatim_plants, center["lat"], center["lon"])
            f_ai = pool.submit(_ai_plants, place, state, center["lat"], center["lon"], deadline)

            def got(f, dflt):
                try:
                    return f.result(timeout=max(1, deadline - time.time() + 5))
                except Exception:
                    return dflt
            elements = got(f_map, None)
            osm = got(f_osm, [])
            mapped = _supplier_rows(elements, center["lat"], center["lon"]) if elements is not None else []
            ai = got(f_ai, []) if len(mapped) + len(osm) < 4 else []
        plants = _merge_plants(mapped, osm, ai)[:SUPPLIER_MAX]
        web = _supplier_web((geo or {}).get("city") or city or loc["label"], state) if len(plants) < 3 else []
        try:
            kv_backend.set(cache_key, {"at": time.time(), "plants": plants, "web": web,
                                       "partial": elements is None})
        except Exception:
            pass
    # Distances from here, not the cell's center the cache was filed under.
    for p in plants:
        if p.get("lat") is not None:
            p["miles"] = round(_miles_between(center["lat"], center["lon"], p["lat"], p["lon"]), 1)
    plants = sorted((p for p in plants if p.get("miles") is None or p["miles"] <= SUPPLIER_RADIUS_MI),
                    key=lambda p: (p.get("miles") is None, p.get("miles") or 0))
    return {"lat": center["lat"], "lon": center["lon"], "plants": plants, "web": web,
            "near_job": loc["how"] != "town", "where": loc["label"], "how": loc["how"]}


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
    city, state = _supplier_where(data)
    if not state:
        return jsonify({"ok": False, "reason": "bad_place"}), 400
    if not _ip_rate_ok(_SUPPLIER_RATE_KEY, key or device or _client_ip(), SUPPLIER_MAX_PER_KEY_PER_DAY):
        return jsonify({"ok": False, "reason": "rate_limited"}), 429
    t = lambda k, n: " ".join(str(data.get(k) or "").split())[:n]
    found = _suppliers_near(city, state, t("near", 160), t("title", 300), t("scope", 1500), t("url", 300))
    if not found:
        return jsonify({"ok": False, "reason": "place_not_found"})
    return jsonify({"ok": True, "city": city, "state": state, "radius_mi": SUPPLIER_RADIUS_MI,
                    "near": found["where"] if found.get("near_job") else "", "how": found.get("how"),
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
