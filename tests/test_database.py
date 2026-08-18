"""Integration tests for database.py.

A step up the test pyramid. These are still fast (a SQLite file in tmp is
sub-millisecond) but they are no longer unit tests: they exercise real SQL,
real constraints and real type coercion. That is the point. The behaviour being
tested here mostly lives *in the schema* -- a UNIQUE index, a nullable column --
and a mocked database layer would test nothing but the mock.

Every test takes the `temp_db` fixture. Requesting a fixture by name in the
signature is how pytest injects it; the fixture runs before the test and tears
down after, and each test gets a brand-new database.
"""

import pytest

from conftest import make_posting, make_signal


@pytest.fixture
def company(temp_db):
    """One company to hang everything else off.

    Fixtures compose: this one asks for `temp_db`, so anything asking for
    `company` gets both, in the right order, without saying so. Chaining
    fixtures like this is how you avoid a 20-line setup block at the top of
    every test.
    """
    return temp_db.create_company("Example Co", website="https://example.com")


# --------------------------------------------------------------------------
# Signal dedupe: the invariant the whole "this week" concept rests on
# --------------------------------------------------------------------------

def test_new_signals_come_back_with_their_row_ids(temp_db, company):
    """insert_signals returns the rows it actually inserted, with ids attached.

    The return value is not a convenience. The orchestrator computes flags from
    exactly these rows, and ai_narrator keys its summary cache on these ids, so
    "what came back" is the definition of "new this week".
    """
    snapshot_id = temp_db.create_snapshot(company)

    inserted = temp_db.insert_signals(snapshot_id, [
        make_signal(company_id=company, url="https://example.com/a"),
        make_signal(company_id=company, url="https://example.com/b"),
    ])

    assert len(inserted) == 2
    assert all(row["id"] for row in inserted)


def test_a_signal_seen_last_week_is_not_reported_again(temp_db, company):
    """The dedupe on (company_id, type, url), which is the heart of the app.

    Two snapshots, the same URL in both. The second week must report nothing.
    Without this, every unchanged item on an RSS feed is "news" again every
    Monday and the digest becomes noise within a month.

    Note the shape: the test walks two weeks of real usage rather than poking
    one function. That is what an integration test is *for* -- the bug it
    catches is not in either call, it is in the relationship between them.
    """
    week_one = temp_db.create_snapshot(company)
    temp_db.insert_signals(week_one, [
        make_signal(company_id=company, url="https://example.com/post-1")
    ])

    week_two = temp_db.create_snapshot(company)
    second_time = temp_db.insert_signals(week_two, [
        make_signal(company_id=company, url="https://example.com/post-1")
    ])

    assert second_time == []
    # And it stays filed under the week it was first seen, so the old digest
    # remains an accurate record of that week.
    assert len(temp_db.get_signals_for_snapshot(week_one)) == 1
    assert temp_db.get_signals_for_snapshot(week_two) == []


def test_the_same_url_under_a_different_type_is_a_different_signal(temp_db, company):
    """The uniqueness key is (company, type, url), not url alone.

    A YouTube URL can legitimately arrive as both `youtube` and `podcast` from
    two different fetchers. This test pins down that the key really is composite
    -- easy to "simplify" to a plain unique index on url, and the effect would be
    that whichever fetcher runs second is silently ignored.
    """
    snapshot_id = temp_db.create_snapshot(company)
    url = "https://youtube.com/watch?v=abc"

    temp_db.insert_signals(snapshot_id, [
        make_signal(company_id=company, type="youtube", url=url)
    ])
    other_type = temp_db.insert_signals(snapshot_id, [
        make_signal(company_id=company, type="podcast", url=url)
    ])

    assert len(other_type) == 1


def test_signals_without_a_url_are_dropped(temp_db, company):
    """No URL means no dedupe key and nothing to link to. Skip it, do not store it."""
    snapshot_id = temp_db.create_snapshot(company)

    inserted = temp_db.insert_signals(snapshot_id, [
        make_signal(company_id=company, url="")
    ])

    assert inserted == []


def test_an_undated_signal_stays_undated(temp_db, company):
    """A missing publish date is stored as empty, never as the time of the crawl.

    Regression test for a bug with a very specific symptom: every undated blog
    post showed up in Monday's email dated Monday, so a two-year-old page read
    as breaking news. The fix was to stop defaulting, and this test is what stops
    a future `published_at or now_iso()` from creeping back in -- which looks
    like an obvious improvement if you do not know the history.
    """
    snapshot_id = temp_db.create_snapshot(company)

    inserted = temp_db.insert_signals(snapshot_id, [
        make_signal(company_id=company, published_at="")
    ])

    assert inserted[0]["published_at"] == ""
    assert temp_db.get_signals_for_snapshot(snapshot_id)[0]["published_at"] == ""


def test_signal_raw_payload_survives_the_json_round_trip(temp_db, company):
    """`raw` is stored as JSON text and must come back as a dict.

    flags.py reads `signal["raw"]["is_early_career"]`. If this round trip ever
    returns a string, no flag lights and nothing raises. Serialisation
    boundaries deserve a test precisely because they fail quietly.
    """
    snapshot_id = temp_db.create_snapshot(company)
    payload = {"is_early_career": True, "location": "New York, NY", "score": 3}

    temp_db.insert_signals(snapshot_id, [
        make_signal(company_id=company, type="job_new", raw=payload)
    ])

    stored = temp_db.get_signals_for_snapshot(snapshot_id)[0]
    assert stored["raw"] == payload


# --------------------------------------------------------------------------
# Jobs: open, still-open, closed
# --------------------------------------------------------------------------

def test_a_job_is_open_until_it_is_closed(temp_db, company):
    """The full lifecycle in one test, because the states only mean anything
    relative to each other.

    Splitting this into three tests would need the same three-step setup in each
    and would assert less: what matters is that inserting makes it open, and
    closing takes it *out* of the open list without deleting the history.
    """
    posting = make_posting(external_id="job-1", title="Engineer")

    temp_db.insert_job(company, posting, False, False, "2026-08-01T00:00:00")
    assert len(temp_db.get_open_jobs(company)) == 1

    temp_db.close_jobs(company, ["job-1"], "2026-08-17T00:00:00")
    assert temp_db.get_open_jobs(company) == []

    # Closed, not deleted: the row is still there with a closed_at stamp.
    all_jobs = temp_db.get_jobs(company, include_closed=True)
    assert len(all_jobs) == 1
    assert all_jobs[0]["closed_at"] == "2026-08-17T00:00:00"


def test_inserting_the_same_job_twice_does_not_duplicate_it(temp_db, company):
    """UNIQUE(company_id, external_id) plus INSERT OR IGNORE.

    This is what makes the weekly diff safe to re-run. A crashed run that is
    retried, or a manual refresh clicked twice, must not double the board.
    """
    posting = make_posting(external_id="job-1")

    temp_db.insert_job(company, posting, False, False, "2026-08-01T00:00:00")
    temp_db.insert_job(company, posting, False, False, "2026-08-08T00:00:00")

    assert len(temp_db.get_open_jobs(company)) == 1


def test_touching_a_job_reopens_it_and_refreshes_its_fields(temp_db, company):
    """A req that comes back after being closed is the same req, not a new one.

    touch_job sets closed_at back to NULL. Boards do this: a role is pulled for
    a week during a hiring freeze and reposted with a tweaked title. Treating it
    as a fresh row would double-count it in headcount forever.
    """
    temp_db.insert_job(company, make_posting(external_id="job-1", title="Engineer"),
                       False, False, "2026-08-01T00:00:00")
    temp_db.close_jobs(company, ["job-1"], "2026-08-08T00:00:00")

    temp_db.touch_job(company, "job-1",
                      make_posting(external_id="job-1", title="Engineer, Infrastructure",
                                   location="New York, NY"),
                      is_early_career=False, is_nyc=True, seen_at="2026-08-15T00:00:00")

    reopened = temp_db.get_open_jobs(company)
    assert len(reopened) == 1
    assert reopened[0]["title"] == "Engineer, Infrastructure"
    assert reopened[0]["is_nyc"] is True
    assert reopened[0]["closed_at"] is None


def test_headcount_counts_open_roles_only(temp_db, company):
    """Two numbers, both restricted to open roles: total, and New York.

    Chosen so a wrong query cannot pass by coincidence: three roles, two of them
    in New York, one of those two closed. Any confusion between total/nyc or
    open/closed produces a different pair of numbers. Fixture data that makes
    every wrong answer distinguishable is worth the extra thirty seconds.
    """
    temp_db.insert_job(company, make_posting(external_id="1"), False, False, "2026-08-01T00:00:00")
    temp_db.insert_job(company, make_posting(external_id="2"), False, True, "2026-08-01T00:00:00")
    temp_db.insert_job(company, make_posting(external_id="3"), False, True, "2026-08-01T00:00:00")
    temp_db.close_jobs(company, ["3"], "2026-08-08T00:00:00")

    assert temp_db.headcount(company) == (2, 1)


def test_booleans_come_back_as_booleans_not_integers(temp_db, company):
    """SQLite has no bool type; _job_row casts on the way out.

    `1 is True` is False in Python, so anywhere the code says `if job["is_nyc"]
    is True` an int would silently fail. Worth one test at the boundary.
    """
    temp_db.insert_job(company, make_posting(external_id="1"),
                       is_early_career=True, is_nyc=False, seen_at="2026-08-01T00:00:00")

    job = temp_db.get_open_jobs(company)[0]
    assert job["is_early_career"] is True
    assert job["is_nyc"] is False


def test_has_any_jobs_distinguishes_a_first_crawl_from_an_empty_board(temp_db, company):
    """"Never looked" and "looked, found nothing open" are different states.

    This one predicate is what stops the first crawl of a 262-role board from
    emitting 262 "new role" signals. It must stay true after every role closes,
    which is the second half of the test and the half that is easy to get wrong.
    """
    assert temp_db.has_any_jobs(company) is False

    temp_db.insert_job(company, make_posting(external_id="1"), False, False, "2026-08-01T00:00:00")
    assert temp_db.has_any_jobs(company) is True

    temp_db.close_jobs(company, ["1"], "2026-08-08T00:00:00")
    assert temp_db.has_any_jobs(company) is True


# --------------------------------------------------------------------------
# The three caches: "nothing is fetched or bought twice"
# --------------------------------------------------------------------------

def test_content_cache_stores_failures_so_they_are_not_retried(temp_db):
    """A paywall is cached exactly like a successful scrape.

    Counter-intuitive and deliberate, which is why it gets a test rather than a
    comment. Caching only successes means every Monday re-requests every dead
    link forever. The `status` column is how the reader tells the two apart.
    """
    temp_db.content_cache_put("https://paywalled.example/article", "", "", "", "error")

    cached = temp_db.content_cache_get("https://paywalled.example/article")

    assert cached is not None
    assert cached["status"] == "error"


def test_content_cache_get_returns_none_for_an_unseen_url(temp_db):
    """The miss case: None, so `if cached:` in content.py takes the fetch path."""
    assert temp_db.content_cache_get("https://never-seen.example/") is None


def test_ai_cache_counts_reuses(temp_db):
    """Every read increments `hits`, which is what /api/ai-cache reports.

    The counter is the only evidence the caching layer is doing anything. Three
    reads, so the assertion cannot pass against an implementation that just sets
    the flag to 1.
    """
    temp_db.ai_cache_put("key-1", "weekly_bullets", "gpt-4o-mini", "a response")

    for _ in range(3):
        assert temp_db.ai_cache_get("key-1") == "a response"

    stats = temp_db.ai_cache_stats()
    assert stats["entries"] == 1
    assert stats["reuses"] == 3


def test_signal_summaries_are_written_once_and_read_back_by_id(temp_db, company):
    """The summaries the digest and the brief both read from."""
    snapshot_id = temp_db.create_snapshot(company)
    rows = temp_db.insert_signals(snapshot_id, [
        make_signal(company_id=company, url="https://example.com/a"),
        make_signal(company_id=company, url="https://example.com/b"),
    ])
    first, second = rows[0]["id"], rows[1]["id"]

    temp_db.save_signal_summaries({first: "They shipped a new inference API."})

    stored = temp_db.get_signal_summaries([first, second])
    assert stored == {first: "They shipped a new inference API."}
    # The unsummarised one is *absent*, not present-and-empty. summarise_signals
    # uses exactly this to decide what still needs a model call.
    assert second not in stored


def test_get_signal_summaries_of_nothing_does_not_hit_the_database(temp_db):
    """The empty-list guard, which also protects the IN (...) query from `IN ()`.

    An empty list would build `WHERE signal_id IN ()`, which is a SQL syntax
    error. Guard clauses like this are exactly what tests should cover, because
    they are the branch nobody exercises by hand.
    """
    assert temp_db.get_signal_summaries([]) == {}
    assert temp_db.get_signal_summaries([None, None]) == {}


# --------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------

def test_latest_snapshot_is_the_most_recent_one(temp_db, company):
    temp_db.create_snapshot(company)
    newest = temp_db.create_snapshot(company)

    assert temp_db.get_latest_snapshot(company)["id"] == newest


def test_previous_snapshot_is_the_one_before_a_given_id(temp_db, company):
    """How the "up 4 since last week" line gets its baseline."""
    older = temp_db.create_snapshot(company)
    newer = temp_db.create_snapshot(company)

    assert temp_db.get_previous_snapshot(company, newer)["id"] == older
    assert temp_db.get_previous_snapshot(company, older) is None


def test_finalize_snapshot_round_trips_flags_and_bullets(temp_db, company):
    """Flags and bullets are JSON columns; they must survive the trip.

    Same reasoning as the `raw` test above: this is a serialisation boundary,
    and the dashboard's whole sort order depends on `flags` coming back as a
    dict rather than the string "{'launch': true}".
    """
    snapshot_id = temp_db.create_snapshot(company)
    computed_flags = {"launch": True, "funding": False}
    bullets = ["They shipped a code interpreter.", "Two new NYC research roles."]

    temp_db.finalize_snapshot(snapshot_id, computed_flags, bullets, {}, 12, 3)

    stored = temp_db.get_latest_snapshot(company)
    assert stored["flags"] == computed_flags
    assert stored["bullets"] == bullets
    assert stored["headcount_total"] == 12
    assert stored["headcount_nyc"] == 3


def test_deleting_a_company_removes_its_children(temp_db, company):
    """ON DELETE CASCADE plus `PRAGMA foreign_keys = ON`, which is not the default.

    SQLite ignores foreign keys unless you turn them on *per connection*.
    get_conn() does; if that pragma is ever dropped, deleting a company leaves
    orphaned signals and jobs behind and nothing complains until the counts stop
    adding up. This is the test that notices.
    """
    snapshot_id = temp_db.create_snapshot(company)
    temp_db.insert_signals(snapshot_id, [make_signal(company_id=company)])
    temp_db.insert_job(company, make_posting(), False, False, "2026-08-01T00:00:00")

    temp_db.delete_company(company)

    assert temp_db._get_company_by_id(company) is None
    assert temp_db.get_open_jobs(company) == []
    assert temp_db.get_latest_snapshot(company) is None


def test_each_test_gets_a_clean_database(temp_db):
    """Proof that the isolation actually works.

    Several tests above have created companies by now. If any of that leaked,
    this fails. One cheap test for a property the entire suite silently assumes
    is a good trade -- and when it does fail, it fails with a much clearer
    message than the twelve unrelated tests that would otherwise break.
    """
    assert temp_db._list_companies() == []
