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
- [ ] **Recall benchmark.** A fixed list of real bids known to be open near
      Springfield and Aurora, and a script that reports what fraction the
      scanner finds. This is the number that has to go up; without it every
      change after this is guesswork

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
- [ ] Bonfire, OpenGov, PlanetBids adapters — same platform-adapter pattern
      as DemandStar; worth checking each one's actual access model (free
      public listing vs. paid vendor tier) *before* investing in a reader,
      given how DemandStar turned out. **Kansas City, MO is a confirmed
      real Bonfire user** (found via the live homepage-fallback above) —
      a concrete, known-good target to verify Bonfire's access model
      against, the same way Springfield anchored the CivicPlus work
- [ ] MissouriBUYS, then the other state portals
- [ ] Contractor-association bid calendars and plan rooms — the highest
      *density* source for this trade specifically, e.g. the Springfield
      Contractors Association calendar carries sidewalk and ADA jobs directly

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

These two are the first acceptance test: when a Springfield scan returns both,
Phase 1 is working.

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
