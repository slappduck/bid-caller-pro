#!/usr/bin/env python3
"""Build the app's "going rates" data from MoDOT's Unit Bid Price Books.

Every year MoDOT publishes, per district and statewide, the average, high
and low bid it received for every pay item on its highway lettings, with the
number of bids and the quantities bid. For a concrete contractor pricing a
sidewalk, curb or ADA ramp job that is the best public benchmark there is,
and nobody puts it in front of them: it sits in a 70-190 page PDF on the
plans room's General Info page.

This reads the concrete flatwork items out of those books. The app's file,
curbcall_netlify_v4/rates/mo.json, is written by tools/build_state_prices.py,
which does every state the same way; this module is its Missouri parser.
Run it once a year, after MoDOT posts the new book, with the new year added
to BOOKS:

    pip install pypdf openpyxl
    python3 tools/build_state_prices.py --states mo

The PDFs are public records on a site with no robots.txt restriction; they
are fetched once each with the same honest User-Agent as everything else.
Nothing here is invented or estimated: every number written is one MoDOT
printed. The build refuses to write if a year yields no flatwork rows or a
row's average falls outside its own high-low range, either of which means
the parse went wrong, not that prices changed.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_PAGE = "https://modotweb.modot.mo.gov/BidLettingPlansRoom/GeneralInfo"
BOOK_URL = ("https://modotweb.modot.mo.gov/BidLettingPlansRoom/Letting/"
            "ViewStream/{id}?type=general_info")
# The document id behind each year's "Unit Bid Price List" link on SOURCE_PAGE.
BOOKS = {2025: 3799, 2024: 2237, 2023: 3479, 2022: 3320, 2021: 2899}
USER_AGENT = ("CurbCallBot/1.0 (+https://curbcallpro.com; concrete bid "
              "aggregator; contact support@curbcallpro.com)")

DISTRICTS = {
    "STATEWIDE": "All of Missouri",
    "NW": "Northwest (St. Joseph)",
    "NE": "Northeast (Hannibal)",
    "KC": "Kansas City",
    "CD": "Central (Jefferson City)",
    "SL": "St. Louis",
    "SW": "Southwest (Springfield, Joplin)",
    "SE": "Southeast (Sikeston, Cape Girardeau)",
}

# Section 608 (sidewalks, ramps, medians, approaches) and 609 (curb and
# gutter). 6096/6097 are rock ditch liners and 6094 drain basins -- in the
# same sections but not flatwork. Asphalt curb shares 6093.
FLATWORK_PREFIXES = ("6081", "6083", "6085", "6086", "6091", "6092")

# Plain names for the items contractors look for. Anything else in the
# prefixes above keeps MoDOT's own description, title-cased.
NAMES = {
    "6081010": "Concrete curb ramp (ADA)",
    "6081012": "Truncated domes (ADA warning)",
    "6081000": "Concrete median",
    "6085007": "Paved approach / driveway, 7 in.",
    "6085008": "Paved approach / driveway, 8 in.",
    "6086004": "Concrete sidewalk, 4 in.",
    "6086005": "Concrete sidewalk, 5 in.",
    "6086006": "Concrete sidewalk, 6 in.",
    "6086007": "Concrete sidewalk, 7 in.",
    "6086008": "Concrete sidewalk, 8 in.",
    "6091010": "Concrete curb, 6 in. and under",
    "6091011": "Concrete curb, over 6 in.",
    "6091041": "Concrete gutter, type A",
    "6091042": "Concrete gutter, type B",
    "6091051": "Curb and gutter, type A",
    "6091052": "Curb and gutter, type B",
    "6091060": "Paved ditch",
}
UNITS = {"S.Y.": "sq yd", "S.F.": "sq ft", "L.F.": "ft", "EACH": "each",
         "C.Y.": "cu yd", "L.S.": "lump sum"}

_NUM = r"\$?(-?[\d,]+\.\d+|-?[\d,]+)"
_ROW = re.compile(
    r"^(\d{7}[A-Z]?)\s+(.+?)\s+(L\.S\.|ACRE|S\.Y\.|L\.F\.|EACH|C\.Y\.|STA\.|"
    r"S\.F\.|100FT|TON|GAL\.?|LB\.?|HOUR|DAY|MILE|[A-Z.]{2,6})\s+"
    + r"\s+".join([_NUM] * 6) + r"\s*$")
# "MoDOT 2025 UNIT BID PRICES NW DISTRICT"; 2022 writes "Statewide".
_HEADER = re.compile(r"UNIT BID PRICES\s+(STATEWIDE|[A-Z]{2})\b", re.I)


def _num(s):
    return float(s.replace(",", ""))


def parse_text(text, year):
    """Every pay-item row in one book's extracted text.

    Columns, as MoDOT prints them: pay item, description, unit, average
    quantity, number of bids, average price, high bid, low bid, total
    quantity. Rows the pattern can't read are skipped, never guessed at.
    """
    rows, district = [], None
    for line in text.splitlines():
        line = line.strip()
        head = _HEADER.search(line)
        if head:
            district = head.group(1).upper()
            continue
        m = _ROW.match(line)
        if not m or district is None:
            continue
        rows.append({
            "year": int(year), "district": district, "item": m.group(1),
            "desc": re.sub(r"\s+", " ", m.group(2)).strip(), "unit": m.group(3),
            "avg_qty": _num(m.group(4)), "bids": int(_num(m.group(5))),
            "avg": _num(m.group(6)), "high": _num(m.group(7)),
            "low": _num(m.group(8)), "total_qty": _num(m.group(9)),
        })
    return rows


def is_flatwork(row):
    return (row["item"].startswith(FLATWORK_PREFIXES)
            and "ASPHALT" not in row["desc"].upper()
            and row["unit"] in UNITS)


def check(rows, years):
    """Reasons the data must not be written, or [] if it may."""
    problems = []
    for y in years:
        if not any(r["year"] == y for r in rows):
            problems.append(f"{y}: no flatwork rows read")
    for r in rows:
        if not (r["low"] - 0.01 <= r["avg"] <= r["high"] + 0.01):
            problems.append(f"{r['year']} {r['district']} {r['item']}: average "
                            f"{r['avg']} outside {r['low']}-{r['high']}")
        if r["bids"] < 1:
            problems.append(f"{r['year']} {r['district']} {r['item']}: no bids")
    seen = set()
    for r in rows:
        key = (r["year"], r["district"], r["item"])
        if key in seen:
            problems.append(f"{key}: listed twice")
        seen.add(key)
    return problems


def build(rows, years):
    """The compact structure the app reads."""
    items, prices = {}, {}
    for r in sorted(rows, key=lambda r: (r["item"], r["year"])):
        items.setdefault(r["item"], {
            "name": NAMES.get(r["item"]) or r["desc"].capitalize(),
            "unit": UNITS[r["unit"]],
        })
        # [average, low, high, bids, average quantity per bid]
        prices.setdefault(r["item"], {}).setdefault(r["district"], {})[str(r["year"])] = [
            round(r["avg"], 2), round(r["low"], 2), round(r["high"], 2),
            r["bids"], round(r["avg_qty"], 1)]
    return {
        "source": "MoDOT Unit Bid Price Books",
        "source_url": SOURCE_PAGE,
        "years": sorted(years),
        "districts": DISTRICTS,
        "fields": ["avg", "low", "high", "bids", "avg_qty"],
        "items": items,
        "prices": prices,
    }


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import build_state_prices
    sys.argv = [sys.argv[0], "--states", "mo"] + sys.argv[1:]
    return build_state_prices.main()


if __name__ == "__main__":
    sys.exit(main())
