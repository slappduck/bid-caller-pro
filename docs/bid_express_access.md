# Bid Express access — the question, the evidence, and the ask

Bid Express (bidx.com / bidexpress.com, by Infotech) is the e-bidding platform
for roughly 44 state agencies, including most state DOTs. For most of them it
is the **only** place the open-project list appears. Florida, Missouri and
Alabama — the three states CurbCall reads today — work because they publish a
public page *as well*, which is the exception rather than the rule.

So Bid Express is the single biggest remaining lever on bid supply, and the
question of how to reach it is a licensing one, not an engineering one.

## What was established (2026-08-25)

- **There is an official API.** Infotech documents it at
  `bidexpress-help.zendesk.com/hc/en-us/articles/27944171821335-APIs`, and
  describes it as being for administrators integrating Bid Express into their
  systems *and for developers who want to script interactions with the
  service*. Scripting is explicitly contemplated, which is the important part.
- **robots.txt is permissive about lettings.** `bidx.com/robots.txt` reads:

      User-agent: *
      Disallow: /*/apparentbids
      Disallow: /*/planholders

  The letting paths are not disallowed. Note they *do* disallow plan-holder
  lists — worth respecting as a signal about that data generally, even though
  the plan-holder feature CurbCall ships reads MoDOT's own site, not theirs.
- **The web UI cannot be read anyway.** Every per-state URL
  (`/oh/lettings`, `/mo/lettings`, `/oh/main`) returns the same 3,216-byte
  JavaScript shell. The content comes from an internal API, and an account has
  been required to browse since October 2023.

## What could not be established here

The API's terms, its tiers, whether vendors can get credentials at all (it may
be agency-only), and whether open solicitations are exposed. The documentation
sits behind a Cloudflare security check. Working around that check is not
something to do — it is an access control, and the whole point of this
document is to go through the front door.

## The recommendation

**Do not scrape it. Ask for API access.** A sanctioned route exists, so using
a free Starter account plus a headless browser to extract data for a
commercial product would be the wrong call even if it worked — it is exactly
the sort of thing that gets an IP banned for every customer at once, and it
would put the business on the wrong side of an agreement nobody has read.

Draft below. It is deliberately short and states plainly what we are and what
we want; procurement platforms field this question regularly.

---

**To:** Infotech / Bid Express support
**Subject:** API access for reading open solicitations

> Hello,
>
> I run CurbCall Pro (curbcallpro.com), a small subscription tool that helps
> concrete contractors find public sidewalk, curb and ADA ramp work near them.
> It reads agency bid pages and shows contractors the open solicitations
> within a chosen radius, linking back to the agency's own posting.
>
> I understand Bid Express offers an API for scripted interaction with the
> service. I would like to ask:
>
> 1. Is API access available to a vendor-side subscriber like us, or is it
>    limited to agencies?
> 2. Can it list **open solicitations** for a state or agency — project
>    number, county or location, work type, and letting date? We do not need
>    bid amounts, apparent bids, or plan-holder lists.
> 3. What are the terms around using that data in a paid product that links
>    users back to the original posting, and is there a fee?
>
> We are happy to identify our client, respect rate limits, and follow
> whatever attribution you require. If an API is not the right route, I would
> rather hear that than have you find us scraping — we are not doing that.
>
> Thanks,
> Josh — Oblique Systems / CurbCall Pro

---

## If the answer is no

Fall back to the other two options in SEARCH_PLAN.md Phase 6: keep
hand-checking individual states for a public page (today's evidence puts the
hit rate low), or accept three states and spend the effort on the levers that
make existing bids better — deep links on state bids, the contact gap, and the
closed-bid share.
