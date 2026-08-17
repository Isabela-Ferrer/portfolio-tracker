"""Podcast appearances, via the free iTunes Search API. No key needed.

One search per tracked person. An episode counts only when the person's full
name actually appears in the episode title or description, because the search
endpoint happily returns loose keyword matches.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from models import Signal

SEARCH_URL = "https://itunes.apple.com/search"
UA = {"User-Agent": "Mozilla/5.0 (compatible; dream-tracker/1.0)"}

LOOKBACK_DAYS = 14
LIMIT = 25


def _parse_release(raw: str):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _mentions(person: str, *texts: str) -> bool:
    needle = (person or "").strip().lower()
    if not needle:
        return False
    return any(needle in (t or "").lower() for t in texts)


async def _search_person(client: httpx.AsyncClient, person: str) -> list:
    r = await client.get(
        SEARCH_URL,
        params={"media": "podcast", "entity": "podcastEpisode",
                "term": person, "limit": LIMIT},
        timeout=25,
    )
    r.raise_for_status()
    return r.json().get("results", [])


async def fetch(company, people: list, client: httpx.AsyncClient = None) -> list:
    """Recent podcast episodes featuring any tracked person at this company.

    Co-founders regularly appear on the same episode, so episodes are deduped on
    the iTunes episode GUID before they become signals.
    """
    if not people:
        return []

    own_client = client is None
    client = client or httpx.AsyncClient(headers=UA, follow_redirects=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    signals, seen_guids = [], set()

    try:
        results = await asyncio.gather(
            *[_search_person(client, p["name"]) for p in people],
            return_exceptions=True,
        )
        for person, episodes in zip(people, results):
            if isinstance(episodes, Exception):
                print(f"[podcasts] {company.name}/{person['name']}: {episodes}")
                continue
            for ep in episodes:
                released = _parse_release(ep.get("releaseDate"))
                if not released or released < cutoff:
                    continue
                title = ep.get("trackName") or ""
                desc = ep.get("description") or ep.get("shortDescription") or ""
                if not _mentions(person["name"], title, desc):
                    continue
                guid = ep.get("episodeGuid") or ep.get("trackViewUrl") or ""
                if not guid or guid in seen_guids:
                    continue
                seen_guids.add(guid)
                url = ep.get("trackViewUrl") or ep.get("episodeUrl") or ""
                if not url:
                    continue
                signals.append(Signal(
                    company_id=company.id,
                    type="podcast",
                    title=f"{person['name']} on {ep.get('collectionName') or 'a podcast'}: {title}"[:280],
                    url=url,
                    published_at=released.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                    # The show notes are the only description of what was said,
                    # and the summariser cannot do better than what it is given.
                    raw={"person": person["name"], "podcast": ep.get("collectionName"),
                         "episode": title, "guid": guid, "summary": desc[:1500]},
                ))
    finally:
        if own_client:
            await client.aclose()

    return signals
