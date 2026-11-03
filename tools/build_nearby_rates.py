"""Starting prices for states that have no going rates of their own.

    python3 tools/build_nearby_rates.py

Seventeen states have their own rates (rates/<st>.json); the rest used to
start every pricing line blank. This writes rates/nearby/<st>.json for each
of the others, from what each item actually won for in the nearest states
that publish bid results (results/<st>.json): the low bidder's unit price on
every state job in the last WIN_YEARS years, converted to one unit per item.

Each state uses the same NEAREST source states for every item (those within
MAX_MILES of its middle), so its label names them once. Each source counts
the same, however many jobs it let, so Texas's hundreds of jobs don't drown
out Kansas next door. An item with fewer than MIN_JOBS jobs among them gets
no price rather than a thin one.
Alaska and Hawaii get no file: lower-48 prices would mislead there.

The app labels these as nearby states' prices, never as the state's own,
and a state's own rates always win (no nearby file is written for it).
Output depends only on the data, so a rebuild with nothing new changes
nothing. tools/refresh_state_data.py runs this after each monthly refresh.
"""
import datetime
import json
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "curbcall_netlify_v4")
RATES = os.path.join(SITE, "rates")
RESULTS = os.path.join(SITE, "results")
OUT = os.path.join(RATES, "nearby")

WIN_YEARS = 4
NEAREST = 3
MAX_MILES = 1000
MIN_JOBS = 8
SKIP = {"AK", "HI"}

# One unit per item, so states that price sidewalk by the square foot and
# states that price it by the square yard can be pooled.
ITEMS = {
    "sidewalk": ("Concrete sidewalk, 4 in.", "sq yd"),
    "sidewalk6": ("Concrete sidewalk, 6 in.", "sq yd"),
    "driveway": ("Concrete driveway", "sq yd"),
    "median": ("Concrete median", "sq yd"),
    "ramp": ("Concrete curb ramp (ADA)", "sq yd"),
    "removal": ("Remove existing concrete", "sq yd"),
    "domes": ("Truncated domes (ADA warning)", "sq ft"),
    "curb": ("Concrete curb", "ft"),
    "curb_gutter": ("Concrete curb and gutter", "ft"),
    "gutter": ("Concrete gutter", "ft"),
}
HEADLINE = ["sidewalk", "curb_gutter", "ramp", "domes"]
# Price per unit `frm` -> price per unit `to`.
PER = {("sq ft", "sq yd"): 9.0, ("sq yd", "sq ft"): 1 / 9.0}

# Rough geographic middle of each state (lat, lon).
CENTERS = {
    "AL": (32.8, -86.8), "AK": (64.7, -152.0), "AZ": (34.3, -111.7), "AR": (34.9, -92.4),
    "CA": (37.2, -119.5), "CO": (39.0, -105.5), "CT": (41.6, -72.7), "DE": (39.0, -75.5),
    "DC": (38.9, -77.0), "FL": (28.6, -82.4), "GA": (32.7, -83.4), "HI": (20.8, -156.3),
    "ID": (44.4, -114.6), "IL": (40.0, -89.2), "IN": (39.9, -86.3), "IA": (42.1, -93.5),
    "KS": (38.5, -98.4), "KY": (37.5, -85.3), "LA": (31.1, -92.0), "ME": (45.4, -69.2),
    "MD": (39.0, -76.8), "MA": (42.3, -71.8), "MI": (44.3, -85.4), "MN": (46.3, -94.3),
    "MS": (32.7, -89.7), "MO": (38.4, -92.5), "MT": (47.0, -109.6), "NE": (41.5, -99.8),
    "NV": (39.3, -116.6), "NH": (43.7, -71.6), "NJ": (40.2, -74.7), "NM": (34.4, -106.1),
    "NY": (42.9, -75.5), "NC": (35.6, -79.4), "ND": (47.5, -100.5), "OH": (40.3, -82.8),
    "OK": (35.6, -97.5), "OR": (43.9, -120.6), "PA": (40.9, -77.8), "RI": (41.7, -71.5),
    "SC": (33.9, -80.9), "SD": (44.4, -100.2), "TN": (35.9, -86.4), "TX": (31.5, -99.3),
    "UT": (39.3, -111.7), "VT": (44.1, -72.7), "VA": (37.5, -78.9), "WA": (47.4, -120.5),
    "WV": (38.6, -80.6), "WI": (44.6, -89.9), "WY": (43.0, -107.6),
}
NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


def miles(a, b):
    (la1, lo1), (la2, lo2) = CENTERS[a], CENTERS[b]
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def convert(price, frm, to):
    if frm == to:
        return price
    k = PER.get((frm, to))
    return price * k if k else None


def weighted_quantile(pairs, q):
    """pairs: [(value, weight)]. The value where q of the weight lies below."""
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= q * total - 1e-9:
            return v
    return pairs[-1][0]


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def source_prices(since):
    """{state: {cat: [(quantity, winning unit price)]}}, in ITEMS units, and
    the newest letting date used."""
    out, newest = {}, ""
    for name in sorted(os.listdir(RESULTS)):
        m = re.fullmatch(r"([a-z]{2})\.json", name)
        rates_path = os.path.join(RATES, name)
        if not m or not os.path.exists(rates_path):
            continue
        st = m.group(1).upper()
        rates, res = load(rates_path), load(os.path.join(RESULTS, name))
        cats = {}
        for cat, code in (rates.get("cats") or {}).items():
            meta = (rates.get("items") or {}).get(code)
            if cat not in ITEMS or not meta:
                continue
            vals = []
            for c in res.get("contracts", []):
                if (c.get("date") or "") < since:
                    continue
                it = dict(c.get("items") or []).get(code)
                try:
                    won, qty = float(it[1][0]), float(it[0] or 0)
                except (TypeError, ValueError, IndexError):
                    continue
                v = convert(won, meta.get("unit"), ITEMS[cat][1])
                if v and v > 0:
                    # A price per sq yd is 9x one per sq ft; the quantity is 1/9.
                    vals.append((round(qty * won / v, 1) if qty > 0 else 0, v))
                    newest = max(newest, c.get("date") or "")
            if vals:
                cats[cat] = vals
        if cats:
            out[st] = cats
    return out, newest


def newest_letting():
    """The newest letting in any results file: the window is counted back
    from it, not from today, so a rebuild with nothing new changes nothing."""
    newest = ""
    for name in os.listdir(RESULTS):
        if re.fullmatch(r"[a-z]{2}\.json", name):
            newest = max([newest] + [c.get("date") or "" for c in load(os.path.join(RESULTS, name)).get("contracts", [])])
    return newest


def build():
    anchor = newest_letting()
    if not anchor:
        return {}
    since = (datetime.date.fromisoformat(anchor) - datetime.timedelta(days=int(WIN_YEARS * 365.25))).isoformat()
    own = {m.group(1).upper() for n in os.listdir(RATES) if (m := re.fullmatch(r"([a-z]{2})\.json", n))}
    sources, newest = source_prices(since)
    files = {}
    for st in sorted(CENTERS):
        if st in own or st in SKIP:
            continue
        within = sorted((s for s in sources if miles(st, s) <= MAX_MILES), key=lambda s: miles(st, s))
        near = within[:NEAREST]
        items, prices, wins, cats, used, jobs, job_from = {}, {}, {}, {}, set(), {}, {}
        year = (newest or anchor)[:4]
        for cat, (label, unit) in ITEMS.items():
            from_ = [s for s in near if cat in sources[s]]
            pairs = [(v, 1.0 / len(sources[s][cat])) for s in from_ for _, v in sources[s][cat]]
            own_where = None
            if len(pairs) < MIN_JOBS:
                # None of the nearest prices it (Kentucky has no ramp item):
                # the nearest state within reach that does, named on the item.
                alt = next((s for s in within if len(sources[s].get(cat, [])) >= MIN_JOBS), None)
                if not alt:
                    continue
                from_, own_where = [alt], f"in {NAMES[alt]}"
                pairs = [(v, 1.0) for _, v in sources[alt][cat]]
            vals = [v for v, _ in pairs]
            mid, p25, p75 = (round(weighted_quantile(pairs, q), 2) for q in (0.5, 0.25, 0.75))
            code = "N-" + cat
            cats[cat], items[code] = code, {"name": label, "unit": unit}
            if own_where:
                items[code]["where"] = own_where
            wins[code] = {"STATEWIDE": {year: [mid, round(min(vals), 2), round(max(vals), 2), len(vals), p25, p75]}}
            prices[code] = {"STATEWIDE": {year: [mid, round(min(vals), 2), round(max(vals), 2), len(vals), None]}}
            # Every job behind it, [quantity, unit price, source], so the app
            # can price at the job's size the way it does with a state's own
            # results: small jobs win at higher unit prices.
            jobs[code] = [[q, round(v, 2), from_.index(s)] for s in from_ for q, v in sources[s][cat]]
            job_from[code] = from_
            used.update(from_)
        if not items:
            continue
        order = sorted(used, key=lambda s: miles(st, s))
        names = [NAMES[s] for s in sorted(set(near) & used, key=lambda s: miles(st, s))] or [NAMES[order[0]]]
        listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        files[st] = {
            "state": st, "state_name": NAMES[st], "basis": "nearby", "from": order,
            "source": f"Winning bids on state road jobs in {listed}",
            "source_url": "", "through": newest, "years": [int(year)],
            "fields": ["avg", "low", "high", "n", "avg_qty"],
            "districts": {"STATEWIDE": f"in {listed}"},
            "cats": cats, "items": items, "prices": prices, "wins": wins,
            "jobs": jobs, "job_from": job_from,
            "headline": [cats[c] for c in HEADLINE if c in cats],
        }
    return files


def main():
    files = build()
    os.makedirs(OUT, exist_ok=True)
    for name in os.listdir(OUT):
        if name.endswith(".json"):
            os.remove(os.path.join(OUT, name))
    for st, d in files.items():
        with open(os.path.join(OUT, st.lower() + ".json"), "w", encoding="utf-8") as f:
            json.dump(d, f, separators=(",", ":"), sort_keys=True)
    index = {st: {"name": d["state_name"], "from": d["from"], "through": d["through"]} for st, d in files.items()}
    with open(os.path.join(OUT, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"states": index}, f, separators=(",", ":"), sort_keys=True)
    print(f"nearby prices for {len(files)} states:", ", ".join(sorted(files)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
