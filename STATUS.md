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
| National bid-portal directory | 3,152 verified city/county bid pages, 52 states. Read directly every scan |
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

## Waiting on Josh (blocked, not forgotten)

| Item | What's needed |
|---|---|
| **Merge `claude/weekly-upcoming`** | Blocks all three items in the section below from running at all |
| **Re-run `supabase_sync_schema.sql`** | Adds `reviews`. Idempotent — safe to run again. Until then the review feature is inert, and reviews are the only planned source of usage data |
| **Allowlist `query.wikidata.org`** | Egress-blocked. The only untested lever for coverage, and it helps most in AR/OK where the sales region is weakest |
| **A campaign list** | The sender is built, approval-gated and CAN-SPAM compliant, but has no recipients and no copy. This is the last gap between "built" and "selling" |
| **Approve reviews / notices** | Both queues are moderated by hand, by design. Nothing publishes itself |

`MAILING_ADDRESS` was set in Render on 2026-08-18. Not yet *verified* — the
`/health` flag that reports it ships with the branch below.

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
