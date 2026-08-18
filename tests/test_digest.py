"""Tests for digest.py -- the Monday email.

Testing rendered HTML has a well-known failure mode: assert on the exact markup
and every styling tweak breaks fifty tests, so people stop running them. The way
out is to be deliberate about *what* you assert.

The rule used throughout this file: **assert on the content, never on the
chrome.** "The company's name appears", "the internship title appears", "the
localhost link does not appear". Never "the div has padding:14px". Restyle the
email freely; these tests keep passing. Change what information reaches Isa, and
they fail. That is the line, and it is the same line as in any UI testing.

render() takes its data as an argument, so most of this file needs no database
at all -- a design property worth copying. collect() is the part that queries,
and it gets its own integration tests at the bottom.
"""

import pytest

import digest
from conftest import make_digest_entry, make_job_row, make_signal_dict


@pytest.fixture(autouse=True)
def public_base_url(monkeypatch):
    """Pin APP_BASE_URL so tests do not depend on the developer's .env.

    digest.base_url() reads the environment on every call, so without this the
    result depends on whoever last edited their local .env -- the tests would
    pass on one machine and fail on another. Autouse because *every* test here
    would otherwise inherit that ambient dependency.

    Pinning ambient state (env vars, clocks, locales, random seeds) is most of
    what makes a suite reproducible.
    """
    monkeypatch.setenv("APP_BASE_URL", "https://tracker.example.com")


# --------------------------------------------------------------------------
# _subject: the line that decides whether the email gets opened
# --------------------------------------------------------------------------

def test_subject_says_nothing_happened_when_nothing_happened():
    """Zero companies with news gets an honest subject line.

    Regression test. The old subject counted companies with any flag lit,
    including the sticky early-career one, so it announced "6 companies moved
    this week" over an email in which every section said "quiet week". The rule
    now is that the subject describes what is actually in the email.
    """
    assert digest._subject(featured=[], total=15) == "Quiet week across all 15 companies"


def test_subject_names_the_single_company_with_news():
    featured = [make_digest_entry(name="Anthropic")]

    assert digest._subject(featured, total=15) == "News this week from Anthropic"


def test_subject_lists_up_to_three_companies_then_counts_the_rest():
    """Three names, then "and N more". Subject lines get truncated in inboxes.

    The boundary is at three, so the test uses five: enough to prove both the
    cut and the count. Four would prove the cut but let an off-by-one in the
    remainder slide through as "and 1 more" either way.
    """
    featured = [make_digest_entry(name=n) for n in
                ["Anthropic", "Cursor", "Notion", "Ramp", "Linear"]]

    subject = digest._subject(featured, total=15)

    assert subject == "5 companies with news this week: Anthropic, Cursor, Notion and 2 more"


# --------------------------------------------------------------------------
# links_reachable: a localhost link is dead in an inbox
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "base, reachable",
    [
        ("https://tracker.example.com", True),
        ("http://localhost:8000", False),
        ("http://127.0.0.1:8000", False),
        ("http://0.0.0.0:8000", False),
    ],
)
def test_dashboard_links_are_dropped_when_the_app_is_only_on_localhost(
    monkeypatch, base, reachable
):
    """An email read on a phone cannot reach http://localhost:8000.

    The behaviour is a small judgement call -- drop the internal links, keep the
    public source links -- and small judgement calls are exactly what gets
    "cleaned up" by someone who does not know why they are there. The test is
    the note explaining it.
    """
    monkeypatch.setenv("APP_BASE_URL", base)

    assert digest.links_reachable() is reachable


def test_the_email_body_contains_no_localhost_links(monkeypatch):
    """The property, asserted on the finished HTML rather than the helper.

    links_reachable() being correct and the renderer *consulting* it everywhere
    are two different things, and there are four separate places in digest.py
    that build a link. Asserting on the whole output covers all four at once,
    and keeps covering the fifth one somebody adds later.
    """
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    entry = make_digest_entry(
        name="Anthropic",
        content=[make_signal_dict(id=1, type="blog", title="A post")],
        early_career=[make_job_row(title="Research Intern", is_early_career=True)],
    )

    _, body = digest.render({"featured": [entry], "quiet": [], "all": [entry]})

    assert "localhost" not in body
    # ...but the public source link survives, which is the whole point of the
    # rule. Dropping every link would be simpler and less useful.
    assert "https://example.com/post" in body


# --------------------------------------------------------------------------
# render: what actually reaches Isa
# --------------------------------------------------------------------------

def test_a_company_section_leads_with_the_cached_summary_not_the_headline():
    """The summary sentence is the point of the section.

    "Future(s) of Work" tells Isa nothing; "the founder argued agents need
    persistent memory" tells her what to say in a coffee chat. The summary was
    written once from the article body and cached, so including it is free -- and
    the digest exists to deliver exactly this.
    """
    signal = make_signal_dict(id=7, type="podcast", title="Future(s) of Work",
                              url="https://podcast.example/ep12")
    entry = make_digest_entry(
        name="Anthropic",
        content=[signal],
        summaries={7: "The founder argued agents need persistent memory."},
    )

    _, body = digest.render({"featured": [entry], "quiet": [], "all": [entry]})

    assert "Future(s) of Work" in body
    assert "The founder argued agents need persistent memory." in body
    assert "https://podcast.example/ep12" in body


def test_quiet_companies_are_named_once_at_the_bottom_not_given_a_section():
    """A section is earned by publishing something.

    Regression test for the change described in CLAUDE.md: quiet companies used
    to get a full section headed by the words "quiet week", every week, forever.
    Now they get their name on one line. Two assertions, because "is mentioned"
    and "does not get a section" are different claims and both matter.
    """
    loud = make_digest_entry(id=1, name="Anthropic",
                             content=[make_signal_dict(id=1, type="blog")])
    quiet = make_digest_entry(id=2, name="Sierra")

    _, body = digest.render({"featured": [loud], "quiet": [quiet], "all": [loud, quiet]})

    assert "Quiet this week: Sierra" in body
    assert "<h2" not in body.split("Quiet this week")[1]   # no section after it


def test_standing_early_career_roles_are_listed_once_at_the_bottom():
    """The sticky flag ranks the dashboard but no longer manufactures a section.

    Without this rule the same five internships were re-listed under the words
    "quiet week" every Monday until they were filled. They still have to appear
    somewhere -- a quiet company with an open new-grad role is the single most
    actionable thing in the email -- so they get one compact block.
    """
    entry = make_digest_entry(
        name="Sierra",
        early_career=[make_job_row(title="Software Engineer Intern", is_early_career=True)],
    )

    _, body = digest.render({"featured": [], "quiet": [entry], "all": [entry]})

    assert "Early career roles still open" in body
    assert "Software Engineer Intern" in body


def test_a_role_that_opened_this_week_is_highlighted_and_not_repeated_below():
    """New this week goes in the company's section; standing roles go in the
    roundup. A role must not appear in both.

    The de-duplication between the two blocks is a one-line list comprehension
    (`if j not in e["early_career_new"]`) and is exactly the sort of thing that
    quietly breaks. The assertion counts occurrences rather than checking
    presence, because "appears" and "appears once" are different claims and only
    the second one is the requirement.
    """
    new_role = make_job_row(external_id="99", title="Research Intern, NLP",
                            is_early_career=True)
    entry = make_digest_entry(
        name="Anthropic",
        content=[make_signal_dict(id=1, type="blog")],
        early_career=[new_role],
        early_career_new=[new_role],
    )

    _, body = digest.render({"featured": [entry], "quiet": [], "all": [entry]})

    assert "Early career roles opened this week" in body
    assert body.count("Research Intern, NLP") == 1


def test_the_hiring_line_reports_open_roles_not_headcount():
    """"262 open roles", not "262 headcount".

    Regression test for a labelling bug with real consequences: the line used to
    say headcount, which made a board of 262 open reqs read as a company of 262
    people. The wording is the fix, so the wording is what is asserted.
    """
    entry = make_digest_entry(
        name="Anthropic", content=[make_signal_dict(id=1, type="blog")],
        headcount_total=262, headcount_nyc=14, prev_headcount=258,
    )

    _, body = digest.render({"featured": [entry], "quiet": [], "all": [entry]})

    assert "262 open roles" in body
    assert "14 in New York" in body
    assert "up 4 since last week" in body
    assert "headcount" not in body.lower()


def test_company_names_are_html_escaped():
    """Company names are user input and go straight into an HTML email.

    A security test, and the reason it belongs here rather than in a checklist:
    the injection point is `_esc()` being applied at every interpolation, and
    the way that breaks is somebody adding a new f-string and forgetting it. A
    test with a live payload in it fails the moment that happens.
    """
    entry = make_digest_entry(name='Evil <script>alert("xss")</script> Co',
                              content=[make_signal_dict(id=1, type="blog")])

    _, body = digest.render({"featured": [entry], "quiet": [], "all": [entry]})

    assert "<script>" not in body
    assert "&lt;script&gt;" in body


def test_signal_titles_and_urls_are_html_escaped():
    """Same rule for everything that arrives from a third-party feed.

    Feed titles are the *least* trusted string in the system: they come from
    arbitrary websites. Testing the company name alone would leave the far more
    likely injection route uncovered.
    """
    nasty = make_signal_dict(id=1, type="blog", title='Post <img src=x onerror=alert(1)>',
                             url='https://example.com/"onmouseover="alert(1)')
    entry = make_digest_entry(name="Example Co", content=[nasty])

    _, body = digest.render({"featured": [entry], "quiet": [], "all": [entry]})

    assert "<img src=x" not in body
    assert 'onmouseover="alert' not in body


def test_an_undated_item_shows_no_date():
    """Never show a date the source did not give -- enforced at the last step too.

    content.py refuses to invent one and database.py refuses to default one, and
    this is the third checkpoint: the renderer omits the date element entirely
    rather than printing an empty span or today's date. Belt, braces, and a
    third belt, for a rule that produced a visible falsehood when it broke.
    """
    undated = make_signal_dict(id=1, type="blog", title="Undated post", published_at="")
    entry = make_digest_entry(name="Example Co", content=[undated])

    _, body = digest.render({"featured": [entry], "quiet": [], "all": [entry]})

    assert "Undated post" in body
    assert "color:#aaa" not in body   # the date span is not emitted at all


def test_a_completely_empty_week_still_renders_a_valid_email():
    """Fifteen companies, nothing anywhere. Must not crash, must say so.

    The degenerate case, and the one most likely to actually happen -- over a
    holiday week, or the first Monday after the app is deployed. An exception
    here means no email at all, which is a worse failure than a boring one.
    """
    subject, body = digest.render({"featured": [], "quiet": [], "all": []})

    assert subject == "Quiet week across all 0 companies"
    assert "Nothing new published anywhere this week." in body
    assert body.strip().startswith("<div")


def test_plain_text_alternative_strips_the_markup():
    """Clients that refuse HTML still get something readable.

    Asserting that the tags are gone and the words survive. Not asserting the
    exact whitespace -- that would be pinning down an implementation detail of
    three chained regexes, and it would break on any of them being reordered.
    """
    entry = make_digest_entry(name="Anthropic",
                              content=[make_signal_dict(id=1, type="blog",
                                                        title="Introducing Skills")])
    subject, body = digest.render({"featured": [entry], "quiet": [], "all": [entry]})

    text = digest._plain_text(subject, body)

    assert "<div" not in text
    assert "Anthropic" in text
    assert "Introducing Skills" in text
    assert text.startswith(subject)


# --------------------------------------------------------------------------
# collect: the database half
# --------------------------------------------------------------------------

def test_collect_separates_companies_with_news_from_quiet_ones(temp_db):
    """The featured/quiet split, driven off real rows.

    An integration test, because the rule spans four tables: a company is
    featured if its latest snapshot's signals include a publishable type. Every
    unit test above took that split as given; this is the one that checks the
    query behind it.
    """
    from conftest import make_signal

    loud_id = temp_db.create_company("Anthropic")
    quiet_id = temp_db.create_company("Sierra")

    loud_snapshot = temp_db.create_snapshot(loud_id)
    temp_db.insert_signals(loud_snapshot, [
        make_signal(company_id=loud_id, type="blog", url="https://anthropic.example/post",
                    published_at=temp_db.now_iso()),
    ])
    temp_db.finalize_snapshot(loud_snapshot, {"launch": True}, [], {}, 10, 2)

    quiet_snapshot = temp_db.create_snapshot(quiet_id)
    temp_db.finalize_snapshot(quiet_snapshot, {}, [], {}, 5, 0)

    data = digest.collect()

    assert [e["name"] for e in data["featured"]] == ["Anthropic"]
    assert [e["name"] for e in data["quiet"]] == ["Sierra"]


def test_collect_ignores_a_stale_snapshot(temp_db):
    """If the weekly refresh has not run, report the company as quiet.

    The alternative is replaying whatever was found a month ago as though it
    happened this week, which is worse than saying nothing. SNAPSHOT_MAX_AGE_DAYS
    is 9; the fixture uses 30 days so the test is not sitting on the boundary of
    a value that might reasonably be tuned.

    `days_ago_iso` comes from the app rather than being hand-written, so the test
    cannot drift out of sync with the format the code compares against.
    """
    from conftest import make_signal

    company_id = temp_db.create_company("Stale Co")
    old_snapshot = temp_db.create_snapshot(company_id)
    temp_db.insert_signals(old_snapshot, [
        make_signal(company_id=company_id, type="blog", url="https://stale.example/post"),
    ])
    temp_db.finalize_snapshot(old_snapshot, {}, [], {}, 5, 0)
    # Backdate the snapshot past the freshness window.
    with temp_db.get_conn() as conn:
        conn.execute("UPDATE snapshots SET created_at = ? WHERE id = ?",
                     (temp_db.days_ago_iso(30), old_snapshot))

    data = digest.collect()

    assert [e["name"] for e in data["quiet"]] == ["Stale Co"]
    assert data["featured"] == []


def test_collect_drops_items_published_long_before_we_found_them(temp_db):
    """Discovered this week, published two years ago: that is backlog, not news.

    This happens whenever a company's blog is crawled for the first time or its
    feed changes shape. ITEM_MAX_AGE_DAYS is 45; the fixture uses 400 days, well
    clear of the boundary, so the test is about the rule and not the number.
    """
    from conftest import make_signal

    company_id = temp_db.create_company("Example Co")
    snapshot_id = temp_db.create_snapshot(company_id)
    temp_db.insert_signals(snapshot_id, [
        make_signal(company_id=company_id, type="blog", url="https://example.com/old",
                    title="An old post", published_at=temp_db.days_ago_iso(400)),
        make_signal(company_id=company_id, type="blog", url="https://example.com/new",
                    title="A new post", published_at=temp_db.now_iso()),
    ])
    temp_db.finalize_snapshot(snapshot_id, {}, [], {}, 0, 0)

    data = digest.collect()

    titles = [s["title"] for s in data["featured"][0]["content"]]
    assert titles == ["A new post"]


def test_collect_keeps_undated_items(temp_db):
    """An undated item is kept, because a missing date is usually a parsing gap.

    The complement of the test above, and the more interesting half: the
    freshness filter has to be lenient in exactly one direction. Dropping
    undated items would silently lose every post from every site whose date
    format the parser does not know.
    """
    from conftest import make_signal

    company_id = temp_db.create_company("Example Co")
    snapshot_id = temp_db.create_snapshot(company_id)
    temp_db.insert_signals(snapshot_id, [
        make_signal(company_id=company_id, type="blog", url="https://example.com/x",
                    title="Undated post", published_at=""),
    ])
    temp_db.finalize_snapshot(snapshot_id, {}, [], {}, 0, 0)

    data = digest.collect()

    assert [s["title"] for s in data["featured"][0]["content"]] == ["Undated post"]
