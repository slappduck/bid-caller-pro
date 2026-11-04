"""Public "concrete bids in <state>" pages, one per state.

    python3 tools/build_bid_pages.py

The price pages (tools/build_cost_pages.py) answer "what does sidewalk go
for here?". These answer the question a contractor looking for work types:
"where are the concrete and sidewalk bids in Missouri?". Each page says,
from the app's own data, what's watched in that state -- how many city,
county, school and other agency bid pages, which state lists are read --
and shows the open bids scans have found there.

The open bids change every day, so they aren't in the file: the page holds a
placeholder (<div id="open-bids" data-state="MO">) that the site's Worker
(worker/index.js) fills at the edge from the server's /bids-in-state, cached
for half an hour. A search engine sees the bids in the HTML; if the server is
slow, the page still loads with everything else and a link to the app.

Never on the page: an agency's bid page address. Which pages are watched is
the product; the page says how many and names towns, not where to look.

Output depends only on the data, so a rebuild with nothing new changes
nothing. A test keeps the committed pages equal to what this builds.
"""
import collections
import csv
import json
import os
import re
import sys

TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)
sys.path.insert(0, ROOT)
import build_cost_pages as C  # noqa: E402
import build_nearby_rates as N  # noqa: E402

SITE = C.SITE
OUT = os.path.join(SITE, "concrete-bids")
DIRECTORY = os.path.join(ROOT, "data", "bid_portal_directory.csv")
STATE_SOURCES = os.path.join(ROOT, "data", "state_bid_sources.csv")
TOWNS_SHOWN = 30
e = C.e

# What each state's own lists add (license_server LOCAL_NOTICE_LABELS), read
# from the server's code so the two never disagree.
def _local_labels():
    try:
        import license_server as ls
        return dict(ls.LOCAL_NOTICE_LABELS)
    except Exception:
        return {}


def coverage():
    """{ST: {"pages": n, "types": Counter, "towns": [name, ...]}} from the directory."""
    import bid_portals
    out = collections.defaultdict(lambda: {"pages": 0, "types": collections.Counter(), "towns": collections.Counter()})
    for key, portals in bid_portals.load_directory().items():
        st = key.rsplit("|", 1)[-1].upper()
        out[st]["pages"] += len(portals)
    with open(DIRECTORY, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("status") != "found":
                continue
            st = (r.get("state") or "").upper()
            out[st]["types"][r.get("type") or "Other"] += 1
            if r.get("type") == "City" and r.get("city") and not re.search(r"\b(county|parish|borough)\b", r["city"], re.I):
                out[st]["towns"][r["city"].strip()] += 1
    return out


def dot_lettings():
    """States whose DOT lettings page each scan reads."""
    with open(STATE_SOURCES, encoding="utf-8") as f:
        return {r["state"].upper() for r in csv.DictReader(f) if r.get("status") == "ok"}


def priced_states():
    """{ST: slug of its price page} for states with a concrete-cost page."""
    out = {}
    for name in os.listdir(C.RATES):
        if re.fullmatch(r"[a-z]{2}\.json", name):
            with open(os.path.join(C.RATES, name), encoding="utf-8") as f:
                d = json.load(f)
            if C.rows_for(d):
                out[d["state"]] = C.slug(d["state_name"])
    return out


TYPE_WORDS = [("City", "city and town"), ("County", "county"), ("School district", "school district"),
              ("Special district", "water, park and other district")]


def state_page(st, cov, lettings, labels, priced, states):
    name = N.NAMES[st]
    c = cov.get(st) or {"pages": 0, "types": collections.Counter(), "towns": collections.Counter()}
    kinds = [f"{C.plural(c['types'][t], w + ' bid page')}" for t, w in TYPE_WORDS if c["types"].get(t)]
    lists = []
    if st in lettings:
        lists.append(f"{C.AGENCY[st]}'s lettings" if st in C.AGENCY else f"the {name} DOT's lettings")
    if st in labels:
        lists.append(labels[st])
    lists.append("federal jobs posted on SAM.gov")
    towns = [t for t, _n in sorted(c["towns"].items(), key=lambda x: (-x[1], x[0]))[:TOWNS_SHOWN]]
    towns.sort()
    price_link = (f'<a href="/concrete-cost/{priced[st]}.html">What concrete work costs in {e(name)}</a>'
                  if st in priced else '<a href="/concrete-cost/index.html">Concrete prices by state</a>')
    qa = [
        (f"Where are concrete and sidewalk bids posted in {name}?",
         f"Mostly on each city, county and school district's own website, one page per agency, plus the state's "
         f"own lettings. CurbCall Pro watches {C.plural(c['pages'], 'agency bid page')} in {name} and lists the "
         "concrete work it finds: sidewalk, curb and gutter, ADA ramps, driveways and flatwork."),
        ("How do I bid a city sidewalk job?",
         "Get the bid documents (some agencies have you register as a plan holder first), read every addendum, "
         "price each line item, get a bid bond if the posting asks for one, fill out the agency's own bid form, "
         "and submit it before the deadline the way the posting says: sealed envelope or online."),
        ("How often is this page updated?",
         "The open bids below are refreshed through the day from the scans CurbCall Pro runs. The agencies "
         "watched are re-checked every few months."),
    ]
    faq_html = "".join(f"<details><summary>{e(q)}</summary><p>{e(a)}</p></details>" for q, a in qa)
    ld = {"@context": "https://schema.org", "@type": "FAQPage",
          "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in qa]}
    more = "".join(f'<a href="/concrete-bids/{C.slug(N.NAMES[o])}.html">{e(N.NAMES[o])}</a>' for o in states if o != st)
    body = f"""<header>
<div class="eyebrow">{e(name)} &middot; open bids</div>
<h1>Concrete and sidewalk bids in {e(name)}</h1>
<p class="lead">Sidewalk, curb and gutter, ADA ramp, driveway and flatwork jobs that {e(name)} cities, counties and school
districts have out for bid, found by CurbCall Pro. Public work only, straight from each agency's own posting.</p>
</header>
<h2>Open now</h2>
<div id="open-bids" data-state="{e(st)}"><p class="note">The open {e(name)} bids our scans have found show here.
<a href="/app.html">See them in the app</a>.</p></div>
<h2>What we watch in {e(name)}</h2>
<div class="card">
<p><b>{C.plural(c['pages'], 'agency bid page')}</b>{(': ' + e(', '.join(kinds)) + '.') if kinds else '.'}</p>
<p class="why">Every scan also reads {e(', '.join(lists[:-1]) + (' and ' if len(lists) > 1 else '') + lists[-1])}.</p>
{f'<p class="note">Towns watched include {e(", ".join(towns))}.</p>' if towns else ''}
</div>
{C.near_box()}
{C.signup()}
<a class="cta" href="/app.html">Find bids near you and price them, free to try</a>
<p class="note" style="text-align:center;">{price_link} &middot; <a href="/concrete-bids/index.html">Bids in other states</a></p>
<h2>Common questions</h2>
{faq_html}
<h2>Other states</h2>
<div class="states">{more}</div>"""
    title = f"Concrete & sidewalk bids in {name}: open city and county jobs"
    desc = (f"Open sidewalk, curb, ADA ramp and concrete bids in {name}, from "
            f"{C.plural(c['pages'], 'city, county and school bid page')} CurbCall Pro watches. Updated daily.")
    return title, desc, body, ld


def index_page(states, cov):
    cards = "".join(f'<a href="/concrete-bids/{C.slug(N.NAMES[st])}.html">{e(N.NAMES[st])}'
                    f'<span>{C.plural(cov[st]["pages"] if st in cov else 0, "bid page")} watched</span></a>' for st in states)
    total = sum(cov[st]["pages"] for st in states if st in cov)
    body = f"""<header>
<div class="eyebrow">Concrete bids by state</div>
<h1>Concrete and sidewalk bids, state by state</h1>
<p class="lead">Open sidewalk, curb and gutter, ADA ramp and flatwork jobs from city, county and school district bid pages
across the country. CurbCall Pro watches {total:,} of them.</p>
</header>
<div class="states">{cards}</div>
{C.near_box()}
{C.signup()}
<a class="cta" href="/app.html">Find bids near you and price them, free to try</a>"""
    return ("Concrete & sidewalk bids by state: open city and county jobs",
            "Open sidewalk, curb, ADA ramp and concrete bids from city, county and school bid pages in every state. "
            "Updated daily.", body)


FOOTER = ("Bids are public postings from each agency, found by CurbCall Pro's scans. Always read the agency's own "
          "posting before bidding.")


def update_sitemap(paths):
    with open(C.SITEMAP, encoding="utf-8") as f:
        xml = f.read()
    block = ("  <!-- bid pages: written by tools/build_bid_pages.py -->\n"
             + "".join(f"  <url><loc>{C.BASE_URL}/{p}</loc><changefreq>daily</changefreq><priority>0.7</priority></url>\n"
                       for p in paths)
             + "  <!-- /bid pages -->\n")
    # Replaced where it stands, so the two page builders can run in any order.
    rx = re.compile(r"  <!-- bid pages:.*?<!-- /bid pages -->\n", re.S)
    xml = rx.sub(lambda _m: block, xml) if rx.search(xml) else xml.replace("</urlset>", block + "</urlset>")
    with open(C.SITEMAP, "w", encoding="utf-8") as f:
        f.write(xml)


def main():
    cov, lettings, labels, priced = coverage(), dot_lettings(), _local_labels(), priced_states()
    states = sorted(N.NAMES, key=lambda s: N.NAMES[s])
    os.makedirs(OUT, exist_ok=True)
    keep, paths = {"index.html"}, ["concrete-bids/index.html"]
    for st in states:
        title, desc, body, ld = state_page(st, cov, lettings, labels, priced, states)
        fname = f"{C.slug(N.NAMES[st])}.html"
        with open(os.path.join(OUT, fname), "w", encoding="utf-8") as f:
            f.write(C.page(title, desc, f"concrete-bids/{fname}", body, ld, footer=FOOTER))
        keep.add(fname)
        paths.append(f"concrete-bids/{fname}")
    title, desc, body = index_page(states, cov)
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(C.page(title, desc, "concrete-bids/index.html", body, footer=FOOTER))
    for name in os.listdir(OUT):
        if name.endswith(".html") and name not in keep:
            os.remove(os.path.join(OUT, name))
    update_sitemap(paths)
    print(f"wrote {len(states)} state bid pages and an index to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
