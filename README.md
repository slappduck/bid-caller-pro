# Bid Caller Pro (CurbCall Pro)

A SaaS tool for concrete/curb-and-gutter contractors: find open public-works
bid solicitations near you, track them, and get alerted to new ones — without
manually checking dozens of municipal procurement sites.

**Live at** [curbcallpro.com](https://curbcallpro.com).

## Where things live

- **Frontend** — `curbcall_netlify_v4/` (`app.html` + `app.js` + `styles.css`,
  plain JS, no framework, no build step), served from Cloudflare Workers
  (`wrangler.toml`).
- **Backend** — `license_server/`, a Flask app served by gunicorn on Render.
  Handles licensing, bid scanning, email, and scheduled jobs. See
  `license_server/__init__.py` for how the package is assembled — it's a
  structural split of what used to be one 8,500-line file, with a specific
  reason for how it's wired together, worth reading before changing it.
- **Data** — Supabase (accounts, terms acceptance) and Upstash Redis
  (license/scan-cache persistence, since Render's own disk doesn't survive
  restarts).
- **Scheduled jobs** — `.github/workflows/*.yml`, cron-triggered GitHub
  Actions hitting `/run-*` endpoints on the backend. `tests.yml` is the one
  exception — it runs the test suite on every push/PR, not on a schedule.

## Before making a change

- `CLAUDE.md` — the map: what to read before touching outreach, cron jobs, or
  Render/Netlify config, and what must never be committed.
- `OUTREACH.md` — read before drafting or sending any outreach email. Standing
  rules, the prospect-list format, and why each safety guard exists.
- `LEAD_GEN_PLAYBOOK.md` — finding new leads.
- `docs/environment_variables.md` — every environment variable the backend
  reads, which are required vs. tuning knobs with working defaults.

## Running tests

```bash
pip install -r requirements.txt
python3 -m unittest discover -s tests
```

No real credentials needed — every external call (Supabase, Resend, SAM.gov,
the search providers) is stubbed in the tests themselves. CI runs this same
command on every push and pull request (`.github/workflows/tests.yml`).
