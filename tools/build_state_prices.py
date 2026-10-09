#!/usr/bin/env python3
"""Build the app's "going rates" for every state whose DOT publishes them.

The app shows a contractor what concrete flatwork actually goes for --
sidewalk, curb and gutter, ADA ramps, driveways -- from the state DOT's own
bid records. Missouri came first (tools/build_unit_prices.py). Each state
publishes differently, so each has an adapter here, and all of them write
the same shape: curbcall_netlify_v4/rates/<st>.json.

    pip install pypdf openpyxl
    python3 tools/build_state_prices.py                 # every state
    python3 tools/build_state_prices.py --states fl,or  # just these
    python3 tools/build_state_prices.py --cache /tmp/rates   # keep downloads

What each state gives, and so what the app can honestly say:

  MO  Unit Bid Price Books: average, low and high of EVERY bid, per district
      and year. ("all_bids")
  OR  Annual bid data: every bidder's unit price on every item, with the
      bidder's rank. The richest of the lot: averages, ranges, and what the
      WINNING bid was. ("all_bids", plus wins)
  FL  Item average unit cost, 12-month moving, statewide and 14 market
      areas. Awarded prices only. ("awarded")
  MN  Average bid prices for awarded projects, statewide, per year.
      ("awarded")
  OK  Weighted average item price report, per district and quarter, awarded
      prices. ("awarded")

"awarded" averages are winning prices; "all_bids" averages include losers,
so the app labels them differently. Nothing is estimated: every number is
one the DOT printed, or (OR) arithmetic over the bids it printed. Only the
items listed per state are kept, so a "sidewalk" price is always a plain
sidewalk item, never a specialty variant averaged in.

The build refuses to write a state whose parse looks wrong (no rows, an
average outside its own range, a year missing) rather than publish it.

Per-state file (the app reads these):
  state, state_name, source, source_url, basis, years, periods?,
  districts {key: label}, district_counties? {key: [county]},
  fields ["avg","low","high","n","avg_qty"],
  items {code: {name, unit, cat?}},
  prices {code: {district: {year: [avg, low|null, high|null, n, avg_qty]}}},
  wins? {code: {district: {year: [avg, low, high, n, p25, p75]}}}  (winning bids)
  cats {category: code}, headline [code]
Categories are what the app matches a posting's words to: sidewalk,
sidewalk6, ramp, domes, curb_gutter, curb, driveway, gutter, median,
removal.
"""
import argparse
import collections
import datetime
import io
import json
import os
import re
import statistics
import sys
import tempfile
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "curbcall_netlify_v4", "rates")
USER_AGENT = ("CurbCallBot/1.0 (+https://curbcallpro.com; concrete bid "
              "aggregator; contact support@curbcallpro.com)")
FIELDS = ["avg", "low", "high", "n", "avg_qty"]
CATEGORIES = ("sidewalk", "sidewalk6", "ramp", "domes", "curb_gutter", "curb",
              "driveway", "gutter", "median", "removal")


def _download(url, cache, name):
    path = os.path.join(cache, name)
    if not os.path.exists(path):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=300) as resp, open(path, "wb") as f:
            f.write(resp.read())
    return path


def _pdf_text(path):
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)


def _rows_xlsx(data, sheet=None):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    for r in ws.iter_rows(values_only=True):
        yield r


def _r2(x):
    return None if x is None else round(float(x), 2)


class State:
    """One state's rates as they are collected, then checked and written."""

    def __init__(self, st, name, source, source_url, basis, items, cats,
                 headline, districts, district_counties=None):
        self.st, self.name = st, name
        self.source, self.source_url, self.basis = source, source_url, basis
        self.items = items              # code -> {"name", "unit", "cat"?}
        self.cats = cats
        self.headline = headline
        self.districts = districts
        self.district_counties = district_counties
        self.prices = {}                # code -> district -> year -> row
        self.wins = {}
        self.periods = {}

    def add(self, code, district, year, avg, low, high, n, avg_qty):
        self.prices.setdefault(code, {}).setdefault(district, {})[str(year)] = [
            _r2(avg), _r2(low), _r2(high), int(n), round(float(avg_qty or 0), 1)]

    def add_win(self, code, district, year, avg, low, high, n, p25=None, p75=None):
        self.wins.setdefault(code, {}).setdefault(district, {})[str(year)] = [
            _r2(avg), _r2(low), _r2(high), int(n), _r2(p25 if p25 is not None else low),
            _r2(p75 if p75 is not None else high)]

    def problems(self):
        out = []
        if not self.prices:
            return [f"{self.st}: no prices read"]
        years = {y for d in self.prices.values() for by in d.values() for y in by}
        for code in self.prices:
            if code not in self.items:
                out.append(f"{self.st} {code}: not a listed item")
        for table, label in ((self.prices, "price"), (self.wins, "win")):
            for code, by_d in table.items():
                for d, by_y in by_d.items():
                    if d not in self.districts:
                        out.append(f"{self.st} {code}: unknown district {d}")
                    for y, row in by_y.items():
                        avg, low, high, n = row[0], row[1], row[2], row[3]
                        if not (avg and avg > 0) or n < 1:
                            out.append(f"{self.st} {code} {d} {y}: empty {label}")
                        if low is not None and high is not None and not (
                                low - 0.01 <= avg <= high + 0.01):
                            out.append(f"{self.st} {code} {d} {y}: {label} average "
                                       f"{avg} outside {low}-{high}")
        for cat, code in self.cats.items():
            if cat not in CATEGORIES:
                out.append(f"{self.st}: unknown category {cat}")
            if code not in self.prices:
                out.append(f"{self.st}: category {cat} -> {code} has no prices")
        for code in self.headline:
            if code not in self.prices:
                out.append(f"{self.st}: headline {code} has no prices")
        if not years:
            out.append(f"{self.st}: no years")
        return out

    def data(self):
        years = sorted({int(y) for d in self.prices.values()
                        for by in d.values() for y in by})
        items = {c: m for c, m in self.items.items() if c in self.prices}
        out = {
            "state": self.st, "state_name": self.name,
            "source": self.source, "source_url": self.source_url,
            "basis": self.basis, "years": years, "fields": FIELDS,
            "districts": self.districts, "items": items,
            "prices": self.prices, "cats": self.cats, "headline": self.headline,
        }
        if self.wins:
            out["wins"] = self.wins
        if self.periods:
            out["periods"] = self.periods
        if self.district_counties:
            out["district_counties"] = self.district_counties
        return out


# ── Missouri ────────────────────────────────────────────────────────────────
def build_mo(cache):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import build_unit_prices as mo
    rows = []
    for year in sorted(mo.BOOKS):
        path = _download(mo.BOOK_URL.format(id=mo.BOOKS[year]), cache,
                         f"modot_unit_bid_prices_{year}.pdf")
        got = [r for r in mo.parse_text(_pdf_text(path), year) if mo.is_flatwork(r)]
        print(f"  MO {year}: {len(got)} flatwork prices")
        rows += got
    bad = mo.check(rows, mo.BOOKS)
    if bad:
        raise ValueError("; ".join(bad[:10]))
    old = mo.build(rows, mo.BOOKS)
    s = State("MO", "Missouri", old["source"], old["source_url"], "all_bids",
              items=old["items"], districts=old["districts"],
              cats={"ramp": "6081010", "domes": "6081012", "sidewalk": "6086004",
                    "curb_gutter": "6091052",
                    "curb": "6091010", "driveway": "6085008",
                    "gutter": "6091042", "median": "6083006"},
              headline=["6081010", "6081012", "6086004", "6091052"])
    for code, meta in s.items.items():
        for cat, c in s.cats.items():
            if c == code:
                meta["cat"] = cat
    for code, by_d in old["prices"].items():
        for d, by_y in by_d.items():
            for y, (avg, low, high, bids, qty) in by_y.items():
                s.add(code, d, y, avg, low, high, bids, qty)
    return s


# ── Florida ─────────────────────────────────────────────────────────────────
FL_PAGE = "https://fdot.gov/fpo/fpc/reports/historicalitemaveragecost"
FL_BASE = ("https://fdotwww.blob.core.windows.net/sitefinity/docs/default-source/"
           "fpo/fpc/reports/historicalitemaveragecost/moving/")
FL_STATEWIDE = FL_BASE + "statewide/excel/historical-item-averages-statewide-12-months.xlsx"
FL_AREA = FL_BASE + "market-area/excel/historical-item-averages-market-area-{n:02d}.xlsx"
# fdot.gov/fpo/fpc/shared/marketareas
FL_AREAS = {
    "01": ["Bay", "Escambia", "Okaloosa", "Santa Rosa", "Walton"],
    "02": ["Calhoun", "Franklin", "Gulf", "Holmes", "Jackson", "Liberty", "Washington"],
    "03": ["Gadsden", "Jefferson", "Leon", "Wakulla"],
    "04": ["Baker", "Bradford", "Columbia", "Dixie", "Gilchrist", "Hamilton",
           "Lafayette", "Levy", "Madison", "Putnam", "Suwannee", "Taylor", "Union"],
    "05": ["Clay", "Duval", "Nassau", "St. Johns"],
    "06": ["Alachua", "Marion", "Volusia"],
    "07": ["Lake", "Pasco", "Hernando", "Citrus", "Sumter", "Flagler"],
    "08": ["Osceola", "Polk", "Brevard", "Hillsborough", "Orange", "Pinellas", "Seminole"],
    "09": ["DeSoto", "Glades", "Hardee", "Hendry", "Highlands", "Okeechobee"],
    "10": ["Charlotte", "Collier", "Lee", "Manatee", "Sarasota"],
    "11": ["Indian River", "Martin", "St. Lucie"],
    "12": ["Broward", "Palm Beach"],
    "13": ["Miami-Dade"],
    "14": ["Monroe"],
}
FL_ITEMS = {
    "0522-1": ("Concrete sidewalk / driveway, 4 in.", "sidewalk"),
    "0522-2": ("Concrete sidewalk / driveway, 6 in.", "sidewalk6"),
    "0520-1-10": ("Curb and gutter, type F", "curb_gutter"),
    "0520-1-7": ("Curb and gutter, type E", None),
    "0520-2-4": ("Concrete curb, type D", "curb"),
    "0520-2-1": ("Concrete curb, type A", None),
    "0520-2-2": ("Concrete curb, type B", None),
    "0520-3": ("Valley gutter", "gutter"),
    "0520-6": ("Shoulder gutter", None),
    "0527-2": ("Detectable warnings (ADA)", "domes"),
    "0110-4-10": ("Remove existing concrete", "removal"),
}
FL_UNITS = {"SY": "sq yd", "SF": "sq ft", "LF": "ft", "EA": "each"}
_FL_PERIOD = re.compile(r"From (\d{4})/(\d{2})/\d{2}\s+to (\d{4})/(\d{2})/\d{2}")


def _fl_code(raw):
    return "-".join(str(raw).split())


def parse_fl(rows):
    """(period_end_year, period_label, {code: (contracts, avg, qty, unit)})."""
    period, out = None, {}
    for r in rows:
        if not r or r[0] is None:
            continue
        m = _FL_PERIOD.search(str(r[0]))
        if m:
            months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
                      "Sep", "Oct", "Nov", "Dec"]
            period = (int(m.group(3)),
                      f"{months[int(m.group(2)) - 1]} {m.group(1)} – "
                      f"{months[int(m.group(4)) - 1]} {m.group(3)}")
            continue
        if len(r) < 8 or not isinstance(r[1], (int, float)):
            continue
        code = _fl_code(r[0])
        if code in FL_ITEMS and str(r[5]).strip() in FL_UNITS:
            out[code] = (int(r[1]), float(r[2]), float(r[4]), FL_UNITS[str(r[5]).strip()])
    return period, out


def build_fl(cache):
    districts = {"STATEWIDE": "All of Florida"}
    for k, counties in FL_AREAS.items():
        districts[k] = f"Area {int(k)}: {', '.join(counties[:3])}{'…' if len(counties) > 3 else ''}"
    items = {c: {"name": n, "unit": None, **({"cat": cat} if cat else {})}
             for c, (n, cat) in FL_ITEMS.items()}
    s = State("FL", "Florida", "FDOT Item Average Unit Cost (12-month)", FL_PAGE,
              "awarded", items=items, districts=districts,
              district_counties=FL_AREAS,
              cats={cat: c for c, (_, cat) in FL_ITEMS.items() if cat},
              headline=["0522-1", "0522-2", "0520-1-10", "0527-2"])
    sources = [("STATEWIDE", FL_STATEWIDE, "fl_statewide.xlsx")] + [
        (k, FL_AREA.format(n=int(k)), f"fl_area_{k}.xlsx") for k in FL_AREAS]
    for d, url, name in sources:
        with open(_download(url, cache, name), "rb") as f:
            period, got = parse_fl(_rows_xlsx(f.read()))
        if not period:
            raise ValueError(f"FL {d}: no reporting period found")
        year, label = period
        s.periods[str(year)] = label
        for code, (n, avg, qty, unit) in got.items():
            s.items[code]["unit"] = unit
            s.add(code, d, year, avg, None, None, n, qty / n if n else 0)
        print(f"  FL {d}: {len(got)} items, {label}")
    return s


# ── Oregon ──────────────────────────────────────────────────────────────────
OR_PAGE = "https://www.oregon.gov/odot/Business/Pages/average_bid_item_prices.aspx"
OR_ZIP = "https://www.oregon.gov/odot/Business/Estimating/{y}%20BID%20DATA%20PROGRAM.zip"
OR_YEARS = (2021, 2022, 2023, 2024, 2025, 2026)
OR_REGIONS = {"STATEWIDE": "All of Oregon", "1": "Region 1 (Portland metro)",
              "2": "Region 2 (Willamette Valley, North Coast)",
              "3": "Region 3 (Southwest)", "4": "Region 4 (Central)",
              "5": "Region 5 (Eastern)"}
# code -> (name, category, the description ODOT uses for the plain item).
# The same code also carries "MODIFIED" and specialty variants; only rows
# with exactly this description count.
OR_ITEMS = {
    "0759-0128000J": ("Concrete walks", "sidewalk", "CONCRETE WALKS"),
    "0759-0126000J": ("Concrete driveways", "driveway", "CONCRETE DRIVEWAYS"),
    "0759-0103000F": ("Curb and gutter", "curb_gutter", "CONCRETE CURBS, CURB AND GUTTER"),
    "0759-0110000F": ("Standard curb", "curb", "CONCRETE CURBS, STANDARD CURB"),
    "0759-0122000J": ("Concrete islands", "median", "CONCRETE ISLANDS"),
    "0759-0135000J": ("Valley gutter", "gutter", "VALLEY GUTTER CONCRETE SURFACING"),
    "0759-0510000J": ("Truncated domes, new surfaces (ADA)", "domes",
                      "TRUNCATED DOMES ON NEW SURFACES"),
    "0759-0154100E": ("Extra for new curb ramps (each, on top of the walk)", None,
                      "EXTRA FOR NEW CURB RAMPS"),
    "0310-0102000J": ("Remove walks and driveways", "removal",
                      "REMOVAL OF WALKS AND DRIVEWAYS"),
    "0310-0101000F": ("Remove curbs", None, "REMOVAL OF CURBS"),
}
OR_UNITS = {"SQFT": "sq ft", "SQYD": "sq yd", "FOOT": "ft", "EACH": "each"}


def parse_or(rows):
    """Every bid row in one year's BID DATA sheet.

    Columns: date, year, quarter, contract, region, project, item, item
    description, unit, quantity, unit price, amount, bidder rank.
    """
    out = []
    for r in rows:
        if len(r) < 14 or not isinstance(r[1], datetime.datetime):
            continue
        code, desc = str(r[7] or "").strip(), re.sub(r"\s+", " ", str(r[8] or "")).strip()
        if code not in OR_ITEMS or desc != OR_ITEMS[code][2]:
            continue
        unit = OR_UNITS.get(str(r[9] or "").strip())
        try:
            qty, price, rank = float(r[10]), float(r[11]), int(r[13])
        except (TypeError, ValueError):
            continue
        if not unit or price <= 0 or qty <= 0:
            continue
        out.append({"year": int(r[2]), "contract": str(r[4]), "region": str(r[5]).strip(),
                    "project": str(r[6] or "").strip(), "code": code, "unit": unit,
                    "qty": qty, "price": price, "rank": rank, "date": r[1].date().isoformat()})
    return out


def build_or(cache):
    items = {c: {"name": n, "unit": None, **({"cat": cat} if cat else {})}
             for c, (n, cat, _) in OR_ITEMS.items()}
    s = State("OR", "Oregon", "ODOT Bid Item Prices (bid tabulations)", OR_PAGE,
              "all_bids", items=items, districts=OR_REGIONS,
              cats={cat: c for c, (_, cat, _) in OR_ITEMS.items() if cat},
              headline=["0759-0128000J", "0759-0103000F", "0759-0126000J", "0759-0510000J"])
    bids = []
    for y in OR_YEARS:
        with zipfile.ZipFile(_download(OR_ZIP.format(y=y), cache, f"or_bid_data_{y}.zip")) as z:
            name = next(n for n in z.namelist() if n.lower().endswith((".xlsx", ".xlsm")))
            got = parse_or(_rows_xlsx(z.read(name), "BID DATA"))
        print(f"  OR {y}: {len(got)} flatwork bids")
        bids += got
    groups = collections.defaultdict(list)
    for b in bids:
        s.items[b["code"]]["unit"] = b["unit"]
        if b["region"] not in OR_REGIONS:
            continue
        for d in (b["region"], "STATEWIDE"):
            groups[(b["code"], d, b["year"])].append(b)
    for (code, d, y), g in groups.items():
        prices = [b["price"] for b in g]
        qty = statistics.mean({b["contract"]: b["qty"] for b in g}.values())
        s.add(code, d, y, statistics.mean(prices), min(prices), max(prices), len(g), qty)
        wins = [b["price"] for b in g if b["rank"] == 1]
        if wins:
            q = statistics.quantiles(sorted(wins), n=4, method="inclusive") if len(wins) > 1 else [wins[0]] * 3
            s.add_win(code, d, y, statistics.mean(wins), min(wins), max(wins), len(wins), q[0], q[2])
    s.bids = bids
    return s


# ── Minnesota ───────────────────────────────────────────────────────────────
MN_PAGE = "https://dot.state.mn.us/pre-letting/cost-estimating/index.html"
MN_DOC = "https://edocs-public.dot.state.mn.us/edocs_public/DMResultSet/download?docId={id}"
# The "Average Bid Prices <year>" Excel file in MnDOT's eDocs folder.
MN_DOCS = {2025: 39047465, 2024: 38747531, 2023: 38580317, 2022: 28526646, 2021: 28521508}
MN_ITEMS = {
    "2521518/00040": ('Concrete walk, 4 in.', "sidewalk"),
    "2521518/00060": ('Concrete walk, 6 in.', "sidewalk6"),
    "2521618/00400": ("Concrete curb ramp walk", "ramp"),
    "2531618/00010": ("Truncated domes (ADA)", "domes"),
    "2531503/02320": ("Curb and gutter, design B624", "curb_gutter"),
    "2531503/02120": ("Curb and gutter, design B424", None),
    "2531603/24070": ("Concrete curb and gutter", None),
    "2531504/00060": ("Concrete driveway pavement, 6 in.", "driveway"),
    "2531504/00080": ("Concrete driveway pavement, 8 in.", None),
    "2531504/00010": ("Concrete median", "median"),
    "2104518/00130": ("Remove concrete sidewalk", "removal"),
    "2104503/00315": ("Remove curb and gutter", None),
}
MN_UNITS = {"SF": "sq ft", "SY": "sq yd", "LF": "ft", "EACH": "each"}


def parse_mn(rows):
    """{code: (avg, contracts, quantity, unit)} from one year's sheet.

    Columns: group, item number, description, units, quantity, total
    dollars, average bid price, contract occurrences.
    """
    out = {}
    for r in rows:
        if len(r) < 8 or not r[1]:
            continue
        code = str(r[1]).strip()
        unit = MN_UNITS.get(str(r[3] or "").replace(" ", "").upper())
        if code not in MN_ITEMS or not unit:
            continue
        try:
            out[code] = (float(r[6]), int(r[7]), float(r[4]), unit)
        except (TypeError, ValueError):
            continue
    return out


def build_mn(cache):
    items = {c: {"name": n, "unit": None, **({"cat": cat} if cat else {})}
             for c, (n, cat) in MN_ITEMS.items()}
    s = State("MN", "Minnesota", "MnDOT Average Bid Prices for Awarded Projects",
              MN_PAGE, "awarded", items=items,
              districts={"STATEWIDE": "All of Minnesota"},
              cats={cat: c for c, (_, cat) in MN_ITEMS.items() if cat},
              headline=["2521518/00040", "2521618/00400", "2531503/02320", "2531618/00010"])
    for y, doc in sorted(MN_DOCS.items()):
        with open(_download(MN_DOC.format(id=doc), cache, f"mn_avg_bid_prices_{y}.xlsx"), "rb") as f:
            got = parse_mn(_rows_xlsx(f.read()))
        print(f"  MN {y}: {len(got)} items")
        for code, (avg, n, qty, unit) in got.items():
            s.items[code]["unit"] = unit
            s.add(code, "STATEWIDE", y, avg, None, None, n, qty / n if n else 0)
    return s


# ── Oklahoma ────────────────────────────────────────────────────────────────
OK_DIR = "https://www.odot.org/contracts/avgprices/"
OK_DISTRICTS = {"STATEWIDE": "All of Oklahoma", **{
    f"{n:02d}": f"Division {n}" for n in range(1, 9)}}
OK_ITEMS = {
    "610(A)5200": ('Concrete sidewalk, 4 in.', "sidewalk"),
    "610(A)5220": ('Concrete sidewalk, 6 in.', "sidewalk6"),
    "610(B)5310": ('Concrete driveway, 6 in.', "driveway"),
    "610(B)5320": ('Concrete driveway, 8 in.', None),
    "609(B)4310": ('Curb and gutter, 6 in. barrier', "curb_gutter"),
    "609(B)4300": ('Curb and gutter, 4 in. mountable', None),
    # Cast with the pavement, so far cheaper than a standalone curb: listed,
    # but never what a posting's bare "curb" is priced at.
    "609(A)4230": ('Concrete curb, 6 in. barrier (integral)', None),
    "619(B)6404": ("Remove sidewalk", "removal"),
    "619(B)6356": ("Remove curb and gutter", None),
}
OK_UNITS = {"SY": "sq yd", "SF": "sq ft", "LF": "ft", "EA": "each"}
_OK_ITEM = re.compile(r"^(\S+) / (.+) / (\S+)\s*$")
# " 2  2.00 $ 14,300.00 $ 7,150.00 $ 8,833.332025Q401" -- occurrences,
# quantity, dollars, average awarded, average of low 3, then quarter and
# district run together. The district is printed once per run of rows; an
# item's closing total row has no quarter at all.
_OK_ROW = re.compile(
    r"^\s*(\d+)\s+([\d,]+\.\d+)\s+\$\s*([\d,]+\.\d+)\s+\$\s*([\d,]+\.\d+)\s+"
    r"\$\s*([\d,]+\.\d{2})(\d{4})Q([1-4])(\d{2})?\s*$")


def _f(s):
    return float(s.replace(",", ""))


def parse_ok(text):
    """[(code, unit, district, year, occurrences, qty, dollars)] rows."""
    out, code, unit, district = [], None, None, None
    for line in text.splitlines():
        m = _OK_ITEM.match(line.strip())
        if m:
            code, unit, district = m.group(1), OK_UNITS.get(m.group(3)), None
            continue
        m = _OK_ROW.match(line)
        if not m or code not in OK_ITEMS or not unit:
            continue
        if m.group(8):
            district = m.group(8)
        if not district:
            continue
        out.append((code, unit, district, int(m.group(6)), int(m.group(1)),
                    _f(m.group(2)), _f(m.group(3))))
    return out


def _ok_latest_report():
    req = urllib.request.Request(OK_DIR, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        names = re.findall(r"eng(\d{6})\.pdf", resp.read().decode("latin-1"))
    if not names:
        raise ValueError("OK: no reports listed")
    return f"eng{max(names)}.pdf"


def build_ok(cache, report=None):
    report = report or _ok_latest_report()
    items = {c: {"name": n, "unit": None, **({"cat": cat} if cat else {})}
             for c, (n, cat) in OK_ITEMS.items()}
    s = State("OK", "Oklahoma", "ODOT Weighted Average Item Price Report",
              OK_DIR + report, "awarded", items=items, districts=OK_DISTRICTS,
              cats={cat: c for c, (_, cat) in OK_ITEMS.items() if cat},
              headline=["610(A)5200", "609(B)4310", "610(B)5310", "610(A)5220"])
    text = _pdf_text(_download(OK_DIR + report, cache, report))
    m = re.search(r"Item Price History from (\w+ \d{2}, \d{4}) to (\w+ \d{2}, \d{4})", text)
    rows = parse_ok(text)
    print(f"  OK {report}: {len(rows)} rows" + (f", {m.group(1)} – {m.group(2)}" if m else ""))
    sums = collections.defaultdict(lambda: [0, 0.0, 0.0])
    for code, unit, d, y, n, qty, dollars in rows:
        s.items[code]["unit"] = unit
        for key in ((code, d, y), (code, "STATEWIDE", y)):
            sums[key][0] += n
            sums[key][1] += qty
            sums[key][2] += dollars
    for (code, d, y), (n, qty, dollars) in sums.items():
        if d in OK_DISTRICTS and qty > 0:
            s.add(code, d, y, dollars / qty, None, None, n, qty / n)
    if m:
        s.periods = {"note": f"{m.group(1)} – {m.group(2)}"}
    return s


BUILDERS = {"mo": build_mo, "fl": build_fl, "or": build_or, "mn": build_mn, "ok": build_ok}


def write_state(s, out_dir=OUT_DIR):
    bad = s.problems()
    if bad:
        raise ValueError("; ".join(bad[:12]))
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, s.st.lower() + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(s.data(), f, separators=(",", ":"), sort_keys=True)
    return path


def write_index(out_dir=OUT_DIR):
    """rates/index.json: which states have rates, so the app asks only for those."""
    states = {}
    for name in sorted(os.listdir(out_dir)):
        if re.fullmatch(r"[a-z]{2}\.json", name):
            with open(os.path.join(out_dir, name), encoding="utf-8") as f:
                d = json.load(f)
            states[d["state"]] = {"name": d["state_name"], "basis": d["basis"],
                                  "years": [min(d["years"]), max(d["years"])]}
            # Bid results (tools/build_bid_results.py) sit beside the rates.
            if os.path.exists(os.path.join(os.path.dirname(out_dir), "results", name)):
                states[d["state"]]["results"] = True
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"states": states}, f, separators=(",", ":"), sort_keys=True)
    return states


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--states", default=",".join(BUILDERS))
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--cache", default=None, help="folder to keep downloads in")
    args = ap.parse_args()
    cache = args.cache or tempfile.mkdtemp(prefix="state_rates_")
    os.makedirs(cache, exist_ok=True)
    failed = []
    for st in [x.strip().lower() for x in args.states.split(",") if x.strip()]:
        print(f"{st.upper()}:")
        try:
            path = write_state(BUILDERS[st](cache), args.out)
            print(f"  wrote {path}")
        except Exception as e:      # one state's bad parse never blocks the rest
            print(f"  NOT written: {e}", file=sys.stderr)
            failed.append(st)
    states = write_index(args.out)
    print("states with rates:", ", ".join(sorted(states)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
