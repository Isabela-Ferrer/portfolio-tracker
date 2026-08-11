"""Refresh coordination.

Every fetcher runs concurrently behind its own exception guard, so one broken
source can never abort a company's refresh, and one broken company can never
abort the weekly run. Whatever came back gets deduped into signals, flagged,
and handed to the AI layer for bullets and a brief update.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

import ai_narrator
import database as db
import flags as flags_mod
from fetchers import (appstore, arxiv, blogs, careers, funding, podcasts,
                      press, product_launches, reddit, youtube)
from models import Company, Signal
from trend_calculator import headcount_trend

UA = {"User-Agent": "Mozilla/5.0 (compatible; dream-tracker/1.0)"}

# How many companies refresh at once. Fifteen at once hammers Google News and
# the ATS APIs; five keeps the full run comfortably inside the 5 minute budget
# without looking like a scraper.
REFRESH_CONCURRENCY = 5

_COMPANY_FIELDS = ("id", "name", "website", "logo_url", "ats_platform", "ats_slug",
                   "enabled_optional_fetchers", "app_store_url", "play_store_url")


def row_to_company(row: dict) -> Company:
    return Company(**{k: row.get(k) for k in _COMPANY_FIELDS})


# --------------------------------------------------------------------------
# Converting the carried-over fetchers' return types into Signals
# --------------------------------------------------------------------------

def _when(raw: str) -> str:
    dt = blogs.parse_date(raw or "")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") if dt else ""


# News search and release feeds happily return results from a year ago. Without
# a window the funding and press flags light up for every company forever,
# which makes flag-based sorting meaningless.
MAX_SIGNAL_AGE_DAYS = 30


def _fresh(published_at: str) -> bool:
    """Undated items pass: a missing date is usually a parsing gap, not age."""
    if not published_at:
        return True
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_SIGNAL_AGE_DAYS)) \
        .strftime("%Y-%m-%dT%H:%M:%S")
    return published_at >= cutoff


def _press_signals(company_id: int, articles: list) -> list:
    out = []
    for a in articles or []:
        when = _when(a.published_at)
        if not a.url or not _fresh(when):
            continue
        out.append(Signal(company_id=company_id, type="press", title=a.title,
                          url=a.url, published_at=when,
                          raw={"source": a.source, "snippet": (a.snippet or "")[:400]}))
    return out


def _funding_signals(company_id: int, items: list) -> list:
    out = []
    for f in items or []:
        when = _when(f.date)
        if not f.url or not _fresh(when):
            continue
        out.append(Signal(company_id=company_id, type="funding", title=f.title,
                          url=f.url, published_at=when,
                          raw={"amount": f.amount_hint, "round_type": f.round_type,
                               "investors": f.investors,
                               "snippet": (f.snippet or "")[:400]}))
    return out


def _launch_signals(company_id: int, items: list) -> list:
    out = []
    for l in items or []:
        when = _when(l.date)
        if not l.url or not _fresh(when):
            continue
        out.append(Signal(company_id=company_id, type="launch", title=l.title,
                          url=l.url, published_at=when, raw={"source": l.source}))
    return out


def _reddit_signals(company_id: int, items: list) -> list:
    return [
        Signal(company_id=company_id, type="reddit", title=s.content, url=s.url,
               published_at=_when(s.date), raw={"platform": s.platform})
        for s in items or [] if s.url
    ]


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------

async def refresh_company(company, run_ai: bool = True) -> dict:
    """Refresh one company and persist a snapshot. Never raises on fetch failure."""
    if isinstance(company, dict):
        company = row_to_company(company)

    people = db.list_people(company.id)
    sources = db.list_sources(company.id)
    previous = db.get_latest_snapshot(company.id)
    since = _since(previous)

    errors = {}

    async def guard(name, coro):
        try:
            return await coro
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {e}"
            return None

    async with httpx.AsyncClient(headers=UA, follow_redirects=True) as client:
        tasks = {
            "careers": guard("careers", careers.fetch(company, client)),
            "blogs": guard("blogs", blogs.fetch(company, sources, client)),
            "press": guard("press", press.fetch(company)),
            "funding": guard("funding", funding.fetch(company)),
            "launches": guard("launches", product_launches.fetch(company, sources, client)),
            "podcasts": guard("podcasts", podcasts.fetch(company, people, client)),
            "youtube": guard("youtube", youtube.fetch(company, people, sources, since, client)),
        }
        if company.wants("arxiv"):
            tasks["arxiv"] = guard("arxiv", arxiv.fetch(company, people, client))
        if company.wants("reddit"):
            tasks["reddit"] = guard("reddit", reddit.fetch(company))
        if company.wants("appstore"):
            tasks["appstore"] = guard("appstore", appstore.fetch(company))

        names = list(tasks)
        results = dict(zip(names, await asyncio.gather(*tasks.values())))

    careers_result = results.get("careers") or {}
    if careers_result.get("skipped"):
        errors["careers"] = f"diff skipped: {careers_result['skipped']}"

    signals = []
    signals += careers_result.get("signals", [])
    signals += results.get("blogs") or []
    signals += results.get("podcasts") or []
    signals += results.get("youtube") or []
    signals += results.get("arxiv") or []
    signals += _press_signals(company.id, results.get("press"))
    signals += _funding_signals(company.id, results.get("funding"))
    signals += _launch_signals(company.id, results.get("launches"))
    signals += _reddit_signals(company.id, results.get("reddit"))

    extras = {}
    if results.get("appstore"):
        extras["appstore"] = [
            {"platform": a.platform, "avg_rating": a.avg_rating,
             "review_count": a.review_count}
            for a in results["appstore"]
        ]

    # Persist: only genuinely new signals come back, which is what "this week" means.
    snapshot_id = db.create_snapshot(company.id)
    new_signals = db.insert_signals(snapshot_id, signals)

    open_jobs = db.get_open_jobs(company.id)
    headcount_total = careers_result.get("headcount_total", len(open_jobs))
    headcount_nyc = careers_result.get("headcount_nyc",
                                       sum(1 for j in open_jobs if j["is_nyc"]))
    delta = headcount_total - int((previous or {}).get("headcount_total") or headcount_total)

    company_flags = flags_mod.compute_flags(new_signals, open_jobs, delta)

    bullets = [ai_narrator.QUIET_BULLET]
    if run_ai:
        bullets = await asyncio.to_thread(
            ai_narrator.generate_weekly_bullets, company.name, new_signals, open_jobs)

        existing = db.get_brief(company.id)
        if new_signals or not existing:
            brief = await asyncio.to_thread(
                ai_narrator.update_brief, company.name,
                (existing or {}).get("content_md", ""),
                new_signals or db.get_recent_signals(company.id, days=90, limit=40),
                open_jobs, people)
            db.save_brief(company.id, brief)
    elif new_signals:
        bullets = [f"{len(new_signals)} new signals picked up."]

    db.finalize_snapshot(snapshot_id, company_flags, bullets, errors,
                         headcount_total, headcount_nyc, extras)

    snapshot = db.get_latest_snapshot(company.id)
    return {
        "company_id": company.id,
        "company": company.name,
        "snapshot_id": snapshot_id,
        "flags": company_flags,
        "flag_count": flags_mod.flag_count(company_flags),
        "bullets": bullets,
        "new_signals": len(new_signals),
        "jobs_opened": careers_result.get("opened", 0),
        "jobs_closed": careers_result.get("closed", 0),
        "headcount": {"total": headcount_total, "nyc": headcount_nyc},
        "trend": headcount_trend(snapshot, previous),
        "errors": errors,
    }


def _since(previous: dict) -> datetime:
    """Look back to the last snapshot, with a 14 day floor for first runs."""
    default = datetime.now(timezone.utc) - timedelta(days=14)
    raw = (previous or {}).get("created_at")
    if not raw:
        return default
    try:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return default


async def refresh_all(run_ai: bool = True) -> dict:
    """Refresh every company, bounded concurrency, one failure never stops the rest."""
    companies = db._list_companies()
    sem = asyncio.Semaphore(REFRESH_CONCURRENCY)

    async def one(row):
        async with sem:
            try:
                return await refresh_company(row_to_company(row), run_ai=run_ai)
            except Exception as e:
                print(f"[orchestrator] {row['name']} failed: {type(e).__name__}: {e}")
                return {"company_id": row["id"], "company": row["name"],
                        "error": f"{type(e).__name__}: {e}", "flags": {},
                        "flag_count": 0, "bullets": [], "new_signals": 0}

    started = datetime.now(timezone.utc)
    results = await asyncio.gather(*[one(c) for c in companies])
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    return {
        "companies": len(results),
        "elapsed_seconds": round(elapsed, 1),
        "failed": [r["company"] for r in results if r.get("error")],
        "results": sorted(results, key=lambda r: (-r.get("flag_count", 0), r["company"])),
    }
