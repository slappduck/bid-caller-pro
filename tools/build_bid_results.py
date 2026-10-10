#!/usr/bin/env python3
"""Build the app's bid results: who bid on concrete flatwork, and who won.

MoDOT posts a "Bid Tabulations" PDF after every monthly letting: each
contract's bidders in rank order with their totals, and every bidder's unit
price on every pay item. For a contractor that answers two questions no
average can:

  - what did the WINNING bid charge for sidewalk, curb and gutter, ramps?
  - who bids this kind of work here, how often do they win, how low do they go?

This reads the tabulations for the last two years of lettings, keeps the
contracts with concrete flatwork items, and writes
curbcall_netlify_v4/results/mo.json:

  contracts [{id, date, desc, counties, district, bidders [[name, total]],
              items {code: [quantity, [unit price by rank]]}}]
  wins {code: {district: {year: [avg, low, high, n]}}}   (rank-1 prices)

    pip install pypdf
    python3 tools/build_bid_results.py
    python3 tools/build_bid_results.py --months 12 --cache /tmp/tabs

Public records, fetched one at a time with an honest User-Agent. A
contract whose columns don't line up with its bidders is dropped, never
guessed at, and the build refuses to write if a letting yields nothing.
"""
import argparse
import collections
import datetime
import http.cookiejar
import io
import json
import os
import re
import statistics
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "curbcall_netlify_v4", "results", "mo.json")
BASE = "https://modotweb.modot.mo.gov/BidLettingPlansRoom/"
USER_AGENT = ("CurbCallBot/1.0 (+https://curbcallpro.com; concrete bid "
              "aggregator; contact support@curbcallpro.com)")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_unit_prices import FLATWORK_PREFIXES  # noqa: E402

DISTRICTS = {"NORTHWEST": "NW", "NORTHEAST": "NE", "KANSAS CITY": "KC",
             "CENTRAL": "CD", "ST. LOUIS": "SL", "ST LOUIS": "SL",
             "SOUTHWEST": "SW", "SOUTHEAST": "SE"}

_CONTRACT = re.compile(r"^Contract ID:\s*(\S+)")
_TAB_CONTRACT = re.compile(r"^CONTRACT ID\s*:\s*(\S+)")
_DATE = re.compile(r"^Letting Date:\s*(\w{3} \d{1,2}, \d{4})")
_DESC = re.compile(r"^Contract Description:\s*(.+)")
_COUNTIES = re.compile(r"^Counties:\s*(.+)")
_DISTRICT = re.compile(r"^District:\s*(.+)")
_VENDOR = re.compile(r"^(\d+)\s+\d{7}\s+(.+?)\s+\$([\d,]+\.\d{2})\s+[\d.]+\s*$")
_VENDOR_LINE = re.compile(r"^\d+\s+\d{7}\s+.+?\$[\d,]+\.\d{2}\s+[\d.]+\s*$", re.M)
_RANKS = re.compile(r"(?:(\d+)(?:st|nd|rd|th) )?Low Bid")
_ITEM = re.compile(r"^(\d{4})\s+(\d{7}[A-Z]?)\s+(.+?)\s+([\d,]+(?:\.\d+)?)\s+([A-Z]{1,6})\s*$")
_PROJECT = re.compile(r"^(Project|Section)\s+(\S+?):")
_ITEM_START = re.compile(r"^\d{4}\s+\d{7}[A-Z]?\s")
_PRICES = re.compile(r"^Bid Price\s+(.+)$")


def _num(s):
    return float(s.replace(",", ""))


def is_flatwork(code, desc):
    return code.startswith(FLATWORK_PREFIXES) and "ASPHALT" not in desc.upper()


def parse_tabs(text):
    """Contracts with their bidders and flatwork unit prices, from one PDF.

    The VENDOR RANKING page gives each contract's bidders in rank order;
    the BID TABULATION pages that follow give "Bid Price" rows whose columns
    are named by a "Low Bid 2nd Low Bid ..." header, five bidders a page.
    """
    contracts, cur, tab, ranks, pending, partial, where = {}, None, None, [], None, None, {}
    for raw in text.splitlines():
        line = raw.strip()
        m = _CONTRACT.match(line)
        if m:
            cur = contracts.setdefault(m.group(1), {"id": m.group(1), "bidders": [],
                                                    "items": {}, "bad": set()})
            continue
        if cur is not None and not cur["bidders"] and tab is None:
            for rx, key in ((_DATE, "date"), (_DESC, "desc"), (_COUNTIES, "counties"),
                            (_DISTRICT, "district")):
                mm = rx.match(line)
                if mm:
                    cur[key] = mm.group(1).strip()
        m = _VENDOR.match(line)
        if m and cur is not None and tab is None:
            cur["bidders"].append([re.sub(r"\s+", " ", m.group(2)).strip(), _num(m.group(3))])
            cur.setdefault("ranks", []).append(int(m.group(1)))
            continue
        m = _TAB_CONTRACT.match(line)
        if m:
            if contracts.get(m.group(1)) is not tab:
                where = {}
            tab = contracts.get(m.group(1))
            if tab is not cur:
                cur = None
            continue
        if "VENDOR RANKING" in line:
            tab, ranks, pending, partial = None, [], None, None
            continue
        if tab is None:
            continue
        if line.startswith("Low Bid") or re.match(r"^\d+(?:st|nd|rd|th) Low Bid", line):
            ranks = [int(r) if r else 1 for r in _RANKS.findall(line)]
            continue
        # An item line can wrap, its unit landing on the next line.
        if _ITEM_START.match(line):
            partial = line
        elif partial and not _PRICES.match(line):
            partial += " " + line
        m = _PROJECT.match(line)
        if m:
            where[m.group(1)] = m.group(2)
            continue
        m = _ITEM.match(partial) if partial else None
        if m:
            code, desc = m.group(2), m.group(3)
            # The same item can appear once per project in a contract; each
            # appearance is kept apart and combined, by quantity, at the end.
            occ = (where.get("Project"), where.get("Section"), m.group(1), code)
            pending = (occ, _num(m.group(4))) if is_flatwork(code, desc) else None
            partial = None
            continue
        m = _PRICES.match(line)
        if m:
            partial = None
        if m and pending:
            occ, qty = pending
            code = occ[3]
            vals = m.group(1).split()
            if len(vals) != len(ranks) or not ranks:
                tab["bad"].add(code)
            else:
                prices = tab["items"].setdefault(occ, [qty, {}])[1]
                for rk, v in zip(ranks, vals):
                    try:
                        prices[rk] = _num(v)
                    except ValueError:
                        tab["bad"].add(code)
            pending = None
    out = []
    for c in contracts.values():
        n = len(c["bidders"])
        combined = {}
        for occ, (qty, by_rank) in c["items"].items():
            code = occ[3]
            if code in c["bad"] or sorted(by_rank) != list(range(1, n + 1)):
                c["bad"].add(code)   # columns didn't line up with the bidders: drop, don't guess
                continue
            q, amt = combined.setdefault(code, [0.0, [0.0] * n])
            combined[code][0] = q + qty
            for r in range(n):
                amt[r] += qty * by_rank[r + 1]
        items = {code: [round(q, 2), [round(a / q, 2) for a in amt]]
                 for code, (q, amt) in combined.items() if code not in c["bad"] and q > 0}
        # Columns are named by rank, so the ranks must run 1..n. (Totals need
        # not be in order: MoDOT ranks a corrected or irregular bid itself.)
        if not items or not n or c.get("ranks") != list(range(1, n + 1)):
            continue
        try:
            date = datetime.datetime.strptime(c.get("date", ""), "%b %d, %Y").date().isoformat()
        except ValueError:
            continue
        out.append({"id": c["id"], "date": date, "desc": c.get("desc", ""),
                    "counties": c.get("counties", ""),
                    "district": DISTRICTS.get(c.get("district", "").upper(), ""),
                    "bidders": c["bidders"], "items": items})
    return out


def quartiles(p):
    """(p25, p75) of a list: the middle half of winning prices."""
    if len(p) < 2:
        return p[0], p[0]
    q = statistics.quantiles(sorted(p), n=4, method="inclusive")
    return q[0], q[2]


def wins(contracts):
    """Rank-1 unit prices per item, district and year (plus STATEWIDE):
    [avg, low, high, n, p25, p75]."""
    g = collections.defaultdict(list)
    for c in contracts:
        y = c["date"][:4]
        for code, (_qty, prices) in c["items"].items():
            for d in filter(None, (c["district"], "STATEWIDE")):
                g[(code, d, y)].append(prices[0])
    out = {}
    for (code, d, y), p in g.items():
        q1, q3 = quartiles(p)
        out.setdefault(code, {}).setdefault(d, {})[y] = [
            round(statistics.mean(p), 2), round(min(p), 2), round(max(p), 2), len(p),
            round(q1, 2), round(q3, 2)]
    return out


# ── Oregon ──────────────────────────────────────────────────────────────────
# ODOT's annual bid data lists every bidder's unit price on every item of
# every contract, by rank but without names. That still gives what the app
# needs for "how many usually bid, how close is second place": bidder counts,
# totals (summed over all items per rank) and flatwork unit prices by rank.
OR_DISTRICTS = ("1", "2", "3", "4", "5")


def parse_or_contracts(rows):
    """Contracts from one year's BID DATA sheet, flatwork items kept."""
    import build_state_prices as P
    flat = {code: desc for code, (_n, _c, desc) in P.OR_ITEMS.items()}
    cs = {}
    for r in rows:
        if len(r) < 14 or not isinstance(r[1], datetime.datetime):
            continue
        try:
            rank, qty, price = int(r[13]), float(r[10]), float(r[11])
            amount = float(r[12]) if r[12] is not None else qty * price
        except (TypeError, ValueError):
            continue
        cid = str(r[4]).strip()
        c = cs.setdefault(cid, {"id": cid, "date": r[1].date().isoformat(),
                                "district": str(r[5]).strip() if str(r[5]).strip() in OR_DISTRICTS else "",
                                "desc": str(r[6] or "").strip(), "totals": {}, "items": {}})
        c["totals"][rank] = c["totals"].get(rank, 0.0) + amount
        code = str(r[7] or "").strip()
        desc = re.sub(r"\s+", " ", str(r[8] or "")).strip()
        if flat.get(code) == desc and qty > 0 and price > 0:
            item = c["items"].setdefault(code, [qty, {}])
            item[1][rank] = price
    out = []
    for c in cs.values():
        ranks = sorted(c["totals"])
        if not ranks or ranks != list(range(1, len(ranks) + 1)):
            continue
        n = len(ranks)
        items = {code: [qty, [p[r] for r in ranks]] for code, (qty, p) in c["items"].items()
                 if sorted(p) == ranks}
        if not items:
            continue
        out.append({"id": c["id"], "date": c["date"], "desc": c["desc"], "counties": "",
                    "district": c["district"], "bidders": [[None, round(c["totals"][r], 2)] for r in ranks],
                    "items": items})
    return out


def build_or(cache):
    import zipfile
    import build_state_prices as P
    contracts = []
    for y, path in P.or_zips(cache):
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith((".xlsx", ".xlsm")))
            got = parse_or_contracts(P._rows_xlsx(z.read(name), "BID DATA"))
        print(f"OR {y}: {len(got)} contracts with flatwork")
        contracts += got
    contracts.sort(key=lambda c: (c["date"], c["id"]))
    return {"state": "OR", "source": "ODOT bid tabulations", "source_url": P.OR_PAGE,
            "lettings": sorted({c["date"] for c in contracts}), "contracts": contracts,
            "wins": wins(contracts), "named": False}


# ── North Carolina ──────────────────────────────────────────────────────────
# NCDOT posts each central letting's bid tabs as a spreadsheet: one row per
# item, three bidders per row (name, city, unit price, amount), and more
# rows, on the next "page" numbers, for bidders four and up -- low bid
# first. Past lettings stay reachable by date, so they're found by trying
# each Tuesday and Thursday (the days lettings fall on).
NC_PAGE = "https://connect.ncdot.gov/letting/Pages/default.aspx"
NC_DETAIL = ("https://connect.ncdot.gov/letting/Pages/Central-Letting-Details.aspx"
             "?let_type=Central&let_date={d}")
# code -> (name, category, the description NCDOT prints for it, unit)
NC_ITEMS = {
    "2591000000-E": ('Concrete sidewalk, 4 in.', "sidewalk", '4" CONCRETE SIDEWALK', "sq yd"),
    "2605000000-N": ("Concrete curb ramp (ADA)", "ramp", "CONCRETE CURB RAMPS", "each"),
    "2549000000-E": ("Curb and gutter, 2 ft 6 in.", "curb_gutter", '2\'-6" CONC CURB & GUTTER', "ft"),
    "2542000000-E": ("Curb and gutter, 1 ft 6 in.", None, '1\'-6" CONC CURB & GUTTER', "ft"),
    "2612000000-E": ('Concrete driveway, 6 in.', "driveway", '6" CONCRETE DRIVEWAY', "sq yd"),
    "2647000000-E": ('Monolithic concrete island, 5 in.', "median", '5" MONO CONC ISLANDS (SURF MTD)', "sq yd"),
    "2556000000-E": ("Shoulder berm gutter", "gutter", "SHOULDER BERM GUTTER", "ft"),
    "2612500000-N": ("Remove and replace curb ramp", None, "REM & REP CONC CURB RAMP", "each"),
}
NC_UNITS = {"SY": "sq yd", "SF": "sq ft", "LF": "ft", "EA": "each"}


def parse_nc_tabs(rows):
    """Contracts with bidders in rank order and flatwork unit prices."""
    cs = {}
    for v in rows:
        if len(v) < 39 or not str(v[5]).startswith("C"):
            continue
        try:
            page = int(float(v[1]))
        except (TypeError, ValueError):
            continue
        cid = str(v[5]).strip()
        c = cs.setdefault(cid, {"pages": {}, "items": {}, "date": str(v[2]), "county": str(v[9]).strip(),
                                "desc": str(v[12]).strip()})
        occ = (str(v[17]), str(v[16]), str(v[14]))
        for k in range(3):
            name = str(v[24 + 5 * k] or "").strip()
            if not name:
                continue
            c["pages"].setdefault(page, {})[k] = name
            try:
                price, amount = float(v[26 + 5 * k] or 0), float(v[27 + 5 * k] or 0)
            except (TypeError, ValueError):
                continue
            c.setdefault("amounts", {}).setdefault((page, k), 0.0)
            c["amounts"][(page, k)] += amount
            code = str(v[14]).strip()
            desc = re.sub(r"\s+", " ", f"{v[19]} {v[20]}").strip()
            if code in NC_ITEMS and desc == NC_ITEMS[code][2]:
                try:
                    qty = float(v[21])
                except (TypeError, ValueError):
                    continue
                unit = NC_UNITS.get(str(v[22]).strip())
                if unit == NC_ITEMS[code][3] and qty > 0 and price > 0:
                    it = c["items"].setdefault(occ, {"code": code, "qty": qty, "unit": unit, "p": {}})
                    it["p"][(page, k)] = price
    out = []
    for cid, c in cs.items():
        if not c["pages"]:
            continue
        p0 = min(c["pages"])
        slots = [(pg, k) for pg in sorted(c["pages"]) for k in sorted(c["pages"][pg])]
        names = [c["pages"][pg][k] for pg, k in slots]
        if len(set(names)) != len(names) or slots[0][0] != p0:
            continue
        totals = [round(c.get("amounts", {}).get(sl, 0.0), 2) for sl in slots]
        n = len(slots)
        combined = {}
        for it in c["items"].values():
            if sorted(it["p"]) != sorted(slots):
                continue
            q, amt = combined.setdefault(it["code"], [0.0, [0.0] * n])
            combined[it["code"]][0] = q + it["qty"]
            for i, sl in enumerate(slots):
                amt[i] += it["qty"] * it["p"][sl]
        items = {code: [round(q, 2), [round(a / q, 2) for a in amt]] for code, (q, amt) in combined.items()}
        if not items or any(t <= 0 for t in totals):
            continue
        try:
            date = datetime.datetime.strptime(c["date"], "%m/%d/%Y").date().isoformat()
        except ValueError:
            continue
        out.append({"id": cid, "date": date, "desc": c["desc"], "counties": c["county"],
                    "district": "", "bidders": [[nm, t] for nm, t in zip(names, totals)],
                    "items": items})
    return out


def _nc_lettings(room, months, cache):
    """[(date, xls path)] for central lettings in the last `months` months.

    Each letting's page is fetched by date; NCDOT's pages are slow (~9 s),
    so a few are asked for at once. Answers are kept in the cache, so a
    rerun only asks about new dates."""
    from concurrent.futures import ThreadPoolExecutor
    index_path = os.path.join(cache, "nc_lettings.json")
    known = json.load(open(index_path)) if os.path.exists(index_path) else {}
    today = datetime.date.today()
    days, day = [], today - datetime.timedelta(days=months * 31)
    while day <= today:
        if day.weekday() in (1, 3) and day.isoformat() not in known:   # Tuesday, Thursday
            days.append(day.isoformat())
        day += datetime.timedelta(days=1)

    def probe(key):
        try:
            html = room.get(NC_DETAIL.format(d=key), timeout=90).decode("utf-8", "replace")
        except Exception:
            return key, None          # unknown: asked again next run
        m = re.search(r'href="([^"]+\.xls)"', html, re.I)
        return key, (m.group(1) if m else "")

    with ThreadPoolExecutor(max_workers=6) as pool:
        for key, href in pool.map(probe, days):
            if href is not None:
                known[key] = href
    json.dump(known, open(index_path, "w"))
    cutoff = (today - datetime.timedelta(days=months * 31)).isoformat()
    out = []
    for key in sorted(k for k, v in known.items() if v and k >= cutoff):
        path = os.path.join(cache, f"nc_tabs_{key}.xls")
        if not os.path.exists(path):
            data = room.get(urllib.parse.quote(known[key], safe=":/%"), timeout=300)
            with open(path, "wb") as f:
                f.write(data)
        out.append((datetime.date.fromisoformat(key), path))
    return out


def build_nc(cache, months):
    import xlrd
    room = Room()
    contracts, used = [], []
    for d, path in _nc_lettings(room, months, cache):
        s = xlrd.open_workbook(path).sheet_by_index(0)
        got = parse_nc_tabs(s.row_values(r) for r in range(s.nrows))
        print(f"NC {d}: {len(got)} contracts with flatwork")
        contracts += got
        used.append(d.isoformat())
    contracts.sort(key=lambda c: (c["date"], c["id"]))
    return {"state": "NC", "source": "NCDOT central letting bid tabs", "source_url": NC_PAGE,
            "lettings": used, "contracts": contracts, "wins": wins(contracts), "named": True}


def nc_rates(results):
    """rates/nc.json from the same bids: every bid's average, range and count,
    per item and year, statewide; winning prices from rank 1."""
    import build_state_prices as P
    items = {c: {"name": n, "unit": u, **({"cat": cat} if cat else {})}
             for c, (n, cat, _d, u) in NC_ITEMS.items()}
    s = P.State("NC", "North Carolina", "NCDOT central letting bid tabs", NC_PAGE, "all_bids",
                items=items, districts={"STATEWIDE": "All of North Carolina"},
                cats={cat: c for c, (_n, cat, _d, _u) in NC_ITEMS.items() if cat},
                headline=["2591000000-E", "2605000000-N", "2549000000-E", "2612000000-E"])
    g = collections.defaultdict(list)
    for c in results["contracts"]:
        for code, (qty, prices) in c["items"].items():
            g[(code, c["date"][:4])].append((qty, prices))
    for (code, y), rows in g.items():
        allp = [p for _q, ps in rows for p in ps]
        win = [ps[0] for _q, ps in rows]
        s.add(code, "STATEWIDE", y, statistics.mean(allp), min(allp), max(allp), len(allp),
              statistics.mean(q for q, _ps in rows))
        q1, q3 = quartiles(win)
        s.add_win(code, "STATEWIDE", y, statistics.mean(win), min(win), max(win), len(win), q1, q3)
    return s


# ── Texas ───────────────────────────────────────────────────────────────────
# TxDOT publishes every bid on every item of its lettings as an open dataset
# (data.texas.gov "Bid Tabulations"): bidder, rank, unit price, the bidder's
# total, district and county. Only the flatwork items are asked for.
TX_DATASET = "https://data.texas.gov/resource/de7b-7dna.json"
TX_PAGE = "https://data.texas.gov/dataset/Bid-Tabulations/de7b-7dna"
TX_ITEMS = {
    "531-7001": ('Concrete sidewalk, 4 in.', "sidewalk", "sq yd"),
    "531-7002": ('Concrete sidewalk, 5 in.', None, "sq yd"),
    "531-7003": ('Concrete sidewalk, 6 in.', "sidewalk6", "sq yd"),
    "529-7009": ("Curb and gutter, type II", "curb_gutter", "ft"),
    "529-7008": ("Curb and gutter, type I", None, "ft"),
    "529-7002": ("Concrete curb, type II", "curb", "ft"),
    "530-7006": ("Concrete driveway", "driveway", "sq yd"),
    "531-7005": ("Curb ramp, type 1 (ADA)", "ramp", "each"),
    "531-7006": ("Curb ramp, type 2 (ADA)", None, "each"),
    "531-7010": ("Curb ramp, type 7 (ADA)", None, "each"),
}
TX_UNITS = {"SY": "sq yd", "LF": "ft", "EA": "each", "SF": "sq ft"}


def _tx_rows(since):
    codes = ",".join(f"'{c}'" for c in TX_ITEMS)
    out, offset = [], 0
    while True:
        q = urllib.parse.urlencode({
            "$select": "project_id,project_actual_let_date,project_name,county,district_division,"
                       "bid_code,bid_item_quantity,measurement_unit,bid_item_unit_price_amount,"
                       "bid_rank_sequence_number,vendor_name,bid_total_amount,alternative_bid_code",
            "$where": f"bid_code in({codes}) AND project_actual_let_date >= '{since}'",
            "$order": "project_id,bid_code", "$limit": 50000, "$offset": offset})
        req = urllib.request.Request(f"{TX_DATASET}?{q}", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=300) as r:
            page = json.loads(r.read().decode("utf-8"))
        out += page
        if len(page) < 50000:
            return out
        offset += 50000


def parse_tx(rows):
    cs = {}
    for r in rows:
        if r.get("alternative_bid_code") or r.get("bid_code") not in TX_ITEMS:
            continue
        try:
            rank = int(float(r["bid_rank_sequence_number"]))
            qty, price = float(r["bid_item_quantity"]), float(r["bid_item_unit_price_amount"])
            total = float(r["bid_total_amount"])
        except (KeyError, TypeError, ValueError):
            continue
        if TX_UNITS.get(str(r.get("measurement_unit")).strip()) != TX_ITEMS[r["bid_code"]][2]:
            continue
        c = cs.setdefault(r["project_id"], {"id": r["project_id"], "date": str(r.get("project_actual_let_date", ""))[:10],
                                            "desc": str(r.get("project_name") or "")[:120],
                                            "counties": str(r.get("county") or ""),
                                            "district": str(r.get("district_division") or ""),
                                            "bidders": {}, "items": {}})
        c["bidders"][rank] = [re.sub(r"\s+", " ", str(r.get("vendor_name") or "")).strip(), round(total, 2)]
        if qty > 0 and price > 0:
            q, amt = c["items"].setdefault(r["bid_code"], {}).setdefault(rank, [0.0, 0.0])
            c["items"][r["bid_code"]][rank] = [q + qty, amt + qty * price]
    out = []
    for c in cs.values():
        ranks = sorted(c["bidders"])
        if ranks != list(range(1, len(ranks) + 1)):
            continue
        items = {}
        for code, by_rank in c["items"].items():
            if sorted(by_rank) != ranks:
                continue
            qty = by_rank[1][0]
            items[code] = [round(qty, 2), [round(by_rank[k][1] / by_rank[k][0], 2) for k in ranks]]
        if items and all(c["bidders"][k][1] > 0 for k in ranks):
            out.append({**c, "bidders": [c["bidders"][k] for k in ranks], "items": items})
    return out


def build_tx(months):
    since = (datetime.date.today() - datetime.timedelta(days=months * 31)).isoformat()
    contracts = parse_tx(_tx_rows(since))
    contracts.sort(key=lambda c: (c["date"], c["id"]))
    print(f"TX: {len(contracts)} contracts with flatwork since {since}")
    return {"state": "TX", "source": "TxDOT bid tabulations (data.texas.gov)", "source_url": TX_PAGE,
            "lettings": sorted({c["date"] for c in contracts}), "contracts": contracts,
            "wins": wins(contracts), "named": True}


# ── Kentucky ────────────────────────────────────────────────────────────────
# KYTC's "Bid Tabs of Awarded Projects" for each letting: per contract
# ("Call: 100" to the next call), every item with each bidder's unit price
# in rank order, the ranked bidder list with totals, county and district.
# Items carry no codes in these tabs, so they're matched on description.
KY_PAGE = "https://transportation.ky.gov/Construction-Procurement/Pages/Unit-Bid-Tabulations.aspx"
KY_ITEMS = {
    "SIDEWALK-4 IN CONCRETE": ('Concrete sidewalk, 4 in.', "sidewalk", "sq yd"),
    "SIDEWALK-6 IN CONCRETE": ('Concrete sidewalk, 6 in.', "sidewalk6", "sq yd"),
    "DETECTABLE WARNINGS": ("Detectable warnings (ADA)", "domes", "sq ft"),
    "STANDARD CURB AND GUTTER": ("Standard curb and gutter", "curb_gutter", "ft"),
    "STANDARD HEADER CURB": ("Standard header curb", "curb", "ft"),
    "REMOVE CONCRETE SIDEWALK": ("Remove concrete sidewalk", "removal", "sq yd"),
}
KY_UNITS = {"SQYD": "sq yd", "SQFT": "sq ft", "LF": "ft", "EACH": "each"}
_KY_ITEM = re.compile(r"^\d{4} (.+?) ([\d,]+\.\d{3}) ([A-Z]+) A A ((?:[\d,]*\.\d{2}\s*)+)$")
_KY_BIDDER = re.compile(r"^(\d+) \d{5} (.+?) ([\d,]+\.\d{2})\s*$")
_KY_WHERE = re.compile(r"^County: (.+?) COUNTY District: (\d+) Date Let: (\d+/\d+/\d+) Contid: (\S+)")


def parse_ky(text):
    out = []
    for block in re.split(r"(?m)^Call: ", text)[1:]:
        bidders, where, items = {}, None, {}
        for line in block.splitlines():
            line = line.strip()
            m = _KY_BIDDER.match(line)
            if m:
                bidders[int(m.group(1))] = [re.sub(r"\s+", " ", m.group(2)).strip(), _num(m.group(3))]
                continue
            m = _KY_WHERE.match(line)
            if m:
                where = m
                continue
            m = _KY_ITEM.match(line)
            if m and m.group(1).strip() in KY_ITEMS and KY_UNITS.get(m.group(3)) == KY_ITEMS[m.group(1).strip()][2]:
                code = m.group(1).strip()
                prices = [_num(x) for x in m.group(4).split()]
                q, ps = items.setdefault(code, [0.0, []])
                items[code] = [q + _num(m.group(2)), ps + [(_num(m.group(2)), prices)]]
        ranks = sorted(bidders)
        if not where or not ranks or ranks != list(range(1, len(ranks) + 1)):
            continue
        n = len(ranks)
        clean = {}
        for code, (qty, occs) in items.items():
            if qty <= 0 or any(len(p) != n for _q, p in occs):
                continue   # a row short of its bidders: dropped, not guessed
            clean[code] = [round(qty, 2), [round(sum(q * p[i] for q, p in occs) / qty, 2) for i in range(n)]]
        if not clean:
            continue
        mo, d, y = where.group(3).split("/")
        out.append({"id": where.group(4), "date": f"20{y[-2:]}-{int(mo):02d}-{int(d):02d}",
                    "desc": block.splitlines()[1].strip()[:120] if len(block.splitlines()) > 1 else "",
                    "counties": where.group(1).title(), "district": str(int(where.group(2))),
                    "bidders": [bidders[k] for k in ranks], "items": clean})
    return out


def build_ky(cache, months):
    from pypdf import PdfReader
    room = Room()
    html = room.get(KY_PAGE, timeout=60).decode("utf-8", "replace")
    cutoff = (datetime.date.today() - datetime.timedelta(days=months * 31)).isoformat()
    dates = sorted({d for d in re.findall(r"Publications/(\d{4}-\d{2}-\d{2})/Unit Bid Tabulations\.pdf", html)
                    if d >= cutoff})
    contracts, used = [], []
    for d in dates:
        path = os.path.join(cache, f"ky_tabs_{d}.pdf")
        if not os.path.exists(path):
            data = room.get(f"https://transportation.ky.gov/Construction-Procurement/Publications/{d}/"
                            "Unit%20Bid%20Tabulations.pdf", timeout=300)
            with open(path, "wb") as f:
                f.write(data)
        text = "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
        got = parse_ky(text)
        print(f"KY {d}: {len(got)} contracts with flatwork")
        contracts += got
        used.append(d)
    contracts.sort(key=lambda c: (c["date"], c["id"]))
    return {"state": "KY", "source": "KYTC bid tabs of awarded projects", "source_url": KY_PAGE,
            "lettings": used, "contracts": contracts, "wins": wins(contracts), "named": True}


# ── Kansas ──────────────────────────────────────────────────────────────────
# KDOT posts each month's lettings as a CSV: one row per bidder per item,
# with the bidder's unit price and extended amount. Bidders are ranked here
# by their total over all items of the proposal, which is how the low bid is
# decided. The CSV gives the month, not the day, of the letting.
KS_PAGE = "https://kdotapp.ksdot.org/HistoricalBidTabs/"
KS_CSV = "https://kdotapp.ksdot.org/burconsmain/bidtabs/CSV/{y}/{yymm}.CSV"
KS_ITEMS = {
    "025026": ('Concrete sidewalk, 4 in.', "sidewalk", "sq yd"),
    "025041": ('Concrete sidewalk, 6 in.', "sidewalk6", "sq yd"),
    "061597": ("Combined curb and gutter", "curb_gutter", "ft"),
    "022625": ("Sidewalk ramp (ADA)", "ramp", "sq yd"),
    "061176": ('Edge curb, 6 in.', "curb", "ft"),
}
KS_UNITS = {"SQYD": "sq yd", "LNFT": "ft", "EACH": "each", "SQFT": "sq ft"}


def parse_ks_csv(text, month):
    import csv as _csv
    cs = {}
    for r in _csv.DictReader(io.StringIO(text)):
        pid = (r.get("PROPOSAL_NM") or "").strip()
        vendor = re.sub(r"\s+", " ", (r.get("VENDORNAME") or "")).strip()
        if not pid or not vendor:
            continue
        try:
            qty, price, ext = float(r["QTY"] or 0), float(r["BIDPRICE"] or 0), float(r["EXTENDEDAMOUNT"] or 0)
        except (KeyError, ValueError):
            continue
        c = cs.setdefault(pid, {"county": (r.get("DESCR") or "").strip().title(),
                                "desc": (r.get("PROJECT_NM") or "").strip(), "totals": {}, "items": {}})
        c["totals"][vendor] = c["totals"].get(vendor, 0.0) + ext
        code, unit = (r.get("REFITEM_NM") or "").strip(), KS_UNITS.get((r.get("UNIT") or "").strip())
        if code in KS_ITEMS and unit == KS_ITEMS[code][2] and qty > 0 and price > 0:
            it = c["items"].setdefault(code, {})
            q, a = it.get(vendor, (0.0, 0.0))
            it[vendor] = (q + qty, a + qty * price)
    out = []
    for pid, c in cs.items():
        order = sorted(c["totals"], key=lambda v: c["totals"][v])
        if not order or any(c["totals"][v] <= 0 for v in order):
            continue
        items = {}
        for code, by_v in c["items"].items():
            if set(by_v) != set(order):
                continue
            qty = by_v[order[0]][0]
            items[code] = [round(qty, 2), [round(by_v[v][1] / by_v[v][0], 2) for v in order]]
        if items:
            out.append({"id": pid, "date": f"{month}-01", "desc": c["desc"], "counties": c["county"],
                        "district": "", "bidders": [[v, round(c["totals"][v], 2)] for v in order],
                        "items": items})
    return out


def build_ks(cache, months):
    room = Room()
    today = datetime.date.today()
    contracts, used = [], []
    y, m = today.year, today.month
    for _ in range(months):
        yymm = f"{y % 100:02d}{m:02d}"
        path = os.path.join(cache, f"ks_{yymm}.csv")
        if not os.path.exists(path):
            try:
                data = room.get(KS_CSV.format(y=y, yymm=yymm), timeout=120)
            except Exception:
                data = b""
            with open(path, "wb") as f:
                f.write(data)
        text = open(path, encoding="utf-8", errors="replace").read()
        if text.startswith('"PROPOSAL_NM"'):
            got = parse_ks_csv(text, f"{y}-{m:02d}")
            print(f"KS {y}-{m:02d}: {len(got)} contracts with flatwork")
            contracts += got
            used.append(f"{y}-{m:02d}-01")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    contracts.sort(key=lambda c: (c["date"], c["id"]))
    return {"state": "KS", "source": "KDOT monthly bid tabs (CSV)", "source_url": KS_PAGE,
            "lettings": sorted(used), "contracts": contracts, "wins": wins(contracts), "named": True}


# ── Iowa ────────────────────────────────────────────────────────────────────
# Iowa DOT posts one PDF per letting: a ranking page per contract (every
# bidder and total), then the tabulation, three bidders to a page, each page
# headed by the ranks it shows. A line's item description is on the line
# under it, so a variant ("6 IN. STAMPED") doesn't match the plain item.
IA_PAGE = "https://iowadot.gov/consultants-contractors/contracts/historical-completed-lettings/bid-tabulations"
IA_ITEMS = {
    "2511-7526004": ("Sidewalk, PCC, 4 in.", "sidewalk", "SIDEWALK, P.C. CONCRETE, 4 IN.", "sq yd"),
    "2511-7526005": ("Sidewalk, PCC, 5 in.", None, "SIDEWALK, P.C. CONCRETE, 5 IN.", "sq yd"),
    "2511-7526006": ("Sidewalk, PCC, 6 in.", "sidewalk6", "SIDEWALK, P.C. CONCRETE, 6 IN.", "sq yd"),
    "2511-7528101": ("Detectable warnings", "domes", "DETECTABLE WARNINGS", "sq ft"),
    "2512-1725206": ("Curb and gutter, PCC, 2.0 ft", None, "CURB AND GUTTER, P.C. CONCRETE, 2.0 FT.", "ft"),
    "2512-1725256": ("Curb and gutter, PCC, 2.5 ft", "curb_gutter", "CURB AND GUTTER, P.C. CONCRETE, 2.5 FT.", "ft"),
    "2512-1725306": ("Curb and gutter, PCC, 3.0 ft", None, "CURB AND GUTTER, P.C. CONCRETE, 3.0 FT.", "ft"),
    "2515-2475006": ("Driveway, PCC, 6 in.", "driveway", "DRIVEWAY, P.C. CONCRETE, 6 IN.", "sq yd"),
    "2515-2475008": ("Driveway, PCC, 8 in.", None, "DRIVEWAY, P.C. CONCRETE, 8 IN.", "sq yd"),
    "2301-4875006": ("Median, PCC, 6 in.", "median", "MEDIAN, P.C. CONCRETE, 6 IN.", "sq yd"),
    "2511-6745900": ("Removal of sidewalk", "removal", "REMOVAL OF SIDEWALK", "sq yd"),
    "2515-6745600": ("Removal of paved driveway", None, "REMOVAL OF PAVED DRIVEWAY", "sq yd"),
}
IA_UNITS = {"sq yd": "SY", "sq ft": "SF", "ft": "LF"}
# rank, vendor ID, name (a long one wraps before its total), total
_IA_RANK = re.compile(r"(?m)^(\d{1,2}) [A-Z0-9.]{4,6} ([^$\n][^$]*?)\s*\$([\d,]+\.\d{2}) \d+\.\d{2}%")
_IA_LINE = re.compile(r"^\d{4} (\d{4}-\d{7}) ([\d,]+\.\d{3}) (\S+) ((?:[\d,]+\.\d{5} [\d,]+\.\d{2} ?)+)$")
_IA_FOOT = re.compile(r"^([A-Z][a-z]+ \d{1,2}, \d{4})\n(\S+) Primary County: (.+)$", re.M)
_IA_HEAD = re.compile(r"(?m)^(\d{2}-[A-Z0-9-]+)\n([A-Z][a-z]+ \d{1,2}, \d{4}) \d")


def parse_ia(pages):
    cs = {}

    def contract(cid):
        return cs.setdefault(cid, {"bidders": {}, "items": {}, "date": None, "county": "", "desc": ""})
    for t in pages:
        if "Project(s) and Vendor Ranking" in t:
            m = _IA_HEAD.search(t)
            if not m:
                continue
            c = contract(m.group(1))
            c["date"] = datetime.datetime.strptime(m.group(2), "%B %d, %Y").date().isoformat()
            w = re.search(r"Location:\n\S+ (.+)\n(.+)\nRoute: .*\n(.+)", t)
            if w and not c["desc"]:
                c["desc"] = f"{w.group(1).strip().title()}, {w.group(3).strip()}"
                c["county"] = c["county"] or w.group(2).strip().title()
            for r in _IA_RANK.finditer(t):
                c["bidders"][int(r.group(1))] = (re.sub(r"\s+", " ", r.group(2)).strip(), _num(r.group(3)))
            continue
        foot = _IA_FOOT.search(t)
        if not foot or "Tabulation of Construction" not in t:
            continue
        c = contract(foot.group(2))
        c["county"] = foot.group(3).strip().title()
        lines = t.splitlines()
        # Ranks heading the columns; two short names can share a line.
        head = t[t.find("Item Description"):t.find("Alt Set / Alt Member")]
        cols = [int(x) for x in re.findall(r"(?:^|\s)\((\d+)\) ", head)]
        for i, line in enumerate(lines[:-1]):
            m = _IA_LINE.match(line.strip())
            if not m or m.group(1) not in IA_ITEMS:
                continue
            _n, _cat, desc, unit = IA_ITEMS[m.group(1)]
            if lines[i + 1].strip() != desc or m.group(3) != IA_UNITS[unit]:
                continue
            nums = [_num(x) for x in m.group(4).split()]
            prices = nums[0::2]
            if len(prices) != len(cols):
                continue
            qty = _num(m.group(2))
            it = c["items"].setdefault(m.group(1), {})
            for rank, p in zip(cols, prices):
                q, a = it.get(rank, (0.0, 0.0))
                it[rank] = (q + qty, a + qty * p)
    out = []
    for cid, c in cs.items():
        ranks = sorted(c["bidders"])
        if not ranks or ranks != list(range(1, len(ranks) + 1)) or not c["date"]:
            continue
        items = {}
        for code, by_r in c["items"].items():
            if sorted(by_r) != ranks or by_r[1][0] <= 0:
                continue
            items[code] = [round(by_r[1][0], 2), [round(by_r[r][1] / by_r[r][0], 2) for r in ranks]]
        if items:
            out.append({"id": cid, "date": c["date"], "desc": c["desc"], "counties": c["county"],
                        "district": "", "bidders": [list(c["bidders"][r]) for r in ranks], "items": items})
    return out


def build_ia(cache, months):
    from pypdf import PdfReader
    room = Room()
    html = room.get(IA_PAGE, timeout=60).decode("utf-8", "replace")
    cutoff = datetime.date.today() - datetime.timedelta(days=months * 31)
    found = []
    for mid, label in re.findall(r'href="/media/(\d+)/download[^"]*"[^>]*>(?:\s*<[^>]+>)*\s*(\d{1,2}/\d{1,2}/\d{2})\s', html):
        day = datetime.datetime.strptime(label, "%m/%d/%y").date()
        if day >= cutoff:
            found.append((day, mid))
    contracts, used = [], []
    for day, mid in sorted(set(found)):
        path = os.path.join(cache, f"ia_tabs_{day}.pdf")
        if not os.path.exists(path):
            data = room.get(f"https://iowadot.gov/media/{mid}/download?inline", timeout=300)
            with open(path, "wb") as f:
                f.write(data)
        got = parse_ia([(p.extract_text() or "") for p in PdfReader(path).pages])
        print(f"IA {day}: {len(got)} contracts with flatwork")
        contracts += got
        used.append(day.isoformat())
    contracts.sort(key=lambda c: (c["date"], c["id"]))
    return {"state": "IA", "source": "Iowa DOT letting bid tabulations", "source_url": IA_PAGE,
            "lettings": used, "contracts": contracts, "wins": wins(contracts), "named": True}


# ── Illinois ────────────────────────────────────────────────────────────────
# IDOT posts a "Unit Price Tabulation of Bids" for each past letting once its
# contracts are executed: a zip of one fixed-width text file per contract,
# every bidder's unit price on every pay item. Past lettings are listed on
# the bulletin home page.
IL_HOME = "https://webapps.dot.illinois.gov/WCTB/LbHome"
IL_BASE = "https://webapps.dot.illinois.gov"
IL_ITEMS = {
    "42400100": ("PC concrete sidewalk, 4 in.", None, "PC CONC SIDEWALK 4", "sq ft"),
    "42400200": ("PC concrete sidewalk, 5 in.", "sidewalk", "PC CONC SIDEWALK 5", "sq ft"),
    "42400300": ("PC concrete sidewalk, 6 in.", "sidewalk6", "PC CONC SIDEWALK 6", "sq ft"),
    "42400800": ("Detectable warnings", "domes", "DETECTABLE WARNINGS", "sq ft"),
    "60603800": ("Combination curb and gutter, B-6.12", "curb_gutter", "COMB CC&G TB6.12", "ft"),
    "60605000": ("Combination curb and gutter, B-6.24", None, "COMB CC&G TY B-6.24", "ft"),
    "60600605": ("Concrete curb, type B", "curb", "CONC CURB TB", "ft"),
    "42300200": ("PCC driveway pavement, 6 in.", "driveway", "PCC DRIVEWAY PAVT 6", "sq yd"),
    "42300400": ("PCC driveway pavement, 8 in.", None, "PCC DRIVEWAY PAVT 8", "sq yd"),
    "60618300": ("Concrete median surface, 4 in.", "median", "CONC MEDIAN SURF 4", "sq ft"),
    "44000600": ("Sidewalk removal", "removal", "SIDEWALK REM", "sq ft"),
    "44000500": ("Combination curb and gutter removal", None, "COMB CURB GUTTER REM", "ft"),
}
IL_DISTRICTS = {"1": "District 1 (Chicago area)", "2": "District 2 (Dixon, Rockford)", "3": "District 3 (Ottawa)",
                "4": "District 4 (Peoria)", "5": "District 5 (Paris, Champaign)", "6": "District 6 (Springfield)",
                "7": "District 7 (Effingham)", "8": "District 8 (Metro East)", "9": "District 9 (Carbondale)"}
IL_UNITS = {"sq ft": "SQ FT", "sq yd": "SQ YD", "ft": "FOOT"}
_IL_PAGE_HEAD = re.compile(r"ILLINOIS DEPARTMENT OF TRANSPORTATION|U N I T  P R I C E|^\s*LETTING DATE:|"
                           r"^\s*RESPONSIBLE DISTRICT:|^\s*SECTION:|^\s*STATE JOB NUMBER:|^\s*PROJECT NUMBER:|"
                           r"ITEM NBR  ITEM DESCRIPTION|BIDR NBR  BIDDER NAME|^\s*-{6,}\s*$")
_IL_ITEM = re.compile(r"^    (\w{8})  (.+?)\s{2,}([\d,]+\.\d{3})\s+(SQ FT|SQ YD|FOOT|EACH|\S+(?: \S+)?)\s*$")
_IL_BIDDER = re.compile(r"^ (\d{4})\s{2,}(\S.*)$")


def parse_il(text):
    """One contract's file -> a contract, or None when it has alternates
    (bidders priced different item groups) or nothing we price."""
    head = lambda pat: (re.search(pat, text) or [None, ""])[1].strip()
    cid, letting = head(r"CONTRACT NUMBER: (\w+)"), head(r"LETTING DATE: (\d\d/\d\d/\d{4})")
    if not cid or not letting:
        return None
    summary, _, detail = text.partition("DETAIL CONTRACTOR BIDS")
    groups = set(re.findall(r"(?m)^\s{20,}(\S.*?)\s{3,}[\d,]+\.\d{2}", summary))
    if groups != {"NO ALT"}:
        return None
    # Summary: a bidder's number and name (which can wrap), then its total.
    names, totals, cur = {}, {}, None
    for line in summary.splitlines():
        m = _IL_BIDDER.match(line)
        if m:
            cur = m.group(1)
            names[cur] = m.group(2).strip()
            continue
        t = re.match(r"^\s{20,}NO ALT\s+([\d,]+\.\d{2})", line)
        if t and cur:
            totals[cur] = _num(t.group(1))
            cur = None
        elif cur and line.strip():
            names[cur] += " " + line.strip()
    if not totals or set(totals) != set(names):
        return None
    # Detail: an item line, then one record per bidder (the name can wrap
    # onto the price line); page headers are dropped first.
    lines = [x for x in detail.splitlines() if not _IL_PAGE_HEAD.search(x)]
    items, code, rec = {}, None, None

    def flush():
        if code and rec:
            p = re.search(r"\s(\d[\d,]*\.\d{4})\s", rec[1] + " ")
            if p:
                items[code][1][rec[0]] = _num(p.group(1))
    for line in lines:
        m = _IL_ITEM.match(line)
        if m:
            flush()
            rec = None
            c = m.group(1)
            ok = (c in IL_ITEMS and re.sub(r"\s+", " ", m.group(2)).strip() == IL_ITEMS[c][2]
                  and m.group(4) == IL_UNITS[IL_ITEMS[c][3]])
            code = c if ok else None
            if code:
                items.setdefault(code, [0.0, {}])[0] += _num(m.group(3))
            continue
        b = _IL_BIDDER.match(line)
        if b:
            flush()
            rec = (b.group(1), line)
        elif rec and line.strip():
            rec = (rec[0], rec[1] + " " + line)
    flush()
    order = sorted(totals, key=lambda k: totals[k])
    priced = {c: [round(q, 2), [round(by[k], 2) for k in order]]
              for c, (q, by) in items.items() if q > 0 and set(by) == set(order)}
    if not priced:
        return None
    m, d, y = letting.split("/")
    district = head(r"RESPONSIBLE DISTRICT: (\d+)").lstrip("0")
    county = head(r"COUNTY: (.+?)(?:\s{2,}|$)").title()
    place = head(r"MUNICIPALITY: (.+?)(?:\s{2,}|$)").title()
    return {"id": cid, "date": f"{y}-{m}-{d}", "desc": head(r"SECTION: (.+?)(?:\s{2,}|$)"),
            "counties": ", ".join(filter(None, (place, county))), "district": district,
            "bidders": [[names[k], totals[k]] for k in order], "items": priced}


def build_il(cache, months):
    room = Room()
    home = room.get(IL_HOME, timeout=60).decode("utf-8", "replace")
    cutoff = datetime.date.today() - datetime.timedelta(days=months * 31)
    contracts, used = [], []
    for gid, label in re.findall(r'<option value="/WCTB/LbLettingDetail/Index/([0-9a-f-]{36})">([^<]+)</option>', home):
        day = datetime.datetime.strptime(label.strip(), "%B %d, %Y").date()
        if day < cutoff:
            continue
        path = os.path.join(cache, f"il_tabs_{day}.zip")
        # A tabulation grows as each contract is executed, for months after
        # the letting; re-fetch until it has had time to settle.
        if not os.path.exists(path) or (datetime.date.today() - day).days < 150:
            page = room.get(f"{IL_BASE}/WCTB/LbLettingDetail/Index/{gid}", timeout=60).decode("utf-8", "replace")
            doc = re.search(r'href="(/WCTB/LettingDateDocument/ViewDocument/[0-9a-f-]{36})"[^>]*>\s*'
                            r'Unit Price Tabulation of Bids', page)
            if not doc:
                print(f"IL {day}: no unit price tabulation yet")
                continue
            data = room.get(IL_BASE + doc.group(1), timeout=300)
            with open(path, "wb") as f:
                f.write(data)
        got = []
        with zipfile.ZipFile(path) as z:
            for n in sorted(z.namelist()):
                c = parse_il(z.read(n).decode("latin-1"))
                if c:
                    got.append(c)
        print(f"IL {day}: {len(got)} contracts with flatwork")
        contracts += got
        used.append(day.isoformat())
    contracts.sort(key=lambda c: (c["date"], c["id"]))
    return {"state": "IL", "source": "IDOT unit price tabulations of bids", "source_url": IL_HOME,
            "lettings": sorted(used), "contracts": contracts, "wins": wins(contracts), "named": True}


def rates_from_results(results, st, name, items_def, page, headline):
    """rates/<st>.json from bid results: every bid's average, range and count
    per item, district and year (plus statewide); winning prices from rank 1."""
    import build_state_prices as P
    items = {c: {"name": n, "unit": u, **({"cat": cat} if cat else {})}
             for c, (n, cat, *rest) in items_def.items() for u in [rest[-1]]}
    districts = {"STATEWIDE": f"All of {name}"}
    for c in results["contracts"]:
        if c["district"]:
            districts[c["district"]] = c["district"]
    s = P.State(st, name, results["source"], page, "all_bids", items=items, districts=districts,
                cats={cat: c for c, (n, cat, *_r) in items_def.items() if cat}, headline=headline)
    g = collections.defaultdict(list)
    for c in results["contracts"]:
        for code, (qty, prices) in c["items"].items():
            for d in filter(None, (c["district"], "STATEWIDE")):
                g[(code, d, c["date"][:4])].append((qty, prices))
    for (code, d, y), rows in g.items():
        allp = [p for _q, ps in rows for p in ps]
        win = [ps[0] for _q, ps in rows]
        s.add(code, d, y, statistics.mean(allp), min(allp), max(allp), len(allp),
              statistics.mean(q for q, _ps in rows))
        q1, q3 = quartiles(win)
        s.add_win(code, d, y, statistics.mean(win), min(win), max(win), len(win), q1, q3)
    return s


def check(contracts, lettings):
    problems = []
    if not contracts:
        problems.append("no contracts read")
    for c in contracts:
        if any(b[1] is None or b[1] <= 0 for b in c["bidders"]):
            problems.append(f"{c['id']}: a bidder with no total")
        for code, (qty, prices) in c["items"].items():
            if qty <= 0 or any(p < 0 for p in prices):
                problems.append(f"{c['id']} {code}: bad numbers")
    return problems


class Room:
    """The plans room needs a session: pick a letting, then download its files."""

    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.opener.addheaders = [("User-Agent", USER_AGENT)]

    def get(self, url, data=None, timeout=120):
        body = urllib.parse.urlencode(data).encode() if data else None
        with self.opener.open(url, body, timeout=timeout) as r:
            return r.read()

    def lettings(self):
        html = self.get(BASE).decode("utf-8", "replace")
        token = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', html)
        opts = re.findall(r'<option[^>]*value="(\d+)"[^>]*>(\d\d/\d\d/\d{4})', html)
        return (token.group(1) if token else ""), [
            (lid, datetime.datetime.strptime(d, "%m/%d/%Y").date()) for lid, d in opts]

    def tabulation(self, token, letting_id, cache, day):
        path = os.path.join(cache, f"modot_tabs_{day.isoformat()}.pdf")
        if os.path.exists(path):
            return path
        html = self.get(BASE + "Letting/ChangeLetting",
                        {"__RequestVerificationToken": token, "SwitchLetting": letting_id}
                        ).decode("utf-8", "replace")
        m = re.search(r'href="(/BidLettingPlansRoom/Letting/ViewStream/\d+\?type=post_info)">\s*Bid Tabulations', html)
        if not m:
            return None
        data = self.get("https://modotweb.modot.mo.gov" + m.group(1), timeout=600)
        if not data.startswith(b"%PDF"):
            return None
        with open(path, "wb") as f:
            f.write(data)
        return path


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--states", default="mo,or,nc,tx,ky,ks,ia,il")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--cache", default=None, help="folder to keep the PDFs in")
    args = ap.parse_args()
    cache = args.cache or tempfile.mkdtemp(prefix="bid_results_")
    os.makedirs(cache, exist_ok=True)
    states = [x.strip().lower() for x in args.states.split(",") if x.strip()]
    if "or" in states:
        data = build_or(cache)
        bad = check(data["contracts"], data["lettings"])
        if bad:
            print("OR not written:", *bad[:20], sep="\n  ", file=sys.stderr)
            return 1
        path = os.path.join(os.path.dirname(args.out), "or.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"), sort_keys=True)
        print(f"wrote {path}: {len(data['contracts'])} contracts")
    if "nc" in states:
        data = build_nc(cache, args.months)
        bad = check(data["contracts"], data["lettings"])
        if bad:
            print("NC not written:", *bad[:20], sep="\n  ", file=sys.stderr)
            return 1
        path = os.path.join(os.path.dirname(args.out), "nc.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"), sort_keys=True)
        print(f"wrote {path}: {len(data['contracts'])} contracts from {len(data['lettings'])} lettings")
        import build_state_prices
        print("wrote", build_state_prices.write_state(nc_rates(data)))
    if "tx" in states:
        data = build_tx(args.months)
        bad = check(data["contracts"], data["lettings"])
        if bad:
            print("TX not written:", *bad[:20], sep="\n  ", file=sys.stderr)
            return 1
        path = os.path.join(os.path.dirname(args.out), "tx.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"), sort_keys=True)
        print(f"wrote {path}: {len(data['contracts'])} contracts")
        import build_state_prices
        print("wrote", build_state_prices.write_state(rates_from_results(
            data, "TX", "Texas", TX_ITEMS, TX_PAGE, ["531-7001", "531-7005", "529-7009", "530-7006"])))
    if "ky" in states:
        data = build_ky(cache, args.months)
        bad = check(data["contracts"], data["lettings"])
        if bad:
            print("KY not written:", *bad[:20], sep="\n  ", file=sys.stderr)
            return 1
        path = os.path.join(os.path.dirname(args.out), "ky.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"), sort_keys=True)
        print(f"wrote {path}: {len(data['contracts'])} contracts")
        import build_state_prices
        s = rates_from_results(data, "KY", "Kentucky", KY_ITEMS, KY_PAGE,
                               ["SIDEWALK-4 IN CONCRETE", "STANDARD CURB AND GUTTER", "DETECTABLE WARNINGS",
                                "STANDARD HEADER CURB"])
        s.districts = {"STATEWIDE": "All of Kentucky",
                       **{k: f"District {k}" for k in s.districts if k != "STATEWIDE"}}
        print("wrote", build_state_prices.write_state(s))
    if "ks" in states:
        data = build_ks(cache, args.months)
        bad = check(data["contracts"], data["lettings"])
        if bad:
            print("KS not written:", *bad[:20], sep="\n  ", file=sys.stderr)
            return 1
        path = os.path.join(os.path.dirname(args.out), "ks.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"), sort_keys=True)
        print(f"wrote {path}: {len(data['contracts'])} contracts")
        import build_state_prices
        print("wrote", build_state_prices.write_state(rates_from_results(
            data, "KS", "Kansas", KS_ITEMS, KS_PAGE, ["025026", "061597", "022625", "025041"])))
    if "ia" in states:
        data = build_ia(cache, args.months)
        bad = check(data["contracts"], data["lettings"])
        if bad:
            print("IA not written:", *bad[:20], sep="\n  ", file=sys.stderr)
            return 1
        path = os.path.join(os.path.dirname(args.out), "ia.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"), sort_keys=True)
        print(f"wrote {path}: {len(data['contracts'])} contracts")
        import build_state_prices
        print("wrote", build_state_prices.write_state(rates_from_results(
            data, "IA", "Iowa", IA_ITEMS, IA_PAGE, ["2511-7526004", "2512-1725256", "2515-2475006", "2511-7526006"])))
    if "il" in states:
        data = build_il(cache, args.months)
        bad = check(data["contracts"], data["lettings"])
        if bad:
            print("IL not written:", *bad[:20], sep="\n  ", file=sys.stderr)
            return 1
        path = os.path.join(os.path.dirname(args.out), "il.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"), sort_keys=True)
        print(f"wrote {path}: {len(data['contracts'])} contracts")
        import build_state_prices
        s = rates_from_results(data, "IL", "Illinois", IL_ITEMS, IL_HOME,
                               ["42400200", "60603800", "42400800", "42300200"])
        s.districts = {"STATEWIDE": "All of Illinois",
                       **{k: IL_DISTRICTS.get(k, f"District {k}")
                          for k in sorted(s.districts, key=lambda k: (len(k), k)) if k != "STATEWIDE"}}
        print("wrote", build_state_prices.write_state(s))
    if "mo" not in states:
        import build_state_prices
        build_state_prices.write_index()
        return 0
    from pypdf import PdfReader
    room = Room()
    token, lettings = room.lettings()
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=args.months * 31)
    picked = [(lid, d) for lid, d in lettings if cutoff <= d <= today]
    contracts, used, problems = [], [], []
    for lid, day in sorted(picked, key=lambda x: x[1]):
        path = room.tabulation(token, lid, cache, day)
        if not path:
            print(f"{day}: no bid tabulations posted")
            continue
        text = "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
        got = parse_tabs(text)
        listed, ranked = text.count("Contract ID:"), len(_VENDOR_LINE.findall(text))
        print(f"{day}: {listed} contracts, {len(got)} with flatwork")
        # A letting with no concrete work is normal; one whose bidders can't
        # be read at all means the layout changed.
        if listed and not ranked:
            problems.append(f"{day}: {listed} contracts listed but no bidders read")
        contracts += got
        used.append(day.isoformat())
    problems += check(contracts, used)
    if problems:
        print("Not written -- the parse looks wrong:", *problems[:20], sep="\n  ", file=sys.stderr)
        return 1
    contracts.sort(key=lambda c: (c["date"], c["id"]))
    data = {"state": "MO", "source": "MoDOT bid tabulations", "source_url": BASE,
            "lettings": used, "contracts": contracts, "wins": wins(contracts), "named": True}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), sort_keys=True)
    print(f"wrote {args.out}: {len(contracts)} contracts from {len(used)} lettings")
    import build_state_prices
    build_state_prices.write_index()
    return 0


if __name__ == "__main__":
    sys.exit(main())
