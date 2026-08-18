"""Integration tests for the weekly job diff (fetchers/careers.py `fetch`).

This is the highest-value file in the suite. The diff is the one piece of logic
that is genuinely stateful -- its output depends on what the database already
knows -- and it has two guards pointing in opposite directions that only
misbehave in situations you cannot conveniently create by hand: a company's very
first crawl, and an ATS having a bad morning.

Two collaborators, faked two different ways:

  the network   httpx.MockTransport, injected through the `client` argument the
                function already accepts. No patching.
  the database  not faked at all. A real SQLite file in tmp. The behaviour under
                test is "what happens on the second run given the first", and
                that state has to be real for the test to mean anything.

Every test here is `async def` with @pytest.mark.asyncio, because `fetch` is a
coroutine. pytest-asyncio supplies the event loop; the marker is what tells it
to. With `asyncio_mode = strict` in pytest.ini, forgetting the marker makes the
test error rather than silently pass without ever running -- which is why strict
mode is worth the extra line.
"""

import httpx
import pytest

from conftest import json_route, mock_http_client
from fetchers import careers
from models import Company


GREENHOUSE_URL = "boards-api.greenhouse.io"


def board(*jobs):
    """A Greenhouse board payload containing the given jobs."""
    return {"jobs": list(jobs)}


def job(job_id, title="Software Engineer", location="San Francisco, CA"):
    return {
        "id": job_id,
        "title": title,
        "company_name": "Example Co",
        "location": {"name": location},
        "departments": [{"name": "Engineering"}],
        "absolute_url": f"https://boards.greenhouse.io/example/jobs/{job_id}",
    }


@pytest.fixture
def tracked_company(temp_db):
    """A company with a Greenhouse board configured."""
    company_id = temp_db.create_company("Example Co", website="https://example.com")
    return Company(id=company_id, name="Example Co", website="https://example.com",
                   ats_platform="greenhouse", ats_slug="example")


async def crawl(company, payload):
    """Run one weekly diff against a board that returns `payload`.

    A helper, not a fixture, because each test needs to run it more than once
    with different payloads -- that is the whole point. Wrapping the three lines
    of client setup here keeps each test down to "week one looked like this,
    week two looked like that, assert on the difference", which is the sentence
    the test is trying to say.
    """
    async with mock_http_client(json_route({GREENHOUSE_URL: payload})) as client:
        return await careers.fetch(company, client)


# --------------------------------------------------------------------------
# The first crawl
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_first_crawl_records_the_board_without_announcing_it(
    temp_db, tracked_company
):
    """262 roles are not 262 pieces of news.

    The single most important test in this file. The first time we look at a
    company, every role on the board is technically unseen, and the naive diff
    emits a job_new for all of them -- producing a first digest that says "262
    new roles this week" and is worthless.

    The assertions say all three halves of the fix: no signals came out, the
    jobs *were* recorded, and the return value flags it as a baseline so the
    orchestrator can tell this apart from a genuinely quiet week.
    """
    result = await crawl(tracked_company, board(job(1), job(2), job(3)))

    assert result["signals"] == []
    assert result["opened"] == 0
    assert result["baseline"] == 3
    assert len(temp_db.get_open_jobs(tracked_company.id)) == 3
    assert result["headcount_total"] == 3


# --------------------------------------------------------------------------
# The steady state
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unchanged_board_produces_no_signals(temp_db, tracked_company):
    """Week two with the same three roles is not news.

    The boring case, and the one that decides whether the weekly email is worth
    opening. If this ever regresses, every company reports its entire board
    every Monday.
    """
    await crawl(tracked_company, board(job(1), job(2), job(3)))

    result = await crawl(tracked_company, board(job(1), job(2), job(3)))

    assert result["signals"] == []
    assert (result["opened"], result["closed"]) == (0, 0)


@pytest.mark.asyncio
async def test_a_new_role_in_week_two_emits_one_job_new_signal(temp_db, tracked_company):
    """Now that a baseline exists, a genuinely new req is genuinely new.

    Note how the classification travels: is_early_career and is_nyc are computed
    here and written into the signal's `raw`, and flags.py reads them back out
    of exactly that field to decide whether to light hiring_shift. This test
    pins down that contract from the producing end; test_flags.py pins down the
    consuming end. Between them the seam is covered from both sides, which is
    what stops a rename in one file from quietly breaking the other.
    """
    await crawl(tracked_company, board(job(1)))

    result = await crawl(tracked_company, board(
        job(1),
        job(2, title="Software Engineer, New Grad", location="New York, NY"),
    ))

    assert len(result["signals"]) == 1
    signal = result["signals"][0]
    assert signal.type == "job_new"
    assert signal.title == "Software Engineer, New Grad"
    assert signal.raw["is_early_career"] is True
    assert signal.raw["is_nyc"] is True
    assert signal.raw["external_id"] == "2"


@pytest.mark.asyncio
async def test_a_role_disappearing_closes_it_and_emits_job_closed(
    temp_db, tracked_company
):
    """A req that is no longer on the board has been filled or pulled."""
    await crawl(tracked_company, board(job(1), job(2)))

    result = await crawl(tracked_company, board(job(1)))

    assert result["closed"] == 1
    assert [s.type for s in result["signals"]] == ["job_closed"]
    assert result["headcount_total"] == 1
    # Closed in the database too, not just in the return value.
    assert [j["external_id"] for j in temp_db.get_open_jobs(tracked_company.id)] == ["1"]


@pytest.mark.asyncio
async def test_a_closed_job_signal_still_has_a_url_to_dedupe_on(temp_db, tracked_company):
    """A closed role's URL may be gone, so the signal synthesises one.

    insert_signals drops any signal without a URL, and dedupes on it. A
    job_closed signal built from a row whose `url` was never populated would
    vanish silently. Hence the `job:{company}:{external_id}` fallback -- an
    implementation detail worth locking down precisely because its absence
    causes a silent no-op rather than an error.
    """
    # Job 9 has no absolute_url, so its jobs-table row has an empty url. Job 1
    # is here only so that week two is not an *empty* board, which would trip
    # the mass-closure guard instead and close nothing at all.
    await crawl(tracked_company, board({"id": 9, "title": "Engineer"}, job(1)))

    result = await crawl(tracked_company, board(job(1)))

    closed = result["signals"][0]
    assert closed.type == "job_closed"
    assert closed.url
    assert "9" in closed.url


# --------------------------------------------------------------------------
# The guards
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_empty_board_response_does_not_mass_close_a_known_board(
    temp_db, tracked_company
):
    """An ATS returning `{"jobs": []}` is a bad morning, not 200 layoffs.

    The other direction of the same problem as the baseline guard. Greenhouse
    does occasionally answer 200 with an empty list, and the honest reading of
    that is "every req at this company closed overnight" -- which would send an
    email announcing a mass layoff that did not happen, and destroy the job
    history needed to notice when the roles come back.

    The assertions cover both the reporting and the data: the run is *skipped*
    with a named reason, and the rows are untouched.
    """
    await crawl(tracked_company, board(job(1), job(2)))

    result = await crawl(tracked_company, board())

    assert result["skipped"] == "empty_board_response"
    assert result["signals"] == []
    assert result["closed"] == 0
    assert len(temp_db.get_open_jobs(tracked_company.id)) == 2
    assert result["headcount_total"] == 2


@pytest.mark.asyncio
async def test_an_empty_board_on_the_very_first_crawl_is_not_treated_as_broken(
    temp_db, tracked_company
):
    """A genuinely empty board on a company we have never crawled is fine.

    The complement of the test above, and the reason the guard reads
    `if not postings and existing` rather than `if not postings`. A company that
    really has no open roles must not be permanently stuck in "skipped". Pairing
    a guard test with its complement is how you check the guard is conditional
    rather than blanket.
    """
    result = await crawl(tracked_company, board())

    assert result["skipped"] is None
    assert result["headcount_total"] == 0


@pytest.mark.asyncio
async def test_a_company_with_no_ats_configured_is_skipped_cleanly(temp_db):
    """No board to diff. Return the known headcount and say why. Do not raise.

    Not every one of the 15 companies has a discoverable ATS. This is a normal
    state, not an error, and the orchestrator has to be able to run straight
    past it.
    """
    company_id = temp_db.create_company("No Board Co")
    company = Company(id=company_id, name="No Board Co", ats_platform=None, ats_slug=None)

    result = await careers.fetch(company)

    assert result["skipped"] == "no_ats_configured"
    assert result["signals"] == []


@pytest.mark.asyncio
async def test_a_failing_board_request_raises_rather_than_reporting_zero_roles(
    temp_db, tracked_company
):
    """A 500 must propagate. Swallowing it would look like an empty board.

    This test asserts that the function does the *less* convenient thing, and
    documents the division of labour: careers.py raises, and the orchestrator's
    per-fetcher exception guard decides what to do about it. If this ever
    started returning `{"signals": []}` instead, a broken ATS would be
    indistinguishable from a quiet week, and the empty-board guard above would
    never even get a chance to fire.

    `pytest.raises` as a context manager is the standard way to assert on an
    exception. The call must be the only thing inside the block -- anything else
    in there could be what raised, and the test would pass for the wrong reason.
    """
    await crawl(tracked_company, board(job(1)))

    async with mock_http_client(json_route({GREENHOUSE_URL: 500})) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await careers.fetch(tracked_company, client)

    # And nothing was written on the way to the exception.
    assert len(temp_db.get_open_jobs(tracked_company.id)) == 1


# --------------------------------------------------------------------------
# The awkward real-world case
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_reposted_role_reopens_the_original_row(temp_db, tracked_company):
    """Closed in week two, back in week three: one row, not two.

    A three-week scenario, which is the shortest story that exercises the
    `if not inserted: touch_job(...)` branch. INSERT OR IGNORE returns nothing
    when the row already exists, and without the fallback the reposted role
    would be counted as opened but never actually reopened -- headcount would
    say 1, the board would say 1, and the open-jobs table would say 0.

    Tests like this are the argument for using a real database. The bug is
    entirely inside "what does INSERT OR IGNORE return when it ignores", and no
    fake would reproduce it.
    """
    await crawl(tracked_company, board(job(1), job(2)))   # week 1: baseline
    await crawl(tracked_company, board(job(1)))           # week 2: job 2 closes

    result = await crawl(tracked_company, board(job(1), job(2)))   # week 3: it returns

    assert result["opened"] == 1
    open_jobs = temp_db.get_open_jobs(tracked_company.id)
    assert len(open_jobs) == 2
    assert temp_db.headcount(tracked_company.id) == (2, 0)
    # Still one row in the table for job 2, reopened rather than duplicated.
    assert len(temp_db.get_jobs(tracked_company.id, include_closed=True)) == 2


@pytest.mark.asyncio
async def test_an_edited_job_title_updates_in_place_without_churn(
    temp_db, tracked_company
):
    """Boards edit titles. The same external_id is the same role.

    Deduping on the title instead of the id is a tempting simplification, and it
    would make every retitled req look like one role closing and another
    opening. The external_id is the identity; this test says so.
    """
    await crawl(tracked_company, board(job(1, title="Engineer")))

    result = await crawl(tracked_company, board(
        job(1, title="Engineer, Distributed Systems", location="New York, NY")
    ))

    assert result["signals"] == []
    updated = temp_db.get_open_jobs(tracked_company.id)[0]
    assert updated["title"] == "Engineer, Distributed Systems"
    assert updated["is_nyc"] is True   # reclassified on the way through
