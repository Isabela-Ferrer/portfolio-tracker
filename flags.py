"""Signal flags, which replace the old 0-100 momentum score.

A quiet week at a company is not a bad week. Thinking Machines shipping nothing
for a month says nothing about whether Isa should want to work there, so there
is no score and nothing gets ranked down for silence. Flags only ever say "this
specific thing happened", and the dashboard sorts by how many are lit.
"""

# Reused from the VC-era tier list.
TIER_1_SOURCES = (
    "techcrunch", "forbes", "bloomberg", "reuters", "wsj", "nytimes",
    "theinformation", "ft.com", "cnbc", "wired", "theverge", "axios",
)

FLAG_ORDER = (
    "funding", "launch", "founder_appearance", "major_press",
    "hiring_shift", "early_career_open",
)

FLAG_LABELS = {
    "founder_appearance": "Founder appearance",
    "launch": "Launch",
    "hiring_shift": "Hiring shift",
    "early_career_open": "Early career open",
    "major_press": "Major press",
    "funding": "Funding",
}

FLAG_ICONS = {
    "founder_appearance": "🎙",
    "launch": "🚀",
    "hiring_shift": "📈",
    "early_career_open": "🎓",
    "major_press": "📰",
    "funding": "💰",
}

# Headcount can drift by a req or two week to week without meaning anything.
HEADCOUNT_NOISE = 3


def is_tier_one(url: str, source: str = "") -> bool:
    blob = f"{url or ''} {source or ''}".lower()
    return any(t in blob for t in TIER_1_SOURCES)


def compute_flags(new_signals: list, open_jobs: list, headcount_delta: int = 0) -> dict:
    """Flags for one company for one snapshot.

    new_signals: signals first seen in this snapshot (dicts from insert_signals)
    open_jobs:   every currently open row in the jobs table
    """
    types = [s.get("type") for s in new_signals]

    major_press = any(
        s.get("type") == "press"
        and is_tier_one(s.get("url", ""), (s.get("raw") or {}).get("source", ""))
        for s in new_signals
    )

    # A board of 130 reqs always has something opening or closing, so "any job
    # signal at all" lit this flag for every company every week and told Isa
    # nothing. A shift is a real move in the total, or a role of the kind she is
    # actually looking for appearing.
    notable_new_role = any(
        s.get("type") == "job_new"
        and ((s.get("raw") or {}).get("is_early_career")
             or (s.get("raw") or {}).get("is_nyc"))
        for s in new_signals
    )

    return {
        "founder_appearance": any(t in ("podcast", "youtube") for t in types),
        "launch": "launch" in types,
        "hiring_shift": abs(headcount_delta) >= HEADCOUNT_NOISE or notable_new_role,
        # Sticky, not weekly: an open new-grad role still matters in week three.
        "early_career_open": any(j.get("is_early_career") for j in open_jobs),
        "major_press": major_press,
        "funding": "funding" in types,
    }


def flag_count(flags: dict) -> int:
    return sum(1 for v in (flags or {}).values() if v)


def active_flags(flags: dict) -> list:
    """Lit flags in display order."""
    return [f for f in FLAG_ORDER if (flags or {}).get(f)]


def sort_key(entry: dict):
    """Dashboard default sort: flag count desc, then name."""
    return (-flag_count(entry.get("flags") or {}), (entry.get("name") or "").lower())
