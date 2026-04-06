import asyncio
import sqlite3
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
from database import _list_companies, _get_db_conn, _get_company_by_id

# Importing your logic and models
import orchestrator
from models import Company, SnapshotResult

app = FastAPI()

# Mount the frontend folder so we can see the website
app.mount("/static", StaticFiles(directory="frontend"), name="static")

DB_PATH = "momentum.db"

# --- PYDANTIC MODELS (For API Validation) ---
class CompanyCreate(BaseModel):
    name: str
    website_url: str
    linkedin_url: Optional[str] = None
    app_store_url: Optional[str] = None
    play_store_url: Optional[str] = None
    github_org: Optional[str] = None
    github_repo: Optional[str] = None
    product_hunt_slug: Optional[str] = None


# --- API ROUTES ---

@app.get("/")
def root(): 
    return FileResponse("frontend/index.html")

# Companies CRUD
@app.get("/api/companies")
def list_companies():
    return _list_companies()

@app.post("/api/companies")
async def create_company(body: CompanyCreate):
    with _get_db_conn() as conn:
        sql = """INSERT INTO companies (name, website_url, linkedin_url, app_store_url,
                 play_store_url, github_org, github_repo, product_hunt_slug)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""
        cursor = conn.execute(sql, (
            body.name, body.website_url, body.linkedin_url, body.app_store_url,
            body.play_store_url, body.github_org, body.github_repo, body.product_hunt_slug
        ))
        new_id = cursor.lastrowid

    company_data = _get_company_by_id(new_id)
    company_obj = Company(**company_data)
    # Fire-and-forget: first scrape runs immediately in background
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
        # Check if it exists first
        exists = conn.execute("SELECT id FROM companies WHERE id = ?", (id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Company not found")
        
        sql = """UPDATE companies SET 
                 name = ?, website_url = ?, linkedin_url = ?, 
                 app_store_url = ?, play_store_url = ?, 
                 github_org = ?, github_repo = ?, product_hunt_slug = ?
                 WHERE id = ?"""
        conn.execute(sql, (
            body.name, body.website_url, body.linkedin_url, 
            body.app_store_url, body.play_store_url, 
            body.github_org, body.github_repo, body.product_hunt_slug, 
            id
        ))
        return {"message": "Updated successfully"}

# Snapshots
@app.get("/api/companies/{id}/snapshots")
def get_snapshots(id: int, limit: int = 20):
    with _get_db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM snapshots WHERE company_id = ? ORDER BY timestamp DESC LIMIT ?", 
            (id, limit)
        ).fetchall()
        return [dict(r) for r in rows]
    
@app.get("/api/companies/{id}/snapshots/latest")
def get_latest_snapshot(id: int):
    with _get_db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE company_id = ? ORDER BY timestamp DESC LIMIT 1", 
            (id,)
        ).fetchone()
        
        if not row:
            return {"message": "No snapshots yet"}
            
        # We convert the row to a dict so FastAPI can send it as JSON
        return dict(row)

# Refresh Logic
@app.post("/api/companies/{id}/refresh")
async def refresh_one(id: int):
    company_data = _get_company_by_id(id)
    if not company_data:
        raise HTTPException(status_code=404, detail="Company not found")
    
    # Convert dict to the Company dataclass your scrapers expect
    company_obj = Company(**company_data)
    result = await orchestrator.refresh_company(company_obj)
    return result

@app.post("/api/refresh-all")
async def refresh_all():
    companies = [_get_company_by_id(c['id']) for c in _list_companies()]
    # asyncio.gather runs all refreshes at the same time
    tasks = [orchestrator.refresh_company(Company(**c)) for c in companies]
    results = await asyncio.gather(*tasks)
    return {"results": len(results), "status": "completed"}