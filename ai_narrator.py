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
