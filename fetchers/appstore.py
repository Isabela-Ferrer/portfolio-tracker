import httpx
import re
from typing import Optional
from models import AppStoreData, AppReview
from google_play_scraper import app, reviews, Sort


# Extract app ID from URL
# iOS: https://apps.apple.com/app/id284882215 → "284882215"
# Android: https://play.google.com/store/apps/details?id=com.whatsapp → "com.whatsapp"

async def fetch(company) -> list[AppStoreData]:
    results = []
    if company.app_store_url:
        ios = await _fetch_ios(company.app_store_url)
        if ios: results.append(ios)
    if company.play_store_url:
        android = await _fetch_android(company.play_store_url)
        if android: results.append(android)
    return results

async def _fetch_ios(url: str) -> Optional[AppStoreData]:
    app_id = re.search(r'id(\d+)', url)
    if not app_id: return None
    aid = app_id.group(1)
    async with httpx.AsyncClient(timeout=10) as client:
        meta = await client.get(f"https://itunes.apple.com/lookup?id={aid}")
        reviews_r = await client.get(
            f"https://itunes.apple.com/us/rss/customerreviews/id={aid}/sortBy=mostRecent/json"
        )
    meta_data = meta.json().get("results", [{}])[0]
    reviews_data = reviews_r.json().get("feed", {}).get("entry", [])[1:6]  # skip first (app meta)
    reviews = [AppReview(
        rating=float(r["im:rating"]["label"]),
        text=r["content"]["label"][:200],
        date=r["updated"]["label"]
    ) for r in reviews_data if "im:rating" in r]
    return AppStoreData(
        platform="ios",
        avg_rating=meta_data.get("averageUserRating", 0),
        review_count=meta_data.get("userRatingCount", 0),
        recent_reviews=reviews
    )
async def _fetch_android(url: str) -> Optional[AppStoreData]:
    # Extract package ID (e.g., com.whatsapp) from the URL
    package_match = re.search(r'id=([^&]+)', url)
    if not package_match: return None
    package_id = package_match.group(1)

    try:
        # Get App Metadata
        info = app(package_id, lang='en', country='us')
        
        # Get 5 most recent reviews
        results, _ = reviews(package_id, count=5, sort=Sort.NEWEST)
        
        recent_reviews = [AppReview(
            rating=float(r['score']),
            text=r['content'][:200],
            date=r['at'].isoformat()
        ) for r in results]

        return AppStoreData(
            platform="android",
            avg_rating=info.get('score', 0),
            review_count=info.get('reviews', 0),
            recent_reviews=recent_reviews
        )
    except Exception as e:
        print(f"Android fetch error: {e}")
        return None