"""Unit tests for trend_calculator.py.

Small module, but it feeds two numbers straight into the dashboard and the
email, so a sign error here is visible to the user immediately and to nobody
else ever.
"""

import pytest

from trend_calculator import headcount_history, headcount_trend


def snapshot(total=0, nyc=0, created_at="2026-08-17T08:00:00"):
    """Local factory. Kept in this file because only this file needs it.

    Shared helpers belong in conftest; single-module helpers belong beside their
    tests. Hoisting everything into conftest eventually produces a 900-line file
    of fixtures nobody can safely change.
    """
    return {"headcount_total": total, "headcount_nyc": nyc, "created_at": created_at}


@pytest.mark.parametrize(
    "current, previous, expected_delta, expected_direction",
    [
        (12, 10, 2, "up"),
        (10, 12, -2, "down"),
        (10, 10, 0, "flat"),
        (0, 5, -5, "down"),      # a board emptying out
        (5, 0, 5, "up"),         # a board opening from nothing
    ],
)
def test_headcount_delta_reports_size_and_direction(
    current, previous, expected_delta, expected_direction
):
    """The delta and its direction label agree, in both directions and at zero.

    "flat" is the case worth having: `up if d > 0 else "down"` is the natural
    thing to write and it labels a zero delta as "down". This row is the only
    thing standing between that bug and the dashboard.
    """
    result = headcount_trend(snapshot(total=current), snapshot(total=previous))

    assert result["total"]["delta"] == expected_delta
    assert result["total"]["direction"] == expected_direction


def test_first_snapshot_compares_against_itself_and_says_so():
    """With no previous snapshot, the delta is zero and first_snapshot is True.

    The alternative -- treating a missing previous as 0 -- makes a company's
    first refresh announce "up 262 since last week" in the first email. The
    `first_snapshot` marker exists so the UI can say "no history yet" instead.
    """
    result = headcount_trend(snapshot(total=262, nyc=30), previous=None)

    assert result["first_snapshot"] is True
    assert result["total"] == {"current": 262, "prev": 262, "delta": 0, "direction": "flat"}
    assert result["nyc"]["delta"] == 0


def test_no_snapshot_at_all_returns_an_empty_trend():
    """A company that has never been refreshed has no trend, not a zero trend.

    An empty dict and a dict full of zeroes render very differently. This is the
    distinction the dashboard relies on to show nothing rather than "0 roles,
    flat".
    """
    assert headcount_trend(None, None) == {}
    assert headcount_trend({}, snapshot(total=5)) == {}


def test_trend_carries_the_previous_snapshot_date_for_the_ui():
    """`prev_date` is the date part only: the UI shows a day, not a timestamp."""
    result = headcount_trend(snapshot(total=10), snapshot(total=8, created_at="2026-08-10T08:30:00"))

    assert result["prev_date"] == "2026-08-10"
    assert result["first_snapshot"] is False


@pytest.mark.parametrize("missing", [None, ""])
def test_delta_treats_missing_headcounts_as_zero(missing):
    """A snapshot written before the column existed must not crash the page.

    Real-world input test. This database has been migrated across a schema
    change, so old rows genuinely do have NULLs in these columns, and the
    `int(x or 0)` in _delta is there because of that. Testing None and "" pins
    down which falsy values are tolerated.
    """
    result = headcount_trend(
        {"headcount_total": missing, "headcount_nyc": missing, "created_at": ""},
        snapshot(total=4, nyc=1),
    )

    assert result["total"] == {"current": 0, "prev": 4, "delta": -4, "direction": "down"}


def test_headcount_history_returns_oldest_first_for_the_sparkline():
    """get_snapshots returns newest-first; a chart needs oldest-first.

    The reversal is the entire function, so the test asserts on order and
    nothing else. Three points, not two: a two-element list would also pass
    against a buggy implementation that just swaps the ends.
    """
    newest_first = [
        snapshot(total=14, created_at="2026-08-17T08:00:00"),
        snapshot(total=12, created_at="2026-08-10T08:00:00"),
        snapshot(total=9, created_at="2026-08-03T08:00:00"),
    ]

    series = headcount_history(newest_first)

    assert [p["date"] for p in series] == ["2026-08-03", "2026-08-10", "2026-08-17"]
    assert [p["total"] for p in series] == [9, 12, 14]


def test_headcount_history_of_nothing_is_an_empty_series():
    """Empty in, empty out. Never None, which a template cannot iterate."""
    assert headcount_history([]) == []
    assert headcount_history(None) == []
