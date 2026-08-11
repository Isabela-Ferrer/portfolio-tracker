"""
Batch AI enrichment for press articles and funding signals.

One GPT call per signal type — extracts key points / structured details
from whatever text we managed to fetch (title + article snippet).
Falls back gracefully if the API call fails.
"""

import json
import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def enrich_press(articles: list, company_name: str) -> list:
    """
    Adds `key_points` (list of strings) to each PressArticle.
    Single batch call — all articles in one request.
    """
    if not articles:
        return articles

    numbered = "\n\n".join(
        f"{i+1}. Title: {a.title}\n   Text: {a.snippet.strip()[:600] if a.snippet else '(title only)'}"
        for i, a in enumerate(articles)
    )

    prompt = f"""You are a VC analyst reviewing press coverage for {company_name}.

For each article extract 2–3 key factual bullet points. Focus on:
- What specifically happened (event, product, deal, milestone)
- Any numbers, metrics or dollar amounts mentioned
- Who was involved (investors, partners, customers)
- Why it matters for the company's trajectory

Articles:
{numbered}

Return JSON with exactly {len(articles)} results in order:
{{"results": [{{"key_points": ["point 1", "point 2"]}}, ...]}}

Each key_point must be a single, concrete sentence. Omit vague generalities."""

    try:
        resp = _client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=1000,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a concise VC analyst. Respond only with valid JSON."},
                {"role": "user", "content": prompt},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        results = data.get("results", [])
        for i, article in enumerate(articles):
            if i < len(results):
                article.key_points = results[i].get("key_points", [])
    except Exception as e:
        print(f"[ai_extractor] Press enrichment failed: {e}")

    return articles


def enrich_funding(signals: list, company_name: str) -> list:
    """
    Adds `round_type`, `investors`, `summary` (and fills `amount_hint` if missing)
    to each FundingSignal. Single batch call.
    """
    if not signals:
        return signals

    numbered = "\n\n".join(
        f"{i+1}. Title: {s.title}\n   Text: {s.snippet.strip()[:600] if s.snippet else '(title only)'}"
        for i, s in enumerate(signals)
    )

    prompt = f"""You are a VC analyst extracting funding details for {company_name}.

Signals:
{numbered}

Return JSON with exactly {len(signals)} results in order:
{{
  "results": [
    {{
      "round_type": "Series D",
      "amount": "$300M",
      "valuation": "$8.1B",
      "investors": ["Thrive Capital", "Goldman Sachs"],
      "summary": "One sentence describing the round and its significance."
    }},
    ...
  ]
}}

Use null for any field you cannot determine from the text."""

    try:
        resp = _client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=800,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a concise VC analyst. Respond only with valid JSON."},
                {"role": "user", "content": prompt},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        results = data.get("results", [])
        for i, signal in enumerate(signals):
            if i < len(results):
                r = results[i]
                signal.round_type = r.get("round_type") or ""
                signal.investors  = r.get("investors") or []
                signal.summary    = r.get("summary") or ""
                # Prefer AI-extracted amount if regex didn't catch one
                if not signal.amount_hint and r.get("amount"):
                    signal.amount_hint = r["amount"]
                # Attach valuation as a badge if present
                if r.get("valuation") and signal.amount_hint:
                    signal.amount_hint = f'{signal.amount_hint} · {r["valuation"]} val.'
    except Exception as e:
        print(f"[ai_extractor] Funding enrichment failed: {e}")

    return signals
