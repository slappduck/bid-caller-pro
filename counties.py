"""County name -> coordinates, so a state-level bid can be placed on the map.

City and county bid pages say where they are by whose site they are on. A
state DOT letting does not: one page carries work from every corner of the
state, and the only location a row gives you is a county name inside its
description ("Route K VERNON County. Resurface from I-49 near Nevada...").
Without a way to turn that into a point, every state bid is either dropped as
unplaceable or shown to everybody in the state regardless of distance.

Coordinates are the Census 2020 population-weighted county centroids, not the
geographic ones. For this purpose that is the better centre: a scan is asking
"how far is this work from the contractor", and highway work follows the
population, not the empty middle of the county.
"""
import csv
import os
import re
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_CSV = os.path.join(_HERE, "data", "county_coords.csv")

_lock = threading.Lock()
_by_state = None          # {"MO": {"vernon": (lat, lon, pop)}}
_names_by_state = None    # {"MO": [("vernon", 6), ...]} longest name first


def _norm(name):
    """Fold the spelling variants a bid description actually uses.

    "ST LOUIS CITY", "St. Louis city", "Saint Louis" and "STLOUIS" are the
    same place to a contractor and four different strings to a dict.
    """
    s = str(name or "").lower()
    s = re.sub(r"\bsaint\b", "st", s)
    s = re.sub(r"\bste\.?\b", "ste", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\b(county|parish|borough|census area|city and borough|"
               r"municipality)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _load():
    global _by_state, _names_by_state
    with _lock:
        if _by_state is not None:
            return
        by_state, names = {}, {}
        try:
            with open(_CSV, newline="") as f:
                for row in csv.DictReader(f):
                    st = (row.get("state") or "").strip().upper()
                    key = _norm(row.get("county"))
                    if not st or not key:
                        continue
                    try:
                        pt = (float(row["lat"]), float(row["lon"]),
                              int(row.get("population") or 0))
                    except (TypeError, ValueError, KeyError):
                        continue
                    by_state.setdefault(st, {})[key] = pt
        except OSError:
            by_state = {}
        for st, d in by_state.items():
            # Longest first so "st louis city" is tried before "st louis".
            names[st] = sorted(d, key=len, reverse=True)
        _by_state, _names_by_state = by_state, names


def lookup(state, county):
    """(lat, lon) for one county, or None."""
    _load()
    pt = (_by_state.get(str(state or "").upper()) or {}).get(_norm(county))
    return (pt[0], pt[1]) if pt else None


def counties_in(text, state):
    """Every county of `state` named in `text`, largest-population first.

    Word-bounded on purpose. A substring match puts "Cass" inside "Cassville"
    and "Clay" inside "Clayton", which would scatter bids into counties the
    posting never mentioned. Matching longest name first also stops "St Louis"
    swallowing a row that actually said "St Louis City".
    """
    _load()
    st = str(state or "").upper()
    table = _by_state.get(st) or {}
    blob = " " + _norm(text) + " "
    found, consumed = [], []
    for key in _names_by_state.get(st, ()):
        m = re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", blob)
        if not m:
            continue
        # Skip a shorter name that sits inside a longer one already taken.
        if any(m.start() >= s and m.end() <= e for s, e in consumed):
            continue
        consumed.append((m.start(), m.end()))
        lat, lon, pop = table[key]
        found.append((key, lat, lon, pop))
    found.sort(key=lambda t: -t[3])
    return [(k, lat, lon) for k, lat, lon, _ in found]


# "... VERNON County", "CALLAWAY, CAMDEN, MARIES ... County", "St. Mary Parish"
_COUNTY_LABEL_RE = re.compile(r"(?:count(?:y|ies)|parish(?:es)?|borough)(?![a-z])",
                              re.I)


def _names_before_label(text, at, lookback=90):
    """County names stated just before a County/Parish label.

    Only the words immediately preceding the label are the name. An earlier
    version captured everything from the start of the cell up to the label,
    so Missouri's "(1): Job JSR0028 Route 18 HENRY County" produced the
    nonsense name "job jsr0028 route 18 henry" -- which matched nothing, and
    then a stray short cell elsewhere in the row supplied a wrong county
    instead. Take the trailing words, and allow a comma list, which is how a
    multi-county job is written.
    """
    window = text[max(0, at - lookback):at]
    out = []
    for frag in re.split(r"[,/&;]|\band\b", window):
        words = re.findall(r"[A-Za-z][\w.'\-]*", frag)
        if not words:
            continue
        # Try the longest trailing phrase first: "St Louis" before "Louis".
        for n in range(min(3, len(words)), 0, -1):
            out.append(" ".join(words[-n:]))
    return out


def counties_named(cells, state, county_column=None):
    """Counties a row states EXPLICITLY. Strict on purpose.

    counties_in() finds any county name anywhere in a blob, which is right for
    a prose description ("Route K VERNON County") and badly wrong for a
    multi-column row. TxDOT's facilities table has a DISTRICT column whose
    values -- Houston, Dallas, Yoakum -- are also county names, so a loose
    match tagged a building at "6601 Boucher Drive Edmond, OK" as Houston
    County, Texas. A bid pinned to the wrong place is worse than one never
    found: the customer drives to it.

    So a bare county name in a cell counts only when the table's own header
    says that column holds counties (`county_column`, an index). Otherwise the
    row has to spell it out with the word County/Parish/Borough.
    """
    _load()
    st = str(state or "").upper()
    table = _by_state.get(st) or {}
    found, seen = [], set()

    def _take(raw):
        key = _norm(raw)
        pt = table.get(key)
        if pt and key not in seen:
            seen.add(key)
            found.append((key, pt[0], pt[1], pt[2]))

    cells = list(cells or ())
    if county_column is not None and 0 <= county_column < len(cells):
        for part in re.split(r"[,/&]|\band\b", str(cells[county_column])):
            _take(part)
    for cell in cells:
        text = str(cell or "")
        for m in _COUNTY_LABEL_RE.finditer(text):
            for cand in _names_before_label(text, m.start()):
                _take(cand)
    found.sort(key=lambda t: -t[3])
    return [(k, lat, lon) for k, lat, lon, _ in found]


def loaded_states():
    _load()
    return sorted(_by_state)


def county_count():
    _load()
    return sum(len(v) for v in _by_state.values())
