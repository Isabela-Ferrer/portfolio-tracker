"""Shared fixtures.

conftest.py is pytest's magic filename: anything defined here is available to
every test module in this directory and below, with no import. That is the whole
mechanism. There is no registration step and no plugin to declare.

Two rules govern everything in this file:

1. **A test must not touch the real world.** No real database, no real network,
   no real OpenAI account. Every test in this suite could run on a plane.
2. **A test must not be able to affect another test.** Each one gets a fresh
   database in a fresh temporary directory, and pytest throws it away
   afterwards. Tests that share state pass in one order and fail in another,
   which is the single most expensive kind of test to own.
"""

import json
import os
import sys
from types import SimpleNamespace

import httpx
import pytest

# The app is a flat package: `import database`, not `from app import database`.
# So the project root has to be importable. This runs at collection time,
# before any test module is imported, which is exactly when it is needed.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# main.py does `StaticFiles(directory="frontend")` at import time, and Starlette
# checks that directory exists immediately. That is a relative path, so the
# process has to be sitting in the project root when main is imported. Doing it
# here rather than in a fixture matters: fixtures run after test modules are
# imported, and by then it would be too late.
os.chdir(PROJECT_ROOT)

import database as db  # noqa: E402  (must follow the sys.path fix above)
from models import Posting, Signal  # noqa: E402


# --------------------------------------------------------------------------
# The database fixture
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _never_the_real_database(tmp_path, monkeypatch):
    """Autouse. Repoints database.DB_PATH away from data/tracker.db for EVERY test.

    `autouse=True` means this runs for every test in the suite whether it asks
    for it or not. That is usually a smell -- invisible setup is hard to reason
    about -- but it is exactly right for a safety interlock.

    The risk it removes is real and nasty. ai_narrator._chat writes to the
    ai_cache table on every model call. A test that exercises it and forgets to
    ask for `temp_db` would quietly read and write the developer's actual
    tracker.db, polluting real data and, worse, passing or failing depending on
    what happened to be in it. Making the unsafe thing impossible beats
    remembering not to do it.

    The design principle: opt-in safety is a bug waiting for a distracted
    afternoon. Opt-out safety is a bug you have to write on purpose.
    """
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "tracker.db"))


@pytest.fixture
def temp_db(_never_the_real_database, tmp_path, monkeypatch):
    """A real, empty SQLite database in a throwaway directory, schema created.

    Note what this is *not*: it is not a mock of the database layer. Faking
    SQLite would mean the tests agree with a fiction of my own writing, and the
    bugs this project actually has (a UNIQUE constraint that does the deduping,
    a NULL closed_at that defines "open") live in the SQL itself. A real SQLite
    file is fast enough (milliseconds) that there is nothing to buy by faking it.

    Two pytest built-ins do the work:

    `tmp_path` is a pathlib.Path to a fresh directory, unique per test, cleaned
    up automatically. Ask for it and you get isolation for free.

    The redirection itself happens in `_never_the_real_database` above, using
    `monkeypatch` -- which sets an attribute and, crucially, *puts it back* when
    the test ends, even if the test failed. It works because every module in the
    app does `import database as db`, so they all share one module object, and
    rebinding the attribute on that object redirects all of them at once. Had
    each caller done `from database import DB_PATH`, this would not work and
    every importer would need patching separately. The general rule: patch the
    name where it is *looked up*, not where it was defined.

    This fixture then adds the schema. Depending on it is how a test says "I
    need tables", and the two-layer split means a test that hits the database
    without asking for it fails with "no such table" rather than silently
    succeeding against real data.
    """
    db.init_db()      # same two calls main.py makes on startup, in the same
    db._migrate_db()  # order, so the tests run against the real schema
    return db


# --------------------------------------------------------------------------
# Object factories
# --------------------------------------------------------------------------
#
# Factories, not fixtures, for plain data. A fixture returning a fixed Signal
# forces every test to accept that Signal's exact fields; a factory lets each
# test override only the field it cares about and ignore the rest. The test then
# reads as "a press signal from TechCrunch" instead of a wall of keyword
# arguments, and the noise-to-signal ratio of the test file stays sane.

def make_signal(**overrides):
    """A Signal dataclass, with sensible defaults for everything unstated."""
    fields = {
        "company_id": 1,
        "type": "blog",
        "title": "A post",
        "url": "https://example.com/post",
        "published_at": "2026-08-10T09:00:00",
        "raw": {},
    }
    fields.update(overrides)
    return Signal(**fields)


def make_signal_dict(**overrides):
    """A signal as it comes *back* out of the database: a plain dict with an id.

    Worth keeping separate from make_signal. Signals cross a boundary in this
    app: fetchers emit dataclasses, the database returns dicts, and flags.py and
    ai_narrator.py only ever see the dict form. Tests for those modules should
    use the shape those modules actually receive.
    """
    fields = {
        "id": 1,
        "company_id": 1,
        "type": "blog",
        "title": "A post",
        "url": "https://example.com/post",
        "published_at": "2026-08-10T09:00:00",
        "created_at": "2026-08-10T09:00:00",
        "raw": {},
    }
    fields.update(overrides)
    return fields


def make_posting(**overrides):
    """A Posting, as the careers fetcher produces after normalising a board."""
    fields = {
        "external_id": "1",
        "title": "Software Engineer",
        "location": "San Francisco, CA",
        "department": "Engineering",
        "url": "https://jobs.example.com/1",
    }
    fields.update(overrides)
    return Posting(**fields)


def make_job_row(**overrides):
    """A row of the jobs table as get_open_jobs returns it."""
    fields = {
        "id": 1,
        "company_id": 1,
        "external_id": "1",
        "title": "Software Engineer",
        "location": "San Francisco, CA",
        "department": "Engineering",
        "url": "https://jobs.example.com/1",
        "first_seen_at": "2026-08-01T00:00:00",
        "last_seen_at": "2026-08-10T00:00:00",
        "closed_at": None,
        "is_early_career": False,
        "is_nyc": False,
    }
    fields.update(overrides)
    return fields


def make_digest_entry(**overrides):
    """One company's row in the digest's collect() output.

    digest.render() takes its data as an argument, so the entire email can be
    rendered from hand-built dicts with no database at all. That is a design
    property worth noticing: a function that takes its input as a parameter is
    dramatically cheaper to test than one that goes and fetches it.
    """
    fields = {
        "id": 1,
        "name": "Example Co",
        "flags": {},
        "content": [],
        "summaries": {},
        "signals": [],
        "early_career": [],
        "early_career_new": [],
        "headcount_total": 0,
        "headcount_nyc": 0,
        "prev_headcount": None,
        "created_at": "2026-08-17T08:00:00",
    }
    fields.update(overrides)
    return fields


# --------------------------------------------------------------------------
# Fake OpenAI client
# --------------------------------------------------------------------------

class FakeCompletions:
    """Stands in for client.chat.completions.

    This is a *fake*, not a mock: it has a working implementation (hand back the
    next queued response) rather than a set of recorded expectations. It also
    records its calls, which is what lets a test assert the interesting thing
    about this codebase, which is not what the model said but *how many times it
    was asked*. The caching in ai_narrator exists to keep that number down, and
    `len(fake.calls)` is how you prove the cache works.

    An empty queue raises rather than returning something plausible. A test that
    makes an unexpected model call should fail loudly; a fake that quietly
    invents an answer turns a real bug into a green tick.
    """

    def __init__(self):
        self.responses = []
        self.calls = []

    def queue(self, payload):
        """Queue one response. Dicts are JSON-encoded, matching json_mode."""
        self.responses.append(json.dumps(payload) if isinstance(payload, dict) else payload)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError(
                f"Unexpected model call, no response queued. Prompt kind: "
                f"{kwargs.get('messages', [{}])[-1].get('content', '')[:80]!r}"
            )
        content = self.responses.pop(0)
        if isinstance(content, Exception):
            raise content
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


@pytest.fixture
def fake_openai(monkeypatch):
    """Replace ai_narrator's module-level OpenAI client with the fake.

    ai_narrator builds its client at import time from OPENAI_API_KEY, and
    load_dotenv() means a developer's real key is often present. Without this
    fixture a test of the bullet logic would spend real money and fail on a
    train. Patching the module attribute is the smallest possible intervention:
    all the app's own logic still runs, only the socket is gone.
    """
    import ai_narrator

    fake = FakeCompletions()
    monkeypatch.setattr(
        ai_narrator, "client", SimpleNamespace(chat=SimpleNamespace(completions=fake))
    )
    return fake


# --------------------------------------------------------------------------
# Fake HTTP
# --------------------------------------------------------------------------

def mock_http_client(handler):
    """An httpx.AsyncClient that never opens a socket.

    httpx ships MockTransport for exactly this, so there is no extra dependency
    (no responses, no respx, no aioresponses). `handler` takes an httpx.Request
    and returns an httpx.Response; the client is otherwise completely real, so
    redirects, headers, .json(), raise_for_status() and timeouts all behave as
    they do in production.

    The reason this works so cleanly is that careers.fetch() and
    content.fetch_many() both accept an optional `client` argument. Code that
    lets you hand it its collaborators is code you can test without patching
    anything. Where a seam like that already exists, use it rather than reaching
    for monkeypatch.
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def json_route(routes, default_status=404):
    """Build a MockTransport handler from a {url_substring: payload} mapping.

    Anything not matched returns `default_status`, which is deliberate: a test
    that accidentally requests an unexpected URL should see a failure, not a
    convincing blank.
    """
    def handler(request):
        for fragment, payload in routes.items():
            if fragment in str(request.url):
                if isinstance(payload, int):
                    return httpx.Response(payload)
                return httpx.Response(200, json=payload)
        return httpx.Response(default_status)
    return handler


def html_route(routes, default_status=404):
    """Same idea, for pages of HTML rather than JSON APIs."""
    def handler(request):
        for fragment, body in routes.items():
            if fragment in str(request.url):
                if isinstance(body, int):
                    return httpx.Response(body)
                return httpx.Response(
                    200, text=body, headers={"content-type": "text/html; charset=utf-8"}
                )
        return httpx.Response(default_status)
    return handler
