"""YouTube activity, via the Data API v3. Needs YOUTUBE_API_KEY.

Two modes:
  channel  uploads from a youtube_channel source since the last snapshot
  search   videos matching "{person}" {company} since the last snapshot

Quota: channel mode costs 1 unit per page (playlistItems), search costs 100 per
call. Fifteen companies with a couple of people each stays far inside the free
10k daily quota. Without a key the fetcher returns nothing rather than failing,
so the rest of the run is unaffected.
"""

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

from models import Signal

load_dotenv()

API = "https://www.googleapis.com/youtube/v3"
UA = {"User-Agent": "Mozilla/5.0 (compatible; dream-tracker/1.0)"}

DEFAULT_LOOKBACK_DAYS = 14
MAX_RESULTS = 15

_CHANNEL_ID_RE = re.compile(r"^UC[\w-]{20,}$")


def api_key() -> str:
    return os.getenv("YOUTUBE_API_KEY") or ""


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(raw: str) -> str:
    try:
        return datetime.fromisoformat((raw or "").replace("Z", "+00:00")) \
            .astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return ""


async def _get(client: httpx.AsyncClient, path: str, params: dict):
    params = {**params, "key": api_key()}
    r = await client.get(f"{API}/{path}", params=params, timeout=25)
    r.raise_for_status()
    return r.json()


async def _uploads_playlist(client: httpx.AsyncClient, channel_url: str):
    """Resolve a channel URL (handle, /channel/UC..., /c/name) to its uploads playlist."""
    path = urlparse(channel_url).path.strip("/")
    params = {"part": "contentDetails"}

    if path.startswith("channel/"):
        cid = path.split("/", 1)[1]
        if _CHANNEL_ID_RE.match(cid):
            params["id"] = cid
        else:
            return None
    elif path.startswith("@"):
        params["forHandle"] = path
    elif path.startswith(("c/", "user/")):
        params["forUsername"] = path.split("/", 1)[1]
    else:
        params["forHandle"] = "@" + path

    data = await _get(client, "channels", params)
    items = data.get("items") or []
    if not items:
        return None
    return (items[0].get("contentDetails", {})
            .get("relatedPlaylists", {})
            .get("uploads"))


async def _channel_uploads(client: httpx.AsyncClient, company, channel_url: str,
                           since: datetime) -> list:
    playlist = await _uploads_playlist(client, channel_url)
    if not playlist:
        return []
    data = await _get(client, "playlistItems", {
        "part": "snippet", "playlistId": playlist, "maxResults": MAX_RESULTS,
    })
    out = []
    for item in data.get("items", []):
        snip = item.get("snippet", {})
        published = _iso(snip.get("publishedAt", ""))
        if not published or published < since.strftime("%Y-%m-%dT%H:%M:%S"):
            continue
        vid = (snip.get("resourceId") or {}).get("videoId")
        if not vid:
            continue
        out.append(Signal(
            company_id=company.id, type="youtube",
            title=snip.get("title", "")[:280],
            url=f"https://www.youtube.com/watch?v={vid}",
            published_at=published,
            raw={"mode": "channel", "channel": snip.get("channelTitle", ""),
                 "summary": (snip.get("description") or "")[:400]},
        ))
    return out


async def _person_search(client: httpx.AsyncClient, company, person: dict,
                         since: datetime) -> list:
    data = await _get(client, "search", {
        "part": "snippet", "q": f'"{person["name"]}" {company.name}',
        "publishedAfter": _rfc3339(since), "type": "video",
        "maxResults": 10, "order": "date",
    })
    needle = person["name"].lower()
    out = []
    for item in data.get("items", []):
        snip = item.get("snippet", {})
        title = snip.get("title", "")
        desc = snip.get("description", "")
        # The search endpoint is loose; require the actual name to appear.
        if needle not in f"{title} {desc}".lower():
            continue
        vid = (item.get("id") or {}).get("videoId")
        if not vid:
            continue
        out.append(Signal(
            company_id=company.id, type="youtube",
            title=f"{person['name']}: {title}"[:280],
            url=f"https://www.youtube.com/watch?v={vid}",
            published_at=_iso(snip.get("publishedAt", "")),
            raw={"mode": "search", "person": person["name"],
                 "channel": snip.get("channelTitle", ""), "summary": desc[:400]},
        ))
    return out


async def fetch(company, people: list, sources: list, since: datetime = None,
                client: httpx.AsyncClient = None) -> list:
    if not api_key():
        return []

    since = since or (datetime.now(timezone.utc) - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    channels = [s["url"] for s in (sources or []) if s["type"] == "youtube_channel"]

    own_client = client is None
    client = client or httpx.AsyncClient(headers=UA, follow_redirects=True)
    try:
        tasks = [_channel_uploads(client, company, url, since) for url in channels]
        tasks += [_person_search(client, company, p, since) for p in (people or [])]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if own_client:
            await client.aclose()

    signals, seen = [], set()
    for res in results:
        if isinstance(res, Exception):
            print(f"[youtube] {company.name}: {res}")
            continue
        for s in res:
            if s.url in seen:
                continue
            seen.add(s.url)
            signals.append(s)
    return signals
