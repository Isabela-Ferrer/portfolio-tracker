"""In-process weekly scheduler.

Sunday 23:00 America/New_York  full refresh of all 15 companies
Monday  08:00 America/New_York  digest email

Deliberately dependency free: a one minute tick beats pulling in APScheduler for
two weekly jobs. Restart safety comes from looking at what is already in the
database rather than from in-memory state, so bouncing the server on a Monday
morning does not send the digest twice.

A job whose scheduled time passed while the server was down runs on the next
tick after startup instead of being skipped for the week: the laptop being
closed at Monday 08:00 delays the digest until the Mac wakes, it no longer
loses it. When both jobs are overdue the refresh goes first, so a caught-up
digest reports fresh data.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import database as db
import digest
import orchestrator

TZ = ZoneInfo(os.getenv("SCHEDULE_TZ") or "America/New_York")

# weekday(): Monday is 0, Sunday is 6
REFRESH_DAY = int(os.getenv("REFRESH_DAY", 6))
REFRESH_HOUR = int(os.getenv("REFRESH_HOUR", 23))
DIGEST_DAY = int(os.getenv("DIGEST_DAY", 0))
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", 8))

TICK_SECONDS = 60
# A failed digest attempt is retried, but not every tick all week.
RETRY_HOURS = 2

_state = {"running": False, "last_refresh": None, "last_digest": None, "task": None}


def now_local() -> datetime:
    return datetime.now(TZ)


def _last_scheduled(now: datetime, day: int, hour: int) -> datetime:
    """The most recent occurrence of day/hour at or before now."""
    behind = (now.weekday() - day) % 7
    candidate = (now - timedelta(days=behind)).replace(
        hour=hour, minute=0, second=0, microsecond=0)
    if candidate > now:
        candidate -= timedelta(days=7)
    return candidate


def _hours_since(iso: str) -> float:
    if not iso:
        return 1e9
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return 1e9
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def _hours_ago(now: datetime, then: datetime) -> float:
    return (now - then).total_seconds() / 3600


def _refresh_due(now: datetime) -> bool:
    """Due when no snapshot exists since the last scheduled refresh time."""
    with db.get_conn() as conn:
        if not conn.execute("SELECT 1 FROM companies LIMIT 1").fetchone():
            return False
        row = conn.execute("SELECT MAX(created_at) AS last FROM snapshots").fetchone()
    scheduled = _last_scheduled(now, REFRESH_DAY, REFRESH_HOUR)
    return _hours_since(row["last"] if row else None) > _hours_ago(now, scheduled)


def _digest_due(now: datetime) -> bool:
    """Due when nothing was sent since the last scheduled digest time.

    A failed attempt does not count as sent, but is only retried every
    RETRY_HOURS so a dead API key does not hammer the transport all week.
    """
    recent = db.list_digests(limit=1)
    if not recent:
        return True
    scheduled = _last_scheduled(now, DIGEST_DAY, DIGEST_HOUR)
    age = _hours_since(recent[0]["sent_at"])
    if recent[0]["status"] in ("sent", "skipped"):
        return age > _hours_ago(now, scheduled)
    return age > RETRY_HOURS


async def run_refresh_job() -> dict:
    print(f"[scheduler] weekly refresh starting at {now_local():%Y-%m-%d %H:%M %Z}")
    result = await orchestrator.refresh_all()
    _state["last_refresh"] = db.now_iso()
    print(f"[scheduler] refresh done: {result['companies']} companies in "
          f"{result['elapsed_seconds']}s, failed: {result['failed'] or 'none'}")
    return result


async def run_digest_job() -> dict:
    print(f"[scheduler] digest starting at {now_local():%Y-%m-%d %H:%M %Z}")
    result = await digest.send()
    _state["last_digest"] = db.now_iso()
    print(f"[scheduler] digest {result['status']}: {result.get('detail') or result.get('to')}")
    return result


async def _loop():
    _state["running"] = True
    print(f"[scheduler] started. refresh {REFRESH_DAY}/{REFRESH_HOUR}:00, "
          f"digest {DIGEST_DAY}/{DIGEST_HOUR}:00, tz {TZ}")
    try:
        while True:
            try:
                now = now_local()
                if _refresh_due(now):
                    await run_refresh_job()
                elif _digest_due(now):
                    await run_digest_job()
            except Exception as e:
                # A failed week must not kill the scheduler for every week after.
                print(f"[scheduler] tick failed: {type(e).__name__}: {e}")
            await asyncio.sleep(TICK_SECONDS)
    except asyncio.CancelledError:
        _state["running"] = False
        raise


def start():
    if os.getenv("ENABLE_SCHEDULER", "1") not in ("1", "true", "True"):
        print("[scheduler] disabled via ENABLE_SCHEDULER")
        return None
    if _state["task"] and not _state["task"].done():
        return _state["task"]
    _state["task"] = asyncio.create_task(_loop())
    return _state["task"]


def stop():
    task = _state.get("task")
    if task and not task.done():
        task.cancel()
    _state["task"] = None
    _state["running"] = False


def _next_run(day: int, hour: int) -> str:
    now = now_local()
    ahead = (day - now.weekday()) % 7
    candidate = (now + timedelta(days=ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate.strftime("%Y-%m-%d %H:%M %Z")


def status() -> dict:
    return {
        "running": _state["running"],
        "timezone": str(TZ),
        "now_local": now_local().strftime("%Y-%m-%d %H:%M %Z"),
        "next_refresh": _next_run(REFRESH_DAY, REFRESH_HOUR),
        "next_digest": _next_run(DIGEST_DAY, DIGEST_HOUR),
        "last_refresh": _state["last_refresh"],
        "last_digest": _state["last_digest"],
    }
