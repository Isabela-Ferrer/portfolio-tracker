import httpx
from bs4 import BeautifulSoup
from typing import Optional
import re
from models import JobsData

CAREERS_PATHS = ["/careers", "/jobs", "/join-us", "/work-with-us", "/about/careers"]

async def fetch(company) -> Optional[JobsData]:
    base = company.website_url.rstrip("/")
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for path in CAREERS_PATHS:
            try:
                full_url = base + path
                r = await client.get(full_url, headers={"User-Agent": "Mozilla/5.0"})
                
                if r.status_code == 200:
                    html = r.text
                    
                    # 1. Platform Detection & API Shortcut
                    # Check for Ashby
                    if "ashbyhq.com" in html:
                        slug = _extract_slug(html, r"ashbyhq\.com/([\w-]+)")
                        if slug: return await _fetch_ashby_api(slug)
                    
                    # Check for Greenhouse
                    if "boards.greenhouse.io" in html:
                        slug = _extract_slug(html, r"boards\.greenhouse\.io/([\w-]+)")
                        if slug: return await _fetch_greenhouse_api(slug)

                    # 2. Fallback: Standard Brute-force Scraper
                    return _parse_jobs_fallback(html, full_url)
                    
            except Exception as e:
                print(f"Error fetching {path}: {e}")
                continue
                
    return JobsData(total_count=0, roles=[], source_url="")

def _extract_slug(html: str, pattern: str) -> Optional[str]:
    """Helper to find company IDs in scripts/iframes."""
    match = re.search(pattern, html)
    return match.group(1) if match else None

async def _fetch_ashby_api(slug: str) -> JobsData:
    """Hits Ashby's public API for a clean list of jobs."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
        if r.status_code == 200:
            data = r.json()
            jobs = data.get("jobs", [])
            roles = [j["title"] for j in jobs]
            return JobsData(
                total_count=len(roles),
                roles=roles[:20],
                source_url=f"https://jobs.ashbyhq.com/{slug}"
            )
    return JobsData(total_count=0, roles=[], source_url="")

async def _fetch_greenhouse_api(slug: str) -> JobsData:
    """Hits Greenhouse's public API."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
        if r.status_code == 200:
            data = r.json()
            jobs = data.get("jobs", [])
            roles = [j["title"] for j in jobs]
            return JobsData(
                total_count=len(roles),
                roles=roles[:20],
                source_url=f"https://boards.greenhouse.io/{slug}"
            )
    return JobsData(total_count=0, roles=[], source_url="")

def _parse_jobs_fallback(html: str, source_url: str) -> JobsData:
    """The original keyword-based scraper for custom career pages."""
    soup = BeautifulSoup(html, "lxml")
    job_keywords = ["job", "role", "position", "opening", "career"]
    candidates = []
    
    for tag in soup.find_all(["li", "article", "div", "a"]):
        # Check class names or IDs for job-related keywords
        attr_string = " ".join(tag.get("class", []) + [tag.get("id", "")])
        if any(kw in attr_string.lower() for kw in job_keywords):
            text = tag.get_text(strip=True)[:60]
            if 5 < len(text) < 100:
                candidates.append(text)
    
    roles = list(dict.fromkeys(candidates))[:20]
    return JobsData(total_count=len(roles), roles=roles, source_url=source_url)