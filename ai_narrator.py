import json
import os
from openai import OpenAI
from dotenv import load_dotenv
from models import SnapshotResult
from database import _fmt_appstore

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def generate_blurb(company_name: str, snapshot: SnapshotResult, score: int) -> str:
    signal_summary = f"""
        Company: {company_name}
        Momentum Score: {score}/100

        Signals:
        - Press mentions in recent search: {len(snapshot.press)} articles
        - Open job postings found: {snapshot.jobs.total_count if snapshot.jobs else 'unknown'}
        - App Store avg rating: {_fmt_appstore(snapshot.appstore)}
        - Recent product launches: {len(snapshot.launches)} in last 60 days
        - Funding signals detected: {len(snapshot.funding)}
        """
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=150,
        messages=[
            {
                "role": "system",
                "content": "You are a VC analyst assistant. Write concise, direct momentum summaries."
            },
            {
                "role": "user",
                "content": f"""Based on these public signals for a portfolio company,
write a 2-3 sentence plain-English momentum summary. Be direct, specific, and analytical.
Do not hedge excessively. Focus on what the signals suggest about trajectory.

{signal_summary}

Respond with only the 2-3 sentence summary, nothing else."""
            }
        ]
    )
    return response.choices[0].message.content.strip()


def generate_highlights(company_name: str, snapshot: SnapshotResult, score: int) -> str:
    """
    Returns a JSON string — list of exactly 3 VC-relevant signal bullets.
    Each bullet is one concrete, specific sentence a VC analyst would act on.
    Stored in the ai_highlights column for display on the dashboard.
    """
    # --- Funding: use enriched details where available ---
    funding_lines = []
    for f in snapshot.funding[:4]:
        parts = [f.title]
        if f.amount_hint:
            parts.append(f.amount_hint)
        if f.round_type:
            parts.append(f.round_type)
        if f.investors:
            parts.append("investors: " + ", ".join(f.investors[:3]))
        if f.summary:
            parts.append(f.summary)
        funding_lines.append(" | ".join(parts))

    # --- Press: use AI-extracted key_points where available, else titles ---
    press_points = []
    for article in snapshot.press[:6]:
        if article.key_points:
            press_points.extend(article.key_points[:2])
        elif article.title:
            press_points.append(article.title)
    press_points = press_points[:8]

    # --- Reddit: surface discussion themes ---
    reddit_titles = [
        s.content for s in (snapshot.social or [])[:5]
        if s.platform == "reddit"
    ]

    context = f"""Company: {company_name}
Momentum score: {score}/100

Funding ({len(snapshot.funding)} signals):
{chr(10).join("- " + d for d in funding_lines) if funding_lines else "- None detected"}

Press ({len(snapshot.press)} articles, key points):
{chr(10).join("- " + p for p in press_points) if press_points else "- None"}

Jobs: {snapshot.jobs.total_count if snapshot.jobs else 0} open roles

App ratings: {_fmt_appstore(snapshot.appstore)}

Product launches (last 60d): {len(snapshot.launches)}

Reddit discussion ({len(snapshot.social or [])} posts):
{chr(10).join("- " + t for t in reddit_titles) if reddit_titles else "- None"}
"""

    prompt = f"""You are a senior VC analyst reviewing public signals for a portfolio company.

{context}

Return a JSON object with key "highlights" containing an array of exactly 3 strings.
Each string is one concrete bullet point — the most important things a VC analyst should know.

Prioritise in order:
1. Funding milestones: specific round, amount, investors, valuation if available
2. Hiring velocity: total openings, growth trajectory, which functions are expanding
3. Product / press traction: notable launches, tier-1 press, app rating trends
4. Reddit / community signals: what users are saying (positive or negative)
5. Risk signals: layoffs, declining metrics, negative coverage

Rules:
- Every bullet must name a specific number, investor, product, or outlet when available
- No vague generalities ("the company is growing")
- Write in present tense, third-person: "Harvey raised…", "Hiring is concentrated in…"
- If a signal category has no data, skip it and pick the next strongest signal"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=350,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a concise VC analyst. "
                        "Respond only with valid JSON: {\"highlights\": [\"...\", \"...\", \"...\"]}"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        highlights = data.get("highlights", [])
        return json.dumps(highlights[:3])
    except Exception as e:
        print(f"[ai_narrator] Highlights generation failed for {company_name}: {e}")
        return json.dumps([])
