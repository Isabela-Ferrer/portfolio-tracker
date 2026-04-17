"""
G2 Reviews fetcher.
Scrapes https://www.g2.com/products/{slug}/reviews for rating, review count,
and the first handful of recent reviews using server-rendered HTML + JSON-LD.
"""

import re
import json
import httpx
from typing import Optional
from models import AppStoreData, AppReview

_TAG_RE   = re.compile(r'<[^>]+>')
_SPACE_RE = re.compile(r'\s+')

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _clean(raw: str) -> str:
    return _SPACE_RE.sub(' ', _TAG_RE.sub(' ', raw)).strip()


def _extract_jsonld(html: str) -> Optional[dict]:
    """Find the first JSON-LD <script> block that contains aggregateRating."""
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    ):
        try:
            data = json.loads(block)
            # Could be a list or a single object
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("aggregateRating"):
                    return item
        except Exception:
            continue
    return None


def _extract_reviews_from_html(html: str) -> list[AppReview]:
    """
    Try to pull recent review snippets from the rendered HTML.
    G2 wraps individual review text in itemprop="description" spans inside
    [itemprop="review"] containers.
    """
    reviews: list[AppReview] = []

    # Find individual review blocks
    review_blocks = re.findall(
        r'itemprop=["\']review["\'][^>]*>(.*?)</(?:div|article|section)',
        html, re.DOTALL | re.IGNORECASE
    )
    for block in review_blocks[:5]:
        # Rating
        rating_m = re.search(
            r'itemprop=["\']ratingValue["\'][^>]*content=["\']([0-9.]+)["\']|'
            r'content=["\']([0-9.]+)["\'][^>]*itemprop=["\']ratingValue["\']',
            block
        )
        rating = float(rating_m.group(1) or rating_m.group(2)) if rating_m else 0.0

        # Review text
        text_m = re.search(
            r'itemprop=["\']description["\'][^>]*>(.*?)</(?:p|span|div)',
            block, re.DOTALL | re.IGNORECASE
        )
        text = _clean(text_m.group(1))[:200] if text_m else ""

        # Date
        date_m = re.search(r'datetime=["\']([0-9T:\-Z]+)["\']', block)
        date = date_m.group(1)[:10] if date_m else ""

        if text:
            reviews.append(AppReview(rating=rating, text=text, date=date))

    return reviews


async def fetch(slug: str) -> Optional[AppStoreData]:
    """Fetch G2 rating + reviews for a given product slug. Returns None on failure."""
    url = f"https://www.g2.com/products/{slug}/reviews"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, headers=_HEADERS)
            if r.status_code != 200:
                print(f"[g2] HTTP {r.status_code} for {slug}")
                return None
            html = r.text
    except Exception as e:
        print(f"[g2] Request failed for {slug}: {e}")
        return None

    # --- 1. Try JSON-LD structured data first (most reliable) ---
    avg_rating   = 0.0
    review_count = 0
    jsonld = _extract_jsonld(html)
    if jsonld:
        ar = jsonld.get("aggregateRating", {})
        try:
            avg_rating   = float(ar.get("ratingValue") or 0)
            review_count = int(ar.get("reviewCount") or ar.get("ratingCount") or 0)
        except (TypeError, ValueError):
            pass

    # --- 2. Regex fallback for rating/count ---
    if not avg_rating:
        m = re.search(r'"ratingValue"\s*:\s*"?([0-9.]+)"?', html)
        if m:
            try: avg_rating = float(m.group(1))
            except ValueError: pass

    if not review_count:
        m = re.search(r'"reviewCount"\s*:\s*"?([0-9]+)"?', html)
        if m:
            try: review_count = int(m.group(1))
            except ValueError: pass

    # Try alternate pattern: "4.5 out of 5" from meta description or page copy
    if not avg_rating:
        m = re.search(r'([0-9]\.[0-9])\s*out of\s*5', html)
        if m:
            try: avg_rating = float(m.group(1))
            except ValueError: pass

    # --- 3. Individual reviews ---
    recent_reviews = _extract_reviews_from_html(html)

    if not avg_rating and not review_count and not recent_reviews:
        print(f"[g2] No data extracted for {slug}")
        return None

    return AppStoreData(
        platform="g2",
        avg_rating=round(avg_rating, 1),
        review_count=review_count,
        recent_reviews=recent_reviews,
    )
