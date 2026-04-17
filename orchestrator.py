import asyncio, json
from database import get_conn
from models import SnapshotResult
from fetchers import press, jobs, appstore, product_launches, funding, reddit
from scorer import calculate_score
from ai_narrator import generate_blurb, generate_highlights
from database import (
    _get_previous_snapshot, _save_snapshot,
    save_signal_metrics, get_metrics_history,
)
from trend_calculator import extract_metrics, calculate_trends
from ai_extractor import enrich_press, enrich_funding

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
        safe("appstore", appstore.fetch(company)),
        safe("launches", product_launches.fetch(company)),
        safe("funding", funding.fetch(company)),
        safe("social", reddit.fetch(company)),
    )

    snapshot = SnapshotResult(
        company_id=company.id,
        press=results[0] or [],
        jobs=results[1],
        appstore=results[2] or [],
        launches=results[3] or [],
        funding=results[4] or [],
        social=results[5] or [],
        errors=errors
    )

    # Gather history BEFORE saving so we can compute deltas vs prior state
    prev = _get_previous_snapshot(company.id)
    metrics_history = get_metrics_history(company.id, limit=12)

    # AI enrichment: extract key points + structured funding details
    # (runs before scoring so the blurb can reference richer signal data)
    enrich_press(snapshot.press, company.name)
    enrich_funding(snapshot.funding, company.name)

    score, breakdown = calculate_score(snapshot, prev)
    blurb = generate_blurb(company.name, snapshot, score)
    highlights = generate_highlights(company.name, snapshot, score)

    # Persist snapshot (returns new snapshot_id)
    snapshot_id = _save_snapshot(snapshot, score, breakdown, blurb, highlights)

    # Extract and persist numeric metrics for this snapshot
    metrics = extract_metrics(snapshot, score)
    save_signal_metrics(
        snapshot_id=snapshot_id,
        company_id=snapshot.company_id,
        recorded_at=_now_iso(),
        metrics=metrics,
    )

    # Compute trend deltas vs prior snapshots
    trends = calculate_trends(metrics, metrics_history)

    return {"score": score, "blurb": blurb, "errors": errors, "trends": trends}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
