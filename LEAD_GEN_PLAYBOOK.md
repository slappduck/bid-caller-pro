# CurbCall / BidCallerPro Contractor Lead Generator

Finds real contractors nationwide who bid sidewalk, ADA-ramp, curb & gutter, and concrete flatwork work — the exact buyer profile for CurbCall/BidCallerPro. Runs weekly, rotating through states so the full country gets covered over time without any single run being overwhelming.

## Files

- `leads_master.csv` — the running database of every lead found. Columns: company_name, city, state, trade_focus, phone, email, website, source, date_found, status, notes.
- `lead_gen_rotation_state.json` — tracks which states have been scanned in the current "round" so each week picks up where the last one left off. When a round finishes (all 50 states + DC covered), it starts a new round to catch new/changed listings.

## How a run works

**Primary method (Nimble is connected):** use the `nimble_search` tool with `focus: "location"`, `search_depth: "lite"`, `query: "sidewalk ADA curb ramp concrete contractor [state name]"`, `max_results: 15`. This returns structured results directly — company name, website, address, phone, star rating, review count — no page-parsing or extra fetches needed. Use `"lite"` depth, not `"deep"`: deep mode pulls full page text per result and is overkill (and can overflow output limits) for what's just a business-listing lookup.

1. Read `lead_gen_rotation_state.json`. Take the next `batch_size` (8) states from `remaining_this_round`.
2. Run one `nimble_search` call per state (see above).
3. From the results, extract: company name, city/state (parsed from the address field), phone, website (discard bare Google Maps search links), rating/review count, and any specialty note from the result description.
4. Skip anything already in `leads_master.csv` (match on company_name + state). Append only new rows, with `date_found` = today and `status` = new.
5. Move the covered states from `remaining_this_round` to `covered_this_round` and update `last_run_date`.
6. If `remaining_this_round` is now empty, bump `round` by 1, reset `covered_this_round` to `[]`, and refill `remaining_this_round` with all 50 states + DC (listings change over time, so re-scanning is worth it).
7. Report back: how many new leads this run, which states were covered, and a few notable names.

**Fallback (if Nimble isn't available):** search `site:network.procore.com [state name] [trade] contractors` and `[state name] sidewalk ADA curb ramp contractor municipal bid` for each trade in `trade_slugs` (sidewalks, curbs-and-gutters, concrete-paving, concrete), via WebSearch. Procore's Network directory (network.procore.com) is a free, public, nationwide contractor directory searchable by state/city and trade — no login needed. This is slower and doesn't reliably surface phone numbers, but works without Nimble.

## Scaling this up

- **Contact enrichment**: most Nimble results already include phone numbers; email usually isn't in the listing. For any lead worth prioritizing, ask Claude to open its website and pull an email/contact form.
- **Filtering by lead quality**: rating and review_count are decent proxies for "established, real business" — leads with 20+ reviews and 4.5+ rating are safer bets to prioritize first.
- **Pipeline tracking**: `status` can be extended beyond new/contacted — e.g. `qualified`, `emailed`, `demo booked` — to turn this into a lightweight pipeline tracker.
