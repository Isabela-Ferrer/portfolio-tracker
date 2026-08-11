"""Dataclasses for the dream companies tracker.

Signal is the central type: every fetcher, old or new, ultimately returns a list
of Signals that get persisted against one snapshot.
"""

from dataclasses import dataclass, field
from typing import Optional

# Signal types stored in signals.type
SIGNAL_TYPES = (
    "podcast", "blog", "changelog", "youtube", "press",
    "funding", "job_new", "job_closed", "launch", "arxiv",
    "reddit", "appstore",
)

OPTIONAL_FETCHERS = ("reddit", "appstore", "g2", "arxiv")
SOURCE_TYPES = ("blog_rss", "changelog", "youtube_channel", "github_org")
ATS_PLATFORMS = ("ashby", "greenhouse", "lever")


@dataclass
class Company:
    id: Optional[int]
    name: str
    website: str = ""
    logo_url: Optional[str] = None
    ats_platform: Optional[str] = None
    ats_slug: Optional[str] = None
    enabled_optional_fetchers: list = field(default_factory=list)
    app_store_url: Optional[str] = None   # only read when 'appstore' is opted in
    play_store_url: Optional[str] = None

    @property
    def website_url(self) -> str:
        """Alias kept so the carried-over press/funding/reddit fetchers work unchanged."""
        return self.website or ""

    def wants(self, fetcher: str) -> bool:
        return fetcher in (self.enabled_optional_fetchers or [])


@dataclass
class Person:
    id: Optional[int]
    company_id: int
    name: str
    role: str = ""
    track_arxiv: bool = False


@dataclass
class Source:
    id: Optional[int]
    company_id: int
    type: str          # blog_rss | changelog | youtube_channel | github_org
    url: str


@dataclass
class Signal:
    """One dated thing that happened. Deduped on (company_id, type, url)."""
    company_id: int
    type: str
    title: str
    url: str
    published_at: str = ""        # ISO 8601, best effort
    raw: dict = field(default_factory=dict)


@dataclass
class Posting:
    """A normalised open role straight off an ATS board."""
    external_id: str
    title: str
    location: str = ""
    department: str = ""
    url: str = ""


@dataclass
class JobRow:
    """A row of the jobs table, as read back for display and briefs."""
    id: Optional[int]
    company_id: int
    external_id: str
    title: str
    location: str = ""
    department: str = ""
    url: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    closed_at: Optional[str] = None
    is_early_career: bool = False
    is_nyc: bool = False


@dataclass
class SnapshotResult:
    company_id: int
    signals: list = field(default_factory=list)      # list[Signal]
    headcount_total: int = 0
    headcount_nyc: int = 0
    errors: dict = field(default_factory=dict)
    flags: dict = field(default_factory=dict)
    bullets: list = field(default_factory=list)


# --------------------------------------------------------------------------
# Types the carried-over fetchers return. The orchestrator converts each of
# these into Signals; nothing downstream of the orchestrator sees them.
# --------------------------------------------------------------------------

@dataclass
class PressArticle:
    title: str
    url: str
    source: str
    published_at: str                               # ISO 8601 string
    snippet: str = ""
    key_points: list = field(default_factory=list)


@dataclass
class FundingSignal:
    title: str
    url: str
    date: str
    amount_hint: Optional[str] = None
    snippet: str = ""
    round_type: str = ""
    investors: list = field(default_factory=list)
    summary: str = ""


@dataclass
class ProductLaunch:
    title: str
    url: str
    date: str
    source: str                                     # "product_hunt" or "github"


@dataclass
class SocialSignal:
    platform: str                                   # "reddit"
    content: str
    engagement_count: int
    url: str
    date: str


@dataclass
class AppReview:
    rating: float
    text: str
    date: str


@dataclass
class AppStoreData:
    platform: str                                   # "ios" or "android"
    avg_rating: float
    review_count: int
    recent_reviews: list = field(default_factory=list)
