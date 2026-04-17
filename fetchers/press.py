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


_FALSE_POS_RE = re.compile(
    r'\b(?:the|a|an|on|off|up|down|over|out|into|'
    r'exit|boat|ski|loading|access|entry|'
    r'great|new|old|big|small|long|short|'
    r'straight|wide|narrow|dark|light|deep|high|'
    r'pure|clean|clear|plain|simple|direct|'
    r'equal|sharp|flat|solid|dense|rich|raw|'
    r'core|true|open|free|full|fast|hard|soft|'
    r'inner|outer|upper|lower|basic|total|local)\s+',
    re.IGNORECASE,
)


def _is_about_company(title: str, snippet: str, company_name: str, domain: str) -> bool:
    """
    Returns True only when the article is genuinely about this company.

    The previous logic had a critical bug: the _BIZ_SIGNALS block was a
    standalone pass condition with no company-name requirement, so any
    article containing words like 'startup', 'announces', or 'funding'
    would pass for every company regardless of whether the company was
    mentioned at all.

    New logic:
    1. Company name MUST appear as a whole word in the title — hard gate.
    2. For short/ambiguous names (≤5 chars) that double as common nouns
       (Arc, Beam, Plain, Mesh…), at least one business-signal word must
       also appear in the title or RSS snippet to confirm context.
    3. Reject grammatical false positives where the name follows a generic
       determiner/adjective: "a linear approach", "the arc of history", etc.
    """
    t = title.lower()
    name_lower = company_name.lower()

    # 1. Hard gate: whole-word name match in title
    if not re.search(r'\b' + re.escape(name_lower) + r'\b', t):
        return False

    # 2. Short/common names need business context to confirm they refer to
    #    the company and not a generic noun or adjective.
    if len(name_lower) <= 5:
        combined = t + ' ' + (snippet or '').lower()
        if not any(sig in combined for sig in _BIZ_SIGNALS):
            return False

    # 3. Reject false positives where the name is used as a generic word
    #    following a determiner or generic qualifier.
    if len(name_lower) <= 7:
        pattern = _FALSE_POS_RE.pattern + re.escape(name_lower) + r'\b'
        if re.search(pattern, t, re.IGNORECASE):
            return False

    return True


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

            if not _is_about_company(title, desc, name, domain):
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
