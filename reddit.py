import httpx
import urllib.parse
from xml.etree.ElementTree import fromstring
from typing import List
from models import SocialSignal

async def fetch(company) -> List[SocialSignal]:
    """
    Searches Reddit for the company name plus context keywords.
    """
    # 1. Refined Query: "CompanyName (startup OR app)"
    # This tells Reddit the post MUST have the company name 
    # AND either the word 'startup' or 'app'.
    raw_query = f'{company.name} (startup OR app)'
    query = urllib.parse.quote(raw_query)
    
    # sort=relevance is often better for specific keywords, 
    # but sort=new is better for "Momentum"
    url = f"https://www.reddit.com/search.rss?q={query}&sort=new"
    
    signals = []
    
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Descriptive User-Agent to avoid 429 errors
            headers = {"User-Agent": "MomentumTracker/1.0 (Contact: your@email.com)"}
            r = await client.get(url, headers=headers)
            
            if r.status_code == 200:
                root = fromstring(r.content)
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                
                for entry in root.findall("atom:entry", ns)[:10]:
                    signals.append(SocialSignal(
                        platform="reddit",
                        content=entry.findtext("atom:title", "", ns),
                        engagement_count=0, 
                        url=entry.find("atom:link", ns).get("href", ""),
                        date=entry.findtext("atom:updated", "", ns)
                    ))
            elif r.status_code == 429:
                print(f"Reddit is rate-limiting us. Try changing the User-Agent.")
                
    except Exception as e:
        print(f"Reddit fetch error for {company.name}: {e}")
        
    return signals