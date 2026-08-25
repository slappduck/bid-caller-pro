# Making the search engine reliable

The plan of record for search. Update the checkboxes as things land; the point
is that "is it better?" stops being an opinion.

---

## The problem in one paragraph

`/scan` pays a search API to *guess* which pages might contain bids, then pays
OpenAI to read whatever comes back. That is expensive per query, capped by
whatever credit is left, dependent on that day's search rankings, and it fails
silently — when the search backend is rate-limited the scan still returns 200,
just nearly empty, which is indistinguishable from "this area has no work."

Public bids don't live in arbitrary corners of the web. They live in a handful
of procurement platforms with stable, predictable URLs. Springfield MO is a
CivicPlus site: every open solicitation has been sitting on
`springfieldmo.gov/Bids.aspx` the whole time, free to read.

**The shift:** use the search engine *once* to work out which platform a town
uses. After that, read that platform directly, every scan, forever.

---

## What "working" means

A 50-mile scan of a metro like Springfield should return **10–40 open bids**.
Not hundreds — public bids stay open 2–4 weeks and a city that size lets a
handful of concrete projects a year. Hundreds only becomes real if we widen
scope deliberately (Phase 4).

Current baseline: **1**.

---

## Phase 0 — Stop the bleeding, start measuring

Nothing else can be judged until we can see what's happening.

- [x] Cut Tavily to `basic` depth — `advanced` cost 2 credits per search against
      1, so a 50mi scan burned ~78 of a 1,000/month allowance
- [x] Report Tavily's real state in `/health` (working, not merely configured)
      and email on the first quota rejection
- [x] `force` flag on `/scan` + a Force-a-fresh-scan control, so one bad scan
      no longer owns an area until midnight
- [x] Per-scan funnel in `/scan`'s debug and in the Render log
- [ ] **Confirm the Tavily allowance** — check `/health` and the Tavily
      dashboard. If it's spent, that alone explains the current result
- [x] **Recall benchmark — built, but not as originally planned.** The
      original idea here was a fixed list of real bids known to be open right
      now near Springfield and Aurora. Checked live against
      springfieldmo.gov/Bids.aspx while building this: the two bids this
      file itself had documented as ground truth (ADA Improvement Project,
      Mt. Vernon & Miller Sidewalks) had both already closed, and nothing
      currently posted there is concrete/sidewalk/ADA work at all — a real,
      normal outcome (a city this size lets a handful of these a year), but
      it means a fixed "must find these live bids" list goes stale within
      weeks and starts reporting 0% for reasons that have nothing to do with
      whether the scanner works.
      **What got built instead:** `tools/recall_check.py`, a standalone CLI
      that runs the exact real funnel (`bid_sources.parse_civicplus_html` →
      `looks_relevant()` → optionally the real `_ai_extract` prompt if
      `OPENAI_API_KEY` is set) against either a live URL or a saved fixture,
      and reports what was parsed, what passed the relevance filter, and
      (given `--expect`) whether specific known titles were found — usable
      on demand any time a real bid is noticed missing, to see exactly which
      stage dropped it. `data/recall_fixtures/springfield_civicplus.html` is
      real captured markup from the live page with the two originally
      documented bids reconstructed in that exact template alongside real
      current unrelated postings, so there's a permanent, stable acceptance
      test (`tests/test_recall_fixtures.py`, runs in the normal suite, no
      network) instead of one that silently breaks when bids close. This is
      the first acceptance test from the "Ground truth for Springfield, MO"
      section below, finally actually locked in — just as a fixture instead
      of a live check.
      **Still open:** this only benchmarks the direct-read/CivicPlus path.
      The search-fallback path (Tavily + DDG) has no equivalent check yet,
      and doing one live against Springfield/Aurora right now would hit the
      same "nothing currently open" problem — needs either a fixture built
      from saved real search results, or picking it back up whenever a real
      concrete bid is next known to be open somewhere in the coverage area.

## Phase 1 — Read the platforms directly

The largest single win. Each adapter is pure text-in/rows-out so it can be
tuned against a saved fixture without waiting on a live site.

- [x] `bid_sources.py`: platform recognition + CivicPlus reader (RSS and the
      `Bids.aspx` listing) + a relevance prefilter
- [x] Wire `bid_sources` into `/scan` ahead of the search path
- [x] **National bid-portal directory.** `tools/discover_bid_portals.py`
      crawled all 12,711 domains in `data/gov_domains.csv` — 3,151 verified
      bid pages found (24.8%: 2,251 cities, 740 counties, 155 special
      districts, 5 school districts), across 52 states/territories. Wired
      into `/scan` via `bid_portals.py`'s `_national_seeds()`, behind the
      5 hand-seeded entries
- [x] **Live homepage-link fallback for cities the crawl didn't cover.**
      A city with no known portal and no hit on the guessed common paths
      (`/Bids.aspx`, `/bids`, ...) used to fall straight through to a
      generic web search — which has no way to tell a result ABOUT the
      searched city apart from one that merely mentions it. A live Kansas
      City, MO scan with no known portal came back with every single raw
      result `out_of_radius`: real, geocodable bids, just from the wrong
      place, because the search itself had nothing local to rank. Fixed:
      `_run_known_portals` now also tries an actual bid-shaped link off
      each entity's own homepage (`bid_sources.extract_bid_link_candidates`,
      shared with the offline crawl) before giving up and falling to
      search. Verified live against `kcmo.gov`: correctly finds
      `/i-want-to/view-bid-opportunities`, its real "Bids and Solicitation
      Information" page — which in turn reveals Kansas City's actual
      listings live on **Bonfire**, not on kcmo.gov itself. This fix gets a
      scan from "nothing" to "the right government page"; a Bonfire
      adapter (below) is still needed to reach KC's actual listings from
      there.
- [~] Seed the portal directory: Springfield/Aurora/Joplin corrected to the
      Bids.aspx module and labelled `civicplus` (two were pointing at
      AgendaCenter, the council-meetings module, which never contains bids).
      Still to do: verify each URL against the live site and widen to the
      rest of the 50mi ring
- [x] **DemandStar / Euna OpenBids — investigated, dropped.** Once
      demandstar.com egress was unblocked and actually reachable: the old
      `bid_list.asp` listing URL is dead, redirected into a JS single-page
      app. Pulled the app's JS bundle apart to find the real API underneath
      (`api.demandstar.com`) — `/bids/search`, the actual open-bid search,
      returns 401 with no credentials, and a real account confirmed it sits
      behind a *paid* vendor subscription, not just a free login. The only
      unauthenticated endpoint found (`/agency/browsebids`) is a public
      "Awarded Bids" directory for SEO, not live open solicitations —
      confirmed Missouri/Springfield are in DemandStar's network via that
      endpoint, but that's not actionable data.
      **Decision: not building this adapter.** Scraping a paid subscription's
      private API to redistribute inside Bid Caller Pro is a ToS problem, not
      a parsing problem, and doesn't fit the free/public-source model every
      other adapter here follows. It doesn't cost us the underlying bids
      either — Springfield's own listing explicitly said it *also* takes
      submissions through Euna OpenBids, meaning the same postings already
      show up for free via the direct-agency read (CivicPlus/Bids.aspx).
      Any city that dual-posts to DemandStar is presumably already covered
      the same way. Don't re-attempt this without a real reason to believe a
      free tier exists.
- [ ] **Bonfire adapter — investigated, looks buildable.** Kansas City, MO's
      real portal is `kcmo.bonfirehub.com/portal/?tab=openOpportunities`.
      Multiple independent sources (a Detroit city vendor FAQ, Bonfire's own
      indexed vendor-support docs) consistently state the open-opportunity
      list (title/deadline) is publicly viewable with no login; an account
      is needed only to download documents or submit a bid, and that account
      is free — a real, meaningfully different model from DemandStar's paid
      subscription, despite Bonfire and DemandStar now sharing a parent
      company (GTY Technology / Euna Solutions, confirmed via
      Crunchbase/BusinessWire — Bonfire acquired DemandStar Dec 2022). The
      `?tab=openOpportunities` URL pattern is itself Google-indexed across
      many agencies (Chicago Public Schools, Louisville KY, PennBid, KC),
      another signal the list isn't gated. ~650 agencies pre-merger, 1,900+
      combined with DemandStar post-merger — a single adapter template could
      plausibly cover hundreds of agencies at once. **Before writing any
      code:** actually read Euna/Bonfire's terms of use in full (couldn't be
      fetched live from this sandbox) to confirm automated list-reading
      isn't prohibited — same discipline that caught the DemandStar problem
      in the first place.
- [ ] **OpenGov procurement adapter — investigated, looks buildable.** Each
      agency gets a portal at `procurement.opengov.com/portal/[agency-code]`
      (real examples: Thousand Oaks CA, Gallup NM, Davie FL, Bridgeport CT,
      Cleveland OH, Bloomington IN). Consistent, multi-source evidence: the
      open-solicitation list is free to browse with no account, matching the
      CivicPlus model; registration is only needed to submit questions/bids
      or get alerts. OpenGov also runs a real developer portal
      (`developer.opengov.com`) with a published procurement API, but it
      reads as agency-integration-scoped, not a public cross-agency search
      endpoint — needs its own follow-up before assuming an API path exists.
      ~2,000 government customers platform-wide.
- [ ] **PlanetBids adapter — investigated, looks buildable, most open of the
      three.** Per-agency portals at
      `vendors.planetbids.com/portal/[id]/portal-home` (e.g. City of
      Oxnard). Sources are explicit: the public does not need to register to
      view bid info, prospective-bidder lists, *or* public documents —
      registration is free and only required to actively participate
      (RSVP, ask questions, submit). No RSS/API found, so this would be an
      HTML reader like CivicPlus, not an API integration. 550+ public
      agencies, concentrated in CA and the West. Read the ToS
      (`solutions.planetbids.com/terms-and-conditions/`) before building —
      one snippet referenced restrictions on using "the Services" under
      someone else's account credentials, which doesn't obviously apply to
      reading public pages anonymously, but should be confirmed directly.
- [ ] MissouriBUYS, then the other state portals
- [x] **Contractor-association bid calendars — investigated, not worth
      building general infrastructure for.** Checked 8+ states (KS, AR, TN,
      ID, CA, AK, NY, plus IA/OK/NE/OH which turned up nothing at all). The
      dominant pattern nationally is a commercial, login-gated "Electronic/
      Online Plan Room" — several literally named "Member-Only Plan Room" —
      the same paid-access shape already ruled out for DemandStar. Found
      exactly one other genuinely public example, Long Island Contractors'
      Association (`licanys.org/bid-list/`), but it's heavy-civil/infra
      focused, not sidewalk/ADA, and not a rural market. **Verdict:**
      Springfield's public calendar is a found asset, not a repeatable
      pattern — don't invest in a crawler category for this; add others ad
      hoc if one is ever noticed.
- [x] **State statutory public-notice sites — investigated, recommend NOT
      building.** Same conclusion as DemandStar, for a related reason.
      Widened the state list well past the original 9: confirmed Alabama,
      Alaska, Arkansas, California, Colorado, Idaho (mid-migration to
      Column), Kansas, Louisiana, Maine, Massachusetts, Minnesota,
      Mississippi, Nevada, New Mexico, New York (now on `newyork.column.us`
      via a 2025 NYPA/Column partnership), North Dakota (likely), Ohio,
      Oklahoma, Oregon, Pennsylvania, Rhode Island (low confidence),
      Tennessee, Utah, Vermont (low confidence), West Virginia (no separate
      domain, hosted on the association's own site), Wisconsin, Wyoming —
      roughly 35-38 states total, plus a combined MDDC site for
      Maryland/Delaware/DC. No dedicated site found for Hawaii or New
      Hampshire. `connecticutpublicnotices.com` is real (a smaller,
      separate vendor called Themis Technology/iCreateAds), not a 50-state
      hub — the state-abbreviation list in its page title turned out to be
      unused shared-template boilerplate, not evidence of a bigger network.
      **Why not building it anyway:** `robots.txt` on the shared ~20-state
      Illinois-Press-Association-licensed platform (checked live on
      Georgia's site, which redirects to `www.georgiapublicnotice.com`)
      explicitly disallows automated crawlers by name — `Scrapy`,
      `Diffbot`, `magpie-crawler`, and a full slate of AI crawlers
      (`GPTBot`, `anthropic-ai`, `ClaudeBot`, `Claude-Web`,
      `PerplexityBot`, `CCBot`, ...) all get `Disallow: /`, while
      `Googlebot` and other search engines are explicitly `Allow: /`. That's
      a deliberate "index us for search, don't scrape us for reuse" policy,
      not an oversight — the same shape of problem as DemandStar's ToS, just
      surfaced through robots.txt instead of a subscription wall. Given that
      signal, the earlier technical blocker (ASP.NET `__doPostBack`/
      `__VIEWSTATE` stateful search, two `ConnectionResetError`s hit while
      live-probing Missouri's site) is moot — this isn't a "come back with a
      more careful implementation" problem, it's a "the site owner asked
      automated tools not to" problem. Don't build a reader for this
      network. `usalegalnotice.com` (linked from Missouri's site) is NOT a
      national aggregator — an unrelated small print-media company's
      marketing site. Don't re-check it.
- [ ] **Non-.gov municipal domains via Wikidata — investigated, recommend
      building.** The .gov registry only covers `.gov` domains, so towns on
      `.org`/`.com`/`.us` (e.g. Aurora, MO's real site,
      `aurora-cityhall.org`) are invisible to it. Wikidata's "official
      website" property (P856) fills this gap for real: a live SPARQL test
      against Missouri found 234 of 716 municipality entities (33%) have
      P856 populated, including tiny places (Rocheport pop. 201, Arbyrd pop.
      404) — not just big cities. Of 211 non-.gov domains found for MO, 112
      (53%) are for towns with no `.gov` entry at all — genuine new
      coverage; the other 99 duplicate an existing `.gov` entry and are
      useful as a liveness cross-check instead (Aurora's own `.gov`,
      `auroramo.gov`, turned out to be dead — TLS handshake failure — while
      its Wikidata-sourced `.org` is live). Spot-checked domains mostly
      resolved fine (4/5 live). Data is CC0, no licensing concern. Query
      needs to be chunked one per state (nationwide traversal times out) —
      about 50 queries total against `query.wikidata.org/sparql`, each
      ~5-20s. **Next step:** build `tools/discover_wikidata_domains.py`
      following this pattern, output alongside `gov_domains.csv` with a
      `source=wikidata` tag, live-HTTP-check each domain before merging into
      `bid_sources.py`/`gov_directory.py`.

## Phase 2 — Every city in the country

- [x] **National government-domain index.** CISA publishes the authoritative
      registry of every .gov domain and who owns it. `data/gov_domains.csv` is
      that registry filtered to local government — **12,711 domains: 8,928
      cities, 2,623 counties, special districts and school districts, across
      all 53 states and territories.** Refresh with
      `tools/refresh_gov_domains.py`
- [x] Use it in `/scan`: a town with no learned portal gets its real domain
      looked up and probed directly, instead of being searched for
- [x] Counties are reachable at all for the first time — they let a lot of
      curb, road and drainage work and were previously invisible
- [ ] Widen `CANDIDATE_BID_PATHS` once real hit rates are known
- [x] **A wide radius actually searches a wide radius.** Reported by Josh as
      "too few results even on a wide radius" — traced to
      `_nearby_anchor_towns`: it samples at most `MAX_ANCHOR_TOWNS` (6)
      geographically-guessed points regardless of how large the radius
      actually is, and returns nothing at all below 40mi. A 125mi radius
      covers ~49,000 sq mi; 6 sample points is a real recall gap, and below
      40mi the scan only ever searched the exact town typed. The actual fix
      needed no new search credits: `tools/discover_bid_portals.py` already
      found 3,151 real bid pages nationally, but `/scan` never asked "which
      of the towns I already trust fall inside this radius" — it only asked
      about the handful of sampled/typed towns. `tools/geocode_bid_portals.py`
      pre-geocodes every "found" row in `data/bid_portal_directory.csv`
      offline into `data/bid_portal_coords.csv` (kept separate so
      `discover_bid_portals.py` re-runs can't silently wipe it), and
      `bid_portals.towns_within_radius()` turns that into cheap arithmetic
      at scan time — real haversine distance against already-known
      coordinates, no live geocode call per candidate. Wired into
      `_perform_scan` as a third, separate worker pool (capped at
      `MAX_KNOWN_TOWNS`=40, closest-first): each hit is a direct page fetch
      only, no search queries, so it's far cheaper per-town than an anchor.
      Full national geocode run in progress (~2,750 towns, ~15-20min,
      zippopotam-then-Nominatim same as everywhere else this codebase
      geocodes).
- [ ] Cities on `.org`/`.com`/`.us` domains — the registry only covers `.gov`,
      so places like Aurora MO (`aurora-cityhall.org`) still need discovery.
      Wikidata's official-website property is the likely second source
- [ ] Demote generic web search to discovery only — never the way an individual
      bid is found
- [x] Age out sources that stop returning content (`MAX_FAIL` already does this)

## Phase 3 — Extraction quality and cost

- [x] Run `looks_relevant()` before the search-results AI call
      (`_run_local_queries`) — a page a search query surfaced with none of
      the niche terms anywhere in it now never reaches OpenAI. Deliberately
      left `_run_known_portals`' AI fallback ungated: that path reads an
      already-trusted, seeded portal, where a parser gap is our problem, not
      evidence the page is irrelevant (`StructuredReadNeverLosesBidsTests`
      locks that in)
- [ ] Revisit the extraction prompt. It currently says "when in doubt, leave it
      out", which protects precision at recall's expense; a bid whose concrete
      work is one line of a larger scope is exactly what we must not miss
- [ ] Separate listing-level extraction (cheap, structured) from detail-page
      extraction (AI, only for bids that passed the filter)
- [ ] Measure precision as well as recall — junk results cost trust faster than
      missing ones

## Phase 4 — Widen what counts (deliberately)

Only after Phases 1–3, and each is a product decision, not just a code change.

- [ ] Bids where concrete is *part* of a larger scope
- [ ] Planned/upcoming work surfaced alongside open bids
- [ ] Private GC subcontract opportunities and plan rooms
- [ ] Adjacent trades the same crew can bid

## Phase 5 — Freshness at scale

The destination. Today everything happens inside one 150-second request, which
caps how much ground a scan can cover.

- [ ] Background crawler that continuously indexes known sources
- [ ] `/scan` reads the index instead of crawling live — instant, and far more
      complete
- [ ] New-bid alerts become genuinely real-time rather than daily
- [ ] Per-source freshness tracking, and an alert when a source goes quiet

---

## Ground truth for Springfield, MO

Known-good sources, found by hand in about a minute:

| Source | What it is |
|---|---|
| `springfieldmo.gov/Bids.aspx` | CivicPlus listing — every open city solicitation |
| `springfieldmo.gov/5375/Current-Bid-Notices` | Human-readable index |
| DemandStar / Euna OpenBids | Where the city takes submissions |
| `springfieldcontractors.org/category/bid-calendar/` | Association calendar — dense in this exact trade |
| `sgfcitizen.org/public-notices/` | Statutory public notices |

Two bids that were open and that the scanner missed:

- **Springfield ADA Improvement Project** — ~5,000 SY concrete ramps, 3,000 SY
  ADA sidewalk (Sunshine, Battlefield, National)
- **Mt. Vernon & Miller Sidewalks** — ~13,500 SF sidewalk, 1,000 SF ADA ramp,
  ~1,000 LF curb & gutter

These two were the first acceptance test. Both have since closed — confirmed
live on 2026-08-12 while building the recall benchmark (Phase 0) — so
"when a Springfield scan returns both" is no longer something a live scan
can pass. They're preserved as a fixture instead:
`data/recall_fixtures/springfield_civicplus.html` +
`tests/test_recall_fixtures.py` lock in that the reader still recognizes
both, permanently, regardless of what's actually posted today.

---

## Running costs

Worth deciding early — the architecture above exists partly to shrink these.

| Item | Now | After Phase 1–3 |
|---|---|---|
| Search credits per 50mi scan | ~39 searches (~78 credits at advanced) | near zero — direct reads are free |
| OpenAI calls per scan | one per fetched page | only pages that pass the prefilter |
| Wall clock | close to the 150s client timeout | dominated by direct fetches, parallel |

The single biggest cost saving and the single biggest recall gain are the same
change: stop searching for pages we already know the address of.

---

## Phase 6 — State-level sources (researched 2026-08-23, not built)

Everything above works on **city and county** domains. `data/gov_domains.csv`
is 12,711 rows and every one is a City, County, or special district — there
is not a single state agency in the corpus. That is the structural gap, and
it is the one the paid services are selling.

### What the competition actually covers

BidPrime advertises ~120,000 sources gathered by "Opportunity Tracer" across
government websites, **purchasing cooperatives**, **state portals**,
e-procurement hubs and newspaper classifieds. ConstructConnect (~$1,000/mo,
some markets $130–200) and Dodge (from $300/user/mo) are commercial-only and
explicitly do **not** cover public procurement. So the category splits: the
expensive commercial products are not competitors at all, and the public-bid
competitor's edge is entirely breadth of source, not smarter reading.

### Measured yield: state DOT beats everything else per fetch

| Source | Fetches | Concrete-relevant bids |
|---|---|---|
| CivicPlus city portals (random live sample) | 90 | 8 |
| MoDOT current letting, one page | 1 | 7 |

`modotweb.modot.mo.gov/BidLettingPlansRoom/Letting` returns plain HTML, HTTP
200, a `<table>` of 21 projects for the current letting — job number, route,
**county**, and a full description — of which 7 pass `looks_relevant()`
unmodified, including a job literally titled *"ADA improvements, 10
Locations"*. County names geocode straight into the existing radius model.
There is a letting roughly monthly.

That is ~90× the concrete-relevant yield per fetch of a city portal. State
DOTs let exactly this trade — sidewalk, curb, ADA ramp, pavement repair — at
volumes no single town approaches.

### What blocks it, precisely

The existing discovery machinery does **not** reach these. Running
`extract_bid_link_candidates` against 18 state DOT and state-procurement
homepages found landing pages every time and a real listing never: every hit
came back with zero dates on it. `modot.org/bidding` is found; the actual
data lives at `modotweb.modot.mo.gov/...`, a **different subdomain, one hop
further on**. The crawler stops at the first bid-shaped link, which for a
city is the listing and for a state is a menu.

So this is not a new adapter — it is one more hop plus a state-domain corpus.

### Dead ends, so nobody re-checks them

- **Bid Express / bidx.com** — the actual letting platform for ~44 state
  agencies including most DOTs. Every per-state URL (`bidx.com/mo/main`,
  `ui.bidx.com/IADOT/lettings`) returns the same 3,216-byte JS shell, and
  the platform has required an account to browse since October 2023. Free
  registration exists, but automated access under an account is a terms
  question, not an engineering one. **Check the ToS before writing any code
  against it.**
- **Bot blocking is real at state level.** KDOT, ARDOT, Arkansas DFA and the
  Texas ESBD all return 403 to a plain urllib request where cities almost
  never do. State sites will need the polite-fetch treatment (browser UA,
  per-domain delay, backoff) that the city crawl has not needed.
- **Municode is not a procurement platform.** An earlier probe flagged 19 of
  160 sampled pages as Municode; every one was a nav link to
  `library.municode.com`, the ordinance code library. No adapter needed.
- **Revize (17 of 160) is real but not worth an adapter.** Its bid pages are
  freeform content blocks — whatever the clerk typed, plus PDF links. No
  schema to parse; the generic path already handles them as well as anything
  would.
- **Bonfire / OpenGov / PlanetBids / QuestCDN / BidExpress together are 15 of
  160 (9%)** — the smallest group of unhandled platforms and the most
  expensive to build (JS SPAs). They are listed in STATUS.md as the known
  gaps; they should be the last thing built, not the first.

### Also unmeasured, in rough order of promise

- **Purchasing cooperatives** (Sourcewell, BuyBoard, OMNIA, TIPS) — named by
  BidPrime, untouched here, and they aggregate many agencies per source.
- **Councils of government / regional planning commissions** — let road and
  sidewalk work for member towns.
- **School districts and universities** — only 5 school districts are in the
  whole 4,428-entry directory, and they build a lot of sidewalk and parking.
- **Newspaper legal classifieds** — BidPrime names these explicitly. Note the
  statutory public-notice network is already ruled out above on robots.txt
  grounds; individual papers are a separate question.

### Phase 6 result — built, measured, 2 states live (2026-08-23)

Built: `counties.py` (3,215 Census population-weighted county centroids),
`tools/state_fetch.py` (polite fetcher), `tools/discover_state_sources.py`
(two-hop crawler), `tools/verify_state_sources.py` (yield measurement),
`bid_sources.parse_state_letting`, `data/county_coords.csv`,
`data/state_bid_sources.csv`.

**The crawl found "convincing listings" in 22 of 50 states. Running the real
parser over them, 2 states actually yield placeable, concrete-relevant rows:
Florida (75) and Missouri (6).** The gap between 22 and 2 is the whole lesson
and the reason the verifier exists.

| Scan centre | +bids at 50mi | +bids at 125mi |
|---|---|---|
| Tampa, FL | 23 | 51 |
| Orlando, FL | 16 | 51 |
| Jacksonville, FL | 1 | 13 |
| Springfield, MO | 1 | 4 |
| Aurora, MO | 1 | 3 |
| Kansas City, MO | 0 | 3 |

#### What the other 48 states are blocked on, precisely

- **20 states: no listing found at all.** 10 have no page the crawler could
  recognise, 6 refuse our honest agent, 2 disallow via robots.txt, 2 error.
- **8 states: landing page only** (RI DE IA OR PA WY CT HI) — the crawler
  reached the menu, not the table. A third hop or a hand-supplied URL.
- **~18 states: wrong page found, or right page with no location column.**
  This is the real ceiling. Louisiana's advertisement table has columns for
  project number, name, contract manager, dates and scope — and **no parish
  column anywhere**. There is nothing to parse; the location simply is not
  published on that page.

#### Four false positives worth never repeating

Every one of these scored well and was wrong. They are now regression tests
in `tests/test_state_letting.py`.

- **South Dakota, 294 dated rows** — a fuel price index.
- **Washington, 13 "usable" rows** — search-facet chips, e.g. "Public Works
  Awarded Pierce County".
- **Arkansas, 633 rows / 32 passing the trade filter** — the site nav: "ADA",
  "Asphalt Binder Price Index", "Historic Structures Bridge Demolition Movie
  Clips".
- **Texas, 3 placed rows** — a facilities table whose DISTRICT column holds
  county names. A building at "6601 Boucher Drive Edmond, **OK**" was tagged
  Houston County, Texas. This is the dangerous class: a bid pinned to the
  wrong place is worse than one never found, because the contractor drives to
  it. The rule now is explicit evidence — a column the table's own header
  calls "County", or a name followed by the word County/Parish/Borough — and
  nothing else.

#### Legal and access posture — do not quietly change this

- The fetcher identifies itself (`CurbCallBot/1.0` with a contact address),
  respects robots.txt, and rate-limits to one request per host with a 1.5s gap.
- **Five states (KS, MA, ME, NH, NV) reject any agent that does not claim to
  be a browser.** They are recorded as blocked. We do not spoof a browser
  User-Agent to get past them — a site that turned away non-browser agents
  made a choice. Their bids have to reach us another way.
- Arkansas was rejecting *incomplete headers*, not our identity; it answers
  fine once Accept-Language and the Sec-Fetch-* set are present.
- **Bid Express (bidx.com), the letting platform for ~44 state agencies, is a
  JS shell and has required an account since Oct 2023.** Free registration
  exists. Automated access under an account is a terms question, not an
  engineering one — read the ToS before writing any code against it.

#### Plan holder lists — a bigger prize than the bids, with a hard rule

MoDOT publishes, per project, the contractors who pulled plans: company,
named contact, address, phone, direct email (`/BidLettingPlansRoom/PlanHolder/
Call/{letting}?call={call}`, ~3 per project this far out, 8 on one). Two of
eight on one job were concrete companies, so subs already work this list. It
turns a highway job a small crew cannot win as prime into a list of GCs who
need a concrete sub.

**Rule: these are named individuals' business emails on a government page.
Surface them in the context of the job they are bidding. Never export them to
a campaign list.**

**Built (2026-08-23).** `bid_sources.parse_plan_holders` reads the table
header-first rather than by position, because column order is not the same
everywhere and guessing wrong attaches a phone number to the wrong company.
`_attach_plan_holders` fetches one list per job, only for jobs that survived
the radius, capped at `SCAN_PLAN_HOLDER_MAX` (12). The detail sheet renders
them under the state-highway note, with Call and Email per contractor.

The rule is enforced in two places and commented in both: `exportCSV` in
app.html carries an explicit note that `plan_holders` must not be added to
the header, and `_attach_plan_holders`'s docstring says the same. A future
change that "just adds all the fields" to the export would otherwise turn
this into the bulk-harvest tool the rule exists to prevent.

Yield is time-dependent and that is worth knowing before reading too much
into a low number: on 2026-08-23, four weeks out from the 9/18 letting, only
1 of 9 Missouri jobs in range had any holders listed. Lists fill as the
letting date approaches -- a call sampled the same day had 8.

"vendor" is deliberately not treated as a company word. MoDOT's header is
"Name - Vendor #", where Vendor is the state's ID for the *person*, so
matching it returned "Rhea, Don 0010907" as the firm and never read the
Organization column at all.

### Phase 6, second pass — wired into /scan, and a filter gap worth more than the crawl

**Live in `/scan` now.** `_run_state_sources` reads the letting page of every
verified state the radius touches (one fetch per state, at most four in a
125-mile circle) and places rows through `_place_state_bid`, before SAM.gov
and before enrichment so they get the same deadline, dedupe and enrichment
passes as everything else.

Three corrections came out of wiring it up, each caught by running it rather
than by reading it:

1. **A state "call" is not a job.** MoDOT numbers several jobs inside one
   cell: "(1): Job JSR0028 Route 18 HENRY County... (2): Job JSR0033 Route 54
   CEDAR, ST CLAIR County." Read whole, the row names four counties and got
   placed at whichever was nearest — so a Springfield scan showed a card
   headed "Polk County, 28mi" whose description was about Henry County work.
   `split_bundled_jobs` now emits one bid per job.
2. **A multi-county job is placed at the county nearest the contractor**, not
   the most populous. The work really is in all of them, so nearest is both
   true and the only useful answer to "how far is it". From Clinton the Henry
   County job now reads 3mi; from Springfield the Cedar one reads 51mi.
3. **The roadway word list had no state-route designations in it.** It was
   written for municipal postings — street, road, avenue, boulevard — so
   "Coldmill and resurface on Ohio Street" passed and the identical
   "...on Route 32" did not. State lettings are written in route numbers
   almost exclusively. Adding Route/I-nn/US nn/SR nn (with bus, transit,
   shuttle, delivery, snow and bike routes excluded) took **Missouri from 6
   usable rows to 18, and a 125-mile Springfield scan from 4 state bids to
   9.** This is a change to `looks_relevant`, so it applies to every source,
   not just state pages.

Measured after all three:

| Scan centre | state bids at 125mi |
|---|---|
| Tampa, FL | 51 across 20 counties |
| Springfield, MO | 9 across 7 counties |

**Still 2 states.** A third crawl hop across the other 48 added nothing — the
blockers are the ones already listed above, not crawl depth.

`tools/discover_state_sources.py` now MERGES its output instead of replacing
it. A `--state` run used to write only the states it touched, which silently
deleted Florida and Missouri from the checked-in file the first time the
other 48 were re-crawled.

### Phase 6, third pass — Alabama live, and the triage of the other 47

**3 states live: Florida 75, Missouri 18, Alabama 13.**

Alabama needed two new capabilities, both of which should generalise:

- **A prose reader.** Alabama's letting is a Notice to Contractors with no
  table at all — numbered blocks of "1. DEMOF-A210(943), TUSCALOOSA COUNTY
  Contract Time: 620 Working Days for constructing the Bridge Replacement and
  Approaches...". Everything needed is there and a table-only reader saw none
  of it. `parse_notice_to_contractors` runs only when the table and list
  readers both come back empty.
- **Index sources.** Alabama has no stable listing URL: the letting lives at
  `.../NTC/2026/NTC_August_28_2026.html` and the address changes every time.
  The URL I first hand-found 404'd within the hour. Sources now carry a
  `kind` column; an `index` source is a page of dated letting links and the
  current listing is resolved from it on every scan.
  `newest_letting_link` prefers a page over a PDF (Alabama offers both and
  the PDF sorts first by link order) and skips prior/archive/results links.

The county rule needed one more refinement for prose: **the county stated
immediately after the project number is where the work is.** A Chilton County
job whose description mentioned "the Shelby County line" was being placed in
Shelby, which is far larger. Only the first 90 characters after the project
id are searched.

#### Triage of all 21 states that had a page (2026-08-23)

Wrong page — the crawler found something dated and repetitive that is not a
letting: **CA** (bid-opening calendar, dates only), **CO** (a Google Sheets
purchasing log), **DE** (asphalt price index), **HI** (traffic notices),
**IL** (Land Acquisition Bulletin), **MD** (cost-classification key),
**NE** (procurement manual), **NM** (sole-source consultant postings),
**RI** (bid tabs — historical awards), **SD** (fuel price index),
**VT** (RFPs for services), **WI** (news and instructions),
**WV** (CEI consultant selections, not construction).

Right page, nothing placeable on it: **LA** publishes project number, name,
contract manager, dates and scope, and **no parish column anywhere**.
**ID** and **TX** both point at facilities (buildings) tables with no county.
**ND** is an e-plans document list — job numbers, no county.

Index shape, resolvable in principle, not yielding yet: **KY** and **TN**
both publish an index of letting dates. `newest_letting_link` resolves both
correctly (KY to its 12/10/2026 letting, TN to October 2), but the resolved
pages parse to zero rows — they are populated closer to the date, or the
project list sits behind another hop. Worth re-checking rather than
re-engineering.

**AL** was in this group and is now live.

### Phase 6, fourth pass — the state lever has a ceiling (2026-08-25)

Three independent attempts to reach a fourth state all failed, and together
they say something structural rather than "not tried hard enough".

1. **A third crawl hop over all 48 unsolved states** — nothing new.
2. **A URL-pattern probe.** The three states that work share a shape: a
   dedicated letting subdomain (`alletting.dot.state.al.us`,
   `modotweb.modot.mo.gov`) or a `/contracts/lettings/` path. Probed 14
   variants of that shape against all 47 remaining states. **Zero hits.**
3. **Ohio, worked by hand**, because it is a big market returning nothing:
   - `letting-schedule.aspx` — a SharePoint shell requiring scripts.
   - `SaleDates.aspx` — a calendar of sale dates. 46 rows, all bare dates,
     earliest from 2022. No projects at all.
   - `admin-let.pdf` — **bid results**: projects already let, with the
     contractors and amounts. The `?ID=` parameter is ignored, every value
     returns the same file.
   - `PrebidQs.pdf` — does carry upcoming work ("Project No. 260352 Sale Date
     - 8/27/2026 JEF-11") but only for projects that received questions, so
     coverage is partial by construction.
   - ODOT's actual open-project list is published through **Bid Express**
     (`bidx.com/oh/lettings`), which needs an account.

**The conclusion to carry forward:** Bid Express serves ~44 state agencies,
and for most of them it is the *only* place the open-project list appears.
Florida, Missouri and Alabama work because they publish a public page **as
well** — that is the exception, not the rule.

So "47 states of hand work" was too optimistic a framing. The real options:

- **Settle the Bid Express terms question.** ANSWERED, 2026-08-25: there is an
  official API. Infotech documents it for administrators integrating the
  service *and for developers who want to script interactions with it*, so
  scripted access is explicitly contemplated. bidx.com's robots.txt is also
  permissive about lettings — it disallows only `/*/apparentbids` and
  `/*/planholders`. The recommendation is therefore to request API access
  rather than scrape; the web UI is a JavaScript shell that cannot be read
  without a browser anyway. Evidence, the limits of what could be checked
  from here, and a ready-to-send email are in `docs/bid_express_access.md`.
- **Keep hand-checking states individually**, accepting that today's evidence
  puts the hit rate low.
- **Accept three states** and spend the effort on the other levers in
  STATUS.md, which make existing bids better rather than finding more.

PDF reading was proven along the way and is worth remembering even though
Ohio did not need it: `pypdf` is available and extracts ODOT's letting
documents cleanly, counties intact.
