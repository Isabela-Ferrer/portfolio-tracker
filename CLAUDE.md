# Dream Companies Tracker

A FastAPI app that tracks 15 companies Isa wants to work at. It watches founder
and team activity, product launches, hiring shifts (especially early career and
NYC roles), press and funding, keeps a living per-company brief for coffee chats
and interviews, and sends a Monday morning digest.

Built on the bones of a VC portfolio tracker. The momentum score is gone,
replaced by signal flags.

## Running the server

```bash
/Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -m uvicorn main:app --reload --port 8000
```

Then open http://localhost:8000/

Seeding and discovery:

```bash
/Library/Frameworks/Python.framework/Versions/3.11/bin/python3 seed.py --discover
```

## Architecture

- **[main.py](main.py)** — FastAPI app, REST endpoints, static file serving
- **[orchestrator.py](orchestrator.py)** — Runs every fetcher concurrently behind its own exception guard, dedupes into signals, computes flags, calls the AI layer, persists a snapshot
- **[discovery.py](discovery.py)** — Setup-time discovery of ATS board, blog feed, changelog, YouTube channel, GitHub org. Verifies everything by fetching it; unverifiable surfaces are recorded as not found
- **[flags.py](flags.py)** — Signal flags, replacing the old momentum score
- **[fetchers/](fetchers/)** — `careers` (Ashby/Greenhouse/Lever + weekly job diff), `blogs` (RSS and changelog), `podcasts` (iTunes), `youtube`, `arxiv`, plus carried-over `press`, `funding`, `product_launches`, `reddit`, `appstore`
- **[content.py](content.py)** — Article body extraction, cached permanently by URL in `content_cache`. Also where a real publish date comes from when the listing page does not carry one
- **[ai_narrator.py](ai_narrator.py)** — Three GPT-4o-mini prompts: per-signal summaries, weekly bullets, the brief updater
- **[digest.py](digest.py)** — Monday digest, plain inline-styled HTML. Two transports: Gmail over SMTP when `SMTP_USER`/`SMTP_PASSWORD` are set, otherwise Resend
- **[scheduler.py](scheduler.py)** — In-process weekly scheduler, no external dependency
- **[trend_calculator.py](trend_calculator.py)** — Headcount deltas, the only thing that trends
- **[database.py](database.py)** — SQLite helpers; `init_db()` + `_migrate_db()` run at startup
- **[models.py](models.py)** — Dataclasses: `Company`, `Person`, `Source`, `Signal`, `Posting`, `JobRow`
- **[seed.py](seed.py)** — The 15 companies and the people tracked at each
- **[frontend/](frontend/)** — Alpine.js + Tailwind: `index.html`, `company.html`, `settings.html`

`ai_extractor.py` is left in the repo from the VC era but is not in the refresh
path. `fetchers/g2.py` and `fetchers/jobs.py` are likewise dormant: G2 is out of
scope for v1, and `jobs.py` was replaced by `careers.py`.

## Key concepts

**Signals** are the central type. Every fetcher ends up producing `Signal` rows,
deduped on `(company_id, type, url)`. That dedupe is what makes "this week"
meaningful: a signal already recorded in an earlier snapshot keeps its original
snapshot and is never reported twice.

**Flags, not scores.** A quiet week at Thinking Machines means nothing and must
never rank it down. Per snapshot: `founder_appearance`, `launch`, `hiring_shift`,
`early_career_open` (sticky), `major_press`, `funding`. Dashboard sorts by flag
count desc, then name.

**The job diff** compares the live ATS board against the `jobs` table. New rows
emit `job_new`, disappeared rows get `closed_at` and emit `job_closed`. Two
guards, in opposite directions: an empty board response when roles were
previously open is treated as a broken fetch rather than a mass closure, and the
very first crawl of a company records the whole board as a baseline without
emitting anything, because 262 roles are not 262 pieces of news.

**Nothing is bought or fetched twice.** Three caches, all keyed on content:

- `content_cache` — article text by URL. A page is scraped once, ever. Failures
  are cached too, so a paywall is not retried every Monday.
- `signal_summaries` — one sentence per signal, saying what it actually said.
  Written once and then read by both the digest and the brief.
- `ai_cache` — every model response, keyed on a hash of (kind, model, prompt).
  A repeated manual refresh, or a rerun after a crash, costs nothing.

A normal week is three calls per company with new content: summarise, compose
bullets, update the brief. A company with no new content makes none at all. Job
churn on its own is not content: the counts are on the dashboard already, so
narrating them is duplication, and `_DASHBOARD_FACT_RE` in `ai_narrator` drops
any bullet that slips through and does it anyway.

## Environment

Copy `.env.example` to `.env`. Only `OPENAI_API_KEY` is required; everything
else degrades gracefully when unset.

## Key API routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/dashboard` | Everything the index page needs in one call |
| GET | `/api/companies/{id}` | Company, brief, signals, jobs, trend, snapshots |
| POST | `/api/companies` | Add a company; runs discovery automatically |
| POST | `/api/companies/{id}/discover` | Re-run ATS / blog / changelog discovery |
| POST | `/api/companies/{id}/refresh` | Full refresh for one company |
| POST | `/api/refresh-all` | Refresh all companies |
| GET | `/api/digest/preview` | Render the digest as HTML without sending |
| POST | `/api/digest/send` | Send the digest via Gmail SMTP or Resend |
| GET | `/api/ai-cache` | Cache entries and reuses, by prompt kind |
| GET | `/api/scheduler` | Next scheduled refresh and digest |

## Conventions

- No em dashes in AI-generated output. `ai_narrator._sanitize` enforces it.
- One broken fetcher, per-domain parser, or company must never abort a run.
- Weekly cadence. Job changes are digest material, not real-time alerts.
- A digest section is earned by publishing something that week. The sticky
  `early_career_open` flag still ranks the dashboard, but it no longer
  manufactures a section: standing early career roles are listed once, compactly,
  at the bottom. Otherwise the same five internships are re-listed every Monday
  under the words "quiet week".
- The subject line describes what is in the email. It counts companies with news,
  not companies with a flag lit.
- Never show a date the source did not give. An undated item shows no date; it
  used to inherit the time of the crawl and read as published that morning.

## Dependencies

`pip3 install -r requirements.txt` (Python 3.11).
