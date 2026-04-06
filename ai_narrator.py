import anthropic, os
from dotenv import load_dotenv
from models import SnapshotResult
from database import _fmt_appstore

load_dotenv()
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

def generate_blurb(company_name: str, snapshot: SnapshotResult, score: int) -> str:
    signal_summary = f"""
        Company: {company_name}
        Momentum Score: {score}/100

        Signals:
        - Press mentions in recent search: {len(snapshot.press)} articles
        - Open job postings found: {snapshot.jobs.total_count if snapshot.jobs else 'unknown'}
        - LinkedIn headcount: {snapshot.linkedin.headcount_band if snapshot.linkedin else 'unknown'}
        - App Store avg rating: {_fmt_appstore(snapshot.appstore)}
        - Recent product launches: {len(snapshot.launches)} in last 60 days
        - Funding signals detected: {len(snapshot.funding)}
        """
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": f"""You are a VC analyst assistant. Based on these public signals for a portfolio company, 
write a 2-3 sentence plain-English momentum summary. Be direct, specific, and analytical. 
Do not hedge excessively. Focus on what the signals suggest about trajectory.

{signal_summary}

Respond with only the 2-3 sentence summary, nothing else."""
        }]
    )
    return message.content[0].text.strip()