import httpx
from models import ProductLaunch
from xml.etree.ElementTree import fromstring


async def fetch(company) -> list[ProductLaunch]:
    launches = []
    if company.product_hunt_slug:
        launches += await _fetch_product_hunt(company.product_hunt_slug)
    if company.github_org and company.github_repo:
        launches += await _fetch_github(company.github_org, company.github_repo)
    return sorted(launches, key=lambda x: x.date, reverse=True)[:10]

async def _fetch_product_hunt(slug: str) -> list[ProductLaunch]:
    url = f"https://www.producthunt.com/@{slug}/posts.rss"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
    root = fromstring(r.text)
    return [ProductLaunch(
        title=item.findtext("title", ""),
        url=item.findtext("link", ""),
        date=item.findtext("pubDate", ""),
        source="product_hunt"
    ) for item in root.findall(".//item")[:5]]

async def _fetch_github(org: str, repo: str) -> list[ProductLaunch]:
    url = f"https://github.com/{org}/{repo}/releases.atom"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
    root = fromstring(r.text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    return [ProductLaunch(
        title=entry.findtext("atom:title", "", ns),
        url=entry.find("atom:link", ns).get("href", ""),
        date=entry.findtext("atom:updated", "", ns),
        source="github"
    ) for entry in root.findall("atom:entry", ns)[:5]]