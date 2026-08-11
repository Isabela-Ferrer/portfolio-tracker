"""In-process weekly scheduler.

Sunday 23:00 America/New_York  full refresh of all 15 companies
Monday  08:00 America/New_York  digest email

Deliberately dependency free: a one minute tick beats pulling in APScheduler for
two weekly jobs. Restart safety comes from looking at what is already in the
database rather than from in-memory state, so bouncing the server on a Monday
morning does not send the digest twice.
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
# How long after the scheduled hour a missed job is still worth running, for
# example if the laptop was closed at 23:00.
GRACE_MINUTES = 90

_state = {"running": False, "last_refresh": None, "last_digest": None, "task": None}


def now_local() -> datetime:
    return datetime.now(TZ)


def _in_window(now: datetime, day: int, hour: int) -> bool:
    if now.weekday() != day:
        return False
    scheduled = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return scheduled <= now < scheduled + timedelta(minutes=GRACE_MINUTES)


def _hours_since(iso: str) -> float:
    if not iso:
        return 1e9
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return 1e9
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def _refresh_ran_recently() -> bool:
    """Any snapshot in the last 6 hours means the weekly refresh already happened."""
    with db.get_conn() as conn:
        row = conn.execute("SELECT MAX(created_at) AS last FROM snapshots").fetchone()
    return _hours_since(row["last"] if row else None) < 6


def _digest_sent_recently() -> bool:
    recent = db.list_digests(limit=1)
    if not recent:
        return False
    if recent[0]["status"] not in ("sent", "skipped"):
        return False
    return _hours_since(recent[0]["sent_at"]) < 12


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
                if _in_window(now, REFRESH_DAY, REFRESH_HOUR) and not _refresh_ran_recently():
                    await run_refresh_job()
                elif _in_window(now, DIGEST_DAY, DIGEST_HOUR) and not _digest_sent_recently():
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
