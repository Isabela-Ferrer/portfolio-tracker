"""End-to-end tests for the HTTP API (main.py).

The top of the test pyramid. These go in through the front door -- a real HTTP
request, real routing, real Pydantic validation, real JSON serialisation, real
database -- and they are the only tests that would catch a route registered at
the wrong path or a response shape the frontend cannot read.

They are also the slowest and the most brittle, which is why there are a dozen
of them rather than a hundred. The shape to aim for:

    many unit tests      cheap, precise, fail with an exact diagnosis
    some integration     prove the pieces are actually connected
    a few end-to-end     prove the thing runs at all

Inverting that -- testing everything through HTTP because it "tests more" -- gets
you a suite that takes ten minutes and, when it goes red, tells you only that
something somewhere is broken. Each layer is testing what only *it* can see.
"""

import pytest
from fastapi.testclient import TestClient

from conftest import make_signal
from main import app


@pytest.fixture
def client(temp_db):
    """A test client over the real app, sharing the temp database.

    Deliberately *not* `with TestClient(app) as client:`. The context-manager
    form runs the app's lifespan, which calls scheduler.start() and would leave
    a live background thread running weekly refreshes for the rest of the test
    session. Constructing the client plainly skips lifespan, so the routes are
    real and the startup side effects are not.

    Depending on `temp_db` is what points the app at the throwaway database. The
    app reads database.DB_PATH at call time, not import time, which is what
    makes that possible -- a module that cached a connection at import would
    have to be designed differently to be testable at all.
    """
    return TestClient(app)


# --------------------------------------------------------------------------
# /api/dashboard
# --------------------------------------------------------------------------

def test_dashboard_of_an_empty_database_is_empty_not_an_error(client):
    """First run, before anything is seeded. Must render, not 500.

    The empty state is the one every developer skips and every user sees first.
    The assertions also cover the static lookup tables, because the frontend
    reads flag labels and icons out of this response -- an empty `companies`
    list with no `flag_labels` would render a blank page with no error anywhere.
    """
    response = client.get("/api/dashboard")

    assert response.status_code == 200
    body = response.json()
    assert body["companies"] == []
    assert body["flag_labels"]
    assert body["flag_order"]


def test_dashboard_puts_the_busiest_company_first(client, temp_db):
    """Sorted by flag count descending, then name. The default view.

    test_flags.py already tests sort_key directly, so why again here? Because
    that test proves the function sorts, and this one proves the endpoint
    *calls* it. Those fail independently: someone can delete the `rows.sort(...)`
    line in main.py and every flags test still passes.

    That is the general answer to "isn't this duplicated coverage" -- it is only
    duplication if both tests break for the same reason.
    """
    quiet = temp_db.create_company("Aardvark Co")
    busy = temp_db.create_company("Zebra Co")
    temp_db.finalize_snapshot(temp_db.create_snapshot(quiet), {"launch": True},
                              [], {}, 5, 0)
    temp_db.finalize_snapshot(temp_db.create_snapshot(busy),
                              {"launch": True, "funding": True}, [], {}, 20, 4)

    names = [c["name"] for c in client.get("/api/dashboard").json()["companies"]]

    assert names == ["Zebra Co", "Aardvark Co"]


def test_dashboard_reports_a_company_that_has_never_been_refreshed(client, temp_db):
    """A company added a minute ago has no snapshot. It still has to render.

    This is the row that produces `None.get("flags")` if the null handling in
    main.py is wrong, and it is a completely normal state -- it exists for every
    company between "add" and "first refresh". The assertions pin the *defaults*
    the frontend relies on: zero and empty, never null.
    """
    temp_db.create_company("Brand New Co")

    company = client.get("/api/dashboard").json()["companies"][0]

    assert company["name"] == "Brand New Co"
    assert company["flags"] == {}
    assert company["active_flags"] == []
    assert company["flag_count"] == 0
    assert company["headcount_total"] == 0
    assert company["trend"] == {}
    assert company["last_refresh"] is None


def test_dashboard_counts_open_early_career_roles(client, temp_db):
    """The badge Isa actually looks at. Counts open roles only.

    Fixture chosen so a wrong query is visible: three roles, two early-career,
    one of those two closed. The right answer is 1, and "count all early career"
    (2) and "count all open" (2) both give the same wrong number -- which is
    exactly why the third role exists, to make them distinguishable from the
    correct answer even though not from each other.
    """
    from conftest import make_posting

    company_id = temp_db.create_company("Example Co")
    temp_db.insert_job(company_id, make_posting(external_id="1", title="Intern"),
                       True, False, "2026-08-01T00:00:00")
    temp_db.insert_job(company_id, make_posting(external_id="2", title="New Grad SWE"),
                       True, False, "2026-08-01T00:00:00")
    temp_db.insert_job(company_id, make_posting(external_id="3", title="Staff Engineer"),
                       False, False, "2026-08-01T00:00:00")
    temp_db.close_jobs(company_id, ["2"], "2026-08-10T00:00:00")

    company = client.get("/api/dashboard").json()["companies"][0]

    assert company["early_career_count"] == 1


# --------------------------------------------------------------------------
# /api/companies
# --------------------------------------------------------------------------

def test_getting_a_company_that_does_not_exist_returns_404(client):
    """Not a 500, and not a 200 with nulls in it.

    Status codes are part of the API contract and are trivially easy to get
    wrong -- FastAPI turns an unhandled exception into a 500, which looks like a
    server fault to every client and to every monitoring dashboard. Asserting
    the code, not just "it failed somehow", is the point.
    """
    response = client.get("/api/companies/9999")

    assert response.status_code == 404
    assert response.json()["detail"] == "Company not found"


def test_creating_a_company_with_an_explicit_ats_skips_discovery(client, temp_db):
    """Passing ats_slug means no discovery, which means no network.

    Worth reading as a testability observation as much as a test. main.py runs
    discovery only `if not body.ats_slug`, so this one field is what makes the
    endpoint reachable in a test at all. When a handler has an unavoidable
    network call on every path, the test either mocks something deep or does not
    exist -- and a small "skip if already provided" branch is usually both good
    behaviour and the seam you needed.
    """
    response = client.post("/api/companies", json={
        "name": "Example Co",
        "website": "https://example.com",
        "ats_platform": "greenhouse",
        "ats_slug": "example",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["company"]["name"] == "Example Co"
    assert body["company"]["ats_slug"] == "example"
    assert body["discovery"] is None            # not attempted
    assert temp_db.get_company_by_name("Example Co") is not None   # really persisted


def test_creating_a_company_without_a_name_is_rejected(client):
    """Pydantic validation, asserted through HTTP rather than trusted.

    422 is FastAPI's validation status. Checking it here rather than assuming it
    catches the case where someone gives the field a default and quietly makes
    it optional -- the model still validates, it just validates something else.
    """
    response = client.post("/api/companies", json={"website": "https://example.com"})

    assert response.status_code == 422


def test_the_company_detail_page_returns_everything_it_needs_in_one_call(
    client, temp_db
):
    """One request, one page. The response shape is a contract with the frontend.

    Asserting on the *keys* rather than the values. company.html reads each of
    these, and a rename that the frontend does not follow produces a page that
    is silently missing a section rather than an error anyone would notice.

    This is one of the few places where asserting on structure is right: the
    structure genuinely is the interface.
    """
    company_id = temp_db.create_company("Example Co")
    snapshot_id = temp_db.create_snapshot(company_id)
    temp_db.insert_signals(snapshot_id, [
        make_signal(company_id=company_id, type="blog", url="https://example.com/post",
                    title="A post"),
    ])
    temp_db.finalize_snapshot(snapshot_id, {"launch": True}, ["They shipped a thing."],
                              {}, 12, 3)

    body = client.get(f"/api/companies/{company_id}").json()

    assert set(body) >= {"company", "people", "sources", "brief", "snapshot",
                         "active_flags", "trend", "headcount_history", "jobs",
                         "signals", "week_signals", "snapshots"}
    assert body["company"]["name"] == "Example Co"
    assert body["active_flags"] == ["launch"]
    assert body["snapshot"]["bullets"] == ["They shipped a thing."]
    assert [s["title"] for s in body["week_signals"]] == ["A post"]


def test_deleting_a_company_removes_it_from_the_dashboard(client, temp_db):
    """A write followed by a read, through HTTP both times.

    The end-to-end shape that catches transaction and caching bugs a
    single-endpoint test cannot: it is entirely possible for DELETE to return
    200 while the row survives, and only a subsequent read notices.
    """
    company_id = temp_db.create_company("Doomed Co")

    assert client.delete(f"/api/companies/{company_id}").status_code == 200

    assert client.get("/api/dashboard").json()["companies"] == []


# --------------------------------------------------------------------------
# /api/digest/preview -- the fullest path in the app
# --------------------------------------------------------------------------

def test_digest_preview_renders_the_email_from_real_rows(client, temp_db):
    """The deepest end-to-end test here: HTTP, database, collect, render.

    Everything from a SQLite row to finished HTML, with no fakes at any layer.
    One test like this is worth having per major flow -- it is the one that
    catches the integration bugs that live *between* well-tested modules, which
    is where they mostly live.

    Note that it asserts only that the content arrived. All the detailed digest
    behaviour is already covered in test_digest.py at a hundredth of the cost;
    duplicating those assertions up here would just make them slower to run and
    harder to read.
    """
    company_id = temp_db.create_company("Anthropic")
    snapshot_id = temp_db.create_snapshot(company_id)
    rows = temp_db.insert_signals(snapshot_id, [
        make_signal(company_id=company_id, type="blog",
                    url="https://anthropic.example/skills", title="Introducing Skills",
                    published_at=temp_db.now_iso()),
    ])
    temp_db.save_signal_summaries({rows[0]["id"]: "A plugin format for agents."})
    temp_db.finalize_snapshot(snapshot_id, {"launch": True}, [], {}, 40, 6)

    response = client.get("/api/digest/preview")

    assert response.status_code == 200
    assert "Anthropic" in response.text
    assert "Introducing Skills" in response.text
    assert "A plugin format for agents." in response.text


def test_digest_preview_does_not_send_anything(client, temp_db):
    """Preview is a GET and must have no side effects.

    A GET that sends email is a bug waiting for a link prefetcher, and the
    evidence is that the digests table stays empty. Asserting the *absence* of a
    side effect is unglamorous and is how you find out that a refactor moved
    `record_digest` one function too far up.
    """
    temp_db.create_company("Example Co")

    client.get("/api/digest/preview")

    assert temp_db.list_digests() == []


def test_the_scheduler_endpoint_reports_without_running_anything(client):
    """A read-only status endpoint. Should answer even with the scheduler stopped.

    The lifespan never ran in these tests, so the scheduler is not started --
    which is precisely the interesting condition. An endpoint that only works
    when a background thread is alive is one that 500s during startup and
    shutdown, and this test is the cheapest way to find that out.
    """
    response = client.get("/api/scheduler")

    assert response.status_code == 200
    assert isinstance(response.json(), dict)
