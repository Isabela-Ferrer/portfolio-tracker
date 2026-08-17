import re
import asyncio
import httpx
from models import FundingSignal
from urllib.parse import quote, urlparse
from xml.etree.ElementTree import fromstring

AMOUNT_RE = re.compile(r'[\$€£][\d.]+\s*[MBKmb](?:illion)?')
_TAG_RE   = re.compile(r'<[^>]+>')
_SPACE_RE = re.compile(r'\s+')

# The news queries below are keyword searches, so they happily return roundup
# pieces like "The July US Venture Capital Funding Report". Without a gate every
# company looks like it raised every week and the funding flag means nothing.
#
# An event, not a topic. The old gate accepted the phrase "funding round"
# anywhere, which let database listings titled "2026 Funding Rounds & List of
# Investors" through and lit the funding flag for companies that raised nothing.
_EVENT_RE = re.compile(
    r'\b(raises?|raised|raising|closes?|closed|secures?|secured|lands?|landed|'
    r'nets?|netted)\b[^.]{0,60}?(\$|€|£|\bround\b|\bfunding\b|\bmillion\b|\bbillion\b)'
    r'|\bseries\s+[a-j]\b[^.]{0,40}?(\bfunding\b|\bround\b|\braise\b|\$)'
    r'|\b(valued at|valuation of|post-money|pre-money)\b'
    r'|[\$€£]\s?\d[\d.,]*\s*(m|bn|b|k|million|billion)\b'
    r'|\b(seed|pre-seed|series\s+[a-j])\s+round\b',
    re.IGNORECASE,
)

# Company database and profile sites. They rank well for "<company> funding" and
# publish a permanent page per company, so they resurface forever and are never
# actually news.
_DIRECTORY_DOMAINS = (
    'tracxn.com', 'crunchbase.com', 'pitchbook.com', 'cbinsights.com',
    'growjo.com', 'dealroom.co', 'owler.com', 'zoominfo.com', 'craft.co',
    'similarweb.com', 'getlatka.com', 'latka.com', 'clay.earth',
    'stockanalysis.com', 'wellfound.com', 'leadiq.com', 'rocketreach.co',
)
_DIRECTORY_TITLE_RE = re.compile(
    r'(list of investors|funding rounds? (&|and) |company profile|'
    r'competitors? (&|and) alternatives|revenue, growth|employee size|'
    r'\bfunding overview\b|\bcap table\b|- Tracxn$|\| Crunchbase)',
    re.IGNORECASE,
)


def _is_directory(url: str, title: str) -> bool:
    host = urlparse(url or '').netloc.replace('www.', '').lower()
    if any(host.endswith(d) for d in _DIRECTORY_DOMAINS):
        return True
    return bool(_DIRECTORY_TITLE_RE.search(title or ''))


def _is_funding_news(title: str, snippet: str, company_name: str,
                     url: str = '') -> bool:
    """Require the company by name in the headline and an actual round event."""
    name = (company_name or '').strip().lower()
    if not name:
        return False
    if not re.search(r'\b' + re.escape(name) + r'\b', (title or '').lower()):
        return False
    if _is_directory(url, title):
        return False
    return bool(_EVENT_RE.search(f"{title} {(snippet or '')[:400]}"))


def _extract_domain(company) -> str:
    url = company.website_url or ''
    try:
        if '://' not in url:
            url = 'https://' + url
        return urlparse(url).netloc.replace('www.', '').strip('/')
    except Exception:
        return ''


def _clean_html(raw: str) -> str:
    return _SPACE_RE.sub(' ', _TAG_RE.sub(' ', raw)).strip()


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
        paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', r.text, re.DOTALL | re.IGNORECASE)
        chunks, total = [], 0
        for p in paragraphs:
            text = _clean_html(p).strip()
            if len(text) < 40:
                continue
            chunks.append(text)
            total += len(text)
            if total >= 600:
                break
        return ' '.join(chunks)[:700]
    except Exception:
        return ""


async def fetch(company) -> list[FundingSignal]:
    domain = _extract_domain(company)
    name   = company.name

    domain_clause = f' {domain}' if domain else ''
    queries = [
        quote(f'"{name}" funding{domain_clause}'),
        quote(f'"{name}" raises{domain_clause}'),
        quote(f'"{name}" Series{domain_clause}'),
    ]

    signals: list[FundingSignal] = []
    seen_urls: set[str] = set()

    try:
        async with httpx.AsyncClient() as client:
            for q in queries:
                url  = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
                r    = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                root = fromstring(r.text)

                for item in root.findall(".//item")[:5]:
                    link = item.findtext("link", "")
                    if link in seen_urls:
                        continue
                    seen_urls.add(link)

                    title  = item.findtext("title", "")
                    amount = AMOUNT_RE.search(title)
                    desc   = _clean_html(item.findtext("description", ""))

                    if not _is_funding_news(title, desc, name, link):
                        continue

                    signals.append(FundingSignal(
                        title=title,
                        url=link,
                        date=item.findtext("pubDate", ""),
                        amount_hint=amount.group(0) if amount else None,
                        snippet=desc,
                    ))

            # Fetch article bodies concurrently
            snippets = await asyncio.gather(
                *[_fetch_snippet(s.url, client) for s in signals]
            )
            for signal, body in zip(signals, snippets):
                if body:
                    signal.snippet = body

    except Exception as e:
        print(f"[funding] Fetcher failed for {name}: {e}")

    return signals[:10]
