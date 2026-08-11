"""Setup-time discovery of a company's public surfaces.

Given only a name and a website, find the ATS board, blog feed, changelog,
YouTube channel and GitHub org. Everything is verified by actually fetching it
and checking it parses. Anything that cannot be verified is recorded as not
found rather than guessed at, so settings can show the gaps honestly.
"""

import asyncio
import re
from urllib.parse import urljoin, urlparse

import httpx

import database as db
from fetchers import blogs, careers

UA = {"User-Agent": "Mozilla/5.0 (compatible; dream-tracker/1.0)"}

FEED_PATHS = (
    "/blog/rss.xml", "/blog/atom.xml", "/blog/feed", "/blog/feed.xml", "/blog/rss",
    "/blog/index.xml", "/rss.xml", "/atom.xml", "/feed", "/feed.xml", "/index.xml",
)
BLOG_PAGES = ("/blog", "/news", "/posts")
CHANGELOG_PATHS = ("/changelog", "/docs/changelog", "/updates", "/releases",
                   "/whats-new", "/product-updates")

_ALT_LINK_RE = re.compile(
    r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', re.IGNORECASE)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
_YOUTUBE_RE = re.compile(
    r'https?://(?:www\.)?youtube\.com/(@[\w.\-]+|channel/UC[\w\-]+|c/[\w\-]+|user/[\w\-]+)',
    re.IGNORECASE)
_GITHUB_RE = re.compile(r'https?://(?:www\.)?github\.com/([A-Za-z0-9][\w.\-]*)', re.IGNORECASE)

_GITHUB_NON_ORGS = {
    "features", "pricing", "about", "login", "join", "sponsors", "topics",
    "explore", "marketplace", "apps", "orgs", "settings", "readme", "site",
}


def _base(website: str) -> str:
    if not website:
        return ""
    site = website.strip().rstrip("/")
    return site if "://" in site else "https://" + site


async def _get(client, url: str):
    try:
        r = await client.get(url, timeout=20)
        return r if r.status_code == 200 else None
    except Exception:
        return None


async def _feed_has_entries(client, url: str) -> bool:
    """A feed only counts if it actually carries entries.

    Several of these companies publish a well-formed but permanently empty
    changelog feed; treating that as a hit would look like coverage we do not
    have.
    """
    r = await _get(client, url)
    if not r:
        return False
    if not blogs._looks_like_xml(r.text, r.headers.get("content-type", "")):
        return False
    try:
        return len(blogs.parse_feed(r.text)) > 0
    except Exception:
        return False


async def _alt_feed_links(client, page_url: str) -> list:
    r = await _get(client, page_url)
    if not r:
        return []
    out = []
    for tag in _ALT_LINK_RE.findall(r.text):
        m = _HREF_RE.search(tag)
        if m:
            out.append(urljoin(page_url, m.group(1)))
    return out


async def discover_blog(client, website: str) -> dict:
    """Find a blog feed, falling back to a parseable listing page."""
    base = _base(website)
    if not base:
        return {"url": None, "method": None}

    # 1. Feeds the site advertises itself.
    advertised = []
    for page in ("",) + BLOG_PAGES:
        advertised.extend(await _alt_feed_links(client, base + page))
    for url in dict.fromkeys(advertised):
        if "comment" in url.lower():
            continue
        if await _feed_has_entries(client, url):
            return {"url": url, "method": "advertised_feed"}

    # 2. Conventional feed paths.
    for path in FEED_PATHS:
        url = base + path
        if await _feed_has_entries(client, url):
            return {"url": url, "method": "feed_path"}

    # 3. No feed anywhere: use the listing page if post links can be read off it.
    for path in BLOG_PAGES:
        url = base + path
        r = await _get(client, url)
        if not r:
            continue
        try:
            if len(blogs.parse_html_post_links(r.text, url)) >= 3:
                return {"url": url, "method": "listing_page"}
        except Exception:
            continue

    return {"url": None, "method": None}


async def discover_changelog(client, website: str) -> dict:
    base = _base(website)
    if not base:
        return {"url": None, "method": None}

    host = urlparse(base).netloc.replace("www.", "")
    candidates = [base + p for p in CHANGELOG_PATHS] + [f"https://changelog.{host}"]

    for url in candidates:
        r = await _get(client, url)
        if not r:
            continue
        # Prefer a feed if the changelog advertises one.
        for alt in await _alt_feed_links(client, url):
            if "changelog" in alt.lower() or "release" in alt.lower():
                if await _feed_has_entries(client, alt):
                    return {"url": alt, "method": "changelog_feed"}
        if blogs._looks_like_xml(r.text, r.headers.get("content-type", "")):
            if await _feed_has_entries(client, url):
                return {"url": url, "method": "changelog_feed"}
            continue
        try:
            if len(blogs.parse_html_entries(r.text, url)) >= 2:
                return {"url": url, "method": "changelog_page"}
        except Exception:
            continue

    # Some companies keep the changelog feed at a path with no HTML page.
    for url in (base + "/changelog/rss.xml", base + "/changelog/feed.xml"):
        if await _feed_has_entries(client, url):
            return {"url": url, "method": "changelog_feed"}

    return {"url": None, "method": None}


async def discover_social(client, website: str) -> dict:
    """YouTube channel and GitHub org, read off the company's own pages."""
    base = _base(website)
    out = {"youtube_channel": None, "github_org": None}
    if not base:
        return out

    pages = [base, base + "/about", base + "/company"]
    texts = []
    for r in await asyncio.gather(*[_get(client, p) for p in pages]):
        if r:
            texts.append(r.text)
    blob = "\n".join(texts)

    m = _YOUTUBE_RE.search(blob)
    if m:
        out["youtube_channel"] = f"https://www.youtube.com/{m.group(1)}"

    for org in _GITHUB_RE.findall(blob):
        if org.lower() in _GITHUB_NON_ORGS:
            continue
        out["github_org"] = org
        break

    return out


async def discover_company(name: str, website: str) -> dict:
    """Run every discovery probe for one company."""
    result = {
        "ats_platform": None, "ats_slug": None, "ats_method": None,
        "blog_rss": None, "blog_method": None,
        "changelog": None, "changelog_method": None,
        "youtube_channel": None, "github_org": None,
        "not_found": [], "checked_at": db.now_iso(),
    }

    ats = await careers.discover_ats(name, website)
    result["ats_platform"] = ats["ats_platform"]
    result["ats_slug"] = ats["ats_slug"]
    result["ats_method"] = ats["method"]

    async with httpx.AsyncClient(headers=UA, follow_redirects=True) as client:
        blog, changelog, social = await asyncio.gather(
            discover_blog(client, website),
            discover_changelog(client, website),
            discover_social(client, website),
        )

    result["blog_rss"] = blog["url"]
    result["blog_method"] = blog["method"]
    result["changelog"] = changelog["url"]
    result["changelog_method"] = changelog["method"]
    result["youtube_channel"] = social["youtube_channel"]
    result["github_org"] = social["github_org"]

    for key in ("ats_slug", "blog_rss", "changelog", "youtube_channel", "github_org"):
        if not result[key]:
            result["not_found"].append(key)

    return result


async def run_discovery(company_id: int, overwrite: bool = False) -> dict:
    """Discover and persist. Existing values are kept unless overwrite is set."""
    company = db._get_company_by_id(company_id)
    if not company:
        raise ValueError(f"company {company_id} not found")

    found = await discover_company(company["name"], company.get("website") or "")

    if found["ats_slug"] and (overwrite or not company.get("ats_slug")):
        db.update_company(company_id, ats_platform=found["ats_platform"],
                          ats_slug=found["ats_slug"])

    existing = {s["type"] for s in db.list_sources(company_id)}
    for key, stype in (("blog_rss", "blog_rss"), ("changelog", "changelog"),
                       ("youtube_channel", "youtube_channel")):
        url = found[key]
        if url and (overwrite or stype not in existing):
            db.replace_source(company_id, stype, url)

    if found["github_org"] and (overwrite or "github_org" not in existing):
        db.replace_source(company_id, "github_org",
                          f"https://github.com/{found['github_org']}")

    db.set_discovery(company_id, found)
    return found
