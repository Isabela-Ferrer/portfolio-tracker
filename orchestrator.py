import asyncio, json
from database import get_conn
from models import SnapshotResult
from fetchers import press, jobs, linkedin, appstore, product_launches, funding
from scorer import calculate_score
from ai_narrator import generate_blurb
from database import _get_previous_snapshot, _save_snapshot

async def refresh_company(company) -> dict:
    errors = {}

    async def safe(name, coro):
        try:
            return await coro
        except Exception as e:
            errors[name] = str(e)
            return None

    # Run all fetchers concurrently
    results = await asyncio.gather(
        safe("press", press.fetch(company)),
        safe("jobs", jobs.fetch(company)),
        safe("linkedin", linkedin.fetch(company)),
        safe("appstore", appstore.fetch(company)),
        safe("launches", product_launches.fetch(company)),
        safe("funding", funding.fetch(company)),
    )

    snapshot = SnapshotResult(
        company_id=company.id,
        press=results[0] or [],
        jobs=results[1],
        linkedin=results[2],
        appstore=results[3] or [],
        launches=results[4] or [],
        funding=results[5] or [],
        errors=errors
    )

    # Get previous snapshot for delta calculations
    prev = _get_previous_snapshot(company.id)
    score, breakdown = calculate_score(snapshot, prev)
    blurb = generate_blurb(company.name, snapshot, score)

    # Write to DB
    _save_snapshot(snapshot, score, breakdown, blurb)
    return {"score": score, "blurb": blurb, "errors": errors}