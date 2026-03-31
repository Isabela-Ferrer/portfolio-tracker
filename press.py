import httpx
from xml.etree.ElementTree import fromstring
from urllib.parse import quote
from models import PressArticle


async def fetch(company) -> list[PressArticle]:
    query = quote(f'"{company.name}"')
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
    root = fromstring(r.text)
    articles = []
    for item in root.findall(".//item")[:10]:
        articles.append(PressArticle(
            title=item.findtext("title", ""),
            url=item.findtext("link", ""),
            source=item.findtext("source", ""),
            published_at=item.findtext("pubDate", ""),
        ))
    return articles


