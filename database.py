import sqlite3, os
import json
from dataclasses import asdict

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tracker.db")

def get_conn(): # createa a database that allows you to fetch data using company name
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # enables dict-like access
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                website_url TEXT,
                linkedin_url TEXT,
                app_store_url TEXT,       -- e.g. https://apps.apple.com/app/id123456
                play_store_url TEXT,      -- e.g. https://play.google.com/store/apps/details?id=com.example
                github_org TEXT,          -- e.g. "stripe" (org name only, not full URL)
                github_repo TEXT,         -- e.g. "stripe-python" (optional, for releases RSS)
                product_hunt_slug TEXT,   -- e.g. "notion" (for producthunt.com/@notion)
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                fetched_at TEXT DEFAULT (datetime('now')),

                -- Raw signal data (JSON strings)
                press_data TEXT,           -- {articles: [{title, url, source, published_at}]}
                jobs_data TEXT,            -- {total_count: int, roles: [str], source_url: str}
                linkedin_data TEXT,        -- {headcount_band: str, headcount_numeric: int}
                appstore_data TEXT,        -- {ios: {rating, review_count, recent_reviews: []}, android: {...}}
                product_launches_data TEXT,-- {launches: [{title, url, date, source}]}
                funding_data TEXT,         -- {rounds: [{title, url, date, amount_hint}]}

                -- Computed outputs
                momentum_score INTEGER,    -- 0-100
                score_breakdown TEXT,      -- JSON: {press: int, jobs: int, linkedin: int, ...}
                ai_blurb TEXT,             -- 2-3 sentence narrative from Claude
                error_log TEXT             -- JSON: {fetcher_name: error_message} for any failures
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_company_id ON snapshots(company_id);
            CREATE INDEX IF NOT EXISTS idx_snapshots_fetched_at ON snapshots(fetched_at);
        """)

def _get_previous_snapshot(company_id: int):
    """Retrieves the most recent record for a company to calculate growth."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        # Get the single most recent snapshot
        row = conn.execute(
            "SELECT * FROM snapshots WHERE company_id = ? ORDER BY timestamp DESC LIMIT 1",
            (company_id,)
        ).fetchone()
        return dict(row) if row else None

def _save_snapshot(snapshot, score, breakdown, blurb):
    """Converts Python objects to JSON and saves to SQLite."""
    with sqlite3.connect(DB_PATH) as conn:
        sql = """
            INSERT INTO snapshots (
                company_id, score, breakdown, blurb,
                press_data, jobs_data, linkedin_data, 
                appstore_data, launches_data, funding_data
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        # asdict() converts our dataclasses into standard Python dictionaries
        # json.dumps() converts those dictionaries into text strings
        conn.execute(sql, (
            snapshot.company_id,
            score,
            json.dumps(breakdown),
            blurb,
            json.dumps([asdict(p) for p in snapshot.press]),
            json.dumps(asdict(snapshot.jobs) if snapshot.jobs else {}),
            json.dumps(asdict(snapshot.linkedin) if snapshot.linkedin else {}),
            json.dumps([asdict(a) for a in snapshot.appstore]),
            json.dumps([asdict(l) for l in snapshot.launches]),
            json.dumps([asdict(f) for f in snapshot.funding])
        ))

def _fmt_appstore(appstore_data_list) -> str:
    """Turns a list of AppStoreData objects into a readable string for the AI."""
    if not appstore_data_list:
        return "No mobile app data found"
    
    parts = []
    for data in appstore_data_list:
        # e.g., "4.8 (ios)"
        parts.append(f"{data.avg_rating} ({data.platform})")
    
    return ", ".join(parts)

def _list_companies():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM companies").fetchall()
        return [dict(r) for r in rows]

def _get_company_by_id(id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM companies WHERE id = ?", (id,)).fetchone()
        return dict(row) if row else None