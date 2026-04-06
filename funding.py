import httpx
from models import FundingSignal
import re
from urllib.parse import quote
from xml.etree.ElementTree import fromstring


FUNDING_QUERIES = ['"{name}" funding', '"{name}" raises', '"{name}" Series']
AMOUNT_RE = re.compile(r'[\$€£][\d.]+\s*[MBKmb](?:illion|illion)?')

async def fetch(company) -> list[FundingSignal]:
    signals = []
    seen_urls = set()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for template in FUNDING_QUERIES:
                q = quote(template.format(name=company.name))
                url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                root = fromstring(r.text)
                for item in root.findall(".//item")[:5]:
                    link = item.findtext("link", "")
                    if link in seen_urls: continue
                    seen_urls.add(link)
                    title = item.findtext("title", "")
                    amount = AMOUNT_RE.search(title)
                    signals.append(FundingSignal(
                        title=title,
                        url=link,
                        date=item.findtext("pubDate", ""),
                        amount_hint=amount.group(0) if amount else None
                    ))
    except Exception as e:
        # Instead of crashing, we log the error and return what we found (or an empty list)
        print(f"Funding fetcher failed for {company.name}: {e}")
        
    return signals[:10]