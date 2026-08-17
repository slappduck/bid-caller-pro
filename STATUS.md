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

## Waiting on Josh (blocked, not forgotten)

| Item | What's needed |
|---|---|
| **PR #8 merge** | Reviews, coverage checker, campaign sender, agency posting — all built and tested, unmerged |
| **Re-run `supabase_sync_schema.sql`** | Adds `reviews` + `user_feeds`. Idempotent. Until then those features are inert |
| **Set `MAILING_ADDRESS` in Render** | Campaign sender refuses to send commercial email without a postal address (CAN-SPAM) |
| **Allowlist `query.wikidata.org`** | Egress-blocked. This is the only untested lever for rural coverage (~750 MO towns with no `.gov`) |
| **Weekly scan schedule** | Which day/time, and Upcoming only or Upcoming + a fresh bid scan? Nothing built yet |
| **Approve reviews / notices** | Both queues are moderated by hand, by design. Nothing publishes itself |

## Built, not yet merged (PR #8)

- Customer reviews — in-app rating, approval-gated, testimonials on the site
- Coverage checker — public `/coverage`, honest per-area numbers before paying
- Outbound campaign sender — draft → approve, CAN-SPAM enforced in code
- Agency bid posting — separate `post-a-bid.html`, moderated, feeds scans

## Known gaps / not started

- **Upcoming is not scheduled.** Works on demand; its logic is inline in the
  route, so the alert job can't reuse it. Needs the same extraction
  `_perform_scan` already had before it can run weekly.
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
- Verified agencies within 50mi: **Boston 144 · Newark 112 · Hartford 92 ·
  Dallas 41 · KC 19 · Springfield MO 9**. Coverage is wildly uneven.
- So expected open concrete bids ≈ agencies × 8%. Aurora at 50mi ≈ 1. That
  is not a bug; southwest Missouri is a thin market minute-to-minute.
- Search still does the heavy lifting: a real Springfield 50mi scan returned
  12-13 bids against the ~1 that portals alone predict, because queries also
  reach counties, school districts and MoDOT.
- **Marketing implication:** the volume pitch is honest in MA/CT/NJ and not
  in rural Missouri. There, sell 125mi (37 agencies) and "catches work you'd
  otherwise miss" — a claim that survives a slow week.
