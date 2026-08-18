# Dream Companies Tracker

A personal intelligence system for the 15 companies I want to work at. It watches
what the founders and teams are doing in public, what is shipping, and what is
opening up in hiring, then keeps a running brief on each company that is good
enough to walk into a coffee chat with.

Repurposed from a VC portfolio tracker. The 0 to 100 momentum score is gone.

---

## What it does

Every Sunday night it refreshes all 15 companies. Every Monday morning it sends
one email.

| Signal | Where it comes from |
|---|---|
| 🎙 Founder appearances | iTunes podcast search per person, YouTube Data API, arXiv for researchers |
| 🚀 Launches | Changelog entries, GitHub releases, Product Hunt |
| 📈 Hiring shifts | Weekly diff of the company's Ashby, Greenhouse or Lever board |
| 🎓 Early career roles | Title match on intern, new grad, university, early career, campus |
| 📰 Major press | Tier 1 outlets only |
| 💰 Funding | News search gated on the company name plus actual round language |

Blog posts and changelogs come off RSS where it exists, and off the listing page
where it does not.

## Flags, not a score

A quiet week at Thinking Machines Lab says nothing about whether it is a good
place to work, so nothing gets ranked down for silence. Each company gets boolean
flags per week, and the dashboard sorts by how many are lit. `early_career_open`
is sticky rather than weekly: an open new grad role still matters in week three.

## The brief

Each company has a living markdown brief, rewritten each week from the existing
brief plus the week's signals:

```
# Company
## What they do
## Key people
## Last 90 days
## Open roles snapshot   (early career and NYC called out)
## Talking points        (3 to 5, for outreach or interviews)
## Open questions        (things to figure out or ask about)
```

Anything older than 90 days rolls out on update. Briefs are editable by hand in
the UI, and hand edits survive the next update because the updater is given the
current brief as input.

## Setup

```bash
pip3 install -r requirements.txt
cp .env.example .env          # fill in OPENAI_API_KEY
python3 seed.py --discover    # 15 companies, their people, and their sources
python3 -m uvicorn main:app --reload --port 8000
```

Open http://localhost:8000/

Only `OPENAI_API_KEY` is required. Without an email transport the digest still
renders at `/api/digest/preview` and records itself as skipped. Without
`YOUTUBE_API_KEY` the YouTube fetcher returns nothing and every other fetcher
carries on.

## Sending the digest

Two transports. SMTP wins when both are configured.

**Gmail over SMTP**, the simpler one. It sends from your own address, so there
is no domain to verify and no deliverability question:

```
DIGEST_EMAIL=you@gmail.com
SMTP_USER=you@gmail.com
SMTP_PASSWORD=xxxx xxxx xxxx xxxx   # the 16 character app password
```

`SMTP_PASSWORD` is a Google [App Password](https://myaccount.google.com/apppasswords),
not your normal password. It needs 2 step verification switched on. Workspace
admins, including a lot of universities, can disable app passwords, so if yours
is blocked use a personal Gmail or Resend instead.

The Gmail API proper would also work, but it wants an OAuth client, a consent
screen and token refresh handling to send one email a week to yourself. SMTP
does the same job with the standard library.

**Resend**, used only when the SMTP variables are unset:

```
DIGEST_EMAIL=you@example.com
RESEND_API_KEY=re_...
```

## Adding a company

Name and website is enough. Discovery finds the ATS board, blog feed, changelog,
YouTube channel and GitHub org, verifies each by fetching it, and marks anything
it cannot verify as "not found" in settings rather than guessing. ATS discovery
tries slug guesses against all three platforms, and falls back to reading the
board token straight out of the company's own careers page, which is how it finds
boards like `gleanwork` and `applied` that no name-based guess would produce.

## Schedule

Refresh Sunday 23:00 America/New_York, digest Monday 08:00. Run in process, no
cron and no APScheduler. Restart safety comes from checking the database rather
than memory, so restarting the server on a Monday morning does not send twice.
Manual refresh buttons work alongside it.

## Tests

```bash
pip3 install -r requirements-dev.txt
pytest
```

236 tests in about two seconds. Nothing in the suite touches the network, an
OpenAI key or the real database: HTTP is faked with `httpx.MockTransport`, the
model client is swapped for a recording fake, and each test gets its own SQLite
file in a temp directory. See [tests/README.md](tests/README.md).

## Out of scope for v1

X/Twitter, real-time job alerts, multi-user auth, G2.
