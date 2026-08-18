"""Unit tests for flags.py.

flags.py is the ideal thing to test: pure functions, no I/O, all the product
judgement in one place. Every test here runs in microseconds and every one of
them encodes a decision someone argued about.

Two conventions used throughout this file:

**Arrange, Act, Assert.** Set up the inputs, call the thing once, then check.
Not "assert, poke, assert, poke" -- when that fails you have to work out which
call broke. One action per test means the failure line *is* the diagnosis.

**The test name is the specification.** `test_quiet_week_lights_no_flags` tells
you the rule without opening the body. When it goes red in CI the name alone is
usually enough to know whether the change was intended.
"""

import pytest

import flags
from conftest import make_job_row, make_signal_dict


# --------------------------------------------------------------------------
# is_tier_one
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url, source, expected",
    [
        # The happy path: the outlet is in the tier-1 list, via the URL.
        ("https://techcrunch.com/2026/08/01/thing", "", True),
        # ...or via the source name, when the URL is a syndication link.
        ("https://news.example.com/x", "Bloomberg", True),
        # Case must not matter. Feeds are inconsistent about it.
        ("https://WWW.REUTERS.COM/article", "", True),
        # A blog nobody has heard of is not major press.
        ("https://someones-substack.com/post", "Some Newsletter", False),
        # Neither field present. The guard clause, which is where crashes live.
        ("", "", False),
        (None, None, False),
    ],
)
def test_is_tier_one_matches_known_outlets(url, source, expected):
    """Tier-1 detection reads both the URL and the source name, case-blind.

    @pytest.mark.parametrize is the single highest-leverage thing in pytest.
    This one decorator produces six independent tests: each gets its own name in
    the output, each passes or fails alone, and a failure tells you *which row*
    broke. The alternative -- one test with a for-loop over six cases -- stops
    at the first failure and hides the other five.

    Notice the last two rows. Tests are cheapest to write for the happy path and
    most valuable for the edges, so the table always includes empty and None.
    """
    assert flags.is_tier_one(url, source) is expected


# --------------------------------------------------------------------------
# compute_flags
# --------------------------------------------------------------------------

def test_quiet_week_lights_no_flags():
    """No signals, no jobs, no headcount movement means nothing is lit.

    The zero case. It looks too trivial to write, and it is the test that
    catches "flag defaults to True" bugs, which are otherwise invisible because
    a lit flag looks like working software.
    """
    result = flags.compute_flags(new_signals=[], open_jobs=[], headcount_delta=0)

    assert result == {
        "founder_appearance": False,
        "launch": False,
        "hiring_shift": False,
        "early_career_open": False,
        "major_press": False,
        "funding": False,
    }
    # Asserting the whole dict, not six separate `is False` lines, means a
    # seventh flag added later fails this test and forces a decision about its
    # default. That is a feature.


@pytest.mark.parametrize("signal_type", ["podcast", "youtube"])
def test_founder_appearance_lights_for_spoken_formats(signal_type):
    """A podcast or a YouTube video counts as the team appearing publicly."""
    signals = [make_signal_dict(type=signal_type)]

    result = flags.compute_flags(signals, open_jobs=[])

    assert result["founder_appearance"] is True


def test_blog_post_is_not_a_founder_appearance():
    """A written post is content, but it is not an appearance.

    The negative twin of the test above. A test that only ever checks the True
    case passes just as happily against `return True`, so every "X lights the
    flag" wants a "Y does not" beside it.
    """
    result = flags.compute_flags([make_signal_dict(type="blog")], open_jobs=[])

    assert result["founder_appearance"] is False


def test_major_press_requires_a_tier_one_outlet():
    """A press signal alone is not major press; the outlet has to be tier one."""
    minor = make_signal_dict(type="press", url="https://smallblog.example/post")

    result = flags.compute_flags([minor], open_jobs=[])

    assert result["major_press"] is False


def test_major_press_lights_for_tier_one_coverage():
    """The same signal type from TechCrunch does light it."""
    major = make_signal_dict(type="press", url="https://techcrunch.com/2026/08/01/x")

    result = flags.compute_flags([major], open_jobs=[])

    assert result["major_press"] is True


def test_major_press_reads_the_outlet_out_of_raw_source():
    """When the URL is a syndication link, the outlet name in `raw` decides.

    This asserts a specific integration point: compute_flags reaches into
    `signal["raw"]["source"]`. That coupling is easy to break when the fetchers
    are refactored, and nothing else in the codebase would notice.
    """
    syndicated = make_signal_dict(
        type="press",
        url="https://finance.yahoo.example/story/123",
        raw={"source": "Reuters"},
    )

    result = flags.compute_flags([syndicated], open_jobs=[])

    assert result["major_press"] is True


# --- hiring_shift: the boundary tests --------------------------------------
#
# HEADCOUNT_NOISE = 3, and the comparison is `abs(delta) >= 3`. Off-by-one is
# the most common bug in the language, so the test cases are 2, 3 and -3: the
# last value that must not fire, the first that must, and the mirror image
# proving `abs()` is really there. Testing 0 and 50 would prove much less.

@pytest.mark.parametrize(
    "delta, expected",
    [
        (0, False),
        (2, False),    # noise, one below the threshold
        (3, True),     # the threshold itself, and `>=` means it counts
        (-3, True),    # shrinking is a shift too
        (-2, False),
        (25, True),
    ],
)
def test_hiring_shift_ignores_headcount_noise_below_the_threshold(delta, expected):
    """Headcount only counts as a shift once it moves by HEADCOUNT_NOISE."""
    result = flags.compute_flags([], open_jobs=[], headcount_delta=delta)

    assert result["hiring_shift"] is expected


def test_hiring_shift_ignores_ordinary_new_roles():
    """A board of 130 reqs always opens something. That is not news.

    Regression test. This flag used to light for every company every week, which
    made it meaningless. The rule now is that the *kind* of role matters, so an
    ordinary backend req in San Francisco changes nothing.
    """
    ordinary = make_signal_dict(
        type="job_new", raw={"is_early_career": False, "is_nyc": False}
    )

    result = flags.compute_flags([ordinary], open_jobs=[], headcount_delta=0)

    assert result["hiring_shift"] is False


@pytest.mark.parametrize("marker", ["is_early_career", "is_nyc"])
def test_hiring_shift_lights_for_a_role_isa_would_actually_apply_to(marker):
    """A new early-career or New York role is a shift on its own, no threshold.

    Parametrizing over the two markers says "these two are equivalent here"
    far more clearly than two near-identical copies of the same test would.
    """
    relevant = make_signal_dict(type="job_new", raw={marker: True})

    result = flags.compute_flags([relevant], open_jobs=[], headcount_delta=0)

    assert result["hiring_shift"] is True


# --- early_career_open: the sticky flag ------------------------------------

def test_early_career_open_reads_the_whole_open_board_not_this_week():
    """The sticky flag: an open new-grad role still counts in week three.

    Every other flag is computed from `new_signals`, meaning "this week". This
    one is computed from `open_jobs`, meaning "right now". The distinction is
    the entire point of the flag, and this test is what stops a well-meaning
    refactor from folding it in with the others.
    """
    open_board = [make_job_row(title="Software Engineer, New Grad", is_early_career=True)]

    result = flags.compute_flags(new_signals=[], open_jobs=open_board)

    assert result["early_career_open"] is True


def test_early_career_open_stays_dark_when_the_board_has_no_junior_roles():
    open_board = [make_job_row(title="Staff Engineer", is_early_career=False)]

    result = flags.compute_flags(new_signals=[], open_jobs=open_board)

    assert result["early_career_open"] is False


# --------------------------------------------------------------------------
# flag_count / active_flags / sort_key
# --------------------------------------------------------------------------

def test_flag_count_counts_only_lit_flags():
    assert flags.flag_count({"launch": True, "funding": True, "hiring_shift": False}) == 2


@pytest.mark.parametrize("empty", [None, {}])
def test_flag_count_tolerates_a_missing_flags_dict(empty):
    """A company with no snapshot yet has no flags at all.

    Defensive-input tests are worth it exactly when the caller is far away. This
    dict comes from `snapshot.get("flags")` on a company that has never been
    refreshed, and the dashboard would 500 on it.
    """
    assert flags.flag_count(empty) == 0


def test_active_flags_returns_display_order_not_dict_order():
    """Lit flags come back in FLAG_ORDER, whatever order the dict was built in.

    Testing behaviour, not implementation. The assertion is about the guaranteed
    ordering the UI depends on; it says nothing about how the function loops.
    Rewrite the internals however you like and this test still holds -- which is
    what makes it worth keeping.
    """
    unordered = {"early_career_open": True, "funding": True, "launch": True}

    assert flags.active_flags(unordered) == ["funding", "launch", "early_career_open"]


def test_active_flags_omits_dark_flags():
    assert flags.active_flags({"funding": True, "launch": False}) == ["funding"]


def test_dashboard_sorts_by_flag_count_then_name():
    """Busiest company first; ties broken alphabetically, case-insensitively.

    sort_key is only ever used as `list.sort(key=...)`, so the test uses it that
    way. Asserting on the tuple it returns would be testing the mechanism; this
    asserts the ordering, which is the thing anyone actually cares about.

    The fixture data is chosen so a wrong implementation cannot pass by luck:
    "apex" sorts before "Beacon" only if the key lowercases, and both have one
    flag only so the tie-break is genuinely exercised.
    """
    companies = [
        {"name": "Beacon", "flags": {"launch": True}},
        {"name": "apex", "flags": {"launch": True}},
        {"name": "Zenith", "flags": {"launch": True, "funding": True, "hiring_shift": True}},
    ]

    companies.sort(key=flags.sort_key)

    assert [c["name"] for c in companies] == ["Zenith", "apex", "Beacon"]


def test_flag_labels_and_icons_cover_every_flag_in_flag_order():
    """Every flag the dashboard can sort by must have a label and an icon.

    A consistency test rather than a behaviour test. It costs two lines and
    catches the specific mistake of adding a flag to FLAG_ORDER and to
    compute_flags but forgetting one of the display dicts -- which renders as a
    blank chip in the email, and which no other test would notice.
    """
    assert set(flags.FLAG_ORDER) == set(flags.FLAG_LABELS)
    assert set(flags.FLAG_ORDER) == set(flags.FLAG_ICONS)


def test_compute_flags_returns_exactly_the_flags_in_flag_order():
    """The computed dict and the display order must not drift apart."""
    computed = flags.compute_flags([], [])

    assert set(computed) == set(flags.FLAG_ORDER)
