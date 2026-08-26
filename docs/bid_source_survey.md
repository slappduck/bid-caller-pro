# Where else can bids come from? — a survey, 2026-08-26

Everything below was measured against the live services on 26 August 2026,
not reasoned about. Counts are what the endpoints actually returned.

The short version: **federal work was the big miss and it is now wired up.**
Most of the rest of the landscape is closed, and it is worth writing down
*which* doors are shut so nobody spends another night pushing on them.

---

## 1. Federal — SAM.gov ✅ BUILT

The largest buyer of concrete in the country, and the app could not see one
job of it.

| measure | value |
|---|---|
| active notices in the concrete NAICS codes, nationwide | ~1,230 |
| 237310 Highway/Street/Bridge Construction | 342 |
| 238910 Site Preparation | 83 |
| 238190 Other Foundation/Structure/Exterior | 32 |
| 238110 Poured Concrete Foundation & Structure | 24 |
| 238140 Masonry | 21 |
| per 125-mile scan, measured over 8 metros | **1.0 bids** |

One bid a scan sounds small. Two things make it worth having anyway:

* **It fills the holes.** Six of those eight metros returned *zero* bids from
  municipal portals. Kansas City read 41 towns and found nothing; it now
  finds Whiteman AFB. Des Moines went from nothing to the Ames concrete job.
* **The data is better than anything else we get.** Of nine placeable jobs in
  a MO/KS/AR probe, **nine had a named contracting officer with a phone or an
  email**, and all nine were small-business set-asides. No extraction call is
  spent: the trade arrives as a NAICS code, the location as city/state/zip/
  street, the contact as a record. The contact gap does not exist here.

### Why it never worked before

It was already wired up, and could not have produced a bid:

1. The default endpoint, `api.sam.gov/prod/opportunities/v2/search`, **404s**
   — every path on that host does. Fixed to `api.data.gov/sam/...`, which
   answers a rate-limit error to the shared demo key, i.e. it is real.
2. Relevance was title keywords, and the list had no "pavement", "paving" or
   "resurfacing". Three real jobs found in one probe — *Whiteman AFB FY27
   Airfield Pavement*, *Ft Leavenworth Asphalt Pavement Rehabilitation*,
   *NICO Waysides and Walk Improvements* — matched none of them. Now NAICS
   decides, with keywords only for notices posted without a code.
3. Only the centre state was asked. A 125-mile circle is usually three or
   four states wide — the same bug `_place_bid` documents for cities.

### What Josh should do

Nothing, to make it work — it runs today against sam.gov's public search.

**Optional, ~2 minutes:** get a free key at `api.data.gov/signup` and set
`SAM_API_KEY` in Render. That switches it to the documented API, which is a
published contract and so will not change shape without notice. `/diag` says
which transport is in use.

---

## 2. Counties, schools, universities — the directory is 95% cities

| entity type | in directory | exists in US |
|---|---|---|
| plain cities/towns | 4,223 | — |
| counties | **52** | 3,143 |
| school districts | **2** | ~13,000 |
| universities | 11 | ~4,000 |
| housing authorities | 8 | ~3,300 |

County road departments and school districts let a great deal of concrete,
and **no new parsing is needed** — they run the same CivicPlus and agency
platforms we already read. `data/gov_domains.csv` already holds 2,955 county
domains; discovery was simply never run over them.

Tested on 80 of them:

* with the 24 candidate paths alone: **5%** found a bid page
* adding the homepage-link fallback: **11%**

Extrapolated, ~315 counties. Worth doing, not transformative. The diagnosis
of the other 89% is the interesting part:

* **30 have no bid link on the homepage** — but many are not procurement
  entities at all. The registry's "county" match catches sheriff's offices,
  county clerks and courts (`pontotocsheriff-ok.gov`, `marshallcoclerkky.gov`,
  `collierclerk.gov`). Filtering those out first would raise the real rate.
* **22 domains are simply dead.**
* **20 found the right page and could not read it** — and a quarter of those
  landed on hosted platforms. `tooeleco.gov` resolves to
  `utah.bonfirehub.com/portal/?tab=openOpportunities`.

That last line is the important one, and it leads to §3.

**Recommended next build:** filter the county list to actual procurement
entities, then run `tools/discover_bid_portals.py` over it. Same for school
districts, where coverage is 2 out of ~13,000.

---

## 3. Hosted platforms — agencies are consolidating onto them

This is the structural threat. Cities and counties keep moving their bids
off their own sites onto a handful of hosted platforms, and if we cannot
read those, coverage decays no matter how many URLs the directory holds.

Checked every one's `robots.txt`:

| platform | verdict |
|---|---|
| **Bonfire** (bonfirehub.com) | `Disallow: /` — **off limits** |
| **BidSync** / Periscope | `Disallow: /` — **off limits** |
| **SciQuest / Jaggaer** | `Disallow: /` — **off limits** |
| **BidNet Direct** (Sovra) | public solicitation paths **allowed** |
| **DemandStar** | `User-agent: *`, no Disallow — **allowed** |
| **Vendor Registry** | only Zoominfobot blocked — **allowed** |
| **Bid Express** | listings allowed (see `bid_express_access.md`) |
| OpenGov Procurement | no robots.txt; public browse path is a JS 404 |

Of the three permitted, only one is actually readable:

* **DemandStar** — JS shell, 66 characters of text. Nothing to read.
* **Vendor Registry** — JS shell, 764 characters. Nothing to read.
* **BidNet Direct** — **server-rendered and searchable.**
  `/public/solicitations/open?keywords=concrete` returns **1,612 open
  results** with title, state, published date and closing date.

### BidNet: ask first, do not read

1,612 concrete solicitations is the largest single number in this document.
Two things say stop and ask rather than start reading:

* BidNet/Sovra is a **commercial aggregator whose business is selling access
  to exactly this data.** That is different in kind from an agency's own
  public posting.
* **Their terms are unreadable to me.** The pages are JS-rendered, and
  `sovra.com` resets the connection to a real browser — bot protection.
  Working around it is not something to do, so nobody here has read the terms
  that would govern this.

robots.txt permitting a path is not the same as a licence. Same posture as
Bid Express: **send an email and ask.** A draft is in
`docs/bid_express_access.md`; the BidNet one should say the same things.

---

## 4. Open data portals — small but clean

**Socrata** has a cross-domain discovery API that searches every city's
catalogue at once. Live bid datasets exist and are genuinely good:
`data.delaware.gov` "Open Bids" and `data.montgomerycountymd.gov`
"Solicitations" were both updated the day of this survey, with structured
deadlines, status and — in Montgomery's case — a `construction: Y/N` flag.

But the coverage is thin: **10 domains, 24 datasets.** Delaware, NY State,
NYC, Montgomery County MD, Texas, Cook County IL, Mesa AZ, Hawaii DOT.

Worth a config-driven adapter eventually — each source is cheap to add once
the shape is known, and the data needs no AI. Not worth building before the
county expansion in §2.

**data.gov's CKAN API is dead** (`/api/3/action/package_search` → 404). They
migrated off it.

---

## 5. Dead ends — proven, so nobody repeats them

* **CivicPlus RSS.** Enumerated every RSS module ID from 1 to 130 across
  several CivicPlus hosts. There are feeds for Photo Gallery, Calendar, Alert
  Center, Jobs, Pages, Facilities and Agenda Center — and **no Bids feed.**
  (An early probe suggesting "22 of 30 hosts have a bid feed" was wrong:
  ModID 65 is the Agenda Center, and those were meeting agendas.)

* **State central procurement.** 26 of our 50 state sources are DOT URLs, and
  only 3 states are usable. Central procurement boards are a separate system,
  so they were checked as a fresh channel — and they hit the same wall:
  Cal eProcure 403, Illinois 404, Michigan 403, Colorado unreachable,
  MissouriBUYS a JS shell. **Texas ESBD and Ohio Buys both `robots.txt`-
  disallow us, and we respect that.** The one clean win is
  **PA eMarketplace** — 18KB of server-rendered text with 40 dated rows.

* **NAICS 237990** ("Other Heavy and Civil Engineering"). Tempting: 727
  active notices, the biggest single code. Checked across six states — 17
  active, 4 passed a title filter, and all 4 were wrong (a boat ramp, two
  stormwater spill gates, a drainage district). The rest were dams, levees,
  powerhouses, cemetery expansions. **Correctly excluded.**

* **PSC codes Y1PZ and Z2AZ.** 8 and 7 notices respectively, none of them
  concrete. Gravesite expansions, switchgear, restroom renovations, 120v
  outlets. Only **Z2PZ** earned inclusion, and it is title-filtered.

---

## Recommended order from here

1. **Get the free SAM key** (2 min) — makes the federal source contractually
   stable rather than dependent on an undocumented endpoint.
2. **Filter the county registry to real procurement entities, then run
   discovery over it.** ~2,900 domains already in hand; the sheriff/clerk
   noise is what is holding the hit rate down.
3. **School districts** — 2 of ~13,000 is the largest untouched pool, and
   they let a lot of sidewalk, parking and ADA work.
4. **Write to BidNet/Sovra** asking about data access, the way the Bid
   Express letter does. 1,612 concrete solicitations is worth one email.
5. **Socrata adapter** for the 10 domains that publish real bid data.
