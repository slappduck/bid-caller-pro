# Git Workflow for This Repo

- After completing a task or fix, stage and commit the changes automatically.
- Commit message format: short present-tense summary (e.g. "Fix /extract 500 error on revoked key")
- Do NOT push to main automatically. Commit locally, then ask me before pushing.
- Never commit .env, *_SECRETS.txt, or any file containing API keys.
- If you touch Render or Netlify config, flag it clearly in the commit message.

# Where to look before starting

- **Outreach email** — read `OUTREACH.md` first, before drafting or sending
  anything. Standing rules, the prospect-list format, the send guards and why
  each exists.
- **Finding new leads** — `LEAD_GEN_PLAYBOOK.md`.
- **Where things live** — the site is on Cloudflare Workers (not Netlify), the
  backend on Render, data in Supabase and Upstash.

# Never commit

- `data/outreach_prospects.csv` or `data/sender.env`. Third-party contact data
  and a home postal address; this repo is public. Both are gitignored — keep it
  that way.
