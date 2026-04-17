import json

TIER_1_SOURCES = ['techcrunch', 'forbes', 'bloomberg', 'reuters', 'wsj', 'nytimes']


def extract_metrics(snapshot, score: int) -> dict:
    """Pull numeric values from a live SnapshotResult for time-series storage."""
    press_count = len(snapshot.press)
    press_tier1_count = sum(
        1 for a in snapshot.press
        if any(s in (a.url or '').lower() for s in TIER_1_SOURCES)
    )
    jobs_count = snapshot.jobs.total_count if snapshot.jobs else 0

    if snapshot.appstore:
        appstore_avg_rating = round(
            sum(a.avg_rating for a in snapshot.appstore) / len(snapshot.appstore), 2
        )
        appstore_review_count = sum(a.review_count for a in snapshot.appstore)
    else:
        appstore_avg_rating = 0.0
        appstore_review_count = 0

    return {
        "press_count": press_count,
        "press_tier1_count": press_tier1_count,
        "jobs_count": jobs_count,
        "appstore_avg_rating": appstore_avg_rating,
        "appstore_review_count": appstore_review_count,
        "launches_count": len(snapshot.launches),
        "funding_count": len(snapshot.funding),
        "reddit_count": len(getattr(snapshot, 'social', []) or []),
        "momentum_score": score,
    }


def extract_metrics_from_row(row: dict) -> dict:
    """Extract metrics from a raw DB snapshot row (JSON columns). Used for backfilling."""
    def parse(col, default):
        try:
            return json.loads(row.get(col) or 'null') or default
        except (json.JSONDecodeError, TypeError):
            return default

    press    = parse('press_data', [])
    jobs     = parse('jobs_data', {})
    appstore = parse('appstore_data', [])
    launches = parse('product_launches_data', [])
    funding  = parse('funding_data', [])

    press_count = len(press)
    press_tier1_count = sum(
        1 for a in press
        if any(s in (a.get('url') or '').lower() for s in TIER_1_SOURCES)
    )
    jobs_count = (jobs.get('total_count') or 0) if isinstance(jobs, dict) else 0

    if appstore:
        ratings = [a.get('avg_rating') or 0 for a in appstore]
        appstore_avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else 0.0
        appstore_review_count = sum(a.get('review_count') or 0 for a in appstore)
    else:
        appstore_avg_rating = 0.0
        appstore_review_count = 0

    return {
        "press_count": press_count,
        "press_tier1_count": press_tier1_count,
        "jobs_count": jobs_count,
        "appstore_avg_rating": appstore_avg_rating,
        "appstore_review_count": appstore_review_count,
        "launches_count": len(launches),
        "funding_count": len(funding),
        "reddit_count": 0,
        "momentum_score": row.get('momentum_score') or 0,
    }


def _signal_delta(curr: dict, prev: dict, key: str, round_digits: int = None) -> dict:
    c = curr.get(key) or 0
    p = prev.get(key) or 0
    d = c - p
    if round_digits is not None:
        d = round(d, round_digits)
        c = round(float(c), round_digits)
        p = round(float(p), round_digits)
    return {
        "current": c,
        "prev": p,
        "delta": d,
        "direction": "up" if d > 0 else ("down" if d < 0 else "flat"),
    }


def calculate_trends(current_metrics: dict, history: list) -> dict:
    """
    Compute per-signal deltas vs the previous snapshot and multi-month lookbacks.

    history: list of metric dicts from the DB, ordered newest-first,
             NOT including the current snapshot's row.

    Returns {} when there is no prior history to compare against.
    """
    if not history:
        return {}

    prev = history[0]  # most recent previous (~1 month ago at monthly cadence)

    trends = {
        "jobs":             _signal_delta(current_metrics, prev, "jobs_count"),
        "press":            _signal_delta(current_metrics, prev, "press_count"),
        "press_tier1":      _signal_delta(current_metrics, prev, "press_tier1_count"),
        "appstore_rating":  _signal_delta(current_metrics, prev, "appstore_avg_rating", round_digits=2),
        "appstore_reviews": _signal_delta(current_metrics, prev, "appstore_review_count"),
        "launches":         _signal_delta(current_metrics, prev, "launches_count"),
        "funding":          _signal_delta(current_metrics, prev, "funding_count"),
        "reddit":           _signal_delta(current_metrics, prev, "reddit_count"),
        "momentum_score":   _signal_delta(current_metrics, prev, "momentum_score"),
        "prev_date":        (prev.get("recorded_at") or "")[:10],
        "snapshot_count":   len(history) + 1,
    }

    # ~3-month lookback (3rd most recent snapshot)
    if len(history) >= 2:
        three_mo = history[min(2, len(history) - 1)]
        trends["three_month"] = {
            "jobs":            _signal_delta(current_metrics, three_mo, "jobs_count"),
            "press":           _signal_delta(current_metrics, three_mo, "press_count"),
            "appstore_rating": _signal_delta(current_metrics, three_mo, "appstore_avg_rating", round_digits=2),
            "launches":        _signal_delta(current_metrics, three_mo, "launches_count"),
            "momentum_score":  _signal_delta(current_metrics, three_mo, "momentum_score"),
            "prev_date":       (three_mo.get("recorded_at") or "")[:10],
        }

    # ~6-month lookback (6th most recent snapshot)
    if len(history) >= 5:
        six_mo = history[min(5, len(history) - 1)]
        trends["six_month"] = {
            "jobs":            _signal_delta(current_metrics, six_mo, "jobs_count"),
            "press":           _signal_delta(current_metrics, six_mo, "press_count"),
            "appstore_rating": _signal_delta(current_metrics, six_mo, "appstore_avg_rating", round_digits=2),
            "launches":        _signal_delta(current_metrics, six_mo, "launches_count"),
            "momentum_score":  _signal_delta(current_metrics, six_mo, "momentum_score"),
            "prev_date":       (six_mo.get("recorded_at") or "")[:10],
        }

    return trends
