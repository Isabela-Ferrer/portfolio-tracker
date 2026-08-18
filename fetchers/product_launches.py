"""Product Hunt launches and GitHub releases.

Carried over from the VC-era tracker with one change: the GitHub org now comes
from the sources table rather than a column on the company.
"""

import asyncio
import re
from xml.etree.ElementTree import fromstring

import httpx
from selectolax.parser import HTMLParser

from models import ProductLaunch

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

MAX_REPOS = 3
MAX_RELEASES = 5
# After filtering. A company shipping three genuinely notable releases in a week
# is already unusual.
MAX_KEPT_RELEASES = 3

_SEMVER_RE = re.compile(r"\bv?(\d+)\.(\d+)\.(\d+)")

# Tags no human wrote. These come out of SDK codegen pipelines and release bots
# and are never the thing Isa wants to read about.
_MACHINE_TAG_RE = re.compile(
    r"(fern-generation|generation-base|snapshot|nightly|canary|"
    r"-rc\.?\d|alpha\.?\d|beta\.?\d|dependabot|renovate)",
    re.IGNORECASE,
)
# Release notes that say nothing: "Full Changelog: ...", "chore: bump version".
_THIN_NOTES_RE = re.compile(
    r"^\s*(full changelog|what'?s changed|chore|bump|version bump|"
    r"no changes|release \d)",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
MIN_NOTES_CHARS = 200


def _notes_text(raw: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", raw or "")).strip()


def is_noteworthy_release(entry_title: str, notes: str) -> bool:
    """Is this release a shipped thing, or a package publish?

    The test that actually separates them is whether a human wrote release
    notes. SDK repos publish constantly and bump the minor version while doing
    it, so version position says nothing: elevenlabs-js v2.62.0 is semantically
    a minor release and editorially nothing at all. A major version is the one
    exception, because x.0.0 is a milestone whether or not anyone wrote it up.
    """
    title = entry_title or ""
    if _MACHINE_TAG_RE.search(title):
        return False

    match = _SEMVER_RE.search(title)
    if not match:
        # No version in the tag usually means a named release, which is worth
        # keeping as long as it is not machine generated.
        return True

    major, minor, patch = (int(g) for g in match.groups())
    if major > 0 and minor == 0 and patch == 0:
        return True

    body = _notes_text(notes)
    return len(body) >= MIN_NOTES_CHARS and not _THIN_NOTES_RE.match(body)


def _ph_slug(company) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (company.name or "").lower()).strip("-")


def _github_org(sources: list) -> str:
    for s in sources or []:
        if s["type"] == "github_org":
            return s["url"].rstrip("/").split("/")[-1]
    return ""


async def fetch(company, sources: list = None, client: httpx.AsyncClient = None) -> list:
    own_client = client is None
    client = client or httpx.AsyncClient(headers=UA, follow_redirects=True)
    try:
        results = await asyncio.gather(
            _fetch_product_hunt(client, _ph_slug(company)),
            _fetch_github_org(client, _github_org(sources or [])),
            return_exceptions=True,
        )
    finally:
        if own_client:
            await client.aclose()

    launches = []
    for res in results:
        if isinstance(res, Exception):
            print(f"[product_launches] {company.name}: {res}")
            continue
        launches.extend(res)
    return sorted(launches, key=lambda x: x.date or "", reverse=True)[:10]


async def _fetch_product_hunt(client: httpx.AsyncClient, slug: str) -> list:
    if not slug:
        return []
    url = f"https://www.producthunt.com/products/{slug}/launches"
    r = await client.get(url, timeout=20)
    if r.status_code != 200:
        return []

    tree = HTMLParser(r.text)
    launches = []
    for item in tree.css('div[class*="styles_item"]')[:5]:
        title_node = item.css_first('strong[class*="styles_title"]')
        link_node = item.css_first('a[href*="/posts/"]')
        date_node = item.css_first("time") or item.css_first('div[class*="styles_date"]')
        if not title_node or not link_node:
            continue
        href = link_node.attributes.get("href", "")
        launches.append(ProductLaunch(
            title=title_node.text(strip=True),
            url=f"https://www.producthunt.com{href}" if href.startswith("/") else href,
            date=(date_node.attributes.get("datetime") if date_node else "")
                 or (date_node.text(strip=True) if date_node else ""),
            source="product_hunt",
        ))
    return launches


async def _fetch_github_org(client: httpx.AsyncClient, org: str) -> list:
    """Releases from the org's most recently pushed repos."""
    if not org:
        return []
    r = await client.get(
        f"https://api.github.com/orgs/{org}/repos",
        params={"sort": "pushed", "per_page": MAX_REPOS},
        headers={"Accept": "application/vnd.github+json"},
        timeout=20,
    )
    if r.status_code != 200:
        return []
    repos = [repo.get("name") for repo in r.json() if repo.get("name")]

    results = await asyncio.gather(
        *[_fetch_repo_releases(client, org, repo) for repo in repos],
        return_exceptions=True,
    )
    out = []
    for res in results:
        if not isinstance(res, Exception):
            out.extend(res)
    return out


async def _fetch_repo_releases(client: httpx.AsyncClient, org: str, repo: str) -> list:
    r = await client.get(f"https://github.com/{org}/{repo}/releases.atom", timeout=20)
    if r.status_code != 200:
        return []
    root = fromstring(r.text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    out = []
    for entry in root.findall("atom:entry", ns)[:MAX_RELEASES]:
        entry_title = entry.findtext("atom:title", "", ns)
        notes = entry.findtext("atom:content", "", ns)
        if not is_noteworthy_release(entry_title, notes):
            continue
        link = entry.find("atom:link", ns)
        out.append(ProductLaunch(
            title=f"{repo} {entry_title}".strip(),
            url=link.get("href", "") if link is not None else "",
            date=entry.findtext("atom:updated", "", ns),
            source="github",
        ))
        if len(out) >= MAX_KEPT_RELEASES:
            break
    return [l for l in out if l.url]
