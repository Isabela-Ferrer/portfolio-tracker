"""Unit tests for the pure parts of fetchers/careers.py.

The careers fetcher does three separable things: classify a posting, guess a
slug, and normalise a board payload. All three are pure functions over strings
and dicts, which means they can be tested exhaustively for nothing. The parts
that talk to the network are tested separately in test_job_diff.py.

Splitting a module's tests by *what kind of test they need* rather than by
module is usually the right call. These run in microseconds; those need a
database and a fake transport. Keeping them apart keeps the fast ones fast.
"""

import pytest

from fetchers import careers


# --------------------------------------------------------------------------
# is_early_career: a regex with a genuinely nasty failure mode
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title",
    [
        "Software Engineering Intern",
        "2027 Summer Internship, Research",
        "New Grad Software Engineer",
        "New-Grad Engineer",
        "New Graduate Rotational Program",
        "University Recruiting: Backend",
        "Early Career Product Manager",
        "Early-Career Data Scientist",
        "Campus Hire, Infrastructure",
        "Apprentice Engineer",
        "Apprenticeship Program",
        "software engineering intern",   # lower case: the regex is IGNORECASE
    ],
)
def test_early_career_titles_are_recognised(title):
    """Every phrasing of "junior" this board vocabulary uses.

    The list is long on purpose. This one function decides whether Isa ever
    hears about a role she could actually apply to, so a false negative is the
    most expensive bug in the project: it is silent. Nothing looks broken. The
    email simply never mentions the internship.
    """
    assert careers.is_early_career(title) is True


@pytest.mark.parametrize(
    "title",
    [
        # The reason EARLY_CAREER_RE is anchored with \b. Without the word
        # boundaries, all three of these match "intern" as a substring and get
        # reported to Isa as internships.
        "Internal Tools Engineer",
        "International Expansion Lead",
        "Head of Internal Communications",
        # Ordinary senior roles, which must never be misread as junior.
        "Staff Software Engineer",
        "Director of Engineering",
        "Principal Researcher",
        "",
        None,
    ],
)
def test_senior_and_lookalike_titles_are_not_early_career(title):
    """"Internal" and "International" contain "intern" and are not internships.

    This is the highest-value test in the file, and it is a *regression* test:
    the word-boundary anchors in the regex exist because of exactly these
    strings, and the comment in careers.py says so. A test like this is how a
    fixed bug stays fixed after everyone has forgotten about it.

    Passing None matters too. Titles come from third-party JSON, and `None`
    arrives more often than anyone expects.
    """
    assert careers.is_early_career(title) is False


# --------------------------------------------------------------------------
# is_nyc
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "location",
    [
        "New York, NY",
        "New York City",
        "NYC",
        "Brooklyn, NY",
        "Manhattan",
        "Long Island City",
        "Remote (New York)",
        "San Francisco or New York",   # multi-city posting still counts
        "new york",                    # NYC_RE is IGNORECASE
        "Albany, NY",                  # the ", NY" state-abbreviation branch
    ],
)
def test_new_york_locations_are_recognised(location):
    assert careers.is_nyc(location) is True


@pytest.mark.parametrize(
    "location",
    [
        "San Francisco, CA",
        "London, UK",
        "Remote - US",
        # The reason _NY_ABBREV_RE is case *sensitive* and requires a comma.
        # A case-blind bare "ny" matches inside all of these.
        "Germany",
        "Anywhere",
        "Sunnyvale, CA",
        "Bonny Doon, CA",
        "",
        None,
    ],
)
def test_non_new_york_locations_are_rejected(location):
    """"Germany" and "Sunnyvale" contain the letters n-y and are not New York.

    The companion regression test to the one above, and the reason there are two
    separate patterns in careers.py: a case-insensitive city list, plus a
    case-sensitive ", NY" that requires the comma. Either one alone is wrong.
    """
    assert careers.is_nyc(location) is False


# --------------------------------------------------------------------------
# slug_candidates
# --------------------------------------------------------------------------

def test_slug_candidates_puts_the_most_literal_guess_first():
    """Order is the contract: discovery probes these in sequence and stops.

    The first hit wins in discover_ats, so the ordering *is* the behaviour, and
    a test that only checked set membership would let a refactor silently make
    discovery try the vaguest guess first and land on the wrong company's board.
    """
    candidates = careers.slug_candidates("Thinking Machines Lab", "https://thinkingmachines.ai")

    assert candidates[0] == "thinkingmachineslab"
    assert candidates[1] == "thinking-machines-lab"
    # "lab" is a stop token, so the core-word forms drop it.
    assert "thinkingmachines" in candidates
    # The broadest guess, most likely to collide with an unrelated company,
    # comes last.
    assert candidates[-1] == "thinking"


def test_slug_candidates_never_repeats_a_guess():
    """Duplicates would mean probing the same URL three times per company.

    A property test in spirit: rather than asserting the exact list, it asserts
    an invariant that must hold for any input. Those survive refactors that
    exact-output assertions do not.
    """
    candidates = careers.slug_candidates("Scale AI", "https://scale.com")

    assert len(candidates) == len(set(candidates))


def test_slug_candidates_uses_the_website_domain_as_a_guess():
    """A company whose domain differs from its name still gets found.

    Real case: the name gives "cursor", the domain gives "anysphere", and only
    the second one is the actual Greenhouse slug.
    """
    candidates = careers.slug_candidates("Cursor", "https://anysphere.co")

    assert "anysphere" in candidates


@pytest.mark.parametrize("name", ["", "   ", None, "!!!"])
def test_slug_candidates_of_an_unusable_name_is_empty(name):
    """No name, no guesses. Empty list, not a list containing "".

    An empty string in this list would send discovery at
    `https://api.ashbyhq.com/posting-api/job-board/` -- a real request to a
    meaningless URL, for every company, forever.
    """
    assert careers.slug_candidates(name) == []


# --------------------------------------------------------------------------
# _name_matches: the guard against landing on someone else's board
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "ours, theirs, expected",
    [
        ("Anthropic", "Anthropic", True),
        ("Scale AI", "Scale", True),          # "ai" is a stop token, so they match
        ("Anthropic", "", True),              # no name reported: nothing to contradict
        ("Anthropic", "Notion Labs", False),  # a real collision, correctly rejected
    ],
)
def test_board_name_check_rejects_only_a_genuine_mismatch(ours, theirs, expected):
    """Guessed slugs are verified against the board's own company name.

    Note the third row. The permissive default is deliberate: Ashby and Lever do
    not report a company name at all, so a strict check would reject every
    correctly-guessed Ashby board. The test documents that this looseness is a
    decision rather than an oversight -- which is most of what a test is for.
    """
    assert careers._name_matches(ours, theirs) is expected


# --------------------------------------------------------------------------
# _normalise: three ATS payload shapes into one Posting
# --------------------------------------------------------------------------
#
# The fixtures below are trimmed versions of what these three APIs really
# return. Trimmed, not invented: field names copied from live payloads, with
# the irrelevant 40 keys dropped. A fixture you made up tests your imagination.

def test_normalise_reads_an_ashby_board():
    """Ashby: id/title/location, and secondary locations folded into one string."""
    payload = {
        "jobs": [
            {
                "id": "abc-123",
                "title": "Member of Technical Staff",
                "location": "San Francisco",
                "secondaryLocations": [{"location": "New York"}],
                "department": "Research",
                "jobUrl": "https://jobs.ashbyhq.com/example/abc-123",
            }
        ]
    }

    postings, board_name = careers._normalise("ashby", payload)

    assert len(postings) == 1
    assert postings[0].external_id == "abc-123"
    assert postings[0].title == "Member of Technical Staff"
    assert "San Francisco" in postings[0].location
    assert "New York" in postings[0].location   # so is_nyc() can see it
    assert board_name == ""                     # Ashby reports no company name


def test_normalise_skips_unlisted_ashby_jobs():
    """`isListed: false` is Ashby's soft-delete. Those roles are not open.

    Without this, a role pulled from the public board still counts toward
    headcount and never emits job_closed, because it never disappears.
    """
    payload = {
        "jobs": [
            {"id": "1", "title": "Open Role", "isListed": True},
            {"id": "2", "title": "Hidden Role", "isListed": False},
        ]
    }

    postings, _ = careers._normalise("ashby", payload)

    assert [p.title for p in postings] == ["Open Role"]


def test_normalise_reads_a_greenhouse_board_and_its_company_name():
    """Greenhouse nests location and departments, and does report a name."""
    payload = {
        "jobs": [
            {
                "id": 4567,
                "title": "Software Engineer, New Grad",
                "company_name": "Example Co",
                "location": {"name": "New York, NY"},
                "departments": [{"name": "Engineering"}],
                "absolute_url": "https://boards.greenhouse.io/example/jobs/4567",
            }
        ]
    }

    postings, board_name = careers._normalise("greenhouse", payload)

    # Greenhouse ids are integers in JSON and text in our schema. The cast is
    # load-bearing: without it, the diff compares 4567 to "4567", finds no
    # match, and reports every role as both closed and newly opened every week.
    assert postings[0].external_id == "4567"
    assert isinstance(postings[0].external_id, str)
    assert postings[0].location == "New York, NY"
    assert postings[0].department == "Engineering"
    assert board_name == "Example Co"


def test_normalise_falls_back_to_greenhouse_offices_when_location_is_missing():
    """Some Greenhouse boards populate `offices` and leave `location` null."""
    payload = {
        "jobs": [
            {
                "id": 1,
                "title": "Engineer",
                "location": None,
                "offices": [{"name": "Brooklyn, NY"}],
            }
        ]
    }

    postings, _ = careers._normalise("greenhouse", payload)

    assert postings[0].location == "Brooklyn, NY"


def test_normalise_reads_a_lever_board():
    """Lever returns a bare list, and hides everything under `categories`."""
    payload = [
        {
            "id": "xyz-789",
            "text": "Product Designer",
            "categories": {
                "location": "New York",
                "allLocations": ["New York", "Remote"],
                "team": "Design",
            },
            "hostedUrl": "https://jobs.lever.co/example/xyz-789",
        }
    ]

    postings, _ = careers._normalise("lever", payload)

    assert postings[0].title == "Product Designer"
    # dict.fromkeys dedupes: "New York" appears in both fields, once in output.
    assert postings[0].location == "New York, Remote"


def test_normalise_returns_nothing_for_a_lever_payload_that_is_not_a_list():
    """A Lever endpoint answering with an error object must not crash the run.

    "One broken fetcher must never abort a run" is a stated project rule, so it
    gets a test. Rules that live only in a README are rules that get broken.
    """
    postings, board_name = careers._normalise("lever", {"error": "not found"})

    assert postings == []
    assert board_name == ""


@pytest.mark.parametrize("payload", [None, {}, {"jobs": []}])
def test_normalise_handles_empty_and_missing_payloads(payload):
    """None, {} and an empty job list all mean "no postings", never an exception."""
    postings, _ = careers._normalise("ashby", payload)

    assert postings == []


def test_normalise_drops_postings_with_no_id_or_no_title():
    """A row without an id cannot be diffed; a row without a title cannot be shown.

    The final filter in _normalise. Half-formed rows do appear on real boards,
    and one with an empty external_id would collide with every other one on the
    jobs table's UNIQUE(company_id, external_id).
    """
    payload = {
        "jobs": [
            {"id": "", "title": "No Id"},
            {"id": "2", "title": ""},
            {"id": "3", "title": "Good Role"},
        ]
    }

    postings, _ = careers._normalise("ashby", payload)

    assert [p.title for p in postings] == ["Good Role"]
