# Where things stand

`SEARCH_PLAN.md` is the plan of record for the search engine specifically.
This is everything else: what's live, what's waiting on a decision, and
what's blocked on something only Josh can do.

Keep it honest. An item moves to "Live" when it is merged AND deployed AND
someone has seen it work — not when the code is written.

---

## Retired 2026-09-10 — the Windows desktop app

Deleted `app.py` and its desktop-only support modules (`applog.py`,
`auth_client.py`, `data_sync.py`, `map_view.py`, `radius_scanner.py`,
`regional_printer.py`, `subscription.py`), plus the build tooling
(`BidCallerPro.spec`, `installer.iss`, `icon.ico`) and its test file
(`tests/test_app_pure_helpers.py`).

**Why:** two UIs for one product meant every feature got hand-ported into
tkinter separately from the web app, in a different language. The desktop
app had already drifted ~3 weeks behind `app.html` and was falling further
back every time the web app shipped something. The marketing site's
Windows download link was already pulled on 2026-09-02 (see the FAQ:
"No — CurbCall Pro runs right in your browser... Add it to your home
screen and it works like an app on your phone") — `app.html` already ships
a full PWA (`manifest.webmanifest` + `sw.js`), so the exe wasn't buying
anything the browser doesn't already cover. No live customers depend on
the installed exe, so nothing server-side changed — `license_server.py`'s
endpoints are shared with the web app and untouched.

Recoverable from git history if ever needed again; nothing to revert on
Render or Cloudflare since neither served the exe.

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

### Added 2026-09-10 — legal docs

| Item | What's needed |
|---|---|
| **Push and deploy the 2026-09-10 legal update** | Committed locally (`ca6b73b`), not pushed. Netlify won't serve the new terms.html/privacy.html and Render won't stamp the new `TERMS_VERSION`/`PRIVACY_VERSION` until it's pushed and both redeploy. Existing accounts get re-prompted to accept once live |
| **Register a DMCA designated agent** | copyright.gov/dmca-directory, ~$6 and your business info — has to be done by Josh, not something that can be automated. Terms section 15 already names a complaints contact (`support@curbcallpro.com`) and cites 17 U.S.C. §512, but full safe-harbor protection only attaches once an agent is actually on file with the Copyright Office |
| **Confirm DPAs are on file with Supabase, Stripe, OpenAI, Resend, Render, Cloudflare, Upstash** | Not checkable from the repo — these are account-level agreements with each vendor, not code. Worth doing once, not urgent while under the state-privacy-law thresholds noted below |

### Added 2026-09-05 — legality

| Item | What's needed |
|---|---|
| ~~Run `supabase_sync_schema.sql` again~~ | **DONE 2026-09-06 and Live.** A real signup wrote a row: correct address, `2026-06-17` / `2026-09-06`. RLS is on, an anon insert is refused (42501), a unique index makes a repeat write impossible, and the foreign key is dropped so deleting an account no longer destroys the record |
| **Archive each published policy version** | Storing "they accepted 2026-06-17" is only evidence if what 2026-06-17 SAID can be produced. Editing terms.html or privacy.html overwrites the only copy. Existing rows already cite `2026-09-07`, a privacy version the page no longer shows. Worth solving before there is money at stake |
| **Missouri LLC** | Everything is currently personally liable to Josh Hukel |
| **E&O / general liability insurance** | Quote it once the LLC exists |
| **Attorney read of the disclaimers and arbitration clause** | The Terms are written; nobody qualified has read them |
| **Accountant on SaaS sales tax nexus** | Missouri plus wherever subscribers are |
| **Trademark search on "CurbCall Pro"** | Before spending anything more on the name |

## Built and pushed, NOT merged (`claude/weekly-upcoming`)

- **Weekly Upcoming scan** — Mondays 12:00 UTC (~7am Central), emails each
  saved search what is newly planned. Uses the same two GitHub secrets the
  daily job already uses, so nothing new to configure.
- **`campaign_sender` in `/health`** — the only way to confirm
  `MAILING_ADDRESS` took effect without attempting a real send.
- **The 9-state sales region section** in this file.

### Fixed 2026-09-06

| What | Notes |
|---|---|
| Reset links opened the signup form | `redirectTo` was `window.location.href`, which carries whatever fragment is in the address bar. Supabase appends its own `#access_token=...`, so a trailing `#` came back doubled and the token parsed as `#access_token`. No session, no error, no clue. All three emailed-link flows now share `authReturnUrl()` |
| One signup wrote two consent rows | `onAuthStateChange` fires more than once per sign-in and the flush is async. Fixed in the browser, the table (unique index) and the server (a duplicate is success, or the browser retries a rejected write forever) |
| A reused email inherited the old account's data | Deleting an account and signing up again with the same address makes a NEW account. `claimDeviceFor()` compared addresses, kept the cache, and `syncPullFeeds()` uploaded it into the new account. Now compares account ids, with a fallback so existing devices are not wiped |
| Consent records died with the account | `terms_acceptances` cascaded from `auth.users`. The evidence disappeared exactly when a dispute became likely. Kept now, with the address replaced by a salted hash, and disclosed in three places |

### Compliance position, selling into all states (2026-09-08)

Where the exposure actually is, and what covers it. Not legal advice --
this is the engineering record of what the software does.

| Area | Where it stands |
|---|---|
| **Statute of limitations** | Consent records kept 10 years after account deletion. NOT because the Terms choose Missouri law -- there is no forum-selection clause, so a customer can sue in their own state and limitations periods are generally procedural. 10 is the longest among MO/IL/IA/KY, the longest in the sales region |
| **Automatic renewal** | Disclosed at the point of purchase on both plans, restated in the purchase email, cancellable from the Account screen. California's law has no revenue threshold, so it applies from the first CA customer. **The 15-45 day reminder is built but not yet armed** -- see below |
| **CAN-SPAM** | Federal and uniform. Physical address in every commercial email, honoured opt-outs, honest headers |
| **State privacy laws** | CCPA and the newer state laws gate on $25M revenue or 100k consumers. Nowhere near any threshold. Revisit at scale, not now |
| **Sales tax nexus** | Economic nexus is generally $100k or 200 transactions per state -- roughly 170 subscribers in one state at $49/mo. Not close. SaaS taxability still varies enough to stay on the accountant's list |
| **Accessibility (ADA/WCAG)** | Audited 2026-09-08 with axe-core against WCAG 2.1 AA, every page, phone viewport. Four violations found, all fixed, zero remaining. Two were critical: pinch-zoom disabled across the whole app, and an unlabelled radius select on the marketing page. `tools/a11y_audit.js` re-runs it; `tests/test_accessibility.py` pins the regressions |
| **CCPA/CPRA rights section** | Added 2026-09-10. Privacy Policy section 8 names the CCPA categories collected, states we neither sell nor share (the "share" concept — cross-context behavioral advertising — is legally distinct from "sale" and was previously undisclosed), and gives a request channel with the statutory 10-day acknowledgment / 45-day response window. Still gated on the same $25M/100k-consumer threshold noted above; added anyway since it costs nothing and a CA resident can now find the section by name |
| **DMCA / copyright complaints** | Added 2026-09-10. Terms section 15 gives reviewers-of-others'-content a notice-and-takedown contact and cites 17 U.S.C. §512. Not yet full §512 safe harbor — that requires registering a designated agent with the Copyright Office, on Josh's list above |

**The annual renewal reminder needs two settings in Stripe before it works.**
Stripe's own "upcoming renewals" email has been turned off, so ours is the
only notice there is -- there is no longer a backstop. It needs
`invoice.upcoming` enabled on the Live-mode webhook endpoint, and the
upcoming-renewal timing changed from Stripe's default 7 days to 30 (Settings
-> Billing -> Prevent failed payments; the same setting drives both). Until
both are done an annual subscriber gets no reminder and nothing looks broken.
Not urgent while there are no annual subscribers; required before the first.

**Legal drafting done 2026-09-09.** The Terms gained a licence grant and a
claim of ownership over the compiled feed (section 5 forbade reselling data
that had never been licensed), a severability and entire-agreement clause, an
indemnity, a forum-selection clause naming Missouri courts, a 30-day
price-change notice, and a changes clause that describes the re-acceptance
prompt the software actually shows. The Privacy Policy gained the three data
flows it never disclosed -- published reviews, contacts shown on a bid, plan
holders, plus the outreach list and how to get off it -- along with a security
statement, a breach-notification promise, an honest account of edge cookies,
and a seven-year figure for billing records. Both are version 2026-09-09,
archived, and existing accounts will be re-prompted. Tests fail if any clause
is dropped or the numbering gaps.

Two things only an attorney can settle, both raised and neither resolved:
a **forum-selection clause** (without one the governing-law clause does less
work than it looks like it does, and a dispute could be heard anywhere), and
**whether to add arbitration at all** -- there is currently none, despite an
earlier note in this file implying otherwise.

### Business intelligence (added 2026-09-09)

What is now recorded, and why it was recorded before there was anybody to
measure: a date nobody wrote down at the moment it happened cannot be
recovered afterwards. The first ten customers decide whether this works, and
they are the ones most easily lost.

| Question | Answerable? |
|---|---|
| What share of trials convert to paid | Yes -- `trial_to_paid_pct` on `/diag` |
| How long a trial takes to convert | Yes -- median days |
| How long a customer lasts before cancelling | Yes -- median days. Previously impossible: a cancellation appended a key to a flat list with no date, plan or reason |
| Which plan sells | Yes -- `plans_sold` |
| How many are paying right now | Yes -- `people.paying_now` |
| Whether the product produces WINS | Yes -- `outcomes` on `/diag`: submitted, won, lost, passed, win rate, and how many distinct customers have won. Counts only; no row content leaves Supabase |
| Engagement -- scans per active customer per week | **No.** The best early warning of churn -- people stop using a thing weeks before they cancel it -- and it is not collected |
| Landing page -> signup conversion | **No.** `/click` counts outreach link opens only |

Events carry a short salted hash, never an address: enough to follow one
person's trial -> paid -> churn arc, useless for enumerating customers. The
rollup is counts and medians only, behind the diag token.

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
