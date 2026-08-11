"""Snapshot deltas.

The only numbers that trend now are open headcount and New York headcount.
Everything else is a flag, and flags are not the sort of thing that goes up or
down.
"""


def _delta(current: int, previous: int) -> dict:
    current = int(current or 0)
    previous = int(previous or 0)
    d = current - previous
    return {
        "current": current,
        "prev": previous,
        "delta": d,
        "direction": "up" if d > 0 else ("down" if d < 0 else "flat"),
    }


def headcount_trend(snapshot: dict, previous: dict) -> dict:
    """Headcount deltas for one snapshot against the one before it."""
    if not snapshot:
        return {}
    if not previous:
        return {
            "total": _delta(snapshot.get("headcount_total"), snapshot.get("headcount_total")),
            "nyc": _delta(snapshot.get("headcount_nyc"), snapshot.get("headcount_nyc")),
            "first_snapshot": True,
        }
    return {
        "total": _delta(snapshot.get("headcount_total"), previous.get("headcount_total")),
        "nyc": _delta(snapshot.get("headcount_nyc"), previous.get("headcount_nyc")),
        "prev_date": (previous.get("created_at") or "")[:10],
        "first_snapshot": False,
    }


def headcount_history(snapshots: list) -> list:
    """Oldest-first series for the dashboard sparkline."""
    return [
        {
            "date": (s.get("created_at") or "")[:10],
            "total": int(s.get("headcount_total") or 0),
            "nyc": int(s.get("headcount_nyc") or 0),
        }
        for s in reversed(snapshots or [])
    ]
