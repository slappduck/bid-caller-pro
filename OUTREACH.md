# Sending outreach email

Read this before drafting or sending a single outreach email. It exists because
the context otherwise lives in a chat transcript, and chats end.

Everything here was learned by getting it wrong once.

## The one command

```bash
python3 tools/outreach_draft.py            # next 5 ready prospects
python3 tools/outreach_draft.py --limit 1
python3 tools/outreach_draft.py --slug procon
python3 tools/outreach_draft.py --json     # machine-readable
```

**It does not send anything.** It prints a brief per prospect — the verified
facts — and a person writes the email. That separation is deliberate; see
"Writing the email" below.

## Standing rules

These come from the owner and are not up for re-litigation by a fresh session:

1. **Do not spam anyone.** Five a day, hand-written. Not a campaign.
2. **Research every recipient before sending.** A bridge was already burned by
   emailing a contractor about a city they aren't in.
3. **Every message carries a working opt-out**, and an opt-out is honoured
   immediately and permanently.
4. **Nothing unlawful.** These are commercial emails and CAN-SPAM applies: real
   postal address, honest subject line, working unsubscribe.

## The prospect list

`data/outreach_prospects.csv` — **not in the repo and must never be.** It is
third-party business contact data and this repo is public. `.gitignore` excludes
`data/*prospects*.csv` (line 47).

It exists only on the owner's PC. A fresh clone will not have it, and
`outreach_draft.py` exits with a clear message when it's missing. That is
correct behaviour, not a bug to work around.

Columns:

| column | meaning |
|---|---|
| `slug` | unique; becomes the `/go/<slug>` tracking link. **Never reuse one.** |
| `company` | for the held/sent report |
| `greeting` | how the email opens: a first name, or `<Company> team` |
| `email` | one address |
| `city` | plain name, no ZIP — this is what `/coverage` is asked about |
| `state` | two letters |
| `intro` | the one personal line. A row without it is held, on purpose. |
| `status` | `ready` \| `hold` \| `sent` \| `unsubscribed` \| `bounced` \| `do-not-contact` |
| `sent_date` | filled in by hand after sending |

## Sender setup

`data/sender.env` — also gitignored (line 52), for the same reason: commercial
email must carry a postal address, this repo is public, and those two facts must
not meet.

```
CURBCALL_SIGNER=Your Name
CURBCALL_ADDRESS=000 Street, Town, ST 00000
CURBCALL_PHONE=optional
```

Without `SIGNER` and `ADDRESS` the tool refuses to draft at all. An email that
can't be signed correctly shouldn't be written.

## The guards, and why each exists

A prospect is **held** rather than drafted if any of these trip. Held is the
safe outcome — a bad email costs a lead permanently, an unsent one costs a day.

| guard | value | why |
|---|---|---|
| No `intro` written | — | The whole pitch is that this isn't a mail merge. |
| Location unconfirmed | — | A.C. Moate was told about 243 bid pages near Toledo. They're in Auburn, WA. They asked to be removed. |
| Coverage lookup failed | — | Never guess the number. |
| Town may be misresolved | 30 mi | `/coverage` answered 8 for Frankfort, IL when the honest answer was 113 — three Illinois towns share the name and the geocoder averaged them. The nearest agency must actually be in the prospect's city. |
| Too few agencies | `MIN_AGENCIES = 30` | Below thirty the honest number argues *against* us. Boise reads 13, Springfield MO 51, Milwaukee 221. |
| Never-contact status | `unsubscribed`, `bounced`, `do-not-contact` | Enforced even against `--slug`. This is the one status an operator cannot override. |

`RADIUS = 125` miles. Not arbitrary — at 25 miles seven of eight metros returned
an empty board, and a contractor will drive 100 miles for a curb job. Indianapolis
reads 15 agencies at 50 miles and 93 at 125. Two early emails quoted the 15 and
undersold the product against the very screen the recipient would open.

If you change `RADIUS`, re-derive `MIN_AGENCIES` — a floor calibrated at 50 miles
waves everything through at 125.

## Writing the email

The tool hands over facts. The prose gets typed, every time.

An earlier version emitted finished emails from a template. They read as
machine-written — five bodies off one skeleton, the same three trades listed in
each, an em dash every other sentence. Two of the five went to contractors in the
same city, so a template was one forwarded email away from being obvious. A merge
field cannot fix that, because *being a merge field* is the problem.

Tone, from direct feedback on rejected drafts:

- **Must not read as AI-written.** This was the first thing rejected.
- **Professional. This is a business,** not a casual note.
- Short. The agency count is the argument; don't pad around it.
- Sign with the full signature from `build_signature()` — name, address, opt-out.
  Not just "Josh", which reads as a note from a person and omits the postal
  address the law requires.

At five a day this is a couple of minutes each, and it's the difference between
mail that gets read and mail that gets binned.

## After sending

1. Set `status` to `sent` and fill `sent_date` in the CSV.
2. Watch for replies. Any request to be removed → set status to `unsubscribed`
   **that day**. The code then refuses that row forever, including via `--slug`.
3. A bounce → `bounced`.

## Never

- Commit `data/outreach_prospects.csv` or `data/sender.env`.
- Reuse a `slug`.
- Send to a held row without fixing the reason it was held.
- Export plan-holder contacts from the app into this list. Those are shown
  per-job inside the product and are never exported.
- Spoof a browser User-Agent to get around a bot block. The honest one is
  `CurbCallBot/1.0 (+https://curbcallpro.com; concrete bid aggregator; contact support@curbcallpro.com)`.

## Finding new prospects

That's a separate job — see `LEAD_GEN_PLAYBOOK.md`. Leads found there still have
to earn a `ready` status here: an `intro` line written by hand, and a location
confirmed.
