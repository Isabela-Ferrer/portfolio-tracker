import re
import asyncio
import httpx
from models import FundingSignal
from urllib.parse import quote, urlparse
from xml.etree.ElementTree import fromstring

AMOUNT_RE = re.compile(r'[\$€£][\d.]+\s*[MBKmb](?:illion)?')
_TAG_RE   = re.compile(r'<[^>]+>')
_SPACE_RE = re.compile(r'\s+')


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
