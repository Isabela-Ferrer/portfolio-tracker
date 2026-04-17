import re
import asyncio
import httpx
from xml.etree.ElementTree import fromstring
from urllib.parse import quote, urlparse
from models import PressArticle


_BIZ_SIGNALS = frozenset([
    'startup', 'company', 'funding', 'raises', 'raised', 'ceo', 'cto', 'cfo',
    'co-founder', 'cofounder', 'founded', 'fintech', 'saas', 'valuation',
    'series', 'seed round', 'ipo', 'acquisition', 'acquires', 'acquired',
    'revenue', 'launches', 'platform', 'customers', 'enterprise',
    'announces', 'announcement', 'partners', 'partnership', 'expands',
    'hires', 'appoints', 'investors', 'unicorn', 'layoffs', 'employees',
    'product launch', 'api', 'integration', 'raises capital',
])

_TAG_RE    = re.compile(r'<[^>]+>')
_SPACE_RE  = re.compile(r'\s+')


def _extract_domain(company) -> str:
    url = company.website_url or ''
    try:
        if '://' not in url:
            url = 'https://' + url
        return urlparse(url).netloc.replace('www.', '').strip('/')
    except Exception:
        return ''


def _clean_html(raw: str) -> str:
    text = _TAG_RE.sub(' ', raw)
    return _SPACE_RE.sub(' ', text).strip()


def _is_about_company(title: str, company_name: str, domain: str) -> bool:
    t = title.lower()
    if domain and domain.lower() in t:
        return True
    if any(sig in t for sig in _BIZ_SIGNALS):
        return True
    if re.search(r'\b' + re.escape(company_name) + r'\b', title):
        if len(company_name) <= 6:
            false_pos = re.compile(
                r'\b(the|a|an|on|off|up|down|over|exit|boat|loading|ski|launch|access|entry)\s+'
                + re.escape(company_name.lower()) + r'\b',
                re.IGNORECASE,
            )
            if false_pos.search(title):
                return False
        return True
    return False


async def _fetch_snippet(url: str, client: httpx.AsyncClient) -> str:
    """Try to grab the first ~600 chars of article body text. Never raises."""
    try:
        r = await client.get(
            url,
            follow_redirects=True,
            timeout=5,
            headers={"User-Agent": "Mozilla/5.0 (compatible; portfolio-tracker/1.0)"},
        )
        if r.status_code != 200:
            return ""
        # Pull all <p> tag contents and take the first substantial paragraphs
        paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', r.text, re.DOTALL | re.IGNORECASE)
        chunks = []
        total = 0
        for p in paragraphs:
            text = _clean_html(p).strip()
            if len(text) < 40:          # skip nav/boilerplate fragments
                continue
            chunks.append(text)
            total += len(text)
            if total >= 600:
                break
        return ' '.join(chunks)[:700]
    except Exception:
        return ""


async def _rss_items(client: httpx.AsyncClient, query: str) -> list:
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    return fromstring(r.text).findall(".//item")


async def fetch(company) -> list[PressArticle]:
    domain = _extract_domain(company)
    name   = company.name

    async with httpx.AsyncClient() as client:
        # Pass 1 — name + domain anchor (highest precision)
        q1 = quote(f'"{name}" {domain}') if domain else quote(f'"{name}"')
        items1 = await _rss_items(client, q1)

        # Pass 2 — name + business-language signals (broader recall)
        q2 = quote(f'"{name}" (startup OR company OR funding OR raises OR CEO OR announces OR product OR platform)')
        items2 = await _rss_items(client, q2)

        # Deduplicate and relevance-filter
        seen_urls: set[str] = set()
        candidates: list[PressArticle] = []

        for item in items1 + items2:
            title     = item.findtext("title", "")
            link      = item.findtext("link", "")
            source    = item.findtext("source", "")
            published = item.findtext("pubDate", "")
            desc      = _clean_html(item.findtext("description", ""))

            if link in seen_urls:
                continue
            seen_urls.add(link)

            if not _is_about_company(title, name, domain):
                continue

            candidates.append(PressArticle(
                title=title,
                url=link,
                source=source,
                published_at=published,
                snippet=desc,   # RSS description as initial snippet; overwritten below
            ))
            if len(candidates) >= 10:
                break

        # Fetch article bodies concurrently (best-effort, failures silently ignored)
        snippets = await asyncio.gather(
            *[_fetch_snippet(a.url, client) for a in candidates]
        )
        for article, body in zip(candidates, snippets):
            if body:
                article.snippet = body      # prefer full article text over RSS desc

    return candidates
