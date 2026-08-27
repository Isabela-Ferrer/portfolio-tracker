# Deploying the tracker to Railway

The app runs as-is on any always-on host with a persistent disk: SQLite,
the in-process scheduler, and the Monday digest all keep working. These steps
are for [Railway](https://railway.app); Render and Fly.io work the same way
with their own volume UI.

## One-time setup

1. Sign in to Railway with GitHub and create a new project from this repo.
   Railway detects the `Dockerfile` and builds it.
2. Add a **volume** to the service, mounted at `/data`. The image already sets
   `DB_PATH=/data/tracker.db`, so the database lands on the volume and
   survives redeploys.
3. Set the environment variables (Service → Variables). Copy the values from
   the local `.env`:
   - `OPENAI_API_KEY`
   - `RESEND_API_KEY`, `DIGEST_FROM`, `DIGEST_EMAIL`
   - `YOUTUBE_API_KEY`
   - `APP_BASE_URL` — set this to the Railway public URL once it exists,
     so digest links point at the hosted dashboard.
4. Generate a public domain (Service → Settings → Networking). Railway sets
   `PORT` itself; the Dockerfile honors it.

## Carrying over the existing database

A fresh deploy starts with an empty database. To bring the history, briefs,
and caches along, copy the local `data/tracker.db` to the volume once:

```bash
railway ssh -- bash -c 'cat > /data/tracker.db' < data/tracker.db
```

Then redeploy (or restart) the service so it picks the file up. Skipping this
also works: re-add the companies in settings and let the first refresh
rebuild, at the cost of losing brief history.

## What the scheduler does there

The container never sleeps, so the in-process scheduler finally runs on time:
full refresh Sunday 23:00 America/New_York, digest Monday 08:00. No cron
configuration on the host is needed.

## Notes

- The Resend key is in test mode: it can only deliver to the account's own
  address. That is where the digest goes anyway; verify a domain at
  resend.com/domains before pointing `DIGEST_EMAIL` anywhere else.
- The local server still works exactly as before; without `DB_PATH` set it
  uses `data/tracker.db` next to the code.
