"""Tests for ai_narrator.py -- testing code that calls a language model.

The obvious objection to testing this module is that its output is
nondeterministic, so there is nothing to assert. That objection is aimed at the
wrong target. **Do not test the model. Test everything you built around it.**

Around the model, this file is entirely deterministic:

  - the guard that decides whether to call the API at all
  - the cache that decides whether to call it *again*
  - the post-filter that drops bullets restating the dashboard
  - the sanitiser that strips em dashes the prompt already forbade
  - the fallback when the call fails or returns nonsense

Every one of those is a plain function of its inputs, every one of them has been
wrong at some point, and every one is testable to the character. The model's
prose is the one part nobody can assert on -- so the fake hands back a fixed
string and the tests are about what the code does with it.

The general lesson transfers: when a system has a nondeterministic component,
push the determinism to the edges and test the edges. What is left in the middle
is a vendor's problem, not yours.
"""

import json

import pytest

import ai_narrator
from conftest import make_signal, make_signal_dict


# --------------------------------------------------------------------------
# _sanitize: the house style rule, enforced in code
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        # The rule that matters: em dashes become commas, spacing tidied up.
        ("They shipped Claude Code — a CLI — this week.",
         "They shipped Claude Code, a CLI, this week."),
        # En dashes too. Models produce these in ranges.
        ("Revenue grew 2024–2025.", "Revenue grew 2024, 2025."),
        # Clean text passes through untouched, which is the property that stops
        # the sanitiser from mangling good output while fixing bad output.
        ("No dashes here.", "No dashes here."),
        # Whitespace debris left behind by the substitutions.
        ("A  double   space.", "A double space."),
        ("Trailing space before period .", "Trailing space before period."),
        ("", ""),
        (None, ""),
    ],
)
def test_sanitize_removes_dashes_and_tidies_up(raw, expected):
    """The no-em-dashes rule is a stated project convention, so it gets a test.

    The prompt already says "no em dashes, ever". This is the belt to that
    braces: models ignore it constantly, and a rule enforced only in a prompt is
    not enforced. That is the general shape -- when you cannot control a
    component, validate its output at the boundary and test the validator.
    """
    assert ai_narrator._sanitize(raw) == expected


def test_sanitize_leaves_no_em_dash_behind_whatever_the_input():
    """The invariant, stated directly rather than case by case.

    Alongside the table above, this asserts the actual guarantee: *no* em dash
    survives. The parametrized cases pin down specific formatting; this one
    would catch a fourteenth exotic input nobody thought to add to the table.
    """
    messy = "One — two – three—four–five"

    assert "—" not in ai_narrator._sanitize(messy)
    assert "–" not in ai_narrator._sanitize(messy)


# --------------------------------------------------------------------------
# _is_dashboard_fact: the backstop filter
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bullet",
    [
        "12 open roles across engineering and research.",
        "They have 3 positions open in New York.",
        "Five roles are currently open.",
        "The role is open on the careers page.",
        "Currently hiring for infrastructure and research.",
        "Is hiring for 4 new teams.",
        "Headcount grew again this month.",
        "Shipped a new API, including two early career roles.",
    ],
)
def test_bullets_that_just_read_the_dashboard_back_are_recognised(bullet):
    """Bullets restating what Isa can already see are filtered out.

    The prompt forbids these. The model writes them anyway, reliably, whenever
    the week is thin -- which is exactly when a made-up bullet is most likely to
    be the only thing in the section. This regex is the backstop, and the test
    table is a record of the phrasings that actually got through.

    When one slips past in production, the fix is to add the sentence here
    first, watch the test fail, then widen the regex. That is the cheapest bug
    workflow there is: the failing test proves you have reproduced the bug
    before you try to fix it, and it stays as the proof it will not come back.
    """
    assert ai_narrator._is_dashboard_fact(bullet) is True


@pytest.mark.parametrize(
    "bullet",
    [
        "Shipped an inference API with sub-100ms latency.",
        "The founder argued on the Latent Space podcast that agents need memory.",
        "Raised a 200 million dollar Series B led by Index.",
        "Published a paper on sparse attention.",
        "",
    ],
)
def test_real_news_survives_the_filter(bullet):
    """The false-positive half, and the one that decides if the filter is usable.

    A filter is only as good as what it lets through. "Raised a 200 million
    dollar Series B" has a number next to a noun and must not be mistaken for a
    role count -- a slightly greedier regex would eat it, and the digest would
    silently drop the single most interesting thing that happened all week.

    Any filtering logic wants both halves tested. Precision tests and recall
    tests catch opposite mistakes, and one without the other is half a test.
    """
    assert ai_narrator._is_dashboard_fact(bullet) is False


# --------------------------------------------------------------------------
# is_content / _sort_content / summarised_lines
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "signal_type, expected",
    [
        ("podcast", True), ("blog", True), ("launch", True), ("funding", True),
        # Job rows are structured data. They are reported as counts and links,
        # never narrated, so they never cost a summarisation call.
        ("job_new", False), ("job_closed", False), ("appstore", False),
    ],
)
def test_only_publishable_signal_types_are_content(signal_type, expected):
    assert ai_narrator.is_content(make_signal_dict(type=signal_type)) is expected


def test_content_is_ordered_with_spoken_formats_first():
    """The priority order the prompt then reinforces: who spoke, what shipped.

    _TYPE_PRIORITY is the codified version of "what would Isa most want to know
    before a coffee chat". Asserting the resulting order is asserting the
    editorial judgement, which is a real product decision and belongs under test
    even though it looks like a sorting detail.
    """
    mixed = [
        make_signal_dict(id=1, type="blog"),
        make_signal_dict(id=2, type="podcast"),
        make_signal_dict(id=3, type="funding"),
        make_signal_dict(id=4, type="launch"),
    ]

    ordered = ai_narrator._sort_content(mixed)

    assert [s["type"] for s in ordered] == ["podcast", "launch", "funding", "blog"]


def test_summarised_lines_falls_back_to_the_title_when_there_is_no_summary():
    """A signal with no cached summary still appears, using its headline.

    The degraded path. It has to keep working, because it is what the digest
    renders when the summarisation call failed -- precisely the moment you least
    want a second failure.
    """
    signals = [make_signal_dict(id=1, type="blog", title="Introducing Skills")]

    lines = ai_narrator.summarised_lines(signals, summaries={})

    assert [text for _, text in lines] == ["Introducing Skills"]


# --------------------------------------------------------------------------
# generate_weekly_bullets: the parts that are not the model
# --------------------------------------------------------------------------
#
# Every test below takes `temp_db` even where it seems unnecessary. _chat reads
# and writes ai_cache on every call, so a test without it would hit a database
# with no tables, raise sqlite3.OperationalError, get swallowed by the
# `except Exception` fallback -- and pass, for entirely the wrong reason. A test
# that passes by accident is worse than no test, because it is also a claim.

def test_a_week_with_no_content_never_calls_the_model(temp_db, fake_openai):
    """A quiet week costs nothing. Not a cheap call: no call.

    This is the project's economics in one test. Fifteen companies, most quiet
    in a given week, and the difference between "return a constant" and "ask the
    model to say there is nothing to say" is most of the bill.

    `fake_openai.calls == []` is the assertion that matters. The returned string
    is almost incidental -- what is being tested is a *negative*, that a
    collaborator was not used at all. Recording fakes make that assertable;
    checking the return value alone never could.
    """
    result = ai_narrator.generate_weekly_bullets("Example Co", new_signals=[])

    assert result == [ai_narrator.QUIET_BULLET]
    assert fake_openai.calls == []


def test_job_churn_alone_is_a_quiet_week(temp_db, fake_openai):
    """Roles opening and closing is not something the company published.

    Job counts are already on the dashboard and in the digest's own hiring line,
    so narrating them is duplication -- and it would spend a model call to do it.
    A board with churn but no publishing is quiet.
    """
    churn = [
        make_signal_dict(id=1, type="job_new", title="Engineer"),
        make_signal_dict(id=2, type="job_closed", title="Designer"),
    ]

    result = ai_narrator.generate_weekly_bullets("Example Co", churn)

    assert result == [ai_narrator.QUIET_BULLET]
    assert fake_openai.calls == []


def test_bullets_that_restate_the_dashboard_are_dropped_from_the_output(
    temp_db, fake_openai
):
    """End to end through the filter: the model's bad bullet never reaches Isa.

    The unit test for _is_dashboard_fact proves the regex matches. This proves
    the regex is actually *wired in* -- a distinction that matters, because
    those are two separate bugs and the first test cannot see the second.
    """
    fake_openai.queue({"bullets": [
        "Shipped a code interpreter for the API.",
        "They have 12 open roles including 3 early career positions.",
    ]})
    signals = [
        make_signal_dict(id=1, type="launch", title="Code interpreter"),
        make_signal_dict(id=2, type="blog", title="Engineering notes"),
    ]

    result = ai_narrator.generate_weekly_bullets(
        "Example Co", signals, summaries={1: "Shipped a code interpreter.",
                                          2: "Notes on their eval stack."}
    )

    assert result == ["Shipped a code interpreter for the API."]


def test_model_output_is_sanitized_on_the_way_out(temp_db, fake_openai):
    """Whatever the model returns, the em dashes are gone by the time it lands.

    Same shape as the test above: the unit test proves _sanitize works, this one
    proves generate_weekly_bullets calls it. Testing a helper and testing that a
    caller uses the helper are different tests.
    """
    fake_openai.queue({"bullets": ["They shipped Skills — a plugin format — on Tuesday."]})
    signals = [make_signal_dict(id=1, type="launch", title="Skills")]

    result = ai_narrator.generate_weekly_bullets("Example Co", signals,
                                                 summaries={1: "Skills shipped."})

    assert result == ["They shipped Skills, a plugin format, on Tuesday."]


def test_one_item_never_becomes_three_bullets(temp_db, fake_openai):
    """Asked for "up to 3", the model splits one blog post across two bullets.

    Regression test with a specific symptom: a week with a single blog post
    rendered as two bullets and read like twice as much had happened. The cap is
    `min(3, len(lines))` -- never more bullets than there were things.

    The fake returning three bullets for one input item is doing something a
    mock library could not: reproducing a real, observed model misbehaviour so
    the guard against it can be tested at all.
    """
    fake_openai.queue({"bullets": [
        "They published a post on evals.",
        "The post argues for smaller benchmarks.",
        "It also mentions their internal tooling.",
    ]})
    one_item = [make_signal_dict(id=1, type="blog", title="On evals")]

    result = ai_narrator.generate_weekly_bullets(
        "Example Co", one_item, summaries={1: "A post about eval design."}
    )

    assert len(result) == 1


def test_a_failed_model_call_falls_back_to_the_cached_summaries(temp_db, fake_openai):
    """When the API is down, the week's content still gets reported.

    The summaries were written and cached in an earlier stage, so the fallback
    is not "sorry, nothing" -- it is the same facts without the composition.
    Graceful degradation only counts if it is tested; otherwise it is a comment.

    Queueing an exception is how the fake simulates a failure. The alternative,
    `mock.side_effect = Exception`, expresses the same thing; the fake keeps it
    in one object with the call recording.
    """
    fake_openai.queue(RuntimeError("openai is down"))
    signals = [
        make_signal_dict(id=1, type="podcast", title="On the Latent Space podcast"),
        make_signal_dict(id=2, type="blog", title="Engineering notes"),
    ]
    summaries = {1: "The founder argued agents need persistent memory.",
                 2: "Notes on their evaluation stack."}

    result = ai_narrator.generate_weekly_bullets("Example Co", signals, summaries=summaries)

    assert result == [summaries[1], summaries[2]]


def test_malformed_json_from_the_model_does_not_crash_the_run(temp_db, fake_openai):
    """json_mode is a request, not a guarantee. Handle the day it is ignored.

    Third-party outputs are untrusted input. This is the same discipline you
    would apply to a JSON API you do not control, and models earn it more than
    most APIs do.
    """
    fake_openai.queue("Here are your bullets! 1. They shipped a thing.")
    signals = [make_signal_dict(id=1, type="launch", title="A thing")]

    result = ai_narrator.generate_weekly_bullets("Example Co", signals,
                                                 summaries={1: "They shipped a thing."})

    assert result == ["They shipped a thing."]


def test_the_same_week_is_never_paid_for_twice(temp_db, fake_openai):
    """The ai_cache, proven by counting calls rather than inspecting the cache.

    Two identical invocations, one API call. This is the invariant the whole
    caching layer exists for -- a repeated manual refresh, or a rerun after a
    crash, costs nothing.

    Note what is *not* asserted: nothing here reads the ai_cache table or checks
    a key. The test states the observable promise ("the vendor is not called
    twice") and stays silent on how it is kept. Swap SHA-256 for something else,
    change the table, restructure the key -- this test keeps passing, because
    none of that is the promise. Tests coupled to implementation are the ones
    that make refactoring feel expensive.
    """
    fake_openai.queue({"bullets": ["They shipped a code interpreter."]})
    signals = [make_signal_dict(id=1, type="launch", title="Code interpreter")]
    summaries = {1: "Shipped a code interpreter."}

    first = ai_narrator.generate_weekly_bullets("Example Co", signals, summaries=summaries)
    second = ai_narrator.generate_weekly_bullets("Example Co", signals, summaries=summaries)

    assert first == second
    assert len(fake_openai.calls) == 1


def test_a_different_week_is_a_different_cache_key(temp_db, fake_openai):
    """The cache must not be so eager that new content returns stale bullets.

    The complement to the test above, and the one that proves the cache is keyed
    on the prompt rather than, say, the company name. A cache with no miss path
    is indistinguishable from a bug.
    """
    fake_openai.queue({"bullets": ["Week one thing."]})
    fake_openai.queue({"bullets": ["Week two thing."]})

    week_one = [make_signal_dict(id=1, type="launch", title="First launch")]
    week_two = [make_signal_dict(id=2, type="launch", title="Second launch")]

    ai_narrator.generate_weekly_bullets("Example Co", week_one, summaries={1: "First."})
    ai_narrator.generate_weekly_bullets("Example Co", week_two, summaries={2: "Second."})

    assert len(fake_openai.calls) == 2


# --------------------------------------------------------------------------
# summarise_signals: write once, read forever
# --------------------------------------------------------------------------

def test_signals_already_summarised_are_not_sent_to_the_model_again(
    temp_db, fake_openai
):
    """Per-signal summaries are written once, ever, and read from the database.

    Set up so the assertion is unambiguous: two signals, one already summarised.
    Exactly one reaches the model, and the prompt it was sent contains the
    unsummarised headline and not the other one. Asserting on the *prompt* is
    the only way to see which signals were actually batched -- the return value
    merges cached and fresh and looks identical either way.

    This is where recording fakes earn their keep. A stub that only returns
    canned data could not answer "what was it asked?".
    """
    company_id = temp_db.create_company("Example Co")
    snapshot_id = temp_db.create_snapshot(company_id)
    rows = temp_db.insert_signals(snapshot_id, [
        make_signal(company_id=company_id, type="blog",
                    url="https://example.com/old", title="Old post"),
        make_signal(company_id=company_id, type="blog",
                    url="https://example.com/new", title="Brand new post"),
    ])
    old_id, new_id = rows[0]["id"], rows[1]["id"]
    temp_db.save_signal_summaries({old_id: "The old post argued for smaller models."})

    fake_openai.queue({"summaries": {"1": "The new post describes their eval stack."}})
    signals = [
        make_signal_dict(id=old_id, type="blog", title="Old post"),
        make_signal_dict(id=new_id, type="blog", title="Brand new post"),
    ]

    result = ai_narrator.summarise_signals("Example Co", signals)

    assert len(fake_openai.calls) == 1
    prompt = fake_openai.calls[0]["messages"][-1]["content"]
    assert "Brand new post" in prompt
    assert "Old post" not in prompt
    # Both come back: one from cache, one freshly written.
    assert result[old_id] == "The old post argued for smaller models."
    assert result[new_id] == "The new post describes their eval stack."
    # And the new one was persisted, so next week is free.
    assert temp_db.get_signal_summaries([new_id])[new_id] == \
        "The new post describes their eval stack."


def test_summarising_nothing_makes_no_call_and_returns_nothing(temp_db, fake_openai):
    """The empty guard. Nothing to summarise means no batch, no request."""
    assert ai_narrator.summarise_signals("Example Co", []) == {}
    assert fake_openai.calls == []


def test_summaries_use_the_scraped_article_body_not_the_headline(temp_db, fake_openai):
    """The whole reason content.py exists: summaries are written from the body.

    A headline like "Future(s) of Work" tells Isa nothing. The `pages` argument
    carries the scraped body text, and this test proves it reaches the prompt --
    the plumbing between the scraper and the model, which nothing else checks.
    """
    company_id = temp_db.create_company("Example Co")
    snapshot_id = temp_db.create_snapshot(company_id)
    rows = temp_db.insert_signals(snapshot_id, [
        make_signal(company_id=company_id, type="blog",
                    url="https://example.com/post", title="Future(s) of Work"),
    ])
    pages = {"https://example.com/post":
             {"text": "We are releasing an open weights model trained on 8k GPUs."}}

    fake_openai.queue({"summaries": {"1": "They released an open weights model."}})
    ai_narrator.summarise_signals(
        "Example Co",
        [make_signal_dict(id=rows[0]["id"], type="blog", title="Future(s) of Work",
                          url="https://example.com/post")],
        pages=pages,
    )

    prompt = fake_openai.calls[0]["messages"][-1]["content"]
    assert "open weights model trained on 8k GPUs" in prompt


def test_summaries_are_requested_in_json_mode(temp_db, fake_openai):
    """response_format is what makes json.loads on the reply reasonable.

    A small assertion about how the vendor is called rather than what comes
    back. Justified here because dropping the flag does not fail loudly -- it
    just makes the parse fail intermittently, on the weeks when the model
    decides to add a preamble.
    """
    company_id = temp_db.create_company("Example Co")
    snapshot_id = temp_db.create_snapshot(company_id)
    rows = temp_db.insert_signals(snapshot_id, [
        make_signal(company_id=company_id, url="https://example.com/x"),
    ])
    fake_openai.queue({"summaries": {"1": "A sentence."}})

    ai_narrator.summarise_signals(
        "Example Co", [make_signal_dict(id=rows[0]["id"], type="blog")]
    )

    assert fake_openai.calls[0]["response_format"] == {"type": "json_object"}


def test_a_summary_is_truncated_so_one_runaway_reply_cannot_bloat_the_digest(
    temp_db, fake_openai
):
    """Summaries are capped at 400 characters on the way into the database.

    Bounds on third-party output. The prompt says "under 28 words"; the code
    does not trust it. Testing the cap means testing at the boundary, so the
    fake returns something far over it rather than one character over.
    """
    company_id = temp_db.create_company("Example Co")
    snapshot_id = temp_db.create_snapshot(company_id)
    rows = temp_db.insert_signals(snapshot_id, [
        make_signal(company_id=company_id, url="https://example.com/x"),
    ])
    fake_openai.queue({"summaries": {"1": "word " * 300}})

    result = ai_narrator.summarise_signals(
        "Example Co", [make_signal_dict(id=rows[0]["id"], type="blog")]
    )

    assert len(result[rows[0]["id"]]) <= 400
