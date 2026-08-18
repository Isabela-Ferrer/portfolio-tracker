"""FastAPI app for the dream companies tracker."""

import asyncio
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ai_narrator
import database as db
import digest as digest_mod
import discovery
import flags as flags_mod
import orchestrator
import scheduler
from trend_calculator import headcount_history, headcount_trend


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    db._migrate_db()
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="Dream Companies Tracker", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="frontend"), name="static")


# --- Pydantic models ---

class CompanyCreate(BaseModel):
    name: str
    website: str = ""
    logo_url: Optional[str] = None
    ats_platform: Optional[str] = None
    ats_slug: Optional[str] = None
    enabled_optional_fetchers: Optional[List[str]] = None
    app_store_url: Optional[str] = None
    play_store_url: Optional[str] = None


class CompanyUpdate(BaseModel):
    name: Optional[str] = None
    website: Optional[str] = None
    logo_url: Optional[str] = None
    ats_platform: Optional[str] = None
    ats_slug: Optional[str] = None
    enabled_optional_fetchers: Optional[List[str]] = None
    app_store_url: Optional[str] = None
    play_store_url: Optional[str] = None


class PersonIn(BaseModel):
    name: str
    role: str = ""
    track_arxiv: bool = False


class PersonUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    track_arxiv: Optional[bool] = None


class SourceIn(BaseModel):
    type: str
    url: str


class BriefIn(BaseModel):
    content_md: str


class DigestSendIn(BaseModel):
    to: Optional[str] = None


# --- Pages ---

@app.get("/", include_in_schema=False)
def root():
    return FileResponse("frontend/index.html")


@app.get("/company.html", include_in_schema=False)
def company_page():
    return FileResponse("frontend/company.html")


@app.get("/settings.html", include_in_schema=False)
def settings_page():
    return FileResponse("frontend/settings.html")


# --- Dashboard ---

@app.get("/api/dashboard")
def dashboard():
    """One call for the whole index page: flags, bullets, headcount trend."""
    rows = []
    for company in db._list_companies():
        snapshot = db.get_latest_snapshot(company["id"])
        previous = (db.get_previous_snapshot(company["id"], snapshot["id"])
                    if snapshot else None)
        open_jobs = db.get_open_jobs(company["id"])
        company_flags = (snapshot or {}).get("flags") or {}
        rows.append({
            "id": company["id"],
            "name": company["name"],
            "website": company["website"],
            "logo_url": company["logo_url"],
            "flags": company_flags,
            "active_flags": flags_mod.active_flags(company_flags),
            "flag_count": flags_mod.flag_count(company_flags),
            "bullets": (snapshot or {}).get("bullets") or [],
            "headcount_total": (snapshot or {}).get("headcount_total") or 0,
            "headcount_nyc": (snapshot or {}).get("headcount_nyc") or 0,
            "trend": headcount_trend(snapshot, previous) if snapshot else {},
            "early_career_count": sum(1 for j in open_jobs if j["is_early_career"]),
            "last_refresh": (snapshot or {}).get("created_at"),
            "errors": (snapshot or {}).get("errors") or {},
        })

    rows.sort(key=flags_mod.sort_key)
    return {
        "companies": rows,
        "flag_labels": flags_mod.FLAG_LABELS,
        "flag_icons": flags_mod.FLAG_ICONS,
        "flag_order": list(flags_mod.FLAG_ORDER),
    }


# --- Companies ---

@app.get("/api/companies")
def list_companies():
    return db._list_companies()


@app.get("/api/companies/{company_id}")
def get_company(company_id: int):
    company = db._get_company_by_id(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    snapshot = db.get_latest_snapshot(company_id)
    previous = db.get_previous_snapshot(company_id, snapshot["id"]) if snapshot else None
    snapshots = db.get_snapshots(company_id, limit=12)
    brief = db.get_brief(company_id)

    return {
        "company": company,
        "people": db.list_people(company_id),
        "sources": db.list_sources(company_id),
        "brief": brief,
        "snapshot": snapshot,
        "active_flags": flags_mod.active_flags((snapshot or {}).get("flags") or {}),
        "trend": headcount_trend(snapshot, previous) if snapshot else {},
        "headcount_history": headcount_history(snapshots),
        "jobs": db.get_jobs(company_id),
        "signals": db.get_all_signals(company_id, limit=200),
        "week_signals": (db.get_signals_for_snapshot(snapshot["id"]) if snapshot else []),
        "snapshots": snapshots,
        "flag_labels": flags_mod.FLAG_LABELS,
        "flag_icons": flags_mod.FLAG_ICONS,
    }


@app.post("/api/companies")
async def create_company(body: CompanyCreate):
    """Create a company. Name plus website is enough: discovery fills in the rest."""
    company_id = db.create_company(
        name=body.name, website=body.website, logo_url=body.logo_url,
        ats_platform=body.ats_platform, ats_slug=body.ats_slug,
        enabled_optional_fetchers=body.enabled_optional_fetchers or [],
    )
    if body.app_store_url or body.play_store_url:
        db.update_company(company_id, app_store_url=body.app_store_url,
                          play_store_url=body.play_store_url)

    found = None
    if not body.ats_slug:
        try:
            found = await discovery.run_discovery(company_id)
        except Exception as e:
            found = {"error": f"{type(e).__name__}: {e}"}

    return {"company": db._get_company_by_id(company_id), "discovery": found}


@app.put("/api/companies/{company_id}")
def update_company(company_id: int, body: CompanyUpdate):
    if not db._get_company_by_id(company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    db.update_company(company_id, **fields)
    return db._get_company_by_id(company_id)


@app.delete("/api/companies/{company_id}")
def delete_company(company_id: int):
    db.delete_company(company_id)
    return {"message": "Deleted"}


@app.post("/api/companies/{company_id}/discover")
async def run_discovery(company_id: int, overwrite: bool = False):
    if not db._get_company_by_id(company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    return await discovery.run_discovery(company_id, overwrite=overwrite)


# --- People ---

@app.get("/api/companies/{company_id}/people")
def list_people(company_id: int):
    return db.list_people(company_id)


@app.post("/api/companies/{company_id}/people")
def add_person(company_id: int, body: PersonIn):
    if not db._get_company_by_id(company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    person_id = db.create_person(company_id, body.name, body.role, body.track_arxiv)
    return {"id": person_id}


@app.put("/api/people/{person_id}")
def update_person(person_id: int, body: PersonUpdate):
    db.update_person(person_id, **{k: v for k, v in body.model_dump().items()
                                   if v is not None})
    return {"message": "Updated"}


@app.delete("/api/people/{person_id}")
def delete_person(person_id: int):
    db.delete_person(person_id)
    return {"message": "Deleted"}


# --- Sources ---

@app.get("/api/companies/{company_id}/sources")
def list_sources(company_id: int):
    return db.list_sources(company_id)


@app.post("/api/companies/{company_id}/sources")
def add_source(company_id: int, body: SourceIn):
    if not db._get_company_by_id(company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    db.replace_source(company_id, body.type, body.url)
    return db.list_sources(company_id)


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int):
    db.delete_source(source_id)
    return {"message": "Deleted"}


# --- Jobs, signals, brief, history ---

@app.get("/api/companies/{company_id}/jobs")
def get_jobs(company_id: int, include_closed: bool = False):
    return db.get_jobs(company_id, include_closed=include_closed)


@app.get("/api/companies/{company_id}/signals")
def get_signals(company_id: int, days: int = 0, limit: int = 200):
    if days:
        return db.get_recent_signals(company_id, days=days, limit=limit)
    return db.get_all_signals(company_id, limit=limit)


@app.get("/api/companies/{company_id}/brief")
def get_brief(company_id: int):
    brief = db.get_brief(company_id)
    if not brief:
        company = db._get_company_by_id(company_id)
        if not company:
            raise HTTPException(status_code=404, detail="Company not found")
        return {"company_id": company_id,
                "content_md": ai_narrator.starter_brief(company["name"]),
                "updated_at": None}
    return brief


@app.put("/api/companies/{company_id}/brief")
def save_brief(company_id: int, body: BriefIn):
    db.save_brief(company_id, body.content_md)
    return db.get_brief(company_id)


@app.get("/api/companies/{company_id}/snapshots")
def get_snapshots(company_id: int, limit: int = 20):
    return db.get_snapshots(company_id, limit=limit)


@app.get("/api/companies/{company_id}/headcount-history")
def get_headcount_history(company_id: int, limit: int = 12):
    return headcount_history(db.get_snapshots(company_id, limit=limit))


# --- Refresh ---

@app.post("/api/companies/{company_id}/refresh")
async def refresh_one(company_id: int, run_ai: bool = True):
    company = db._get_company_by_id(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return await orchestrator.refresh_company(
        orchestrator.row_to_company(company), run_ai=run_ai)


@app.post("/api/refresh-all")
async def refresh_all(run_ai: bool = True):
    return await orchestrator.refresh_all(run_ai=run_ai)


# --- Digest ---

@app.get("/api/digest/preview", response_class=HTMLResponse)
def digest_preview():
    _, html = digest_mod.render()
    return HTMLResponse(html)


@app.post("/api/digest/send")
async def digest_send(body: DigestSendIn = None):
    return await digest_mod.send(to=(body.to if body else None))


@app.get("/api/digests")
def list_digests(limit: int = 10):
    return db.list_digests(limit=limit)


@app.get("/api/ai-cache")
def ai_cache_stats():
    """How much of the AI work is being served from cache rather than rebought."""
    return db.ai_cache_stats()


@app.get("/api/digests/{digest_id}", response_class=HTMLResponse)
def get_digest(digest_id: int):
    record = db.get_digest(digest_id)
    if not record:
        raise HTTPException(status_code=404, detail="Digest not found")
    return HTMLResponse(record["html"] or "")


# --- Scheduler / status ---

@app.get("/api/scheduler")
def scheduler_status():
    return scheduler.status()


@app.post("/api/scheduler/run-refresh")
async def scheduler_run_refresh():
    return await scheduler.run_refresh_job()


@app.post("/api/scheduler/run-digest")
async def scheduler_run_digest():
    return await scheduler.run_digest_job()
