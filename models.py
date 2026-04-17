from dataclasses import dataclass, field
from typing import Optional

@dataclass
class Company:
    id: Optional[int]
    name: str
    website_url: str
    app_store_url: Optional[str] = None
    play_store_url: Optional[str] = None
    github_org: Optional[str] = None
    github_repo: Optional[str] = None
    product_hunt_slug: Optional[str] = None
    g2_slug: Optional[str] = None

@dataclass
class PressArticle:
    title: str
    url: str
    source: str
    published_at: str                           # ISO 8601 string
    snippet: str = ""                           # First few paragraphs of article text
    key_points: list = field(default_factory=list)  # AI-extracted bullet points

@dataclass
class JobsData:
    total_count: int
    roles: list[str]
    source_url: str

@dataclass
class AppReview:
    rating: float
    text: str
    date: str

@dataclass
class AppStoreData:
    platform: str              # "ios" or "android"
    avg_rating: float
    review_count: int
    recent_reviews: list[AppReview]

@dataclass
class ProductLaunch:
    title: str
    url: str
    date: str
    source: str                # "product_hunt" or "github"

@dataclass
class FundingSignal:
    title: str
    url: str
    date: str
    amount_hint: Optional[str] = None          # e.g. "$10M" extracted from headline
    snippet: str = ""                          # First few paragraphs of article text
    round_type: str = ""                       # "Series A", "Seed", etc.
    investors: list = field(default_factory=list)   # Named investors
    summary: str = ""                          # AI-generated one-sentence summary

@dataclass
class SocialSignal:
    platform: str      # "reddit"
    content: str       # The title of the post
    engagement_count: int
    url: str
    date: str          # ISO timestamp


@dataclass
class SnapshotResult:
    company_id: int
    press: list[PressArticle] = field(default_factory=list)
    jobs: Optional[JobsData] = None
    appstore: list[AppStoreData] = field(default_factory=list)
    launches: list[ProductLaunch] = field(default_factory=list)
    funding: list[FundingSignal] = field(default_factory=list)
    errors: dict = field(default_factory=dict)
    social: list[SocialSignal] = field(default_factory=list)

