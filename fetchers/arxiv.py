"""arXiv papers by author, for people flagged track_arxiv.

Free API, no key. Only runs for people explicitly flagged, which is a handful
of researchers (Tri Dao, John Schulman, Barret Zoph, Aidan Gomez).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from xml.etree.ElementTree import fromstring

import httpx

from models import Signal

API = "https://export.arxiv.org/api/query"
UA = {"User-Agent": "Mozilla/5.0 (compatible; dream-tracker/1.0)"}
NS = "{http://www.w3.org/2005/Atom}"

LOOKBACK_DAYS = 45
MAX_RESULTS = 10
TIMEOUT = 45

# arXiv asks callers to space requests out, and answering several at once just
# earns read timeouts. One request at a time across the whole run, with a
# courtesy gap. Companies still refresh concurrently around it.
_LOCK = asyncio.Lock()
_COURTESY_DELAY = 3


def _iso(raw: str) -> str:
    try:
        return datetime.fromisoformat((raw or "").replace("Z", "+00:00")) \
            .astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return ""


async def _author_papers(client: httpx.AsyncClient, name: str) -> list:
    async with _LOCK:
        r = await client.get(API, params={
            "search_query": f'au:"{name}"',
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": MAX_RESULTS,
        }, timeout=TIMEOUT)
        await asyncio.sleep(_COURTESY_DELAY)
    r.raise_for_status()
    root = fromstring(r.text)

    papers = []
    for entry in root.findall(f"{NS}entry"):
        authors = [a.findtext(f"{NS}name", "") for a in entry.findall(f"{NS}author")]
        # arXiv author search is fuzzy; require the name to actually be on it.
        if not any(name.lower() == (a or "").strip().lower() for a in authors):
            continue
        papers.append({
            "title": " ".join((entry.findtext(f"{NS}title") or "").split()),
            "url": (entry.findtext(f"{NS}id") or "").strip(),
            "published": entry.findtext(f"{NS}published") or "",
            "summary": " ".join((entry.findtext(f"{NS}summary") or "").split())[:400],
            "authors": authors,
        })
    return papers


async def fetch(company, people: list, client: httpx.AsyncClient = None) -> list:
    tracked = [p for p in (people or []) if p.get("track_arxiv")]
    if not tracked:
        return []

    own_client = client is None
    client = client or httpx.AsyncClient(headers=UA, follow_redirects=True)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)) \
        .strftime("%Y-%m-%dT%H:%M:%S")

    try:
        results = await asyncio.gather(
            *[_author_papers(client, p["name"]) for p in tracked],
            return_exceptions=True,
        )
    finally:
        if own_client:
            await client.aclose()

    signals, seen = [], set()
    for person, papers in zip(tracked, results):
        if isinstance(papers, Exception):
            print(f"[arxiv] {company.name}/{person['name']}: "
                  f"{type(papers).__name__}: {papers}")
            continue
        for p in papers:
            published = _iso(p["published"])
            if not published or published < cutoff:
                continue
            if not p["url"] or p["url"] in seen:
                continue
            seen.add(p["url"])
            signals.append(Signal(
                company_id=company.id, type="arxiv",
                title=f"{person['name']}: {p['title']}"[:280],
                url=p["url"], published_at=published,
                raw={"person": person["name"], "authors": p["authors"][:8],
                     "summary": p["summary"]},
            ))
    return signals
