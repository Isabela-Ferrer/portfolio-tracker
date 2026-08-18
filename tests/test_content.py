"""Tests for content.py -- HTML extraction and the permanent scrape cache.

Two very different kinds of test share this file, and the split is worth
noticing.

The extraction functions (`extract_text`, `extract_date`, `extract_title`) take
a parsed tree and return a string. Pure, synchronous, instant. They get ordinary
unit tests with HTML written inline, because a fixture you can read next to the
assertion is worth more than a tidier one in another file.

`fetch_many` coordinates the cache and the network. It gets integration tests
with a real database and a fake transport, because the behaviour under test is
"how many requests did that make", which cannot be observed from a return value.
"""

import pytest
from selectolax.parser import HTMLParser

import content
from conftest import html_route, mock_http_client


def parse(html):
    """Small adapter so the tests read as `extract_text(parse(HTML))`."""
    return HTMLParser(html)


# A page shaped like the ones this scraper actually meets: real content wrapped
# in navigation, a cookie line and a footer. Long enough that <article> clears
# content.py's 400-character density threshold, because a fixture that does not
# reach the threshold silently exercises a different code path than intended.
ARTICLE_PAGE = """
<html>
  <head>
    <title>Example Co Blog</title>
    <meta property="og:title" content="Introducing our inference API">
    <meta property="article:published_time" content="2026-03-04T09:30:00Z">
  </head>
  <body>
    <nav><a href="/">Home</a><a href="/careers">Careers</a></nav>
    <header><p>We use cookies to improve your experience on this website.</p></header>
    <article>
      <h1>Introducing our inference API</h1>
      <p>Today we are releasing an inference API that serves our open weights
         model at under one hundred milliseconds of latency per token.</p>
      <p>The API is built on a scheduler we wrote in Rust, and it batches
         requests across tenants without leaking timing information between
         them, which took most of the last two quarters to get right.</p>
      <p>Short note.</p>
      <p>Subscribe to our newsletter for more updates like this one delivered
         to your inbox every week.</p>
      <li>Pricing starts at two dollars per million input tokens, with volume
          discounts available for annual commitments.</li>
    </article>
    <footer><p>Copyright 2026 Example Co. All rights reserved worldwide.</p></footer>
  </body>
</html>
"""


# --------------------------------------------------------------------------
# extract_text
# --------------------------------------------------------------------------

def test_extract_text_returns_the_article_body():
    """The paragraphs that are actually the article come back, in order."""
    text = content.extract_text(parse(ARTICLE_PAGE))

    assert "releasing an inference API" in text
    assert "scheduler we wrote in Rust" in text
    assert "two dollars per million input tokens" in text   # <li> counts as body


def test_extract_text_strips_navigation_and_footers():
    """<nav>, <header> and <footer> are decomposed before anything is read.

    Without this, every summary in the digest is written from a prompt whose
    first two hundred characters are "Home Careers Blog Contact". The model
    dutifully mentions the navigation menu.
    """
    text = content.extract_text(parse(ARTICLE_PAGE))

    assert "Careers" not in text
    assert "All rights reserved" not in text


def test_extract_text_drops_boilerplate_and_fragments():
    """Newsletter pitches and one-line captions are not the article.

    Two separate filters tested together because they defend the same thing: the
    length floor drops "Short note.", and the boilerplate pattern drops the
    subscribe line, which is long enough to survive the floor on its own.
    """
    text = content.extract_text(parse(ARTICLE_PAGE))

    assert "Short note" not in text
    assert "Subscribe to our newsletter" not in text
    assert "We use cookies" not in text


def test_extract_text_falls_back_to_the_whole_body_when_there_are_no_paragraphs():
    """A page built entirely from <div>s still yields something.

    The fallback path. Plenty of modern sites render body copy into divs with no
    <p> anywhere, and returning "" for those would mean the digest summarises
    those posts from the headline forever, silently.
    """
    divs = "<html><body><div>" + ("Some readable text about the product. " * 10) + \
           "</div></body></html>"

    text = content.extract_text(parse(divs))

    assert "readable text about the product" in text


def test_extract_text_is_capped():
    """Long articles are truncated, because the excerpt is going into a prompt.

    Testing a bound rather than a value. The exact number is
    content.MAX_TEXT_CHARS, and referring to the constant rather than hardcoding
    4000 means tuning it does not break the test -- the *rule* is what is under
    test, not this month's setting.
    """
    huge = "<html><body><article>" + \
           ("<p>This is a long paragraph of text about the product and it goes on.</p>" * 400) + \
           "</article></body></html>"

    text = content.extract_text(parse(huge))

    assert len(text) <= content.MAX_TEXT_CHARS


@pytest.mark.parametrize("html", ["", "<html></html>", "<html><body></body></html>"])
def test_extract_text_of_an_empty_page_is_empty(html):
    """Degenerate input returns "", never raises. Real crawls hit blank pages."""
    assert content.extract_text(parse(html)) == ""


# --------------------------------------------------------------------------
# extract_date -- "never show a date the source did not give"
# --------------------------------------------------------------------------

def test_extract_date_reads_the_standard_meta_tag():
    """article:published_time, normalised to the app's ISO format.

    The Z suffix in, no suffix out. Dates are compared as *strings* elsewhere in
    this codebase (see digest._age_ok), so a consistent format is not cosmetic:
    "2026-03-04T09:30:00Z" and "2026-03-04T09:30:00" sort differently, and the
    freshness filter would start dropping items.
    """
    assert content.extract_date(parse(ARTICLE_PAGE)) == "2026-03-04T09:30:00"


def test_extract_date_prefers_the_first_listed_meta_tag():
    """_DATE_META is an ordered preference list, and the order is the contract.

    Pages carry several date-ish tags: published, modified, "date". Picking the
    wrong one shows a two-year-old post as updated yesterday. The fixture
    deliberately puts the *less* preferred tag first in the document, so a
    document-order implementation fails this test.
    """
    html = """<html><head>
      <meta name="date" content="2020-01-01">
      <meta property="article:published_time" content="2026-03-04T09:30:00Z">
    </head><body></body></html>"""

    assert content.extract_date(parse(html)).startswith("2026-03-04")


def test_extract_date_falls_back_to_json_ld():
    """Many CMSes only put the date in a JSON-LD block."""
    html = """<html><head>
      <script type="application/ld+json">
        {"@type": "BlogPosting", "datePublished": "2026-02-14T12:00:00Z"}
      </script>
    </head><body></body></html>"""

    assert content.extract_date(parse(html)).startswith("2026-02-14")


def test_extract_date_survives_malformed_json_ld():
    """Broken JSON on a page must not take down the crawl.

    Sites ship invalid JSON-LD constantly. Untrusted input, so the test feeds it
    untrusted input -- this is the same discipline as the malformed-model-output
    test in test_ai_narrator.py, applied to a different vendor.
    """
    html = """<html><head>
      <script type="application/ld+json">{not valid json at all</script>
    </head><body></body></html>"""

    assert content.extract_date(parse(html)) == ""


def test_a_page_with_no_date_returns_an_empty_string():
    """The stated rule: never show a date the source did not give.

    Empty, not today's date. The consequence of getting this wrong was that
    every undated page in the digest read as published the morning of the crawl.
    The absence of a fallback is the feature, and asserting on "" is how you
    stop someone helpfully adding one back.
    """
    html = "<html><head><title>No date here</title></head><body><p>Words.</p></body></html>"

    assert content.extract_date(parse(html)) == ""


# --------------------------------------------------------------------------
# extract_title
# --------------------------------------------------------------------------

def test_extract_title_prefers_the_open_graph_title():
    """og:title is the clean headline; <title> usually has the site name glued on."""
    assert content.extract_title(parse(ARTICLE_PAGE)) == "Introducing our inference API"


def test_extract_title_falls_back_to_h1_then_title():
    html = "<html><head><title>Fallback Title</title></head><body><h1>The H1</h1></body></html>"

    assert content.extract_title(parse(html)) == "The H1"


# --------------------------------------------------------------------------
# fetch_many -- the permanent cache
# --------------------------------------------------------------------------

class CountingTransport:
    """A MockTransport handler that records how many requests it served.

    The assertions in this section are all about request *counts*, and there is
    no way to see a count from a return value. Making the fake observable is
    what turns "I believe the cache works" into a test.
    """

    def __init__(self, handler):
        self._handler = handler
        self.requests = []

    def __call__(self, request):
        self.requests.append(str(request.url))
        return self._handler(request)


@pytest.mark.asyncio
async def test_a_url_is_fetched_once_ever(temp_db):
    """"A page is scraped once, ever." Two calls, one request.

    The single most important property of content.py, and it is only observable
    across two calls -- which is why this is an integration test with a real
    database rather than a unit test. The cache lives in SQLite; faking it away
    would leave nothing to test.
    """
    counter = CountingTransport(html_route({"example.com": ARTICLE_PAGE}))

    async with mock_http_client(counter) as client:
        first = await content.fetch_many(["https://example.com/post"], client)
        second = await content.fetch_many(["https://example.com/post"], client)

    assert len(counter.requests) == 1
    assert first == second
    assert "inference API" in first["https://example.com/post"]["text"]


@pytest.mark.asyncio
async def test_a_failed_fetch_is_cached_so_a_paywall_is_not_retried_weekly(temp_db):
    """Failures are cached exactly like successes.

    The counter-intuitive half of the caching rule, and the one that saves the
    Monday run from re-requesting every dead link and paywall it has ever seen.
    The status is recorded as "error" so the reader can tell the difference; what
    it must not do is try again.
    """
    counter = CountingTransport(html_route({"paywalled.example": 403}))

    async with mock_http_client(counter) as client:
        first = await content.fetch_many(["https://paywalled.example/story"], client)
        await content.fetch_many(["https://paywalled.example/story"], client)

    assert len(counter.requests) == 1
    assert first["https://paywalled.example/story"]["status"] == "error"


@pytest.mark.asyncio
async def test_only_the_uncached_urls_are_requested(temp_db):
    """A mixed batch requests the new URL and serves the old one from cache.

    The realistic weekly case: a feed of twenty items, one of them new. This is
    the test that would catch a cache lookup that runs but whose result is
    thrown away -- the whole batch would still return correct data, and the bill
    would quietly be twenty times what it should be.
    """
    pages = html_route({"example.com/a": ARTICLE_PAGE, "example.com/b": ARTICLE_PAGE})
    counter = CountingTransport(pages)

    async with mock_http_client(counter) as client:
        await content.fetch_many(["https://example.com/a"], client)
        counter.requests.clear()
        result = await content.fetch_many(
            ["https://example.com/a", "https://example.com/b"], client
        )

    assert counter.requests == ["https://example.com/b"]
    assert set(result) == {"https://example.com/a", "https://example.com/b"}


@pytest.mark.asyncio
async def test_non_http_and_empty_urls_are_never_requested(temp_db):
    """Input validation before the socket.

    Signals arrive with all sorts of junk in the url field, including the
    synthesised `job:1:abc` used by job_closed signals. None of it is fetchable,
    and attempting it would be a request per company per week that can only fail.
    """
    counter = CountingTransport(html_route({}))

    async with mock_http_client(counter) as client:
        result = await content.fetch_many(["", None, "job:1:abc", "ftp://x.example"], client)

    assert result == {}
    assert counter.requests == []


@pytest.mark.asyncio
async def test_a_thin_page_is_recorded_as_empty_rather_than_ok(temp_db):
    """Under 120 characters of body text is not usable content.

    A three-word page returns 200 and parses fine, so nothing errors -- it just
    produces a summary written from nothing. The `empty` status is how the
    caller tells "we have the text" from "we have text but it is useless", and
    both are cached so neither is retried.
    """
    thin = "<html><body><article><p>Hello.</p></article></body></html>"
    counter = CountingTransport(html_route({"thin.example": thin}))

    async with mock_http_client(counter) as client:
        result = await content.fetch_many(["https://thin.example/x"], client)

    assert result["https://thin.example/x"]["status"] == "empty"


@pytest.mark.asyncio
async def test_resolve_dates_returns_only_the_urls_that_advertise_one(temp_db):
    """The narrow interface the fetchers use to backfill missing publish dates.

    Undated URLs are *absent* from the mapping rather than present with "". The
    caller does `dates.get(url)` and must be able to tell "no date" from "empty
    date" -- otherwise the never-show-a-date-the-source-did-not-give rule leaks.
    """
    undated = "<html><body><article>" + ("<p>Text with no date anywhere on it at all.</p>" * 5) + \
              "</article></body></html>"
    # Host names chosen so neither is a substring of the other. The route
    # matcher in conftest matches on substrings, and "dated.example" lives
    # inside "undated.example" -- which routed both URLs to the same page and
    # made this test fail on the fixture rather than on the code. Worth the
    # sentence: a test that fails for a reason you did not intend has told you
    # nothing about the software, and the fix belongs in the test.
    routes = html_route({"has-date.example": ARTICLE_PAGE, "no-date.example": undated})

    async with mock_http_client(routes) as client:
        dates = await content.resolve_dates(
            ["https://has-date.example/a", "https://no-date.example/b"], client
        )

    assert dates == {"https://has-date.example/a": "2026-03-04T09:30:00"}
