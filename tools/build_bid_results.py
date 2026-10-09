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
import json
import os
import re
import statistics
import sys
import tempfile
import urllib.parse
import urllib.request

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
    for y in P.OR_YEARS:
        with zipfile.ZipFile(P._download(P.OR_ZIP.format(y=y), cache, f"or_bid_data_{y}.zip")) as z:
            name = next(n for n in z.namelist() if n.lower().endswith((".xlsx", ".xlsm")))
            got = parse_or_contracts(P._rows_xlsx(z.read(name), "BID DATA"))
        print(f"OR {y}: {len(got)} contracts with flatwork")
        contracts += got
    contracts.sort(key=lambda c: (c["date"], c["id"]))
    return {"state": "OR", "source": "ODOT bid tabulations", "source_url": P.OR_PAGE,
            "lettings": sorted({c["date"] for c in contracts}), "contracts": contracts,
            "wins": wins(contracts), "named": False}


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
    ap.add_argument("--states", default="mo,or")
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
