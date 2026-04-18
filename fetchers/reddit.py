import httpx
import urllib.parse
import re
from xml.etree.ElementTree import fromstring
from typing import List, Optional
from models import SocialSignal

# Subreddits that produce too much incidental noise (politics, entertainment, etc.)
_NOISE_SUBREDDITS = {
    'worldnews', 'news', 'politics', 'askreddit', 'todayilearned',
    'funny', 'pics', 'gifs', 'videos', 'aww', 'gaming', 'movies',
    'music', 'sports', 'science', 'history', 'philosophy', 'books',
    'television', 'food', 'travel', 'fitness', 'relationships',
    'amitheasshole', 'tifu', 'showerthoughts', 'unpopularopinion',
    'mildlyinteresting', 'interestingasfuck', 'facepalm', 'cringe',
}

# At least one of these must appear in the title alongside the company name,
# signalling the post is about the product/company rather than a casual mention.
_SIGNAL_WORDS = {
    'app', 'startup', 'product', 'launch', 'update', 'feature', 'review',
    'funding', 'raises', 'raised', 'acquired', 'acquisition', 'ipo',
    'ceo', 'founder', 'api', 'integration', 'pricing', 'subscription',
    'alternative', 'vs', 'versus', 'recommend', 'recommendation',
    'thoughts', 'experience', 'using', 'tried', 'built', 'released',
    'company', 'software', 'platform', 'tool', 'service', 'saas',
    'plugin', 'extension', 'dashboard', 'interface', 'workflow',
    'bug', 'issue', 'support', 'feedback', 'hire', 'hiring', 'layoffs',
    'competitor', 'comparison', 'better', 'worse', 'switching',
}


def _extract_domain(website_url: Optional[str]) -> Optional[str]:
    """Extract bare domain (e.g. 'notion.so') from a full URL."""
    if not website_url:
        return None
    m = re.search(r'https?://(?:www\.)?([^/]+)', website_url)
    return m.group(1).lower() if m else None


def _subreddit_from_url(post_url: str) -> Optional[str]:
    m = re.search(r'/r/([^/]+)/', post_url)
    return m.group(1).lower() if m else None


def _is_relevant(
    title: str,
    company_name: str,
    post_url: str,
    company_domain: Optional[str],
) -> bool:
    """
    Return True only if the Reddit post is genuinely about the company.

    Checks (in order):
    1. Company name appears as a whole word in the title (word-boundary regex).
       This stops e.g. "Notion" matching "notional" or "Arc" matching "arcade".
    2. If the post links directly to the company's domain → accept immediately
       (link posts to the company's own site are almost certainly on-topic).
    3. Company-specific subreddit (e.g. r/notion, r/cursor) → accept immediately.
       Being in the company's own sub is strong on-topic evidence.
    4. Reject posts from generic/entertainment subreddits.
    5. For longer names (> 6 chars), the word-boundary match alone is strong
       enough evidence — distinctive names like "Anthropic", "Harvey", "Waymo"
       are unlikely to appear incidentally. Skip the signal-word gate.
    6. For short/common names (≤ 6 chars, e.g. "Arc", "Beam") require at least
       one business/tech signal word to avoid false positives like "arc of history".
    """
    title_lower = title.lower()
    name_lower = company_name.lower()
    name_slug = name_lower.replace(' ', '')

    # 1. Word-boundary check — whole-word match only
    if not re.search(r'\b' + re.escape(name_lower) + r'\b', title_lower):
        return False

    # 2. Domain link → high confidence, accept immediately
    if company_domain and company_domain in post_url.lower():
        return True

    subreddit = _subreddit_from_url(post_url)

    # 3. Company-specific subreddit → accept immediately
    if subreddit and subreddit in (name_lower, name_slug, name_lower.replace(' ', '_')):
        return True

    # 4. Noise subreddit → reject
    if subreddit and subreddit in _NOISE_SUBREDDITS:
        return False

    # 5. Longer/distinctive names don't need a signal word — the name match is enough
    if len(name_lower) > 6:
        return True

    # 6. Short name: require at least one business/tech signal word in the title
    title_words = set(re.findall(r'\b\w+\b', title_lower))
    return bool(title_words.intersection(_SIGNAL_WORDS))



async def fetch(company) -> List[SocialSignal]:
    """
    Fetch Reddit posts that are genuinely about the company.

    Two-pass strategy:
      Pass 1 — exact phrase search: "CompanyName"
      Pass 2 — domain link search: site:companydomain.com  (if URL is set)

    Posts are deduped by URL and filtered for relevance before being returned.
    """
    company_domain = _extract_domain(getattr(company, 'website_url', None))

    queries = [f'"{company.name}"']  # exact phrase; Reddit honours quotes
    if company_domain:
        queries.append(f'site:{company_domain}')

    signals: List[SocialSignal] = []
    seen_urls: set = set()

    async with httpx.AsyncClient(timeout=15) as client:
        headers = {"User-Agent": "MomentumTracker/1.0 (portfolio-tracker)"}

        for raw_query in queries:
            encoded = urllib.parse.quote(raw_query)
            url = f"https://www.reddit.com/search.rss?q={encoded}&sort=new&limit=25"

            try:
                r = await client.get(url, headers=headers)
            except Exception as e:
                print(f"Reddit fetch error for {company.name!r} (query={raw_query!r}): {e}")
                continue

            if r.status_code == 429:
                print(f"Reddit rate limit hit for {company.name!r}")
                continue
            if r.status_code != 200:
                print(f"Reddit returned {r.status_code} for {company.name!r}")
                continue

            try:
                root = fromstring(r.content)
            except Exception as e:
                print(f"Reddit XML parse error for {company.name!r}: {e}")
                continue

            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall("atom:entry", ns):
                title = entry.findtext("atom:title", "", ns).strip()
                link_el = entry.find("atom:link", ns)
                post_url = (link_el.get("href", "") if link_el is not None else "").strip()

                if not post_url or post_url in seen_urls:
                    continue

                if _is_relevant(title, company.name, post_url, company_domain):
                    seen_urls.add(post_url)
                    signals.append(SocialSignal(
                        platform="reddit",
                        content=title,
                        engagement_count=0,
                        url=post_url,
                        date=entry.findtext("atom:updated", "", ns),
                    ))

    # Return up to 15 most recent (RSS already orders newest-first)
    return signals[:15]
