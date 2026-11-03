"""Local labor pay and the ready-mix price trend, for the profit estimate.

    python3 tools/build_cost_data.py            (BLS_API_KEY in the environment if there is one)

Until a contractor enters their own costs, the app estimates what a bid
makes from starting guesses. Two of those guesses can be real numbers:

  - labor: the median hourly pay of cement masons and concrete finishers
    (occupation 47-2051) in each state, from the Bureau of Labor Statistics'
    Occupational Employment and Wage Statistics. Published once a year.
    The app adds LABOR_BURDEN on top for payroll taxes, workers' comp and
    insurance, and says so.
  - concrete: the BLS producer price index for ready-mixed concrete
    (PCU327320327320), monthly, as the change over the last 12 months. It
    is national: no one publishes local ready-mix prices.

Writes curbcall_netlify_v4/rates/costs.json. If BLS can't be reached or
answers with an error, the file already there is left alone and this exits
1, so last month's numbers stay rather than going blank.

BLS's API allows a small number of requests a day without a key (shared by
every caller on the same address); a free key raises it. The monthly
refresh workflow passes one if the BLS_API_KEY secret is set.
"""
import datetime
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "curbcall_netlify_v4", "rates", "costs.json")
API = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
UA = "BidCallerPro/1.0 (concrete cost data; contact via github.com/slappduck/bid-caller-pro)"
OCC = "472051"            # Cement masons and concrete finishers
WAGE_MEDIAN_HOURLY = "08"
READY_MIX_PPI = "PCU327320327320"
BATCH = 25                # series per request without a key

FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09",
    "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17",
    "IN": "18", "IA": "19", "KS": "20", "KY": "21", "LA": "22", "ME": "23", "MD": "24",
    "MA": "25", "MI": "26", "MN": "27", "MS": "28", "MO": "29", "MT": "30", "NE": "31",
    "NV": "32", "NH": "33", "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38",
    "OH": "39", "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53", "WV": "54",
    "WI": "55", "WY": "56",
}


def wage_series(st):
    """OEWS series id: OE U S <area: state FIPS + 00000> <industry 000000> <occupation> <data type>."""
    return f"OEUS{FIPS[st]}00000000000{OCC}{WAGE_MEDIAN_HOURLY}"


def fetch(series, start, end, key=None):
    body = {"seriesid": series, "startyear": str(start), "endyear": str(end)}
    if key:
        body["registrationkey"] = key
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
    if d.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError("BLS: " + " ".join(d.get("message") or [d.get("status") or "no status"]))
    return {s["seriesID"]: s.get("data") or [] for s in (d.get("Results") or {}).get("series", [])}


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None      # "-" and "*" mark suppressed or missing values
    return x if x > 0 else None


def latest_annual(rows):
    """(value, year) of the newest annual figure, or None."""
    best = None
    for r in rows:
        v = _num(r.get("value"))
        if v is not None and str(r.get("period", "")).startswith("A"):
            y = int(r["year"])
            if not best or y > best[1]:
                best = (v, y)
    return best


def yearly_change(rows):
    """{"month": "YYYY-MM", "change_12m": pct} from monthly index rows, or None."""
    by = {}
    for r in rows:
        p, v = str(r.get("period", "")), _num(r.get("value"))
        if p.startswith("M") and p != "M13" and v is not None:
            by[(int(r["year"]), int(p[1:]))] = v
    if not by:
        return None
    y, m = max(by)
    prev = by.get((y - 1, m))
    if not prev:
        return None
    return {"month": f"{y:04d}-{m:02d}", "change_12m": round((by[(y, m)] / prev - 1) * 100, 1)}


def build(key=None, today=None, fetcher=None):
    today = today or datetime.date.today()
    fetcher = fetcher or fetch
    ids = {wage_series(st): st for st in FIPS}
    rows = {}
    names = list(ids)
    for i in range(0, len(names), BATCH):
        rows.update(fetcher(names[i:i + BATCH], today.year - 3, today.year, key))
    wages = {}
    for sid, st in ids.items():
        w = latest_annual(rows.get(sid, []))
        if w:
            wages[st] = {"median": round(w[0], 2), "year": w[1]}
    if len(wages) < len(FIPS) // 2:
        raise RuntimeError(f"only {len(wages)} states came back with a wage")
    ppi = yearly_change(fetcher([READY_MIX_PPI], today.year - 2, today.year, key).get(READY_MIX_PPI, []))
    return {
        "labor": {"occupation": "Cement masons and concrete finishers", "statistic": "median hourly pay",
                  "source": "U.S. Bureau of Labor Statistics, Occupational Employment and Wage Statistics",
                  "by_state": wages},
        "ready_mix": dict(ppi or {}, source="U.S. Bureau of Labor Statistics, producer price index for ready-mixed concrete"),
    }


def main():
    try:
        data = build(os.environ.get("BLS_API_KEY") or None)
    except Exception as e:      # keep last month's file rather than writing a broken one
        print(f"costs.json NOT updated: {e}", file=sys.stderr)
        return 1
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), sort_keys=True)
    print(f"wrote {OUT}: pay for {len(data['labor']['by_state'])} states, ready-mix {data['ready_mix'].get('change_12m')}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
