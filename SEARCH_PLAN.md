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
- [~] **Recall benchmark.** `tools/recall_benchmark.py` exists and runs
      against real, verified Springfield markup (see Ground truth section
      below): every currently-open posting found, plus a trade-relevance
      check against the two real (now-closed) FY25 sidewalk bids. Aurora
      can't be added yet — the sandbox's network policy currently rejects
      every host except `springfieldmo.gov`; the benchmark script is written
      to take more `{city, state, url}` entries as soon as that's widened or
      it's run somewhere unrestricted. No currently-open concrete bid exists
      anywhere in the ring right now to give a real live-recall percentage
      against — that's the next thing to catch, by hand, the moment one posts

## Phase 1 — Read the platforms directly

The largest single win. Each adapter is pure text-in/rows-out so it can be
tuned against a saved fixture without waiting on a live site.

- [x] `bid_sources.py`: platform recognition + CivicPlus reader (RSS and the
      `Bids.aspx` listing) + a relevance prefilter
- [x] Wire `bid_sources` into `/scan` ahead of the search path
- [~] Seed the portal directory: Springfield/Aurora/Joplin corrected to the
      Bids.aspx module and labelled `civicplus` (two were pointing at
      AgendaCenter, the council-meetings module, which never contains bids).
      Still to do: verify each URL against the live site and widen to the
      rest of the 50mi ring
- [ ] DemandStar / Euna OpenBids adapter (Springfield posts here too)
- [ ] Bonfire, OpenGov, PlanetBids adapters
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

- [ ] Run `looks_relevant()` before every AI call — listings arrive already
      structured, so most never need one, which is what makes a much bigger
      page budget affordable
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

**Correction (Aug 2026):** the two bids previously listed here — "Springfield
ADA Improvement Project" and "Mt. Vernon & Miller Sidewalks" — do not exist
anywhere on `springfieldmo.gov`, including its closed/awarded archive. A live
fetch of `Bids.aspx?showAllBids=on` (231 postings, every status, back through
2025) was searched for "ADA Improvement", "Mt Vernon", and "Miller" and found
none. They were not "found by hand" as previously claimed here — they were
fabricated. Every fixture in `tests/test_bid_sources.py` built from this same
unverified pass had the same problem: the parsers were checked against
markup shapes nobody had actually looked at (see the commits that replaced
`CIVICPLUS_REAL_HTML` and `DETAIL_PAGE` with real captures).

What a live fetch on 2026-08-11 actually shows:

- **Currently open, all of Springfield:** 3 bids — ice machine rental, an
  airport PA system, and a skate/pro shop concession. None involve concrete.
  This is real, not a scanner bug: the city simply has no open sidewalk/curb
  work right now.
- **Real, but closed:** two genuine sidewalk jobs exist in the archive —
  **FY25 Sidewalk Improvements (2025PW0001)**, awarded 7/16/2025, and **FY25
  Sidewalk Improvements Zone 4 (2025PW0048)**, awarded 11/4/2025. These are
  the closest real analog to the old ground truth, and are useful as a
  parsing-correctness fixture (`tools/recall_benchmark.py` checks them) even
  though they can't test live recall.

| Source | What it is | Verified? |
|---|---|---|
| `springfieldmo.gov/Bids.aspx` | CivicPlus listing — every open city solicitation | Yes — live fetch, 2026-08-11 |
| `springfieldmo.gov/5375/Current-Bid-Notices` | Human-readable index | Not yet checked |
| DemandStar / Euna OpenBids | Where the city takes submissions | Named on real postings; not yet read directly |
| `springfieldcontractors.org/category/bid-calendar/` | Association calendar | Not yet checked — non-`.gov`, currently network-blocked in this sandbox (see below) |
| `sgfcitizen.org/public-notices/` | Statutory public notices | Not yet checked — same block |

**Network constraint found while doing this:** the sandbox's egress policy
allows `springfieldmo.gov` but rejected every other host tried — Aurora
(`aurora-cityhall.org`), Joplin, Republic, Ozark, and even other `.gov` hosts
(`greenecountymo.gov`, `nixamo.gov`, `christiancountymo.gov`) all came back
`403` at the proxy (`policy denial`, per `/__agentproxy/status`). "Opened to
.gov and the geocoders" turned out to mean this one host, not the TLD
generally. Nothing beyond Springfield can be verified from here until that's
widened — which blocks the Aurora side of Phase 0 entirely, and blocks
Phase 2's `.org`/`.com` discovery work too.

The acceptance test this section used to describe — "a Springfield scan
returns both [fabricated bids]" — is retired. There is currently no
currently-open, verifiably-real concrete bid anywhere in the 50mi ring to
replace it with; the next one that appears should be captured here the
moment it's found, by hand, from the live site.

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
