"""AI layer: weekly bullets and the living per-company brief.

Two prompts, both gpt-4o-mini, both capped around 800 output tokens. The framing
throughout is "someone who wants to work here", not "an investor sizing this up".

Every generated string goes through _sanitize, which strips em dashes because
the spec forbids them anywhere in generated output and models reach for them
constantly no matter how the prompt is worded.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

MODEL = "gpt-4o-mini"
MAX_TOKENS = 800
BRIEF_WINDOW_DAYS = 90

_api_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=_api_key) if _api_key else None

_DASH_RE = re.compile(r"\s*[—–]\s*")
_SPACE_RE = re.compile(r"[ \t]{2,}")

STYLE_RULES = (
    "Write plain, direct prose. No em dashes, ever. No filler, no hype adjectives "
    "(avoid revolutionary, cutting-edge, game-changing, incredible, exciting). "
    "Name specific things: products, people, numbers, role titles, dates. "
    "Never pad. If there is nothing to say, say less."
)

BRIEF_TEMPLATE = """# {company}

## What they do

## Key people

## Last 90 days

## Open roles snapshot

## Talking points

## Open questions
"""


def _sanitize(text: str) -> str:
    """Strip em and en dashes and tidy the whitespace they leave behind."""
    if not text:
        return ""
    out = _DASH_RE.sub(", ", text)
    out = out.replace("—", ",").replace("–", "-")
    out = _SPACE_RE.sub(" ", out)
    out = re.sub(r",\s*,", ",", out)
    out = re.sub(r"\s+([.,;:])", r"\1", out)
    return out.strip()


def _chat(messages: list, max_tokens: int = MAX_TOKENS, json_mode: bool = False):
    if client is None:
        raise RuntimeError("OPENAI_API_KEY is not set")
    kwargs = {"model": MODEL, "max_tokens": max_tokens, "messages": messages}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


# --------------------------------------------------------------------------
# Signal formatting
# --------------------------------------------------------------------------

_TYPE_LABELS = {
    "podcast": "Podcast appearance",
    "youtube": "YouTube",
    "blog": "Blog post",
    "changelog": "Changelog",
    "launch": "Launch",
    "press": "Press",
    "funding": "Funding",
    "job_new": "New role posted",
    "job_closed": "Role closed",
    "arxiv": "Paper",
    "reddit": "Reddit discussion",
    "appstore": "App store",
}


# Ordering matters more than it looks. A first run can produce 250 job_new
# signals, and if they go in raw they bury the one podcast appearance that is
# actually the most interesting thing that happened. High value types go first
# and job churn gets capped and summarised.
_TYPE_PRIORITY = {
    "podcast": 0, "youtube": 0, "arxiv": 1, "launch": 2, "changelog": 3,
    "funding": 4, "press": 5, "blog": 6, "reddit": 8,
    "job_new": 9, "job_closed": 9,
}
JOB_SIGNAL_CAP = 8


def _format_one(s: dict) -> str:
    label = _TYPE_LABELS.get(s.get("type"), s.get("type", "signal"))
    when = (s.get("published_at") or "")[:10]
    raw = s.get("raw") or {}
    extra = ""
    if s.get("type") in ("job_new", "job_closed"):
        bits = [raw.get("location") or "", raw.get("department") or ""]
        if raw.get("is_early_career"):
            bits.append("EARLY CAREER")
        if raw.get("is_nyc"):
            bits.append("NYC")
        extra = " [" + ", ".join(b for b in bits if b) + "]"
    elif raw.get("person"):
        extra = f" [{raw['person']}]"
    elif raw.get("source"):
        extra = f" [{raw['source']}]"
    return f"- {label} ({when}): {s.get('title', '')}{extra}"


def _format_signals(signals: list, limit: int = 40) -> str:
    if not signals:
        return "(nothing new)"

    jobs = [s for s in signals if s.get("type") in ("job_new", "job_closed")]
    other = [s for s in signals if s.get("type") not in ("job_new", "job_closed")]
    other.sort(key=lambda s: (_TYPE_PRIORITY.get(s.get("type"), 7),
                              -(len(s.get("published_at") or ""))))

    # Keep the roles that actually matter to Isa, not the first eight alphabetically.
    jobs.sort(key=lambda s: (
        not (s.get("raw") or {}).get("is_early_career"),
        not (s.get("raw") or {}).get("is_nyc"),
    ))
    shown_jobs = jobs[:JOB_SIGNAL_CAP]

    lines = [_format_one(s) for s in other[:limit]]
    if len(other) > limit:
        lines.append(f"- ... and {len(other) - limit} more")
    lines += [_format_one(s) for s in shown_jobs]
    if len(jobs) > len(shown_jobs):
        opened = sum(1 for s in jobs if s.get("type") == "job_new")
        closed = len(jobs) - opened
        lines.append(f"- Role churn overall: {opened} opened, {closed} closed "
                     f"({len(jobs) - len(shown_jobs)} not listed above)")
    return "\n".join(lines)


def _format_jobs(open_jobs: list) -> str:
    if not open_jobs:
        return "No open roles on record."
    early = [j for j in open_jobs if j.get("is_early_career")]
    nyc = [j for j in open_jobs if j.get("is_nyc")]
    lines = [f"{len(open_jobs)} open roles, {len(nyc)} in New York, "
             f"{len(early)} early career."]
    if early:
        lines.append("Early career roles:")
        lines += [f"- {j['title']} ({j.get('location') or 'location unlisted'})"
                  for j in early[:10]]
    if nyc:
        lines.append("New York roles:")
        lines += [f"- {j['title']} ({j.get('department') or 'team unlisted'})"
                  for j in nyc[:10]]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Weekly bullets
# --------------------------------------------------------------------------

QUIET_BULLET = "Quiet week. No new public activity picked up."


def generate_weekly_bullets(company_name: str, new_signals: list,
                            open_jobs: list = None) -> list:
    """Up to 3 bullets answering what a would-be employee wants to know this week.

    A genuinely quiet week returns one bullet saying so and never calls the API,
    which is both cheaper and a hard guarantee against padding.
    """
    if not new_signals:
        return [QUIET_BULLET]

    prompt = f"""Company: {company_name}

Signals picked up this week:
{_format_signals(new_signals)}

Current hiring:
{_format_jobs(open_jobs or [])}

Write up to 3 bullets answering: what would someone who wants to work at this
company want to know this week?

Priority order:
1. Team and founder public appearances (podcasts, talks, videos, papers)
2. Product launches and shipped features
3. Hiring shifts, especially early career and New York roles
4. Strategic moves: funding, partnerships, major press

Rules:
- Fewer than 3 bullets is correct when there is less than 3 bullets worth of news.
- One bullet per distinct thing. Do not split one event across bullets.
- Each bullet is one sentence, under 30 words, and names the specific thing.
- Do not restate the company name at the start of every bullet.
- {STYLE_RULES}

Return JSON: {{"bullets": ["...", "..."]}}"""

    try:
        raw = _chat(
            [{"role": "system",
              "content": "You brief a job seeker on companies she wants to work at. "
                         "Respond only with valid JSON."},
             {"role": "user", "content": prompt}],
            json_mode=True,
        )
        bullets = json.loads(raw).get("bullets", [])
        out = [_sanitize(b) for b in bullets if isinstance(b, str) and b.strip()]
        return out[:3] or [QUIET_BULLET]
    except Exception as e:
        print(f"[ai_narrator] bullets failed for {company_name}: {e}")
        # Fall back to the raw signal titles rather than losing the week entirely.
        return [_sanitize(f"{_TYPE_LABELS.get(s.get('type'), 'Signal')}: {s.get('title', '')}")
                for s in new_signals[:3]]


# --------------------------------------------------------------------------
# Brief updater
# --------------------------------------------------------------------------

def _cutoff_note() -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=BRIEF_WINDOW_DAYS)
    return cutoff.strftime("%Y-%m-%d")


def update_brief(company_name: str, existing_brief: str, signals: list,
                 open_jobs: list = None, people: list = None) -> str:
    """Return the full updated brief markdown.

    Input is the existing brief plus the week's signals; output is the whole
    document rewritten, with anything older than 90 days rolled out of the
    Last 90 days section.
    """
    people_lines = "\n".join(
        f"- {p['name']}" + (f", {p['role']}" if p.get("role") else "")
        for p in (people or [])
    ) or "(none on record)"

    base = existing_brief.strip() if existing_brief and existing_brief.strip() else \
        BRIEF_TEMPLATE.format(company=company_name)

    prompt = f"""Update the running brief for {company_name}.

Existing brief:
---
{base}
---

People on record:
{people_lines}

New signals since the last update:
{_format_signals(signals)}

Current hiring:
{_format_jobs(open_jobs or [])}

Return the complete updated brief in markdown with exactly this structure and
these headings:

# {company_name}
## What they do
## Key people
## Last 90 days
## Open roles snapshot
## Talking points
## Open questions

Section rules:
- What they do: one paragraph. Only revise it if the new signals change the picture.
- Key people: the people on record, each with what they have been publicly doing.
- Last 90 days: dated entries, newest first. Fold the new signals in. Drop anything
  dated before {_cutoff_note()}.
- Open roles snapshot: total count, then call out early career and New York roles
  by title.
- Talking points: 3 to 5, things worth raising in a coffee chat or interview.
- Open questions: things Isa should figure out or ask about. Keep the ones still
  unanswered, drop any the new signals answered.

{STYLE_RULES}
Return only the markdown, no preamble and no code fence."""

    try:
        out = _chat(
            [{"role": "system",
              "content": "You maintain interview prep briefs for a job seeker. "
                         "You return complete markdown documents, nothing else."},
             {"role": "user", "content": prompt}],
            max_tokens=MAX_TOKENS,
        )
        out = _sanitize(out)
        out = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", out.strip())
        return out if out.strip() else base
    except Exception as e:
        print(f"[ai_narrator] brief update failed for {company_name}: {e}")
        return base


def starter_brief(company_name: str) -> str:
    return BRIEF_TEMPLATE.format(company=company_name)
