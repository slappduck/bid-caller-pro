# Where things stand

`SEARCH_PLAN.md` is the plan of record for the search engine specifically.
This is everything else: what's live, what's waiting on a decision, and
what's blocked on something only Josh can do.

Keep it honest. An item moves to "Live" when it is merged AND deployed AND
someone has seen it work — not when the code is written.

---

## Live now (merged to main, deployed)

| What | Notes |
|---|---|
| National bid-portal directory | 6,869 verified agency bid pages across 5,703 towns, 52 states -- cities, counties, school districts, special districts. Read directly every scan |
| Non-`.gov` portals | 308 more, from Wikidata. Region 443 → 751 agencies; Topeka 50mi tripled |
| Live homepage-link fallback | A town with no known portal still gets a real shot at its own bid page |
| Radius scan reaches the whole radius | Was ~6 guessed points at any radius; now every verified town in range |
| Anchor towns are real procurement offices | Aurora 50mi now searches Springfield, Branson, Monett… instead of guessed map points |
| Bid cards carry phone + email | Enricher no longer treats a contact *name* as "reachable" |
| Map fits the chosen radius | Was pinned at one zoom level regardless of setting |
| Support email works | Was a Cloudflare 403/1010 block on urllib's default user-agent |
| Feeds sync across browsers | Bids/Upcoming/Leads + lead statuses follow the account |
| Referral program | Give-a-month-get-a-month, in Account |
| Search cost trimmed ~30% | Skip generic queries when a known portal already answered |
| Recall benchmark | `tools/recall_check.py` + a fixture test that can't go stale |
| Custom icon set, no emoji | Both app and marketing page |
| Customer reviews | In-app rating, approval-gated, testimonials on the site |
| Coverage checker | Public `/coverage`, honest per-area numbers before paying |
| Outbound campaign sender | Draft → approve, CAN-SPAM enforced in code |
| Agency bid posting | Separate `post-a-bid.html`, moderated, feeds scans |
| Daily open-bid alerts | GitHub Actions → `/run-saved-search-alerts`, 23 consecutive successful runs |
| **State DOT lettings** | **FL, MO, AL wired into `/scan`.** 3 state pages out-produced ~4,400 city portals 2:1 in a five-market live test (19 bids vs 10) |
| **County placement** | `counties.py`, 3,215 Census population-weighted centroids — a state row is placed at the county nearest the contractor |
| **Plan holder lists** | MoDOT: the primes bidding each job, with contacts, shown on the card. Never exported — see the rule in SEARCH_PLAN.md |
| **robots.txt respected** | Scanner asks before reading. Cost measured first: 3 of 150 portals disallow (2%) |
| **Free sources survive an empty OpenAI balance** | Direct portal reads and state lettings no longer sit behind `OPENAI_API_KEY` |

## Waiting on Josh (blocked, not forgotten)

| Item | What's needed |
|---|---|
| ~~Merge `claude/weekly-upcoming`~~ | **DONE — and do NOT merge that branch now.** Its content is all in main already (weekly Upcoming scan, `campaign_sender` in `/health`, the Wikidata tools, the admin export). Its last commit is 2026-08-18 and main has moved a week past it, so merging today would revert that week of work on `license_server.py`. Safe to delete the branch. |
| **Re-run `supabase_sync_schema.sql`** | Adds `reviews`. Idempotent — safe to run again. Until then the review feature is inert, and reviews are the only planned source of usage data |
| **Allowlist `query.wikidata.org`** | Egress-blocked. The only untested lever for coverage, and it helps most in AR/OK where the sales region is weakest |
| **A campaign list** | The sender is built, approval-gated and CAN-SPAM compliant, but has no recipients and no copy. This is the last gap between "built" and "selling" |
| **Approve reviews / notices** | Both queues are moderated by hand, by design. Nothing publishes itself |

`MAILING_ADDRESS` was set in Render on 2026-08-18 and is now confirmed live:
`/health` reports `campaign_sender: true`, which is the flag that depends on
it.

### Added 2026-08-25

| Item | What's needed |
|---|---|
| **Email Infotech about Bid Express API access** | The single highest-value thing on this list. Bid Express is the only public route to open lettings for ~44 state agencies. They have an official API and their robots.txt permits the letting paths. Draft email ready in `docs/bid_express_access.md` — it only needs sending |
| **Put a contact route on the marketing site** | There is currently no way for a prospect to reach a human anywhere on curbcallpro.com. The two "contact" mentions on the page are feature copy about emailing bid buyers. Biggest credibility gap |
| **Put the mailing address in the site footer** | Already configured in Render for CAN-SPAM on email; absent from the site. Clearest "real business" signal there is |
| **Get one real review** | The review system is built, approval-gated and empty. One quote with a name, company and city outweighs every line of copy on the landing page |
| **Delete `claude/weekly-upcoming`** | Stale and now dangerous to merge — see above |

## Built and pushed, NOT merged (`claude/weekly-upcoming`)

- **Weekly Upcoming scan** — Mondays 12:00 UTC (~7am Central), emails each
  saved search what is newly planned. Uses the same two GitHub secrets the
  daily job already uses, so nothing new to configure.
- **`campaign_sender` in `/health`** — the only way to confirm
  `MAILING_ADDRESS` took effect without attempting a real send.
- **The 9-state sales region section** in this file.

## Known gaps / not started

- **Bonfire / OpenGov / PlanetBids adapters.** Researched, all three look
  freely readable, none built. All three are JS single-page apps, so they
  need either a headless browser per scan or their internal API reverse
  engineered — a different architecture from every adapter so far.
- **Agency-side marketing copy.** The campaign sender is list-driven, so this
  is copy + a list, not code.
- **No usage data.** Nothing measures whether a contractor who scans actually
  bids, or wins. Reviews will be the first signal.

## Measured, and settled — don't redo these

- **DemandStar** — paid vendor subscription behind the real API. Not building.
- **State statutory public-notice network** (~35-38 states) — the shared
  platform's `robots.txt` names AI crawlers and scrapers and disallows them
  while allowing search engines. Deliberate. Not building.
- **Contractor-association bid calendars** — checked 8+ states, almost all are
  members-only paid plan rooms. Springfield's public one is the exception.
- **Widening `CANDIDATE_BID_PATHS`** — 5 → 24 patterns, re-probed 269 missed
  Missouri domains, found **1** page. The misses are 300-person towns and
  rural water districts with no bid page, and three dead domains. Missouri's
  `.gov` coverage is at its practical ceiling (~66 of 335). Kept the wider
  list (costs a live scan nothing) but it is not the lever.

## The numbers that should drive decisions

- **~8%** of live city bid pages have concrete/sidewalk/ADA work open at any
  given moment (sampled 120 pages nationally). This is the single most
  important figure in the product.
- Search does the heavy lifting, not the directory: a real Springfield MO 50mi
  scan returned 12-13 bids against the ~1 the portal count alone predicts,
  because queries also reach counties, school districts and MoDOT. Treat every
  agency count below as **relative density**, never as a bid forecast.
- Coverage is wildly uneven nationally — Boston 144 agencies within 50mi
  against Springfield MO's 9. That is why the coverage checker exists.

## The sales region: Missouri + the 8 states around it

This is the market Josh is actually selling into. **443 verified agencies**
across the nine, 14% of the national directory.

| | found | probed | rate |
|---|---|---|---|
| TN | 82 | 278 | 29% |
| IL | 73 | 420 | 17% |
| MO | 67 | 335 | 20% |
| KS | 52 | 173 | 30% |
| NE | 39 | 130 | 30% |
| IA | 34 | 157 | 22% |
| KY | 34 | 212 | 16% |
| AR | 33 | 331 | 10% |
| OK | 29 | 214 | 14% |

Agencies within 50mi / 125mi of each metro center, which is where ad spend
should go:

- **Tier 1, the volume pitch is honest (20+ at 50mi).** Cincinnati/N.KY 35/89 ·
  Chicagoland 34/86 · Nashville 26/63 · St. Louis 23/45 · Kansas City 22/47 ·
  Metro East 21/46 · Lawrence–Topeka 20/46
- **Tier 2, solid at 50mi (15-19).** Tri-Cities TN 19/84 · Clarksville 17/58 ·
  Wichita 17/31 · Oklahoma City 16/31 · Chattanooga 15/**124** ·
  Rockford 15/**100** · Knoxville 15/76 · Little Rock 15/22 · Des Moines 14/26
- **Tier 3, sell the 125mi radius instead.** Springfield MO 9/39 · Omaha 9/39 ·
  Lincoln 9/33 · Jefferson City 10/55 · Columbia 7/52 · Joplin 7/43 · Tulsa 5/45

Chattanooga and Rockford are the non-obvious ones: thin at 50mi, best-in-region
at 125mi. Wide-radius markets.

**Two caveats that must travel with these numbers.**

- **AR (10%) and OK (14%) are probably understated.** Too far below TN and KS
  (29-30%) across similarly-sized probe sets to be all real. Do *not* answer
  this with another re-probe — Missouri's cost 269 domains and returned 1 page.
  The likely cause is those states' cities using `.com`/`.org`/`.us` rather
  than `.gov`, which the directory cannot see. **Hypothesis, not measured.**
  It is also the strongest argument for unblocking Wikidata: the only untested
  lever, and it helps most exactly where this region is weakest.
- **Louisville 6/51 and Memphis 8/18 are not crawl failures.** Both are
  consolidated city-county governments, so there are genuinely fewer separate
  municipalities holding bid pages.


---

## Measured accuracy (2026-08-25)

Numbers here came from running the real pipeline against live sites, not from
fixtures. Re-runnable: `tools/verify_state_sources.py`, and the probes in the
session scratchpad.

| Measure | Value | How |
|---|---|---|
| Relevance precision | **34/34 correct** | Every row 300 live CivicPlus portals hold (270 rows), filter run, results read by hand. Was 41 passes with 7 wrong |
| Bids verified real | **29/29** | Five live market scans; every returned bid opened — link live, title on page, deadline ahead |
| CivicPlus parse miss | **1.8%** | 220 live portals |
| Open bids per scan | **5–8 at 125mi** | 25 production scans; range 0–17 |
| Cost per scan | **~$0.007** | ~29 AI calls, ~46k input tokens, gpt-4o-mini |

**Recall against the real market is unmeasured and stays that way.** It needs a
verified list of every open concrete bid in an area; that was tried and
abandoned because such a list goes stale within weeks. What is measured is
what a portal holds versus what we extract — not what the market holds versus
what we find.

## Open threads

1. **PDF-published lettings.** Ohio's letting page is a SharePoint JS shell and
   the real document is a PDF. `pypdf` is available and extracts cleanly with
   counties intact — but the file pulled was bid *results*, not upcoming work.
   Building the PDF path plus finding each state's upcoming-letting document
   likely unlocks several states, not just Ohio. This is the next state lever,
   not more crawling.
2. **47 states still unwired.** See SEARCH_PLAN.md Phase 6 for the triage of
   all of them, including 13 that are the wrong page entirely and 4 that
   publish no location at all.
3. **Legitimacy gaps on the marketing site**, in order: no way to contact a
   human anywhere on it (the two "contact" mentions are feature copy about
   emailing bid buyers); no physical address in the footer though
   `MAILING_ADDRESS` is already set in Render for CAN-SPAM; the review system
   is built, approval-gated and empty; no named person; no refund guarantee.
   The coverage checker is the strongest trust asset on the site and is
   under-used — no competitor will tell you the honest number before you pay.
4. **Half of bids have no contact** — 197 found against 207 missing in
   production.
5. **61% of kept bids are already closed** — hidden from the customer, but
   they consume the enrichment budget. Fixed for state rows; city rows need
   more care because someone may have saved one.
