"""Public "what concrete work costs" pages, one per state, from the app's own data.

    python3 tools/build_cost_pages.py

Reads curbcall_netlify_v4/rates/*.json (the same going rates the app shows)
and writes curbcall_netlify_v4/concrete-cost/<state>.html plus an index,
and lists them in sitemap.xml. No login, no server call: plain pages a search
engine can read, for anyone pricing concrete work -- a contractor checking a
number, or someone starting out who has no numbers yet.

One page per state, not per town, on purpose: each state's page carries data
no other page has (that DOT's prices, by district), where a page per town
would repeat its state's numbers under a new name, which search engines treat
as spam.

Output depends only on the data, never on the date it was built, so a
rebuild with nothing new changes nothing. tools/refresh_state_data.py runs
this after each monthly data refresh.
"""
import html
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "curbcall_netlify_v4")
RATES = os.path.join(SITE, "rates")
OUT = os.path.join(SITE, "concrete-cost")
SITEMAP = os.path.join(SITE, "sitemap.xml")
BASE_URL = "https://curbcallpro.com"

AGENCY = {"MO": "MoDOT", "FL": "FDOT", "OR": "ODOT", "MN": "MnDOT", "OK": "ODOT", "NC": "NCDOT",
          "TX": "TxDOT", "TN": "TDOT", "IN": "INDOT", "MT": "MDT", "SD": "SDDOT", "KY": "KYTC",
          "KS": "KDOT", "AR": "ARDOT", "IA": "Iowa DOT", "IL": "IDOT", "NE": "NDOT"}
# The same typical ready-mix figures the app's concrete panel uses (READY_MIX
# in app.js; a test keeps the two equal). National 2026 cost-guide ranges.
READY_MIX = {"low": 110, "high": 175, "truckCY": 10, "deliveryHigh": 180, "shortLow": 40, "shortHigh": 100}
# The order work is listed in, and what each category is called in plain words.
CATS = [("sidewalk", "Concrete sidewalk"), ("sidewalk6", "Sidewalk, 6 in."), ("ramp", "ADA curb ramp"),
        ("domes", "Detectable warnings (truncated domes)"), ("curb_gutter", "Curb and gutter"),
        ("curb", "Concrete curb"), ("driveway", "Concrete driveway"), ("gutter", "Concrete gutter"),
        ("median", "Concrete median"), ("removal", "Removing old concrete")]
# Non-breaking spaces: "a sq yd" never splits across lines on a phone.
UNIT_WORD = {"sq yd": "a\u00a0sq\u00a0yd", "sq ft": "a\u00a0sq\u00a0ft", "ft": "a\u00a0foot", "each": "each",
             "cu yd": "a\u00a0cubic\u00a0yard"}

e = lambda s: html.escape(str(s), quote=True)


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def money(n):
    n = float(n)
    if n >= 1000:
        return f"${n:,.0f}"
    return f"${n:,.0f}" if abs(n - round(n)) < 0.005 else f"${n:,.2f}"


def latest(d, code, district="STATEWIDE"):
    by_y = ((d.get("prices") or {}).get(code) or {}).get(district) or {}
    if not by_y:
        return None
    y = max(by_y, key=int)
    avg, low, high, n = (by_y[y] + [None] * 5)[:4]
    return {"year": y, "avg": avg, "low": low, "high": high, "n": n,
            "period": (d.get("periods") or {}).get(y) or y}


def latest_win(d, code, district="STATEWIDE"):
    by_y = ((d.get("wins") or {}).get(code) or {}).get(district) or {}
    if not by_y:
        return None
    y = max(by_y, key=int)
    avg, low, high, n = by_y[y][:4]
    return {"year": y, "avg": avg, "n": n}


def plural(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def rows_for(d):
    """One row per category the state prices: (label, item name, unit, latest, win)."""
    out = []
    for cat, label in CATS:
        code = (d.get("cats") or {}).get(cat)
        r = latest(d, code) if code else None
        if r:
            item = d["items"][code]
            out.append({"cat": cat, "label": label, "name": item["name"], "unit": item["unit"],
                        "code": code, "r": r, "win": latest_win(d, code)})
    return out


def price_cell(d, row):
    r, unit = row["r"], UNIT_WORD.get(row["unit"], row["unit"])
    main = f"<b>{money(r['avg'])}</b> {e(unit)}"
    # No low-high here: one stray bid ($7, $1,269) makes the range say nothing
    # to someone pricing a job. The count and the winning average do.
    bits = []
    if r["n"]:
        bits.append(plural(r["n"], "bid" if d["basis"] == "all_bids" else "contract"))
    if row["win"] and d["basis"] == "all_bids":
        bits.append(f"winning bids averaged {money(row['win']['avg'])}")
    return main + (f"<div class=\"sub\">{e(', '.join(bits))}</div>" if bits else "")


def faq(d, rows, agency):
    """Questions people actually type, answered with this state's numbers."""
    word = "average bid" if d["basis"] == "all_bids" else "average winning price"
    qa = []
    for row in rows:
        if row["cat"] in ("sidewalk", "curb_gutter", "ramp", "driveway"):
            r = row["r"]
            unit = UNIT_WORD.get(row["unit"], row["unit"])
            qa.append((f"How much does {row['label'].lower()} cost in {d['state_name']}?",
                       f"On {agency} jobs ({r['period']}), the {word} for {row['name'].lower()} was "
                       f"{money(r['avg'])} {unit}"
                       + (f", from {plural(r['n'], 'bid' if d['basis'] == 'all_bids' else 'contract')}" if r["n"] else "")
                       + ". That's state highway work; city jobs can run different, and the price includes "
                         "labor, materials and the contractor's markup."))
    qa.append(("How much is ready-mix concrete per yard?",
               f"Typically ${READY_MIX['low']}–${READY_MIX['high']} a cubic yard in 2026, before any delivery "
               f"charge, and often less in the Midwest and Southeast. Orders under a full truck "
               f"(about {READY_MIX['truckCY']} yards) usually carry a short-load charge. A local plant's quote "
               "is the real number."))
    return qa


STYLE = """*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
:root{--bg:#0b0d14;--card:#1c2035;--line:#2a2f4a;--amber:#f59e0b;--text:#f1f5f9;--text2:#9aa4ba;--text3:#8b93a7;
  --ui:'Inter',sans-serif;--display:'Bebas Neue',sans-serif;--mono:'DM Mono',monospace;}
body{font-family:var(--ui);background:var(--bg);color:var(--text);line-height:1.6;-webkit-font-smoothing:antialiased;}
a{color:var(--amber);}
.wrap{max-width:46rem;margin:0 auto;padding:0 1rem;}
.hazard{height:6px;background:repeating-linear-gradient(135deg,var(--amber) 0 14px,#0b0d14 14px 28px);opacity:0.85;}
nav{border-bottom:1px solid var(--line);}
.nav-in{display:flex;align-items:center;justify-content:space-between;height:62px;max-width:46rem;margin:0 auto;padding:0 1rem;}
.logo{font-family:var(--display);font-size:1.4rem;letter-spacing:0.05em;color:var(--text);text-decoration:none;}
.logo b{color:var(--amber);font-weight:400;}
.nav-in a.cta-s{font-size:0.85rem;font-weight:700;text-decoration:none;}
header{padding:2.5rem 0 1rem;}
.eyebrow{font-family:var(--mono);font-size:0.72rem;letter-spacing:0.22em;text-transform:uppercase;color:var(--amber);}
h1{font-family:var(--display);font-size:clamp(2.2rem,7vw,3.4rem);line-height:1.02;margin:0.5rem 0 0.6rem;letter-spacing:0.01em;}
h2{font-size:1.15rem;margin:2rem 0 0.6rem;}
.lead{color:var(--text2);font-size:1rem;}
table{width:100%;border-collapse:collapse;margin:0.5rem 0;font-size:0.95rem;}
th{text-align:left;color:var(--text3);font-weight:600;font-size:0.78rem;text-transform:uppercase;letter-spacing:0.05em;padding:0.4rem 0.5rem;border-bottom:1px solid var(--line);}
td{padding:0.6rem 0.5rem;border-bottom:1px solid var(--line);vertical-align:top;}
td .sub{color:var(--text3);font-size:0.8rem;}
.note{color:var(--text3);font-size:0.85rem;margin:0.5rem 0;}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:1.1rem 1.2rem;margin:1rem 0;}
.calc{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:0.6rem 0.9rem;}
.calc label{display:flex;flex-direction:column;font-size:0.8rem;color:var(--text2);gap:0.25rem;}
.calc input,.calc select{width:100%;min-width:0;min-height:44px;background:var(--bg);border:1px solid var(--line);border-radius:10px;color:var(--text);padding:0.4rem 0.6rem;font-family:var(--ui);font-size:16px;}
.big{font-size:1.7rem;font-weight:800;margin-top:0.8rem;}
.why{color:var(--text2);font-size:0.85rem;}
.cta{display:block;text-align:center;background:var(--amber);color:#10131c;font-weight:800;text-decoration:none;border-radius:12px;padding:0.95rem;margin:1.2rem 0 0.4rem;}
.states{display:grid;grid-template-columns:repeat(auto-fill,minmax(10rem,1fr));gap:0.5rem;margin:0.8rem 0;}
.states a{display:block;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:0.7rem 0.8rem;text-decoration:none;color:var(--text);font-weight:600;}
.states a span{display:block;color:var(--text3);font-weight:400;font-size:0.8rem;}
details{border-bottom:1px solid var(--line);padding:0.7rem 0;}
summary{cursor:pointer;font-weight:600;}
details p{color:var(--text2);margin-top:0.4rem;font-size:0.93rem;}
footer{border-top:1px solid var(--line);padding:2rem 0;color:var(--text3);font-size:0.82rem;margin-top:2.5rem;}
"""

CALC_JS = """(function(){
var R=%s;
var y=document.getElementById("yd"),t=document.getElementById("tk"),out=document.getElementById("cost"),why=document.getElementById("why");
function m(n){return "$"+Math.round(n).toLocaleString();}
function go(){
  var yards=Math.max(0,Number(y.value)||0),truck=Number(t.value)||R.truckCY;
  if(!yards){out.textContent="Enter the yards";why.textContent="";return;}
  var trucks=Math.ceil(yards/truck),short=yards<truck?truck-yards:0;
  var lo=yards*R.low+short*R.shortLow,hi=yards*R.high+trucks*R.deliveryHigh+short*R.shortHigh;
  out.textContent="About "+m(Math.round((lo+hi)/2/100)*100);
  why.textContent=trucks+(trucks===1?" truck":" trucks")+". Could run "+m(lo)+"\\u2013"+m(hi)+"."+(short?" Includes a short-load charge: "+short+" yards under a full truck.":"");
}
y.oninput=go;t.onchange=go;go();
})();"""


def calculator():
    opts = "".join(f'<option value="{v}"{" selected" if v == READY_MIX["truckCY"] else ""}>{v} yards</option>'
                   for v in (8, 9, 10, 11, 12))
    return f"""<h2>What the concrete itself will cost</h2>
<div class="card">
  <div class="calc">
    <label>Yards of concrete <input id="yd" type="number" min="0" step="0.5" value="10" inputmode="decimal"></label>
    <label>Truck size <select id="tk">{opts}</select></label>
  </div>
  <div class="big" id="cost">About $1,500</div>
  <div class="why" id="why"></div>
  <p class="note">Typical U.S. ready-mix price, ${READY_MIX['low']}–${READY_MIX['high']} a yard, plus delivery where it isn't included
  (up to ${READY_MIX['deliveryHigh']} a truck) and ${READY_MIX['shortLow']}–${READY_MIX['shortHigh']} for each yard a small order falls short of a full truck.
  A plant's quote is the real number.</p>
</div>
<script>{CALC_JS % json.dumps(READY_MIX)}</script>"""


def page(title, desc, path, body, ld=None):
    canon = f"{BASE_URL}/{path}"
    # "<" escaped so text from the data can never close the script tag early.
    ld_json = json.dumps(ld, separators=(",", ":")).replace("<", "\\u003c") if ld else ""
    ld_tag = f'<script type="application/ld+json">{ld_json}</script>' if ld else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>{e(title)}</title>
<meta name="description" content="{e(desc)}" />
<link rel="canonical" href="{e(canon)}" />
<meta property="og:type" content="article" />
<meta property="og:site_name" content="CurbCall Pro" />
<meta property="og:url" content="{e(canon)}" />
<meta property="og:title" content="{e(title)}" />
<meta property="og:description" content="{e(desc)}" />
<meta property="og:image" content="{BASE_URL}/og-card.png" />
<meta name="theme-color" content="#0b0d14" />
<link rel="icon" href="/favicon.ico" />
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Inter:wght@400;600;700;800&family=DM+Mono&display=swap" rel="stylesheet" />
<style>{STYLE}</style>
{ld_tag}
</head>
<body>
<div class="hazard"></div>
<nav><div class="nav-in"><a class="logo" href="/index.html">Curb<b>Call</b> Pro</a><a class="cta-s" href="/app.html">Find bids near you &rarr;</a></div></nav>
<main class="wrap">
{body}
</main>
<footer><div class="wrap">Prices are from each state DOT's own published bid data, as linked on each page. They're a guide, not a quote.<br />
<a href="/concrete-cost/index.html">Concrete prices by state</a> &middot; <a href="/index.html">CurbCall Pro</a> &middot; <a href="mailto:support@curbcallpro.com">support@curbcallpro.com</a><br />
&copy; CurbCall Pro by Oblique Systems, LLC.</div></footer>
</body>
</html>
"""


def state_page(d, others):
    st, name = d["state"], d["state_name"]
    agency = AGENCY.get(st, f"{name} DOT")
    rows = rows_for(d)
    if not rows:
        return None
    side = next((r for r in rows if r["cat"] == "sidewalk"), rows[0])
    period = side["r"]["period"]
    word = "average bid" if d["basis"] == "all_bids" else "average winning price"
    def when(r):  # only where a row's period differs from the table's
        p = r["r"]["period"]
        return f"<div class=\"sub\">{e(p)}</div>" if p != period else ""
    table = "".join(f"<tr><td>{e(r['label'])}"
                    + (f"<div class=\"sub\">{e(r['name'])}</div>" if r["name"].lower() != r["label"].lower() else "")
                    + f"</td><td>{price_cell(d, r)}{when(r)}</td></tr>" for r in rows)
    # Trend: the sidewalk price, year by year, where there's more than one year.
    by_y = d["prices"][side["code"]]["STATEWIDE"]
    trend = ""
    if len(by_y) > 1:
        trend = ("<p class=\"note\">" + e(side["label"]) + ", year by year: "
                 + " &middot; ".join(f"{e(y)} {money(by_y[y][0])}" for y in sorted(by_y, key=int)) + "</p>")
    # Districts: the sidewalk price in each, where the state reports them.
    dist = ""
    dists = [k for k in d["districts"] if k != "STATEWIDE"]
    if dists:
        drows = []
        for k in dists:
            r = latest(d, side["code"], k)
            if r:
                drows.append(f"<tr><td>{e(d['districts'][k])}</td><td><b>{money(r['avg'])}</b> "
                             f"{e(UNIT_WORD.get(side['unit'], side['unit']))}"
                             + (f"<div class=\"sub\">{plural(r['n'], 'bid' if d['basis'] == 'all_bids' else 'contract')}</div>" if r["n"] else "")
                             + "</td></tr>")
        if len(drows) > 1:
            dist = (f"<h2>{e(side['label'])} by {e(agency)} district</h2><table><thead><tr><th>District</th>"
                    f"<th>{e(word.capitalize())}</th></tr></thead><tbody>{''.join(drows)}</tbody></table>"
                    "<p class=\"note\">Latest year each district reported.</p>")
    qa = faq(d, rows, agency)
    faq_html = "".join(f"<details><summary>{e(q)}</summary><p>{e(a)}</p></details>" for q, a in qa)
    ld = {"@context": "https://schema.org", "@type": "FAQPage",
          "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in qa]}
    more = "".join(f'<a href="/concrete-cost/{slug(o["state_name"])}.html">{e(o["state_name"])}</a>'
                   for o in others if o["state"] != st)
    sr = side["r"]
    title = f"Concrete prices in {name}: sidewalk, curb & ready-mix ({period})"
    desc = (f"What concrete work costs in {name}: {side['label'].lower()} averaged {money(sr['avg'])} "
            f"{UNIT_WORD.get(side['unit'], side['unit'])} on {agency} jobs ({period}). Curb and gutter, ADA ramps, "
            "driveways, and a ready-mix cost calculator.")
    body = f"""<header>
<div class="eyebrow">{e(name)} &middot; {e(agency)} prices</div>
<h1>What concrete work costs in {e(name)}</h1>
<p class="lead">The prices contractors bid on {e(agency)} jobs: sidewalk, curb, ADA ramps and more, from {e(agency)}'s own published bid data.
These are state highway jobs; city and county work can run different.</p>
</header>
<h2>{e(name)} prices, {e(agency)} jobs, {e(period)}</h2>
<table><thead><tr><th>Work</th><th>{e(word.capitalize())}</th></tr></thead><tbody>{table}</tbody></table>
{trend}
<p class="note">Source: <a href="{e(d['source_url'])}" rel="nofollow noopener" target="_blank">{e(d['source'])}</a>.
{"Averages of every bid received." if d["basis"] == "all_bids" else "Averages of winning prices only."}</p>
{dist}
{calculator()}
<a class="cta" href="/app.html">See open sidewalk &amp; curb bids near you</a>
<p class="note" style="text-align:center;">CurbCall Pro finds city, county and state concrete bids near you, shows these prices on each one,
lists the ready-mix plants nearby, and works out what your concrete costs on every line. Free to try.</p>
<h2>Common questions</h2>
{faq_html}
<h2>Other states</h2>
<div class="states">{more}</div>"""
    return title, desc, body, ld


def index_page(states):
    cards = "".join(
        f'<a href="/concrete-cost/{slug(d["state_name"])}.html">{e(d["state_name"])}'
        f'<span>{e(AGENCY.get(d["state"], "DOT"))} &middot; {e(min(d["years"]))}–{e(max(d["years"]))}</span></a>'
        for d in states)
    body = f"""<header>
<div class="eyebrow">Concrete prices by state</div>
<h1>What concrete work costs, state by state</h1>
<p class="lead">Sidewalk, curb and gutter, ADA ramps and driveways: the prices contractors actually bid on state DOT jobs,
from each DOT's own published data, plus what the ready-mix itself costs.</p>
</header>
<div class="states">{cards}</div>
{calculator()}
<a class="cta" href="/app.html">See open sidewalk &amp; curb bids near you</a>"""
    title = "Concrete prices by state: sidewalk, curb, ADA ramps & ready-mix"
    desc = ("What concrete work costs in " + str(len(states)) + " states, from state DOT bid data: sidewalk, curb and "
            "gutter, ADA ramps, driveways, plus a ready-mix cost calculator.")
    return title, desc, body


def update_sitemap(paths):
    with open(SITEMAP, encoding="utf-8") as f:
        xml = f.read()
    block = ("  <!-- concrete-cost pages: written by tools/build_cost_pages.py -->\n"
             + "".join(f"  <url><loc>{BASE_URL}/{p}</loc><changefreq>monthly</changefreq><priority>0.6</priority></url>\n"
                       for p in paths)
             + "  <!-- /concrete-cost pages -->\n")
    xml = re.sub(r"  <!-- concrete-cost pages:.*?<!-- /concrete-cost pages -->\n", "", xml, flags=re.S)
    xml = xml.replace("</urlset>", block + "</urlset>")
    with open(SITEMAP, "w", encoding="utf-8") as f:
        f.write(xml)


def main():
    states = []
    for name in sorted(os.listdir(RATES)):
        if re.fullmatch(r"[a-z]{2}\.json", name):
            with open(os.path.join(RATES, name), encoding="utf-8") as f:
                d = json.load(f)
            if rows_for(d):
                states.append(d)
    states.sort(key=lambda d: d["state_name"])
    os.makedirs(OUT, exist_ok=True)
    keep, paths = {"index.html"}, ["concrete-cost/index.html"]
    for d in states:
        title, desc, body, ld = state_page(d, states)
        fname = f"{slug(d['state_name'])}.html"
        path = f"concrete-cost/{fname}"
        with open(os.path.join(OUT, fname), "w", encoding="utf-8") as f:
            f.write(page(title, desc, path, body, ld))
        keep.add(fname)
        paths.append(path)
    title, desc, body = index_page(states)
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(page(title, desc, "concrete-cost/index.html", body))
    for name in os.listdir(OUT):           # a state that lost its data loses its page
        if name.endswith(".html") and name not in keep:
            os.remove(os.path.join(OUT, name))
    update_sitemap(paths)
    print(f"wrote {len(states)} state pages and an index to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
