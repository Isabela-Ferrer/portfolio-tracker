import httpx
from models import ProductLaunch
from xml.etree.ElementTree import fromstring
from selectolax.parser import HTMLParser


async def fetch(company) -> list[ProductLaunch]:
    launches = []
    if company.product_hunt_slug:
        launches += await _fetch_product_hunt(company.product_hunt_slug)
    if company.github_org and company.github_repo:
        launches += await _fetch_github(company.github_org, company.github_repo)
    return sorted(launches, key=lambda x: x.date, reverse=True)[:10]

async def _fetch_product_hunt(slug: str) -> list[dict]:
    # Target the /launches subpage instead of the RSS feed
    url = f"https://www.producthunt.com/products/{slug}/launches"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers=headers)
        if r.status_code != 200:
            return []

    tree = HTMLParser(r.text)
    launches = []

    # Product Hunt lists launches in specific containers
    # We look for the launch items (based on the current structure)
    items = tree.css('div[class*="styles_item"]')[:5]

    for item in items:
        title_node = item.css_first('strong[class*="styles_title"]')
        desc_node = item.css_first('div[class*="styles_tagline"]')
        link_node = item.css_first('a[href*="/posts/"]')
        date_node = item.css_first('time') or item.css_first('div[class*="styles_date"]')

        launch_data = {
            "title": title_node.text(strip=True) if title_node else "N/A",
            "description": desc_node.text(strip=True) if desc_node else "N/A",
            "url": f"https://www.producthunt.com{link_node.attributes['href']}" if link_node else "N/A",
            "date": date_node.text(strip=True) if date_node else "N/A",
            "source": "product_hunt_launches"
        }
        launches.append(launch_data)

    return launches

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