import json
from datetime import datetime, timedelta
from dataclasses import asdict
from models import SnapshotResult

# --- CONFIGURATION ---
WEIGHTS = {
    "press":    0.20,
    "jobs":     0.25,
    "linkedin": 0.20,
    "appstore": 0.15,
    "launches": 0.15,
    "funding":  0.05,
}

TIER_1_SOURCES = ['techcrunch', 'forbes', 'bloomberg', 'reuters', 'wsj', 'nytimes', 'techcrunch.com']

# --- HELPER FUNCTIONS ---

def _prev_data(prev_row, key: str):
    """Safely extracts a specific data block from the previous DB row."""
    if not prev_row or not prev_row.get(key):
        return None
    try:
        return json.loads(prev_row[key])
    except (json.JSONDecodeError, TypeError):
        return None

def _count_recent(items, days=60) -> int:
    """Generic counter for list-based signals (launches, funding)."""
    if not items: return 0
    cutoff = datetime.now() - timedelta(days=days)
    count = 0
    for item in items:
        try:
            # Handle potential different date formats from RSS/API
            date_str = item.date if hasattr(item, 'date') else item.get('date')
            dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            if dt > cutoff: count += 1
        except: continue
    return count

# --- THE MAIN SCORER ---

def calculate_score(snapshot: SnapshotResult, prev_row=None) -> tuple[int, dict]:
    breakdown = {}

    # 1. PRESS (Count + Quality Bonus)
    press_score = 0
    for article in snapshot.press:
        press_score += 10 # Base points
        if any(src in article.url.lower() for src in TIER_1_SOURCES):
            press_score += 15 # Tier 1 Bonus
    breakdown["press"] = min(press_score, 100)

    # 2. JOBS (Count + Growth Velocity)
    curr_jobs = snapshot.jobs.total_count if snapshot.jobs else 0
    prev_job_data = _prev_data(prev_row, 'jobs_data')
    prev_jobs = prev_job_data.get('total_count', 0) if prev_job_data else 0
    
    job_base = min(curr_jobs * 5, 60) # Points for having openings
    job_growth = 40 if curr_jobs > prev_jobs and curr_jobs > 0 else 0
    breakdown["jobs"] = min(job_base + job_growth, 100)

    # 3. LINKEDIN (Size + Growth Rate)
    # Note: Using snapshot.linkedin_numeric if you added it to models
    curr_li = getattr(snapshot, 'headcount_numeric', 0) 
    prev_li = _prev_data(prev_row, 'linkedin_data').get('headcount_numeric', 0) if prev_row else 0
    
    if curr_li > 0:
        li_base = min(curr_li / 50, 40) # Scale up to 2000 employees
        # Growth bonus: +60 points if they added ANYONE
        li_growth = 60 if curr_li > prev_li and prev_li > 0 else 0
        breakdown["linkedin"] = min(li_base + li_growth, 100)
    else:
        breakdown["linkedin"] = 0

    # 4. APP STORE (Rating + Review Volume)
    if snapshot.appstore:
        avg_rating = sum(a.avg_rating for a in snapshot.appstore) / len(snapshot.appstore)
        # Normalized rating (4.0/5.0 = 80pts)
        breakdown["appstore"] = round((avg_rating / 5.0) * 100)
    else:
        breakdown["appstore"] = 0

    # 5. LAUNCHES (Freshness)
    recent_launches = _count_recent(snapshot.launches, days=60)
    breakdown["launches"] = min(recent_launches * 50, 100)

    # 6. FUNDING (The Multiplier)
    recent_funding = _count_recent(snapshot.funding, days=120)
    breakdown["funding"] = 100 if recent_funding > 0 else 0

    # FINAL WEIGHTED CALCULATION
    total = sum(breakdown[k] * WEIGHTS[k] for k in WEIGHTS)
    
    return round(total), breakdown