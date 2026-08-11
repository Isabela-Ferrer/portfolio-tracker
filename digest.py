"""The Monday morning digest.

Plain HTML, inline styles only, no external assets. Gmail strips <style> blocks
and blocks remote images, so everything is inlined and there are no images.

Rules, straight off the spec:
  - only companies with at least one flag get a section
  - companies with an open early career role always appear, with the roles listed
  - everything else collapses into one line at the bottom
  - every item links to its source, every section links to the company page
"""

import html
import os
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

import database as db
import flags as flags_mod

load_dotenv()

RESEND_ENDPOINT = "https://api.resend.com/emails"
DEFAULT_FROM = "Dream Tracker <onboarding@resend.dev>"

TYPE_LABELS = {
    "podcast": "Podcast", "youtube": "Video", "blog": "Blog", "changelog": "Changelog",
    "launch": "Launch", "press": "Press", "funding": "Funding", "arxiv": "Paper",
    "job_new": "New role", "job_closed": "Role closed", "reddit": "Reddit",
}
# Job churn is noted per company, not listed line by line. Order is priority
# order: what gets shown first, and what gets dropped when a section is capped.
LINKED_TYPES = ("funding", "podcast", "youtube", "arxiv", "launch", "changelog",
                "press", "blog")

# Gmail clips messages over roughly 102KB, and a busy week across 15 companies
# blows past that easily if every signal is listed. Cap per section and say so
# rather than silently truncating.
MAX_SIGNALS_PER_SECTION = 8


def base_url() -> str:
    return (os.getenv("APP_BASE_URL") or "http://localhost:8000").rstrip("/")


def _esc(text) -> str:
    return html.escape(str(text or ""), quote=True)


# --------------------------------------------------------------------------
# Data gathering
# --------------------------------------------------------------------------

def collect() -> dict:
    """Everything the digest needs, one row per company."""
    entries = []
    for row in db._list_companies():
        snapshot = db.get_latest_snapshot(row["id"])
        if not snapshot:
            continue
        open_jobs = db.get_open_jobs(row["id"])
        previous = db.get_previous_snapshot(row["id"], snapshot["id"])
        entries.append({
            "id": row["id"],
            "name": row["name"],
            "flags": snapshot.get("flags") or {},
            "bullets": snapshot.get("bullets") or [],
            "signals": db.get_signals_for_snapshot(snapshot["id"]),
            "early_career": [j for j in open_jobs if j["is_early_career"]],
            "headcount_total": snapshot.get("headcount_total") or 0,
            "headcount_nyc": snapshot.get("headcount_nyc") or 0,
            "prev_headcount": (previous or {}).get("headcount_total"),
            "created_at": snapshot.get("created_at") or "",
        })

    featured, quiet = [], []
    for e in entries:
        if flags_mod.flag_count(e["flags"]) > 0 or e["early_career"]:
            featured.append(e)
        else:
            quiet.append(e)

    featured.sort(key=lambda e: (-flags_mod.flag_count(e["flags"]), e["name"].lower()))
    quiet.sort(key=lambda e: e["name"].lower())
    return {"featured": featured, "quiet": quiet}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_WRAP = ("font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,"
         "sans-serif;color:#1a1a1a;line-height:1.5;")


def _flag_chips(company_flags: dict) -> str:
    lit = flags_mod.active_flags(company_flags)
    if not lit:
        return ""
    chips = "".join(
        f'<span style="display:inline-block;background:#f1f3f5;border-radius:10px;'
        f'padding:2px 8px;margin:0 6px 4px 0;font-size:12px;color:#444;">'
        f'{flags_mod.FLAG_ICONS.get(f, "")} {_esc(flags_mod.FLAG_LABELS.get(f, f))}</span>'
        for f in lit
    )
    return f'<div style="margin:6px 0 10px;">{chips}</div>'


def _headcount_line(entry: dict) -> str:
    total, nyc = entry["headcount_total"], entry["headcount_nyc"]
    prev = entry.get("prev_headcount")
    move = ""
    if prev is not None and prev != total:
        d = total - prev
        move = f' ({"+" if d > 0 else ""}{d} since last week)'
    return (f'<div style="font-size:13px;color:#666;margin:4px 0 10px;">'
            f'{total} open roles{move}, {nyc} in New York</div>')


def _signal_items(signals: list, company_id: int = None) -> str:
    linked = [s for s in signals if s.get("type") in LINKED_TYPES]
    linked.sort(key=lambda s: (LINKED_TYPES.index(s["type"]),
                               -len(s.get("published_at") or "")))
    shown, dropped = linked[:MAX_SIGNALS_PER_SECTION], linked[MAX_SIGNALS_PER_SECTION:]

    rows = []
    for s in shown:
        label = TYPE_LABELS.get(s["type"], s["type"])
        date = (s.get("published_at") or "")[:10]
        rows.append(
            f'<li style="margin:4px 0;">'
            f'<span style="color:#888;font-size:12px;">{_esc(label)}</span> '
            f'<a href="{_esc(s.get("url"))}" style="color:#1a56db;text-decoration:none;">'
            f'{_esc(s.get("title"))}</a>'
            f'<span style="color:#aaa;font-size:12px;"> {_esc(date)}</span></li>'
        )
    if not rows:
        return ""

    more = ""
    if dropped:
        link = f'{base_url()}/company.html?id={company_id}' if company_id else base_url()
        more = (f'<li style="margin:4px 0;list-style:none;color:#888;font-size:12px;">'
                f'{len(dropped)} more not shown, '
                f'<a href="{_esc(link)}" style="color:#1a56db;text-decoration:none;">'
                f'see the full feed</a></li>')
    return (f'<ul style="margin:8px 0 0;padding-left:18px;font-size:14px;">'
            f'{"".join(rows)}{more}</ul>')


def _job_churn(signals: list) -> str:
    opened = sum(1 for s in signals if s.get("type") == "job_new")
    closed = sum(1 for s in signals if s.get("type") == "job_closed")
    if not opened and not closed:
        return ""
    parts = []
    if opened:
        parts.append(f"{opened} new")
    if closed:
        parts.append(f"{closed} closed")
    return (f'<div style="font-size:13px;color:#666;margin-top:8px;">'
            f'Roles this week: {", ".join(parts)}</div>')


def _early_career_block(jobs: list) -> str:
    if not jobs:
        return ""
    items = "".join(
        f'<li style="margin:3px 0;">'
        f'<a href="{_esc(j.get("url"))}" style="color:#1a56db;text-decoration:none;">'
        f'{_esc(j["title"])}</a>'
        f'<span style="color:#888;font-size:12px;"> {_esc(j.get("location") or "")}</span></li>'
        for j in jobs[:8]
    )
    return (
        '<div style="background:#fff8e1;border-left:3px solid #f0b429;padding:8px 12px;'
        'margin:10px 0 0;">'
        '<div style="font-size:13px;font-weight:600;color:#7a5200;">Early career open</div>'
        f'<ul style="margin:6px 0 0;padding-left:18px;font-size:14px;">{items}</ul></div>'
    )


def _section(entry: dict) -> str:
    link = f'{base_url()}/company.html?id={entry["id"]}'
    bullets = "".join(
        f'<li style="margin:5px 0;">{_esc(b)}</li>' for b in entry["bullets"]
    )
    return f"""
<div style="border-top:1px solid #e5e7eb;padding:18px 0;">
  <h2 style="margin:0;font-size:17px;">
    <a href="{_esc(link)}" style="color:#111;text-decoration:none;">{_esc(entry["name"])}</a>
  </h2>
  {_flag_chips(entry["flags"])}
  <ul style="margin:8px 0 0;padding-left:18px;font-size:14px;">{bullets}</ul>
  {_headcount_line(entry)}
  {_signal_items(entry["signals"], entry["id"])}
  {_job_churn(entry["signals"])}
  {_early_career_block(entry["early_career"])}
</div>"""


def render(data: dict = None) -> tuple:
    """Return (subject, html)."""
    data = data or collect()
    featured, quiet = data["featured"], data["quiet"]
    today = datetime.now(timezone.utc).strftime("%d %b %Y")

    subject = f"Dream companies, week of {today}"
    if featured:
        top = featured[0]["name"]
        subject = f"{len(featured)} companies moved this week, starting with {top}"

    sections = "".join(_section(e) for e in featured) or (
        '<p style="font-size:14px;color:#666;">Nothing moved anywhere this week.</p>')

    quiet_line = ""
    if quiet:
        names = ", ".join(_esc(e["name"]) for e in quiet)
        quiet_line = (
            '<div style="border-top:1px solid #e5e7eb;padding:14px 0 0;font-size:13px;'
            f'color:#888;">Quiet this week: {names}</div>')

    body = f"""<div style="{_WRAP}max-width:640px;margin:0 auto;padding:24px 16px;">
  <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#999;">
    Dream companies
  </div>
  <h1 style="margin:4px 0 2px;font-size:22px;">Week of {today}</h1>
  <div style="font-size:13px;color:#888;margin-bottom:4px;">
    {len(featured)} with activity, {len(quiet)} quiet.
    <a href="{base_url()}/" style="color:#1a56db;text-decoration:none;">Open dashboard</a>
  </div>
  {sections}
  {quiet_line}
</div>"""
    return subject, body


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------

async def send(subject: str = None, body_html: str = None, to: str = None) -> dict:
    """Send via Resend. Records the attempt either way."""
    if subject is None or body_html is None:
        subject, body_html = render()

    recipient = to or os.getenv("DIGEST_EMAIL") or ""
    api_key = os.getenv("RESEND_API_KEY") or ""

    if not api_key or not recipient:
        missing = [n for n, v in (("RESEND_API_KEY", api_key),
                                  ("DIGEST_EMAIL", recipient)) if not v]
        detail = f"not sent, missing env: {', '.join(missing)}"
        digest_id = db.record_digest(subject, body_html, "skipped", detail)
        return {"status": "skipped", "detail": detail, "digest_id": digest_id}

    payload = {
        "from": os.getenv("DIGEST_FROM") or DEFAULT_FROM,
        "to": [recipient],
        "subject": subject,
        "html": body_html,
    }
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                RESEND_ENDPOINT, json=payload, timeout=30,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
            )
        if r.status_code >= 300:
            digest_id = db.record_digest(subject, body_html, "failed", r.text[:500])
            return {"status": "failed", "detail": r.text[:500], "digest_id": digest_id}
        digest_id = db.record_digest(subject, body_html, "sent", recipient)
        return {"status": "sent", "to": recipient, "digest_id": digest_id}
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        digest_id = db.record_digest(subject, body_html, "failed", detail)
        return {"status": "failed", "detail": detail, "digest_id": digest_id}
