import asyncio
import sqlite3
from dataclasses import fields as dc_fields
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
from database import (
    _list_companies, _get_db_conn, _get_company_by_id, init_db,
    save_signal_metrics, get_metrics_history, _migrate_db,
)

import orchestrator
from models import Company, SnapshotResult
from trend_calculator import calculate_trends, extract_metrics_from_row

app = FastAPI()

@app.on_event("startup")
def startup():
    init_db()
    _migrate_db()
    _backfill_signal_metrics()


def _backfill_signal_metrics():
    """One-time backfill: create signal_metrics rows for any snapshots that lack them."""
    with _get_db_conn() as conn:
        missing = conn.execute("""
            SELECT s.* FROM snapshots s
            LEFT JOIN signal_metrics m ON m.snapshot_id = s.id
            WHERE m.id IS NULL
        """).fetchall()

    for row in missing:
        row_dict = dict(row)
        metrics = extract_metrics_from_row(row_dict)
        save_signal_metrics(
            snapshot_id=row_dict['id'],
            company_id=row_dict['company_id'],
            recorded_at=row_dict['fetched_at'],
            metrics=metrics,
        )

# Mount the frontend folder
app.mount("/static", StaticFiles(directory="frontend"), name="static")

# --- PYDANTIC MODELS ---
class CompanyCreate(BaseModel):
    name: str
    website_url: str
    app_store_url: Optional[str] = None
    play_store_url: Optional[str] = None
    github_org: Optional[str] = None
    github_repo: Optional[str] = None
    product_hunt_slug: Optional[str] = None
    g2_slug: Optional[str] = None

# DB rows include created_at / updated_at which Company dataclass doesn't accept.
_COMPANY_FIELDS = {f.name for f in dc_fields(Company)}
def _row_to_company(row: dict) -> Company:
    return Company(**{k: v for k, v in row.items() if k in _COMPANY_FIELDS})

# --- ROUTES: HTML pages ---

@app.get("/")
def root():
    return FileResponse("frontend/index.html")

@app.get("/company.html")
def company_page():
    return FileResponse("frontend/company.html")

@app.get("/settings.html")
def settings_page():
    return FileResponse("frontend/settings.html")

# --- ROUTES: Companies CRUD ---

@app.get("/api/companies")
def list_companies():
    return _list_companies()

@app.get("/api/companies/{id}")
def get_company(id: int):
    company = _get_company_by_id(id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company

@app.post("/api/companies")
async def create_company(body: CompanyCreate):
    with _get_db_conn() as conn:
        sql = """INSERT INTO companies (name, website_url, app_store_url,
                 play_store_url, github_org, github_repo, product_hunt_slug, g2_slug)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""
        cursor = conn.execute(sql, (
            body.name, body.website_url, body.app_store_url,
            body.play_store_url, body.github_org, body.github_repo,
            body.product_hunt_slug, body.g2_slug
        ))
        new_id = cursor.lastrowid

    company_data = _get_company_by_id(new_id)
    company_obj = _row_to_company(company_data)
    # Fire-and-forget: first scrape starts immediately in background
    asyncio.create_task(orchestrator.refresh_company(company_obj))
    return company_data

@app.delete("/api/companies/{id}")
def delete_company(id: int):
    with _get_db_conn() as conn:
        conn.execute("DELETE FROM companies WHERE id = ?", (id,))
        return {"message": "Deleted"}

@app.put("/api/companies/{id}")
def update_company(id: int, body: CompanyCreate):
    with _get_db_conn() as conn:
        exists = conn.execute("SELECT id FROM companies WHERE id = ?", (id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Company not found")
        sql = """UPDATE companies SET
                 name = ?, website_url = ?,
                 app_store_url = ?, play_store_url = ?,
                 github_org = ?, github_repo = ?, product_hunt_slug = ?,
                 g2_slug = ?
                 WHERE id = ?"""
        conn.execute(sql, (
            body.name, body.website_url,
            body.app_store_url, body.play_store_url,
            body.github_org, body.github_repo, body.product_hunt_slug,
            body.g2_slug, id
        ))
        return _get_company_by_id(id)

# --- ROUTES: Snapshots ---

@app.get("/api/companies/{id}/snapshots")
def get_snapshots(id: int, limit: int = 20):
    with _get_db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM snapshots WHERE company_id = ? ORDER BY fetched_at DESC LIMIT ?",
            (id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

@app.get("/api/companies/{id}/snapshots/latest")
def get_latest_snapshot(id: int):
    with _get_db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE company_id = ? ORDER BY fetched_at DESC LIMIT 1",
            (id,)
        ).fetchone()
        if not row:
            return {"message": "No snapshots yet"}
        result = dict(row)

    # Attach trend data: history[0] = current, history[1:] = previous snapshots
    history = get_metrics_history(id, limit=13)
    if len(history) >= 2:
        result["signal_trends"] = calculate_trends(history[0], history[1:])
    else:
        result["signal_trends"] = None

    return result


@app.get("/api/companies/{id}/signal-history")
def get_signal_history(id: int, limit: int = 12):
    """Time-series of extracted numeric metrics for sparklines and trend charts."""
    return get_metrics_history(id, limit=limit)

# --- ROUTES: Refresh ---

@app.post("/api/companies/{id}/refresh")
async def refresh_one(id: int):
    company_data = _get_company_by_id(id)
    if not company_data:
        raise HTTPException(status_code=404, detail="Company not found")
    company_obj = _row_to_company(company_data)
    result = await orchestrator.refresh_company(company_obj)
    return result

@app.post("/api/refresh-all")
async def refresh_all():
    companies = [_get_company_by_id(c['id']) for c in _list_companies()]
    tasks = [orchestrator.refresh_company(_row_to_company(c)) for c in companies]
    results = await asyncio.gather(*tasks)
    return {"results": len(results), "status": "completed"}
