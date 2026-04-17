import sqlite3, os
import json
from dataclasses import asdict

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tracker.db")

def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # enables dict-like access
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

_get_db_conn = get_conn  # alias used by main.py

def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                website_url TEXT,
                app_store_url TEXT,
                play_store_url TEXT,
                github_org TEXT,
                github_repo TEXT,
                product_hunt_slug TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                fetched_at TEXT DEFAULT (datetime('now')),

                -- Raw signal data (JSON strings)
                press_data TEXT,
                jobs_data TEXT,
                appstore_data TEXT,
                product_launches_data TEXT,
                funding_data TEXT,
                social_data TEXT,

                -- Computed outputs
                momentum_score INTEGER,
                score_breakdown TEXT,
                ai_blurb TEXT,
                error_log TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_company_id ON snapshots(company_id);
            CREATE INDEX IF NOT EXISTS idx_snapshots_fetched_at ON snapshots(fetched_at);

            -- Extracted numeric metrics per snapshot for historical trend queries
            CREATE TABLE IF NOT EXISTS signal_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
                company_id INTEGER NOT NULL,
                recorded_at TEXT NOT NULL,
                press_count INTEGER DEFAULT 0,
                press_tier1_count INTEGER DEFAULT 0,
                jobs_count INTEGER DEFAULT 0,
                appstore_avg_rating REAL DEFAULT 0,
                appstore_review_count INTEGER DEFAULT 0,
                launches_count INTEGER DEFAULT 0,
                funding_count INTEGER DEFAULT 0,
                reddit_count INTEGER DEFAULT 0,
                momentum_score INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_signal_metrics_company_id ON signal_metrics(company_id);
            CREATE INDEX IF NOT EXISTS idx_signal_metrics_recorded_at ON signal_metrics(recorded_at);
        """)

def _migrate_db():
    """Add new columns to existing tables if they don't exist yet."""
    migrations = [
        "ALTER TABLE snapshots ADD COLUMN social_data TEXT",
        "ALTER TABLE companies ADD COLUMN g2_slug TEXT",
        "ALTER TABLE snapshots ADD COLUMN ai_highlights TEXT",
    ]
    with get_conn() as conn:
        for stmt in migrations:
            try:
                conn.execute(stmt)
            except Exception:
                pass  # Column already exists


def _get_previous_snapshot(company_id: int):
    """Retrieves the most recent snapshot to calculate growth deltas."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE company_id = ? ORDER BY fetched_at DESC LIMIT 1",
            (company_id,)
        ).fetchone()
        return dict(row) if row else None

def _save_snapshot(snapshot, score, breakdown, blurb, highlights="") -> int:
    """Converts Python objects to JSON and saves to SQLite. Returns the new snapshot_id."""
    with get_conn() as conn:
        sql = """
            INSERT INTO snapshots (
                company_id, momentum_score, score_breakdown, ai_blurb, ai_highlights,
                press_data, jobs_data,
                appstore_data, product_launches_data, funding_data,
                social_data, error_log
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        cursor = conn.execute(sql, (
            snapshot.company_id,
            score,
            json.dumps(breakdown),
            blurb,
            highlights,
            json.dumps([asdict(p) for p in snapshot.press]),
            json.dumps(asdict(snapshot.jobs) if snapshot.jobs else {}),
            json.dumps([asdict(a) for a in snapshot.appstore]),
            json.dumps([asdict(l) for l in snapshot.launches]),
            json.dumps([asdict(f) for f in snapshot.funding]),
            json.dumps([asdict(s) for s in (snapshot.social or [])]),
            json.dumps(snapshot.errors)
        ))
        return cursor.lastrowid

def _fmt_appstore(appstore_data_list) -> str:
    """Turns a list of AppStoreData objects into a readable string for the AI."""
    if not appstore_data_list:
        return "No mobile app data found"
    parts = []
    for data in appstore_data_list:
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


def save_signal_metrics(snapshot_id: int, company_id: int, recorded_at: str, metrics: dict):
    """Persist extracted numeric metrics for a snapshot."""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO signal_metrics (
                snapshot_id, company_id, recorded_at,
                press_count, press_tier1_count, jobs_count,
                appstore_avg_rating, appstore_review_count,
                launches_count, funding_count, reddit_count, momentum_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            snapshot_id,
            company_id,
            recorded_at,
            metrics.get('press_count', 0),
            metrics.get('press_tier1_count', 0),
            metrics.get('jobs_count', 0),
            metrics.get('appstore_avg_rating', 0.0),
            metrics.get('appstore_review_count', 0),
            metrics.get('launches_count', 0),
            metrics.get('funding_count', 0),
            metrics.get('reddit_count', 0),
            metrics.get('momentum_score', 0),
        ))


def get_metrics_history(company_id: int, limit: int = 12) -> list:
    """Return signal_metrics rows for a company, newest-first."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM signal_metrics
            WHERE company_id = ?
            ORDER BY recorded_at DESC
            LIMIT ?
        """, (company_id, limit)).fetchall()
        return [dict(r) for r in rows]
