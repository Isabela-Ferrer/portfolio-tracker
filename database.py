"""SQLite layer for the dream companies tracker.

Schema is handled at startup: init_db() creates the current tables, and
_migrate_db() applies additive column migrations. The one-time move off the
old VC-era schema happens in _archive_legacy_schema(), which renames the old
tables out of the way rather than dropping them.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tracker.db")


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_get_db_conn = get_conn  # alias used by main.py


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def days_ago_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    website TEXT,
    logo_url TEXT,
    ats_platform TEXT,                          -- 'ashby' | 'greenhouse' | 'lever' | NULL
    ats_slug TEXT,
    enabled_optional_fetchers TEXT DEFAULT '[]',-- JSON list: ["reddit","appstore","g2","arxiv"]
    discovery_json TEXT,                        -- what setup found / marked as not found
    app_store_url TEXT,                         -- only used when 'appstore' is opted in
    play_store_url TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    role TEXT,
    track_arxiv INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_people_company ON people(company_id);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    type TEXT NOT NULL,     -- 'blog_rss' | 'changelog' | 'youtube_channel' | 'github_org'
    url TEXT NOT NULL,
    UNIQUE(company_id, type, url)
);
CREATE INDEX IF NOT EXISTS idx_sources_company ON sources(company_id);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    flags_json TEXT DEFAULT '{}',
    bullets_json TEXT DEFAULT '[]',
    errors_json TEXT DEFAULT '{}',
    extras_json TEXT DEFAULT '{}',   -- opt-in fetcher payloads that are readings, not events
    headcount_total INTEGER DEFAULT 0,
    headcount_nyc INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snap_company ON snapshots(company_id);
CREATE INDEX IF NOT EXISTS idx_snap_created ON snapshots(created_at);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
    type TEXT NOT NULL,
    title TEXT,
    url TEXT,
    published_at TEXT,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(company_id, type, url)
);
CREATE INDEX IF NOT EXISTS idx_signals_company ON signals(company_id);
CREATE INDEX IF NOT EXISTS idx_signals_snapshot ON signals(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_signals_published ON signals(published_at);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    title TEXT,
    location TEXT,
    department TEXT,
    url TEXT,
    first_seen_at TEXT,
    last_seen_at TEXT,
    closed_at TEXT,
    is_early_career INTEGER DEFAULT 0,
    is_nyc INTEGER DEFAULT 0,
    UNIQUE(company_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_id);
CREATE INDEX IF NOT EXISTS idx_jobs_open ON jobs(company_id, closed_at);

CREATE TABLE IF NOT EXISTS briefs (
    company_id INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    content_md TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at TEXT NOT NULL,
    subject TEXT,
    html TEXT,
    status TEXT,
    detail TEXT
);

-- Every OpenAI response, keyed by a hash of (kind, model, prompt). The same
-- prompt is never billed twice, which matters most on repeated manual refreshes
-- and on reruns after a crash mid-week.
CREATE TABLE IF NOT EXISTS ai_cache (
    key TEXT PRIMARY KEY,
    kind TEXT,
    model TEXT,
    response TEXT,
    created_at TEXT NOT NULL,
    hits INTEGER DEFAULT 0
);

-- Article body text, keyed by URL. A page is scraped once, ever. Failures are
-- cached too, so a paywalled or dead link is not retried every week.
CREATE TABLE IF NOT EXISTS content_cache (
    url TEXT PRIMARY KEY,
    title TEXT,
    text TEXT,
    published_at TEXT,
    status TEXT,                     -- 'ok' | 'empty' | 'error'
    fetched_at TEXT NOT NULL
);

-- One sentence saying what a signal actually was, generated once per signal and
-- reused by the digest and the brief. This is the main reason the weekly run
-- does not re-summarise anything it has already seen.
CREATE TABLE IF NOT EXISTS signal_summaries (
    signal_id INTEGER PRIMARY KEY REFERENCES signals(id) ON DELETE CASCADE,
    summary TEXT,
    created_at TEXT NOT NULL
);
"""

_LEGACY_INDEXES = (
    "idx_snapshots_company_id", "idx_snapshots_fetched_at",
    "idx_signal_metrics_company_id", "idx_signal_metrics_recorded_at",
)


def _table_columns(conn, table: str) -> set:
    try:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _archive_legacy_schema():
    """Move the VC-era tables aside so the new schema can be created cleanly.

    The old companies table is keyed on website_url / momentum scoring and shares
    almost nothing with the new one, so it is renamed rather than migrated in
    place. Nothing is deleted: the rows stay readable as legacy_* tables.
    """
    with get_conn() as conn:
        cols = _table_columns(conn, "companies")
        if not cols or "website_url" not in cols or "ats_platform" in cols:
            return  # fresh DB, or already on the new schema

        for name in _LEGACY_INDEXES:
            try:
                conn.execute(f"DROP INDEX IF EXISTS {name}")
            except sqlite3.Error:
                pass

        for old in ("companies", "snapshots", "signal_metrics"):
            if _table_columns(conn, old) and not _table_columns(conn, f"legacy_{old}"):
                try:
                    conn.execute(f"ALTER TABLE {old} RENAME TO legacy_{old}")
                except sqlite3.Error as e:
                    print(f"[database] could not archive {old}: {e}")


def init_db():
    _archive_legacy_schema()
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def _migrate_db():
    """Additive column migrations. Each runs at most once, failures are no-ops."""
    migrations = [
        "ALTER TABLE companies ADD COLUMN discovery_json TEXT",
        "ALTER TABLE companies ADD COLUMN logo_url TEXT",
        "ALTER TABLE snapshots ADD COLUMN bullets_json TEXT DEFAULT '[]'",
        "ALTER TABLE snapshots ADD COLUMN errors_json TEXT DEFAULT '{}'",
        "ALTER TABLE snapshots ADD COLUMN extras_json TEXT DEFAULT '{}'",
        "ALTER TABLE jobs ADD COLUMN url TEXT",
        "ALTER TABLE companies ADD COLUMN app_store_url TEXT",
        "ALTER TABLE companies ADD COLUMN play_store_url TEXT",
    ]
    with get_conn() as conn:
        for stmt in migrations:
            try:
                conn.execute(stmt)
            except sqlite3.Error:
                pass  # column already exists


# --------------------------------------------------------------------------
# Companies
# --------------------------------------------------------------------------

def _company_row(row) -> dict:
    d = dict(row)
    try:
        d["enabled_optional_fetchers"] = json.loads(d.get("enabled_optional_fetchers") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["enabled_optional_fetchers"] = []
    try:
        d["discovery"] = json.loads(d.get("discovery_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["discovery"] = {}
    return d


def _list_companies() -> list:
    with get_conn() as conn:
        return [_company_row(r) for r in conn.execute("SELECT * FROM companies ORDER BY name")]


def _get_company_by_id(company_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        return _company_row(row) if row else None


def get_company_by_name(name: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM companies WHERE name = ?", (name,)).fetchone()
        return _company_row(row) if row else None


def create_company(name: str, website: str = "", logo_url: str = None,
                   ats_platform: str = None, ats_slug: str = None,
                   enabled_optional_fetchers: list = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO companies (name, website, logo_url, ats_platform, ats_slug,
                                      enabled_optional_fetchers)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, website, logo_url, ats_platform, ats_slug,
             json.dumps(enabled_optional_fetchers or [])),
        )
        return cur.lastrowid


def update_company(company_id: int, **fields) -> None:
    allowed = {"name", "website", "logo_url", "ats_platform", "ats_slug",
               "enabled_optional_fetchers", "discovery_json",
               "app_store_url", "play_store_url"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "enabled_optional_fetchers" and not isinstance(v, str):
            v = json.dumps(v or [])
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return
    sets.append("updated_at = ?")
    vals.append(now_iso())
    vals.append(company_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE companies SET {', '.join(sets)} WHERE id = ?", vals)


def delete_company(company_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))


def set_discovery(company_id: int, discovery: dict) -> None:
    update_company(company_id, discovery_json=json.dumps(discovery))


# --------------------------------------------------------------------------
# People and sources
# --------------------------------------------------------------------------

def list_people(company_id: int = None) -> list:
    with get_conn() as conn:
        if company_id is None:
            rows = conn.execute("SELECT * FROM people ORDER BY company_id, id").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM people WHERE company_id = ? ORDER BY id", (company_id,)
            ).fetchall()
        return [dict(r) for r in rows]


def create_person(company_id: int, name: str, role: str = "", track_arxiv: bool = False) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO people (company_id, name, role, track_arxiv) VALUES (?, ?, ?, ?)",
            (company_id, name, role, 1 if track_arxiv else 0),
        )
        return cur.lastrowid


def update_person(person_id: int, **fields) -> None:
    allowed = {"name", "role", "track_arxiv"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "track_arxiv":
            v = 1 if v else 0
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return
    vals.append(person_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE people SET {', '.join(sets)} WHERE id = ?", vals)


def delete_person(person_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM people WHERE id = ?", (person_id,))


def list_sources(company_id: int = None, type: str = None) -> list:
    sql = "SELECT * FROM sources WHERE 1=1"
    args = []
    if company_id is not None:
        sql += " AND company_id = ?"
        args.append(company_id)
    if type is not None:
        sql += " AND type = ?"
        args.append(type)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id", args)]


def create_source(company_id: int, type: str, url: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO sources (company_id, type, url) VALUES (?, ?, ?)",
            (company_id, type, url),
        )
        return cur.lastrowid


def delete_source(source_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))


def replace_source(company_id: int, type: str, url: str) -> None:
    """A company has at most one canonical URL per discovered source type."""
    with get_conn() as conn:
        conn.execute("DELETE FROM sources WHERE company_id = ? AND type = ?", (company_id, type))
        if url:
            conn.execute(
                "INSERT OR IGNORE INTO sources (company_id, type, url) VALUES (?, ?, ?)",
                (company_id, type, url),
            )


# --------------------------------------------------------------------------
# Snapshots and signals
# --------------------------------------------------------------------------

def create_snapshot(company_id: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO snapshots (company_id, created_at) VALUES (?, ?)",
            (company_id, now_iso()),
        )
        return cur.lastrowid


def finalize_snapshot(snapshot_id: int, flags: dict, bullets: list, errors: dict,
                      headcount_total: int, headcount_nyc: int,
                      extras: dict = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE snapshots SET flags_json = ?, bullets_json = ?, errors_json = ?,
                                    extras_json = ?, headcount_total = ?, headcount_nyc = ?
               WHERE id = ?""",
            (json.dumps(flags), json.dumps(bullets), json.dumps(errors),
             json.dumps(extras or {}), headcount_total, headcount_nyc, snapshot_id),
        )


def delete_snapshot(snapshot_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))


def insert_signals(snapshot_id: int, signals: list) -> list:
    """Insert signals, ignoring ones already seen. Returns only the new rows.

    Deduping on (company_id, type, url) is what makes "this week" meaningful:
    a signal already recorded in an earlier snapshot keeps its original
    snapshot_id and is not reported again.

    published_at is stored exactly as the fetcher resolved it, empty string
    included. It used to fall back to the fetch time, which made every undated
    blog post look like it was published the morning of the run.
    """
    created = now_iso()
    new_rows = []
    with get_conn() as conn:
        for s in signals:
            if not s.url:
                continue
            cur = conn.execute(
                """INSERT OR IGNORE INTO signals
                   (company_id, snapshot_id, type, title, url, published_at, raw_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (s.company_id, snapshot_id, s.type, s.title, s.url,
                 s.published_at or "", json.dumps(s.raw or {}), created),
            )
            if cur.rowcount:
                new_rows.append({
                    "id": cur.lastrowid, "company_id": s.company_id, "type": s.type,
                    "title": s.title, "url": s.url,
                    "published_at": s.published_at or "",
                    "created_at": created,
                    "raw": s.raw or {},
                })
    return new_rows


def _signal_row(row) -> dict:
    d = dict(row)
    try:
        d["raw"] = json.loads(d.pop("raw_json", None) or "{}")
    except (json.JSONDecodeError, TypeError):
        d["raw"] = {}
    return d


def get_signals_for_snapshot(snapshot_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE snapshot_id = ? ORDER BY published_at DESC",
            (snapshot_id,),
        ).fetchall()
        return [_signal_row(r) for r in rows]


def get_recent_signals(company_id: int, days: int = 90, limit: int = 200) -> list:
    cutoff = days_ago_iso(days)
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM signals
               WHERE company_id = ?
                 AND COALESCE(NULLIF(published_at, ''), created_at) >= ?
               ORDER BY COALESCE(NULLIF(published_at, ''), created_at) DESC
               LIMIT ?""",
            (company_id, cutoff, limit),
        ).fetchall()
        return [_signal_row(r) for r in rows]


def get_all_signals(company_id: int, limit: int = 300) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM signals WHERE company_id = ?
               ORDER BY COALESCE(NULLIF(published_at, ''), created_at) DESC LIMIT ?""",
            (company_id, limit),
        ).fetchall()
        return [_signal_row(r) for r in rows]


def _snapshot_row(row) -> dict:
    d = dict(row)
    for col, key, default in (("flags_json", "flags", {}),
                              ("bullets_json", "bullets", []),
                              ("errors_json", "errors", {}),
                              ("extras_json", "extras", {})):
        try:
            d[key] = json.loads(d.get(col) or json.dumps(default))
        except (json.JSONDecodeError, TypeError):
            d[key] = default
    return d


def get_latest_snapshot(company_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE company_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (company_id,),
        ).fetchone()
        return _snapshot_row(row) if row else None


def get_snapshots(company_id: int, limit: int = 20) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM snapshots WHERE company_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (company_id, limit),
        ).fetchall()
        return [_snapshot_row(r) for r in rows]


def get_previous_snapshot(company_id: int, before_id: int):
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM snapshots WHERE company_id = ? AND id < ?
               ORDER BY id DESC LIMIT 1""",
            (company_id, before_id),
        ).fetchone()
        return _snapshot_row(row) if row else None


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

def _job_row(row) -> dict:
    d = dict(row)
    d["is_early_career"] = bool(d.get("is_early_career"))
    d["is_nyc"] = bool(d.get("is_nyc"))
    return d


def has_any_jobs(company_id: int) -> bool:
    """Has this company's board ever been recorded, open or closed?

    The weekly diff needs this to tell a genuine wave of new postings apart from
    the very first crawl, where every role on the board is 'new' and nothing is.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE company_id = ? LIMIT 1", (company_id,)
        ).fetchone()
        return row is not None


def get_open_jobs(company_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM jobs WHERE company_id = ? AND closed_at IS NULL
               ORDER BY is_early_career DESC, is_nyc DESC, title""",
            (company_id,),
        ).fetchall()
        return [_job_row(r) for r in rows]


def get_jobs(company_id: int, include_closed: bool = False, limit: int = 500) -> list:
    sql = "SELECT * FROM jobs WHERE company_id = ?"
    if not include_closed:
        sql += " AND closed_at IS NULL"
    sql += " ORDER BY closed_at IS NOT NULL, is_early_career DESC, is_nyc DESC, title LIMIT ?"
    with get_conn() as conn:
        return [_job_row(r) for r in conn.execute(sql, (company_id, limit))]


def insert_job(company_id: int, posting, is_early_career: bool, is_nyc: bool, seen_at: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO jobs
               (company_id, external_id, title, location, department, url,
                first_seen_at, last_seen_at, closed_at, is_early_career, is_nyc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
            (company_id, posting.external_id, posting.title, posting.location,
             posting.department, posting.url, seen_at, seen_at,
             1 if is_early_career else 0, 1 if is_nyc else 0),
        )
        return cur.lastrowid


def touch_job(company_id: int, external_id: str, posting, is_early_career: bool,
              is_nyc: bool, seen_at: str) -> None:
    """Mark a job as still open, refreshing the fields that can change."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE jobs SET last_seen_at = ?, closed_at = NULL, title = ?,
                               location = ?, department = ?, url = ?,
                               is_early_career = ?, is_nyc = ?
               WHERE company_id = ? AND external_id = ?""",
            (seen_at, posting.title, posting.location, posting.department, posting.url,
             1 if is_early_career else 0, 1 if is_nyc else 0, company_id, external_id),
        )


def close_jobs(company_id: int, external_ids: list, closed_at: str) -> None:
    if not external_ids:
        return
    with get_conn() as conn:
        conn.executemany(
            "UPDATE jobs SET closed_at = ? WHERE company_id = ? AND external_id = ?",
            [(closed_at, company_id, eid) for eid in external_ids],
        )


def headcount(company_id: int) -> tuple:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS total, COALESCE(SUM(is_nyc), 0) AS nyc
               FROM jobs WHERE company_id = ? AND closed_at IS NULL""",
            (company_id,),
        ).fetchone()
        return int(row["total"]), int(row["nyc"])


# --------------------------------------------------------------------------
# Briefs
# --------------------------------------------------------------------------

def get_brief(company_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM briefs WHERE company_id = ?", (company_id,)).fetchone()
        return dict(row) if row else None


def save_brief(company_id: int, content_md: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO briefs (company_id, content_md, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(company_id) DO UPDATE SET content_md = excluded.content_md,
                                                     updated_at = excluded.updated_at""",
            (company_id, content_md, now_iso()),
        )


# --------------------------------------------------------------------------
# Caches
#
# Three of them, all doing the same job: make sure nothing is ever paid for or
# fetched twice. ai_cache covers model calls, content_cache covers scraping,
# signal_summaries covers the per-item summaries that both the digest and the
# brief read from.
# --------------------------------------------------------------------------

def ai_cache_get(key: str):
    """Return a cached model response, recording the hit. None when absent."""
    with get_conn() as conn:
        row = conn.execute("SELECT response FROM ai_cache WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE ai_cache SET hits = hits + 1 WHERE key = ?", (key,))
        return row["response"]


def ai_cache_put(key: str, kind: str, model: str, response: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO ai_cache (key, kind, model, response, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET response = excluded.response""",
            (key, kind, model, response, now_iso()),
        )


def ai_cache_stats() -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS entries, COALESCE(SUM(hits), 0) AS hits FROM ai_cache"
        ).fetchone()
        by_kind = [dict(r) for r in conn.execute(
            "SELECT kind, COUNT(*) AS entries, COALESCE(SUM(hits), 0) AS hits "
            "FROM ai_cache GROUP BY kind ORDER BY entries DESC"
        )]
    return {"entries": int(row["entries"]), "reuses": int(row["hits"]), "by_kind": by_kind}


def content_cache_get(url: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM content_cache WHERE url = ?", (url,)).fetchone()
        return dict(row) if row else None


def content_cache_put(url: str, title: str, text: str, published_at: str,
                      status: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO content_cache (url, title, text, published_at, status, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET
                   title = excluded.title, text = excluded.text,
                   published_at = excluded.published_at, status = excluded.status,
                   fetched_at = excluded.fetched_at""",
            (url, title or "", text or "", published_at or "", status, now_iso()),
        )


def get_signal_summaries(signal_ids: list) -> dict:
    """{signal_id: summary} for the ids that already have one."""
    ids = [int(i) for i in signal_ids if i is not None]
    if not ids:
        return {}
    out = {}
    with get_conn() as conn:
        # Chunked to stay under SQLite's variable limit on a big first run.
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT signal_id, summary FROM signal_summaries "
                f"WHERE signal_id IN ({placeholders})", chunk
            ):
                out[int(row["signal_id"])] = row["summary"]
    return out


def save_signal_summaries(pairs: dict) -> None:
    """pairs: {signal_id: summary}."""
    if not pairs:
        return
    created = now_iso()
    with get_conn() as conn:
        conn.executemany(
            """INSERT INTO signal_summaries (signal_id, summary, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(signal_id) DO UPDATE SET summary = excluded.summary""",
            [(int(sid), text, created) for sid, text in pairs.items() if text],
        )


def get_signals_since(company_id: int, since_iso: str, limit: int = 200) -> list:
    """Signals first recorded since a timestamp, newest publish date first.

    Keyed on created_at, not published_at: 'what did we discover this week' is
    the question the digest is asking, and an undated post still counts.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM signals
               WHERE company_id = ? AND created_at >= ?
               ORDER BY COALESCE(NULLIF(published_at, ''), created_at) DESC
               LIMIT ?""",
            (company_id, since_iso, limit),
        ).fetchall()
        return [_signal_row(r) for r in rows]


# --------------------------------------------------------------------------
# Digests
# --------------------------------------------------------------------------

def record_digest(subject: str, html: str, status: str, detail: str = "") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO digests (sent_at, subject, html, status, detail) VALUES (?, ?, ?, ?, ?)",
            (now_iso(), subject, html, status, detail),
        )
        return cur.lastrowid


def list_digests(limit: int = 10) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, sent_at, subject, status, detail FROM digests ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_digest(digest_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
        return dict(row) if row else None
