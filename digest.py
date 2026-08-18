"""The Monday morning digest.

Plain HTML, inline styles only, no external assets. Gmail strips <style> blocks
and blocks remote images, so everything is inlined and there are no images.

Rules, straight off the spec:
  - a company gets a section when it actually published something this week
  - open early career roles are listed, but standing ones do not manufacture a
    section every week for a company that has been silent for a month
  - everything else collapses into one line at the bottom
  - every item links to its source, every section links to the company page

"This week" means signals recorded by the most recent refresh, discarding
anything that turned out to have been published long before we found it. A
first crawl picking up a July blog post in September is backlog, not news.
"""

import asyncio
import html
import os
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import httpx
from dotenv import load_dotenv

import database as db
import flags as flags_mod

load_dotenv()

RESEND_ENDPOINT = "https://api.resend.com/emails"
SHARED_RESEND_SENDER = "Dream Tracker <onboarding@resend.dev>"

# Gmail SMTP. Sending one email a week to yourself does not need the Gmail API's
# OAuth dance: an app password over SMTP does the same job with the standard
# library and no domain to verify.
SMTP_HOST = os.getenv("SMTP_HOST") or "smtp.gmail.com"
SMTP_PORT = int(os.getenv("SMTP_PORT") or 587)

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
MAX_SIGNALS_PER_SECTION = 6

# How stale a snapshot may be before its contents stop counting as this week.
SNAPSHOT_MAX_AGE_DAYS = 9
# An item discovered this week but published well before it is backlog. Undated
# items are kept: a missing date is a parsing gap far more often than age.
ITEM_MAX_AGE_DAYS = 45


def base_url() -> str:
    return (os.getenv("APP_BASE_URL") or "http://localhost:8000").rstrip("/")


def links_reachable() -> bool:
    """Is the dashboard reachable from wherever this email is being read?

    A localhost URL is dead in an inbox. Rather than ship links that go nowhere,
    the dashboard and company links are dropped and the source links, which are
    public URLs, are kept.
    """
    return not re.search(r"//(localhost|127\.0\.0\.1|0\.0\.0\.0)\b", base_url())


def _esc(text) -> str:
    return html.escape(str(text or ""), quote=True)


def _age_ok(published_at: str, max_days: int) -> bool:
    if not published_at:
        return True
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_days)) \
        .strftime("%Y-%m-%dT%H:%M:%S")
    return published_at >= cutoff


def _fresh_snapshot(snapshot: dict) -> bool:
    created = (snapshot or {}).get("created_at") or ""
    return _age_ok(created, SNAPSHOT_MAX_AGE_DAYS)


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

        # A stale snapshot means the refresh has not run; report the company as
        # quiet rather than replaying whatever it found a month ago.
        week_signals = (db.get_signals_for_snapshot(snapshot["id"])
                        if _fresh_snapshot(snapshot) else [])
        week_signals = [s for s in week_signals
                        if _age_ok(s.get("published_at") or "", ITEM_MAX_AGE_DAYS)]

        content = [s for s in week_signals if s.get("type") in LINKED_TYPES]
        summaries = db.get_signal_summaries([s.get("id") for s in content])

        open_jobs = db.get_open_jobs(row["id"])
        early = [j for j in open_jobs if j["is_early_career"]]
        new_job_ids = {(s.get("raw") or {}).get("external_id")
                       for s in week_signals if s.get("type") == "job_new"}
        previous = db.get_previous_snapshot(row["id"], snapshot["id"])

        entries.append({
            "id": row["id"],
            "name": row["name"],
            "flags": snapshot.get("flags") or {},
            "content": content,
            "summaries": summaries,
            "signals": week_signals,
            "early_career": early,
            "early_career_new": [j for j in early
                                 if j.get("external_id") in new_job_ids],
            "headcount_total": snapshot.get("headcount_total") or 0,
            "headcount_nyc": snapshot.get("headcount_nyc") or 0,
            "prev_headcount": (previous or {}).get("headcount_total"),
            "created_at": snapshot.get("created_at") or "",
        })

    # A section is earned by publishing something, not by having a standing
    # internship req. The early career roles still get reported, in their own
    # block at the bottom, so a quiet company with an open new grad role is
    # never lost.
    featured = [e for e in entries if e["content"]]
    quiet = [e for e in entries if not e["content"]]

    featured.sort(key=lambda e: (-len(e["content"]),
                                 -flags_mod.flag_count(e["flags"]),
                                 e["name"].lower()))
    quiet.sort(key=lambda e: e["name"].lower())
    return {"featured": featured, "quiet": quiet, "all": entries}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_WRAP = ("font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,"
         "sans-serif;color:#1a1a1a;line-height:1.5;")


def _company_link(entry: dict) -> str:
    return f'{base_url()}/company.html?id={entry["id"]}' if links_reachable() else ""


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


def _hiring_line(entry: dict) -> str:
    """Open roles on the board, plus this week's movement.

    This is a count of live postings, not headcount. It used to be labelled as
    headcount, which made 262 open reqs read like a company of 262 people.
    """
    total, nyc = entry["headcount_total"], entry["headcount_nyc"]
    if not total:
        return ""
    prev = entry.get("prev_headcount")
    move = ""
    if prev is not None and prev != total:
        d = total - prev
        move = f', {"up" if d > 0 else "down"} {abs(d)} since last week'
    nyc_part = f", {nyc} in New York" if nyc else ""
    return (f'<div style="font-size:13px;color:#666;margin:4px 0 10px;">'
            f'{total} open roles{nyc_part}{move}</div>')


def _signal_items(entry: dict) -> str:
    """The week's items, each with the one line saying what it actually said.

    The summary is the point of the section. A headline like "Future(s) of Work"
    tells Isa nothing; the sentence underneath it does, and it was written once
    from the article body and cached, so putting it here is free.
    """
    linked = sorted(entry["content"],
                    key=lambda s: (LINKED_TYPES.index(s["type"]),
                                   -len(s.get("published_at") or "")))
    shown = linked[:MAX_SIGNALS_PER_SECTION]
    dropped = linked[MAX_SIGNALS_PER_SECTION:]
    summaries = entry.get("summaries") or {}

    rows = []
    for s in shown:
        label = TYPE_LABELS.get(s["type"], s["type"])
        date = (s.get("published_at") or "")[:10]
        date_html = (f'<span style="color:#aaa;font-size:12px;"> {_esc(date)}</span>'
                     if date else "")
        summary = summaries.get(s.get("id")) or ""
        summary_html = (f'<div style="color:#555;font-size:13px;margin:2px 0 0;">'
                        f'{_esc(summary)}</div>') if summary else ""
        rows.append(
            f'<li style="margin:8px 0;">'
            f'<span style="color:#888;font-size:12px;">{_esc(label)}</span> '
            f'<a href="{_esc(s.get("url"))}" style="color:#1a56db;text-decoration:none;">'
            f'{_esc(s.get("title"))}</a>{date_html}{summary_html}</li>'
        )
    if not rows:
        return ""

    more = ""
    if dropped:
        link = _company_link(entry)
        tail = (f', <a href="{_esc(link)}" style="color:#1a56db;text-decoration:none;">'
                f'see the full feed</a>' if link else "")
        more = (f'<li style="margin:4px 0;list-style:none;color:#888;font-size:12px;">'
                f'{len(dropped)} more not shown{tail}</li>')
    return (f'<ul style="margin:8px 0 0;padding-left:18px;font-size:14px;">'
            f'{"".join(rows)}{more}</ul>')


def _job_churn(entry: dict) -> str:
    signals = entry["signals"]
    opened = sum(1 for s in signals if s.get("type") == "job_new")
    closed = sum(1 for s in signals if s.get("type") == "job_closed")
    if not opened and not closed:
        return ""
    parts = []
    if opened:
        parts.append(f"{opened} opened")
    if closed:
        parts.append(f"{closed} closed")
    return (f'<div style="font-size:13px;color:#666;margin-top:8px;">'
            f'Roles this week: {", ".join(parts)}</div>')


def _job_list(jobs: list, heading: str) -> str:
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
        f'<div style="font-size:13px;font-weight:600;color:#7a5200;">{_esc(heading)}</div>'
        f'<ul style="margin:6px 0 0;padding-left:18px;font-size:14px;">{items}</ul></div>'
    )


def _section(entry: dict) -> str:
    """One company's week.

    There is deliberately no separate bullets block. The snapshot bullets are a
    synthesis of exactly these items, written from exactly these summaries, so
    printing both said the same thing twice in a section that usually has one or
    two items in it. The bullets still lead the company page on the dashboard.
    """
    link = _company_link(entry)
    name = (f'<a href="{_esc(link)}" style="color:#111;text-decoration:none;">'
            f'{_esc(entry["name"])}</a>') if link else _esc(entry["name"])
    return f"""
<div style="border-top:1px solid #e5e7eb;padding:18px 0;">
  <h2 style="margin:0;font-size:17px;">{name}</h2>
  {_flag_chips(entry["flags"])}
  {_signal_items(entry)}
  {_hiring_line(entry)}
  {_job_churn(entry)}
  {_job_list(entry["early_career_new"], "Early career roles opened this week")}
</div>"""


def _early_career_roundup(entries: list) -> str:
    """Standing early career roles, once, at the bottom.

    These used to force a full section per company every week, which meant the
    same five internships were re-listed indefinitely with 'quiet week' above
    them. They are worth knowing about, once, compactly.
    """
    rows = []
    for e in sorted(entries, key=lambda e: e["name"].lower()):
        standing = [j for j in e["early_career"]
                    if j not in e["early_career_new"]]
        if not standing:
            continue
        titles = ", ".join(_esc(j["title"]) for j in standing[:3])
        extra = f" and {len(standing) - 3} more" if len(standing) > 3 else ""
        link = _company_link(e)
        name = (f'<a href="{_esc(link)}" style="color:#1a56db;text-decoration:none;">'
                f'{_esc(e["name"])}</a>') if link else f'<b>{_esc(e["name"])}</b>'
        rows.append(f'<li style="margin:4px 0;">{name}: {titles}{extra}</li>')

    if not rows:
        return ""
    return (
        '<div style="border-top:1px solid #e5e7eb;padding:14px 0 0;">'
        '<div style="font-size:13px;font-weight:600;color:#7a5200;">'
        'Early career roles still open</div>'
        f'<ul style="margin:6px 0 0;padding-left:18px;font-size:13px;color:#444;">'
        f'{"".join(rows)}</ul></div>'
    )


def _subject(featured: list, total: int) -> str:
    """Say what is actually in the email.

    The old subject counted companies with any flag lit, including the sticky
    early career one, so it announced that six companies moved in a week where
    every section said 'quiet week'.
    """
    if not featured:
        return f"Quiet week across all {total} companies"
    names = ", ".join(e["name"] for e in featured[:3])
    if len(featured) > 3:
        names += f" and {len(featured) - 3} more"
    if len(featured) == 1:
        return f"News this week from {names}"
    return f"{len(featured)} companies with news this week: {names}"


def render(data: dict = None) -> tuple:
    """Return (subject, html)."""
    data = data or collect()
    featured, quiet, everything = data["featured"], data["quiet"], data["all"]
    today = datetime.now(timezone.utc).strftime("%d %b %Y")

    subject = _subject(featured, len(everything))

    sections = "".join(_section(e) for e in featured) or (
        '<p style="font-size:14px;color:#666;">Nothing new published anywhere '
        'this week.</p>')

    quiet_line = ""
    if quiet:
        names = ", ".join(_esc(e["name"]) for e in quiet)
        quiet_line = (
            '<div style="border-top:1px solid #e5e7eb;padding:14px 0 0;font-size:13px;'
            f'color:#888;">Quiet this week: {names}</div>')

    dashboard = ""
    if links_reachable():
        dashboard = (f'<a href="{base_url()}/" style="color:#1a56db;'
                     f'text-decoration:none;">Open dashboard</a>')

    body = f"""<div style="{_WRAP}max-width:640px;margin:0 auto;padding:24px 16px;">
  <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#999;">
    Dream companies
  </div>
  <h1 style="margin:4px 0 2px;font-size:22px;">Week of {today}</h1>
  <div style="font-size:13px;color:#888;margin-bottom:4px;">
    {len(featured)} with news, {len(quiet)} quiet. {dashboard}
  </div>
  {sections}
  {_early_career_roundup(everything)}
  {quiet_line}
</div>"""
    return subject, body


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------

def _plain_text(subject: str, body_html: str) -> str:
    """Rough text alternative. Clients that refuse HTML still get something."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body_html, flags=re.S | re.I)
    text = re.sub(r"</(p|div|li|h1|h2|tr)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    out = f"{subject}\n\n{text.strip()}"
    if links_reachable():
        out += f"\n\nOpen the dashboard: {base_url()}/"
    return out


def _send_via_smtp(subject: str, body_html: str, recipient: str,
                   user: str, password: str, sender: str) -> None:
    """Blocking SMTP send. Raises on failure so the caller can record it."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(_plain_text(subject, body_html))
    message.add_alternative(body_html, subtype="html")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(user, password)
        server.send_message(message)


def _warnings() -> list:
    """Configuration that will make the email worse, surfaced with the result."""
    out = []
    if not links_reachable():
        out.append(
            f"APP_BASE_URL is {base_url()}, which is not reachable from an inbox, "
            f"so dashboard and company links were left out. Set it to a public "
            f"URL to get them back.")
    if not (os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD")) \
            and not os.getenv("DIGEST_FROM"):
        out.append(
            f"Sending as {SHARED_RESEND_SENDER}, Resend's shared test sender. It "
            f"can only deliver to your own address. Set DIGEST_FROM to an address "
            f"on a domain you verified at resend.com/domains, or set SMTP_USER "
            f"and SMTP_PASSWORD to send from Gmail instead.")
    return out


async def send(subject: str = None, body_html: str = None, to: str = None) -> dict:
    """Send the digest, then record the attempt either way.

    Transport is chosen by whichever credentials are present. Gmail SMTP wins
    when set, because sending from your own address avoids Resend's domain
    verification entirely.
    """
    if subject is None or body_html is None:
        subject, body_html = render()

    recipient = to or os.getenv("DIGEST_EMAIL") or ""
    smtp_user = os.getenv("SMTP_USER") or ""
    smtp_password = os.getenv("SMTP_PASSWORD") or ""
    api_key = os.getenv("RESEND_API_KEY") or ""
    warnings = _warnings()

    if not recipient:
        detail = "not sent, missing env: DIGEST_EMAIL"
        digest_id = db.record_digest(subject, body_html, "skipped", detail)
        return {"status": "skipped", "detail": detail, "digest_id": digest_id,
                "warnings": warnings}

    if not (smtp_user and smtp_password) and not api_key:
        detail = ("not sent, no transport configured. Set SMTP_USER and "
                  "SMTP_PASSWORD for Gmail, or RESEND_API_KEY for Resend")
        digest_id = db.record_digest(subject, body_html, "skipped", detail)
        return {"status": "skipped", "detail": detail, "digest_id": digest_id,
                "warnings": warnings}

    # --- Gmail (or any SMTP host) ---
    if smtp_user and smtp_password:
        sender = os.getenv("DIGEST_FROM") or smtp_user
        try:
            await asyncio.to_thread(_send_via_smtp, subject, body_html, recipient,
                                    smtp_user, smtp_password, sender)
            digest_id = db.record_digest(subject, body_html, "sent",
                                         f"{recipient} via smtp:{SMTP_HOST}")
            return {"status": "sent", "to": recipient, "transport": "smtp",
                    "digest_id": digest_id, "warnings": warnings}
        except smtplib.SMTPAuthenticationError as e:
            detail = (f"SMTP auth rejected ({e.smtp_code}). With Gmail this is "
                      f"almost always an ordinary password instead of a 16 character "
                      f"app password, or 2 step verification being off.")
            digest_id = db.record_digest(subject, body_html, "failed", detail)
            return {"status": "failed", "detail": detail, "digest_id": digest_id,
                    "warnings": warnings}
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            digest_id = db.record_digest(subject, body_html, "failed", detail)
            return {"status": "failed", "detail": detail, "digest_id": digest_id,
                    "warnings": warnings}

    # --- Resend ---
    payload = {
        "from": os.getenv("DIGEST_FROM") or SHARED_RESEND_SENDER,
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
            return {"status": "failed", "detail": r.text[:500],
                    "digest_id": digest_id, "warnings": warnings}
        digest_id = db.record_digest(subject, body_html, "sent", recipient)
        return {"status": "sent", "to": recipient, "transport": "resend",
                "digest_id": digest_id, "warnings": warnings}
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        digest_id = db.record_digest(subject, body_html, "failed", detail)
        return {"status": "failed", "detail": detail, "digest_id": digest_id,
                "warnings": warnings}
