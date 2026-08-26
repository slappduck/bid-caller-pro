# BidNet Direct (Sovra) — the question and the evidence

BidNet Direct carries **1,612 open solicitations matching "concrete"** at the
time of writing. That is the largest single number in
`docs/bid_source_survey.md`, and unlike most of the landscape the listing is
server-rendered rather than a JavaScript shell — title, state, publication
date and closing date all arrive in the HTML.

So the question is a licensing one, not an engineering one, and it should be
asked before anything is read.

## What was established (2026-08-26)

- **The listing is readable.**
  `bidnetdirect.com/public/solicitations/open?keywords=concrete` returns 200
  with ~10,700 characters of text, 50 dated rows per page, and rows in the
  shape *"Timbers Concrete Barrier Replacement · Colorado · 13 days left ·
  Published 08/25/2026 · Closing 09/08/2026"*.

- **robots.txt permits that path.** `bidnetdirect.com/robots.txt` disallows
  `/private/`, `/favorites`, `/public/registration/`,
  `/public/authentication/` and `/public/info`. The public solicitation
  listing is not among them.

- **Their terms could not be read.** `bidnetdirect.com/terms-of-use` 404s;
  the footer points at `sovra.com/privacy-policy/`. The Sovra terms pages
  return 200 with about 12 KB of markup and **82 characters of text** — they
  are JavaScript-rendered. Loading one in a real headless browser gets
  `ERR_CONNECTION_RESET`, which is bot protection.

  **That is where this stopped.** Getting around a bot check to read the
  terms that would govern our use of the data is precisely the wrong way to
  start, so nobody here has read them.

## Why robots.txt is not enough on its own

For an agency's own bid page, robots.txt is a fair signal: the agency wants
the notice seen, and the data is a public record it is obliged to publish.

BidNet is a different case. Sovra is a **commercial aggregator whose business
is selling access to exactly this collection** — the same product category
CurbCall is in. A permissive robots.txt is a crawler instruction, not a
licence, and reading a competitor's aggregated database on the strength of
one is the kind of decision that should be made deliberately and in writing,
not inferred.

Hence: ask.

## The recommendation

**Write and ask.** One email against 1,612 concrete solicitations is a good
trade even if the answer is no, and a "no" is worth having explicitly.

There are two things worth asking for, and the second may be easier for them
to say yes to than the first.

---

**To:** BidNet Direct / Sovra — via `bidnetdirect.com` contact form, or the
support line published there (800-835-4603)
**Subject:** Data access for a niche contractor tool

> Hello,
>
> I run CurbCall Pro (curbcallpro.com), a small subscription tool that helps
> concrete contractors find public sidewalk, curb and ADA ramp work near
> them. It reads agency bid pages and shows contractors the open
> solicitations within a chosen radius, always linking back to the original
> posting.
>
> I'd like to ask about two possibilities:
>
> 1. **Data access.** Is there an API, a feed, or a licensing arrangement
>    that would let us surface BidNet solicitations to our users — project
>    title, agency, location, work type and closing date — with every result
>    linking back to BidNet? We do not need documents, plan holders or award
>    data.
>
> 2. **Referral.** If licensing the data is not something you do, would you
>    be open to the reverse? Our users are exactly the small trade
>    contractors your member agencies want bidding, and we would be glad to
>    point them at BidNet registration where a solicitation is yours.
>
> I should be straightforward about why I'm writing rather than just
> reading: your robots.txt does permit the public solicitation path, but I
> couldn't reach your terms of use to check what they say about automated
> access, and I'd rather ask than assume. We are not scraping the site.
>
> Happy to identify our client, respect any rate limit, and follow whatever
> attribution you require.
>
> Thanks,
> Josh — Oblique Systems / CurbCall Pro

---

## If the answer is no

Nothing changes. BidNet stays off the source list, which is where it is
today. The federal reader (`federal_bids.py`) is already live and needed no
permission, because SAM.gov is a public government system publishing
public-domain notices — and that is the distinction worth keeping in mind
whenever a new source comes up.

## Status

Not sent. This is Josh's to review and send, same as
`docs/bid_express_access.md`.
