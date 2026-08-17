"""Article text extraction, cached permanently by URL.

The summaries in the digest are only as good as what we feed them. Titles alone
produce bullets that restate the headline, so every signal that points at a
readable page gets its body text pulled once and stored in content_cache. A URL
is fetched exactly once, ever: failures are cached too, so a paywall or a dead
link is not retried every Monday.

This is also where a real publish date comes from. RSS gives one, but listing
pages and changelogs frequently do not, and an undated post used to inherit the
time of the crawl.
"""

import asyncio
import json
import re
from datetime import datetime, timezone

import httpx
from selectolax.parser import HTMLParser

import database as db

UA = {"User-Agent": "Mozilla/5.0 (compatible; dream-tracker/1.0)"}

MAX_HTML_BYTES = 2_000_000
MAX_TEXT_CHARS = 4_000
FETCH_TIMEOUT = 15
FETCH_CONCURRENCY = 6

# Chrome that lives inside <p> on nearly every site.
_BOILERPLATE_RE = re.compile(
    r"^(cookie|we use cookies|subscribe|sign up|sign in|log in|advertisement|"
    r"share this|follow us|related:|read more|all rights reserved|copyright)",
    re.IGNORECASE,
)
_STRIP_TAGS = ("script", "style", "noscript", "nav", "header", "footer", "aside",
               "form", "svg", "iframe")
_CONTENT_SELECTORS = ("article", "main", '[role="main"]', ".post-content",
                      ".article-body", ".entry-content", ".prose")

_SPACE_RE = re.compile(r"\s+")

# Meta tags that carry a publish date, best first.
_DATE_META = (
    ("meta[property='article:published_time']", "content"),
    ("meta[property='og:published_time']", "content"),
    ("meta[name='article:published_time']", "content"),
    ("meta[name='publish-date']", "content"),
    ("meta[name='publication_date']", "content"),
    ("meta[itemprop='datePublished']", "content"),
    ("meta[name='date']", "content"),
    ("time[datetime]", "datetime"),
)


def _clean(text: str) -> str:
    return _SPACE_RE.sub(" ", text or "").strip()


def _iso(dt) -> str:
    if not dt:
        return ""
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_date(raw: str) -> str:
    """ISO 8601 in, normalised ISO out. Imported lazily to avoid a cycle."""
    from fetchers.blogs import parse_date
    return _iso(parse_date(raw or ""))


def _jsonld_date(tree) -> str:
    """datePublished out of any JSON-LD block on the page."""
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text(strip=True) or "{}")
        except (json.JSONDecodeError, ValueError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            graph = entry.get("@graph")
            if isinstance(graph, list):
                candidates.extend(g for g in graph if isinstance(g, dict))
            for key in ("datePublished", "dateCreated", "uploadDate"):
                if entry.get(key):
                    parsed = _parse_date(str(entry[key]))
                    if parsed:
                        return parsed
    return ""


def extract_date(tree) -> str:
    for selector, attr in _DATE_META:
        node = tree.css_first(selector)
        if node is None:
            continue
        parsed = _parse_date(node.attributes.get(attr) or "")
        if parsed:
            return parsed
    return _jsonld_date(tree)


def extract_text(tree) -> str:
    """Readable body text: the densest content container's paragraphs."""
    for tag in _STRIP_TAGS:
        for node in tree.css(tag):
            node.decompose()

    container = None
    for selector in _CONTENT_SELECTORS:
        found = tree.css_first(selector)
        if found is not None and len(found.text(strip=True) or "") > 400:
            container = found
            break
    container = container if container is not None else tree.body or tree

    chunks, total = [], 0
    for p in container.css("p, li"):
        text = _clean(p.text(strip=True))
        # Short fragments are nav and captions; long ones are the article.
        if len(text) < 45 or _BOILERPLATE_RE.match(text):
            continue
        chunks.append(text)
        total += len(text)
        if total >= MAX_TEXT_CHARS:
            break

    if not chunks:
        text = _clean(container.text(separator=" ", strip=True))
        return text[:MAX_TEXT_CHARS]
    return " ".join(chunks)[:MAX_TEXT_CHARS]


def extract_title(tree) -> str:
    for selector, attr in (("meta[property='og:title']", "content"),
                           ("meta[name='twitter:title']", "content")):
        node = tree.css_first(selector)
        if node is not None and (node.attributes.get(attr) or "").strip():
            return _clean(node.attributes[attr])[:280]
    node = tree.css_first("h1") or tree.css_first("title")
    return _clean(node.text(strip=True))[:280] if node is not None else ""


async def _fetch_one(client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore) -> dict:
    """Fetch and parse one page. Never raises; the failure is the return value."""
    async with sem:
        try:
            r = await client.get(url, timeout=FETCH_TIMEOUT, follow_redirects=True)
            if r.status_code != 200:
                return {"url": url, "status": "error", "title": "", "text": "",
                        "published_at": ""}
            if "html" not in (r.headers.get("content-type") or "").lower():
                return {"url": url, "status": "empty", "title": "", "text": "",
                        "published_at": ""}
            tree = HTMLParser(r.text[:MAX_HTML_BYTES])
            text = extract_text(tree)
            return {
                "url": url,
                "status": "ok" if len(text) >= 120 else "empty",
                "title": extract_title(tree),
                "text": text,
                "published_at": extract_date(tree),
            }
        except Exception:
            return {"url": url, "status": "error", "title": "", "text": "",
                    "published_at": ""}


async def fetch_many(urls, client: httpx.AsyncClient = None) -> dict:
    """{url: {title, text, published_at, status}} for every URL asked for.

    Anything already in content_cache is returned from there without a request,
    which is what keeps the weekly run from re-scraping the whole back catalogue.
    """
    wanted = [u for u in dict.fromkeys(urls) if u and u.startswith("http")]
    if not wanted:
        return {}

    out, missing = {}, []
    for url in wanted:
        cached = db.content_cache_get(url)
        if cached:
            out[url] = {"title": cached["title"], "text": cached["text"],
                        "published_at": cached["published_at"],
                        "status": cached["status"]}
        else:
            missing.append(url)

    if not missing:
        return out

    own_client = client is None
    client = client or httpx.AsyncClient(headers=UA, follow_redirects=True)
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    try:
        results = await asyncio.gather(
            *[_fetch_one(client, u, sem) for u in missing], return_exceptions=True
        )
    finally:
        if own_client:
            await client.aclose()

    for url, res in zip(missing, results):
        if isinstance(res, Exception):
            res = {"url": url, "status": "error", "title": "", "text": "",
                   "published_at": ""}
        db.content_cache_put(url, res["title"], res["text"],
                             res["published_at"], res["status"])
        out[url] = {k: res[k] for k in ("title", "text", "published_at", "status")}
    return out


async def resolve_dates(urls, client: httpx.AsyncClient = None) -> dict:
    """{url: iso_date} for URLs whose page advertises a publish date."""
    pages = await fetch_many(urls, client)
    return {u: p["published_at"] for u, p in pages.items() if p.get("published_at")}
