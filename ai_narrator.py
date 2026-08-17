"""AI layer: per-signal summaries, weekly bullets, and the living brief.

Three prompts, all gpt-4o-mini, and the work is arranged so nothing is ever paid
for twice:

  1. summarise_signals  One sentence per signal saying what it actually said,
                        written from the article body rather than the headline.
                        Stored in signal_summaries and never regenerated.
  2. weekly_bullets     Composes the week from those cached summaries. Its input
                        is a few hundred tokens of summary, not the articles.
  3. update_brief       Same cached summaries, and only when real content
                        arrived. Job churn alone no longer triggers a rewrite.

On top of that every call goes through an ai_cache lookup keyed on a hash of
(kind, model, prompt), so a repeated manual refresh or a rerun after a crash
costs nothing.

Every generated string goes through _sanitize, which strips em dashes because
the spec forbids them anywhere in generated output and models reach for them
constantly no matter how the prompt is worded.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from openai import OpenAI

import database as db

load_dotenv()

MODEL = "gpt-4o-mini"
MAX_TOKENS = 800
BRIEF_WINDOW_DAYS = 90

# Signals worth spending a summary on. Job rows are structured data; they are
# reported as counts and links, never narrated.
CONTENT_TYPES = ("podcast", "youtube", "blog", "changelog", "launch", "press",
                 "funding", "arxiv", "reddit")

# How many signals go into one summarisation call. Small enough that the model
# keeps each summary specific, large enough that a normal week is one call.
SUMMARY_BATCH = 12
EXCERPT_CHARS = 700

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


def _cache_key(kind: str, prompt: str) -> str:
    return hashlib.sha256(f"{kind}|{MODEL}|{prompt}".encode("utf-8")).hexdigest()


def _chat(messages: list, kind: str, max_tokens: int = MAX_TOKENS,
          json_mode: bool = False, use_cache: bool = True):
    """One model call, served from ai_cache when the same prompt has been asked.

    The cache key covers the full prompt, so any change to the signals, the
    brief or the wording produces a genuine call and everything else is free.
    """
    if client is None:
        raise RuntimeError("OPENAI_API_KEY is not set")

    payload = json.dumps(messages, sort_keys=True)
    key = _cache_key(kind, payload)
    if use_cache:
        hit = db.ai_cache_get(key)
        if hit is not None:
            return hit

    kwargs = {"model": MODEL, "max_tokens": max_tokens, "messages": messages}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    out = resp.choices[0].message.content or ""
    if use_cache and out.strip():
        db.ai_cache_put(key, kind, MODEL, out)
    return out


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

_TYPE_PRIORITY = {
    "podcast": 0, "youtube": 0, "arxiv": 1, "launch": 2, "changelog": 3,
    "funding": 4, "press": 5, "blog": 6, "reddit": 8,
    "job_new": 9, "job_closed": 9,
}


def is_content(signal: dict) -> bool:
    return signal.get("type") in CONTENT_TYPES


def _excerpt(signal: dict, pages: dict) -> str:
    """Best available body text for a signal: scraped page, then feed summary."""
    page = (pages or {}).get(signal.get("url")) or {}
    text = (page.get("text") or "").strip()
    if not text:
        raw = signal.get("raw") or {}
        text = (raw.get("summary") or raw.get("snippet") or "").strip()
    return text[:EXCERPT_CHARS]


def _sort_content(signals: list) -> list:
    return sorted(
        [s for s in signals if is_content(s)],
        key=lambda s: (_TYPE_PRIORITY.get(s.get("type"), 7),
                       -(len(s.get("published_at") or ""))),
    )


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
# Stage 1: per-signal summaries, written once and stored
# --------------------------------------------------------------------------

def _summary_prompt(company_name: str, batch: list, pages: dict) -> str:
    blocks = []
    for i, s in enumerate(batch, start=1):
        label = _TYPE_LABELS.get(s.get("type"), s.get("type", "signal"))
        when = (s.get("published_at") or "")[:10] or "date unknown"
        raw = s.get("raw") or {}
        who = raw.get("person") or ""
        excerpt = _excerpt(s, pages) or "(no body text available)"
        blocks.append(
            f"[{i}] {label} ({when})"
            + (f" featuring {who}" if who else "")
            + f"\nHeadline: {s.get('title', '')}"
            + f"\nBody: {excerpt}"
        )

    return f"""Company: {company_name}

Below are things published about or by this company. For each one, write a
single sentence saying what it actually said or announced.

{chr(10).join(blocks)}

Rules:
- Summarise the substance, not the headline. If the body text says what was
  announced, what was argued, or what was built, that is the sentence.
- Name the specific thing: the product, the number, the person, the claim.
- If the body text is missing or useless, summarise the headline and say nothing
  you cannot support.
- One sentence each, under 28 words.
- Do not mention how many roles are open. That is not what these items are.
- {STYLE_RULES}

Return JSON mapping each number to its sentence:
{{"summaries": {{"1": "...", "2": "..."}}}}"""


def summarise_signals(company_name: str, signals: list, pages: dict = None) -> dict:
    """{signal_id: one sentence} for content signals, generated once per signal.

    Anything already in signal_summaries is returned from there. Only the
    genuinely unsummarised remainder reaches the model, in batches.
    """
    content = [s for s in _sort_content(signals) if s.get("id")]
    if not content:
        return {}

    have = db.get_signal_summaries([s["id"] for s in content])
    todo = [s for s in content if not have.get(s["id"])]
    if not todo or client is None:
        return have

    produced = {}
    for start in range(0, len(todo), SUMMARY_BATCH):
        batch = todo[start:start + SUMMARY_BATCH]
        prompt = _summary_prompt(company_name, batch, pages or {})
        try:
            raw = _chat(
                [{"role": "system",
                  "content": "You summarise company news for someone deciding "
                             "where to work. Respond only with valid JSON."},
                 {"role": "user", "content": prompt}],
                kind="signal_summary", json_mode=True, max_tokens=700,
            )
            mapping = json.loads(raw).get("summaries", {}) or {}
        except Exception as e:
            print(f"[ai_narrator] summaries failed for {company_name}: {e}")
            continue

        for i, signal in enumerate(batch, start=1):
            text = mapping.get(str(i)) or mapping.get(i)
            if isinstance(text, str) and text.strip():
                produced[signal["id"]] = _sanitize(text)[:400]

    db.save_signal_summaries(produced)
    return {**have, **produced}


def summarised_lines(signals: list, summaries: dict, limit: int = 20) -> list:
    """[(signal, summary_text)] in priority order, summary falling back to title."""
    out = []
    for s in _sort_content(signals)[:limit]:
        text = summaries.get(s.get("id")) or s.get("title") or ""
        if text:
            out.append((s, text))
    return out


# --------------------------------------------------------------------------
# Stage 2: weekly bullets, composed from the cached summaries
# --------------------------------------------------------------------------

QUIET_BULLET = "Quiet week. No new public activity picked up."

# Bullets that just read the dashboard back to her. The prompt forbids these and
# this is the backstop, because the model reliably produces one anyway when the
# week is thin.
_DASHBOARD_FACT_RE = re.compile(
    r"(\b\d+\s+(open\s+)?(roles?|positions?|jobs?|openings?)\b"
    r"|\brole[s]?\s+(are|is)\s+(currently\s+)?open\b"
    r"|\bcurrently\s+hiring\s+for\b"
    r"|\b(is|are)\s+hiring\s+for\s+\d+"
    r"|\bincluding\s+\w+\s+early[\s-]career\s+(roles?|positions?)\b"
    r"|\bheadcount\b)",
    re.IGNORECASE,
)


def _is_dashboard_fact(bullet: str) -> bool:
    return bool(_DASHBOARD_FACT_RE.search(bullet or ""))


def generate_weekly_bullets(company_name: str, new_signals: list,
                            open_jobs: list = None, summaries: dict = None) -> list:
    """Up to 3 bullets on what this company actually published this week.

    A week with no content returns one bullet saying so and never calls the API.
    Job churn on its own is a quiet week: the counts are on the dashboard and in
    the digest's own hiring line, so narrating them here is duplication.
    """
    lines = summarised_lines(new_signals, summaries or {})
    if not lines:
        return [QUIET_BULLET]

    items = "\n".join(
        f"- {_TYPE_LABELS.get(s.get('type'), 'Signal')}"
        f" ({(s.get('published_at') or '')[:10] or 'date unknown'}): {text}"
        for s, text in lines
    )

    prompt = f"""Company: {company_name}

What this company published or was covered for this week, already summarised:
{items}

Write up to 3 bullets telling someone who wants to work here what happened.

Priority order:
1. What the team said publicly: podcasts, talks, videos, papers, and what they argued
2. What shipped: launches, features, changelog entries, and what they do
3. Strategic moves: funding, partnerships, major press, and what changed

Rules:
- Each bullet is about the substance of a specific item above. Say what was
  said, shipped or announced, not that it happened.
- Fewer than 3 bullets is correct when there is less than 3 bullets worth of news.
- One bullet per distinct thing. Do not split one event across bullets.
- Each bullet is one sentence, under 30 words.
- Never mention how many roles are open, how many are early career, or anything
  about headcount. She has that on the dashboard already and it is not news.
- Do not restate the company name at the start of every bullet.
- {STYLE_RULES}

Return JSON: {{"bullets": ["...", "..."]}}"""

    try:
        raw = _chat(
            [{"role": "system",
              "content": "You brief a job seeker on companies she wants to work at. "
                         "Respond only with valid JSON."},
             {"role": "user", "content": prompt}],
            kind="weekly_bullets", json_mode=True, max_tokens=400,
        )
        bullets = json.loads(raw).get("bullets", [])
        out = [_sanitize(b) for b in bullets
               if isinstance(b, str) and b.strip() and not _is_dashboard_fact(b)]
        # Never more bullets than there were things. Asked for "up to 3" on a
        # week with one blog post, the model splits that post across two bullets
        # and the section reads like twice as much happened.
        return out[:min(3, len(lines))] or [_sanitize(text) for _, text in lines[:2]]
    except Exception as e:
        print(f"[ai_narrator] bullets failed for {company_name}: {e}")
        # The cached summaries are already sentences, so falling back to them
        # loses the composition but keeps the week's actual content.
        return [_sanitize(text) for _, text in lines[:3]]


# --------------------------------------------------------------------------
# Stage 3: brief updater
# --------------------------------------------------------------------------

def _cutoff_note() -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=BRIEF_WINDOW_DAYS)
    return cutoff.strftime("%Y-%m-%d")


def update_brief(company_name: str, existing_brief: str, signals: list,
                 open_jobs: list = None, people: list = None,
                 summaries: dict = None) -> str:
    """Return the full updated brief markdown.

    Input is the existing brief plus this week's cached summaries, not the raw
    articles: the expensive reading was done once in summarise_signals.
    """
    people_lines = "\n".join(
        f"- {p['name']}" + (f", {p['role']}" if p.get("role") else "")
        for p in (people or [])
    ) or "(none on record)"

    base = existing_brief.strip() if existing_brief and existing_brief.strip() else \
        BRIEF_TEMPLATE.format(company=company_name)

    lines = summarised_lines(signals, summaries or {}, limit=30)
    new_items = "\n".join(
        f"- {_TYPE_LABELS.get(s.get('type'), 'Signal')}"
        f" ({(s.get('published_at') or '')[:10] or 'date unknown'}): {text}"
        for s, text in lines
    ) or "(no new content this week)"

    prompt = f"""Update the running brief for {company_name}.

Existing brief:
---
{base}
---

People on record:
{people_lines}

New since the last update:
{new_items}

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
- What they do: one paragraph. Only revise it if the new items change the picture.
- Key people: the people on record, each with what they have been publicly doing.
- Last 90 days: dated entries, newest first. Fold the new items in. Drop anything
  dated before {_cutoff_note()}.
- Open roles snapshot: total count, then call out early career and New York roles
  by title. This is the only section where role counts belong.
- Talking points: 3 to 5, things worth raising in a coffee chat or interview.
  Base them on what the company has actually said and shipped.
- Open questions: things Isa should figure out or ask about. Keep the ones still
  unanswered, drop any the new items answered.

{STYLE_RULES}
Return only the markdown, no preamble and no code fence."""

    try:
        out = _chat(
            [{"role": "system",
              "content": "You maintain interview prep briefs for a job seeker. "
                         "You return complete markdown documents, nothing else."},
             {"role": "user", "content": prompt}],
            kind="brief", max_tokens=MAX_TOKENS,
        )
        out = _sanitize(out)
        out = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", out.strip())
        return out if out.strip() else base
    except Exception as e:
        print(f"[ai_narrator] brief update failed for {company_name}: {e}")
        return base


def starter_brief(company_name: str) -> str:
    return BRIEF_TEMPLATE.format(company=company_name)
