"""Careers fetcher: Ashby, Greenhouse and Lever public job boards.

Three jobs in one module:
  1. ATS discovery  -> which platform and slug does this company use
  2. Fetch postings -> normalised Posting list off the public JSON endpoint
  3. Weekly diff    -> reconcile against the jobs table, emit job_new/job_closed

No auth, no keys. Every endpoint here is the public job board API.
"""

import asyncio
import re
from urllib.parse import urlparse

import httpx

import database as db
from models import Posting, Signal

UA = {"User-Agent": "Mozilla/5.0 (compatible; dream-tracker/1.0)"}

ENDPOINTS = {
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
}

# Word-boundary anchored so "internal" and "international" do not read as "intern".
EARLY_CAREER_RE = re.compile(
    r"\b(intern|interns|internship|internships|new[\s-]?grad(?:uate)?|university|"
    r"early[\s-]?career|campus|apprentice(?:ship)?)\b",
    re.IGNORECASE,
)
NYC_RE = re.compile(r"\b(new york|nyc|brooklyn|manhattan)\b", re.IGNORECASE)

# ATS references embedded in a company's own careers page. Both the hosted
# board form (jobs.ashbyhq.com/slug) and the API form the page's own JS calls
# (boards-api.greenhouse.io/v1/boards/slug) appear in the wild, and for some
# companies only the second one is present.
_ATS_LINK_PATTERNS = (
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9._-]+)", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/v\d/boards/([A-Za-z0-9._-]+)", re.I)),
    ("lever", re.compile(r"api\.lever\.co/v\d/postings/([A-Za-z0-9._-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9._-]+)", re.I)),
    ("greenhouse", re.compile(r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board\?for=)?"
                              r"([A-Za-z0-9._-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9._-]+)", re.I)),
)
_SLUG_BLOCKLIST = {"v0", "v1", "v2", "embed", "posting-api", "job-board",
                   "boards", "jobs", "postings", "job_board"}

# Plenty of careers pages build the board URL client side and keep the slug in
# a JS constant: `const company = "gleanwork"`. Nothing to match on in a URL,
# so pull the token out of the script and probe it.
_BOARD_TOKEN_RE = re.compile(
    r"""(?:company|companyName|boardToken|board_token|jobBoard|job_board|
         boardName|orgSlug|org|ashbyJobBoardName|leverSite|ghBoard|token)
        \s*[:=]\s*["']([A-Za-z0-9._-]{2,40})["']""",
    re.IGNORECASE | re.VERBOSE,
)
_ATS_HOST_HINTS = (
    ("ashby", re.compile(r"ashbyhq\.com", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io", re.I)),
    ("lever", re.compile(r"lever\.co", re.I)),
)

_STOP_TOKENS = {"ai", "inc", "labs", "lab", "the", "co", "corp", "technologies", "systems"}


def is_early_career(title: str) -> bool:
    return bool(EARLY_CAREER_RE.search(title or ""))


def is_nyc(location: str) -> bool:
    return bool(NYC_RE.search(location or ""))


# --------------------------------------------------------------------------
# Slug candidates
# --------------------------------------------------------------------------

def slug_candidates(name: str, website: str = "") -> list:
    """Slug guesses for a company name, strongest first."""
    clean = re.sub(r"[^\w\s-]", "", (name or "")).strip().lower()
    words = [w for w in re.split(r"[\s-]+", clean) if w]
    if not words:
        return []

    core = [w for w in words if w not in _STOP_TOKENS] or words

    out = [
        "".join(words),          # thinkingmachineslab
        "-".join(words),         # thinking-machines-lab
        "".join(core),           # thinkingmachines
        "-".join(core),
    ]
    if website:
        host = urlparse(website if "://" in website else "https://" + website).netloc
        domain = host.replace("www.", "").split(".")[0].lower()
        if domain:
            out.append(domain)
    # First word last: broad, and the most likely to collide with someone else.
    out.append(core[0])
    if len(core) > 1:
        out.append(core[0] + core[1])

    seen, ordered = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            ordered.append(s)
    return ordered


def _name_tokens(name: str) -> set:
    return {w for w in re.split(r"[^\w]+", (name or "").lower()) if w and w not in _STOP_TOKENS}


def _name_matches(ours: str, theirs: str) -> bool:
    """Loose check that a board really belongs to the company we asked for."""
    if not theirs:
        return True  # board does not report a name, nothing to contradict
    a, b = _name_tokens(ours), _name_tokens(theirs)
    if not a or not b:
        return True
    return bool(a & b) or "".join(sorted(a)) == "".join(sorted(b))


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

def _ashby_location(job: dict) -> str:
    parts = [job.get("location") or ""]
    for sec in job.get("secondaryLocations") or []:
        loc = sec.get("location") if isinstance(sec, dict) else None
        if loc:
            parts.append(loc)
    addr = ((job.get("address") or {}).get("postalAddress") or {})
    for key in ("addressLocality", "addressRegion"):
        if addr.get(key):
            parts.append(addr[key])
    seen, out = set(), []
    for p in parts:
        p = (p or "").strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return ", ".join(out)


def _normalise(platform: str, payload) -> tuple:
    """Return (postings, board_company_name)."""
    postings, board_name = [], ""

    if platform == "ashby":
        for j in (payload or {}).get("jobs", []):
            if j.get("isListed") is False:
                continue
            postings.append(Posting(
                external_id=str(j.get("id") or ""),
                title=(j.get("title") or "").strip(),
                location=_ashby_location(j),
                department=(j.get("department") or j.get("team") or "").strip(),
                url=j.get("jobUrl") or j.get("applyUrl") or "",
            ))

    elif platform == "greenhouse":
        jobs = (payload or {}).get("jobs", [])
        if jobs:
            board_name = jobs[0].get("company_name") or ""
        for j in jobs:
            offices = ", ".join(
                o.get("name", "") for o in (j.get("offices") or []) if o.get("name")
            )
            loc = (j.get("location") or {}).get("name") or offices
            depts = ", ".join(
                d.get("name", "") for d in (j.get("departments") or []) if d.get("name")
            )
            postings.append(Posting(
                external_id=str(j.get("id") or ""),
                title=(j.get("title") or "").strip(),
                location=(loc or "").strip(),
                department=depts,
                url=j.get("absolute_url") or "",
            ))

    elif platform == "lever":
        if not isinstance(payload, list):
            return [], ""
        for j in payload:
            cats = j.get("categories") or {}
            locs = cats.get("allLocations") or []
            if not isinstance(locs, list):
                locs = []
            loc = ", ".join(dict.fromkeys(
                [str(cats.get("location") or "")] + [str(x) for x in locs]
            ).keys()).strip(", ")
            postings.append(Posting(
                external_id=str(j.get("id") or ""),
                title=(j.get("text") or "").strip(),
                location=loc,
                department=(cats.get("department") or cats.get("team") or ""),
                url=j.get("hostedUrl") or j.get("applyUrl") or "",
            ))

    return [p for p in postings if p.external_id and p.title], board_name


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

async def _probe(client: httpx.AsyncClient, platform: str, slug: str) -> tuple:
    """Return (postings, board_name) or (None, '') when the board does not exist."""
    try:
        r = await client.get(ENDPOINTS[platform].format(slug=slug), timeout=20)
    except Exception:
        return None, ""
    if r.status_code != 200:
        return None, ""
    try:
        payload = r.json()
    except Exception:
        return None, ""
    postings, board_name = _normalise(platform, payload)
    return (postings or None), board_name


async def fetch_postings(platform: str, slug: str, client: httpx.AsyncClient = None) -> list:
    """Fetch open postings. Raises on transport failure so callers can skip the diff."""
    if not platform or not slug or platform not in ENDPOINTS:
        return []
    own_client = client is None
    client = client or httpx.AsyncClient(headers=UA, follow_redirects=True)
    try:
        r = await client.get(ENDPOINTS[platform].format(slug=slug), timeout=25)
        r.raise_for_status()
        postings, _ = _normalise(platform, r.json())
        return postings
    finally:
        if own_client:
            await client.aclose()


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

async def _ats_refs_from_site(client: httpx.AsyncClient, website: str) -> list:
    """Scrape the company's own careers pages for ATS links. Strongest evidence."""
    if not website:
        return []
    base = website.rstrip("/")
    if "://" not in base:
        base = "https://" + base
    paths = ["/careers", "/jobs", "/company/careers", "/about/careers", ""]
    found, seen = [], set()
    fallback = []

    def add(bucket, platform, slug):
        slug = (slug or "").strip()
        if not slug or slug.lower() in _SLUG_BLOCKLIST:
            return
        key = (platform, slug.lower())
        if key not in seen:
            seen.add(key)
            bucket.append((platform, slug))

    async def grab(url):
        try:
            r = await client.get(url, timeout=20)
            if r.status_code != 200:
                return
        except Exception:
            return

        for platform, pattern in _ATS_LINK_PATTERNS:
            for slug in pattern.findall(r.text):
                add(found, platform, slug)

        # Only worth harvesting JS tokens on pages that actually talk to an ATS.
        hosts = [p for p, rx in _ATS_HOST_HINTS if rx.search(r.text)]
        if hosts:
            for token in dict.fromkeys(_BOARD_TOKEN_RE.findall(r.text)):
                for platform in hosts:
                    add(fallback, platform, token)

    await asyncio.gather(*[grab(base + p) for p in paths])
    return found + fallback


async def discover_ats(name: str, website: str = "") -> dict:
    """Find the ATS platform and slug for a company.

    Order matters. A slug lifted off the company's own careers page is treated
    as authoritative; generated guesses are verified against the board's own
    company name where the platform reports one.
    """
    result = {"ats_platform": None, "ats_slug": None, "method": None,
              "open_roles": 0, "tried": []}

    async with httpx.AsyncClient(headers=UA, follow_redirects=True) as client:
        # 1. The company's careers page usually links straight at its board.
        for platform, slug in await _ats_refs_from_site(client, website):
            result["tried"].append(f"site:{platform}/{slug}")
            postings, _ = await _probe(client, platform, slug)
            if postings:
                result.update(ats_platform=platform, ats_slug=slug,
                              method="careers_page", open_roles=len(postings))
                return result

        # 2. Generated slug candidates against all three platforms.
        for slug in slug_candidates(name, website):
            probes = await asyncio.gather(
                *[_probe(client, p, slug) for p in ENDPOINTS]
            )
            for platform, (postings, board_name) in zip(ENDPOINTS, probes):
                result["tried"].append(f"{platform}/{slug}")
                if not postings:
                    continue
                if not _name_matches(name, board_name):
                    result["tried"].append(f"rejected:{platform}/{slug}({board_name})")
                    continue
                result.update(ats_platform=platform, ats_slug=slug,
                              method="slug_guess", open_roles=len(postings))
                return result

    return result


# --------------------------------------------------------------------------
# Weekly diff
# --------------------------------------------------------------------------

async def fetch(company, client: httpx.AsyncClient = None) -> dict:
    """Reconcile the live board against the jobs table.

    Returns {"signals": [...], "headcount_total": n, "headcount_nyc": n,
             "opened": n, "closed": n, "skipped": reason|None}.
    """
    if not company.ats_platform or not company.ats_slug:
        total, nyc = db.headcount(company.id)
        return {"signals": [], "headcount_total": total, "headcount_nyc": nyc,
                "opened": 0, "closed": 0, "skipped": "no_ats_configured"}

    postings = await fetch_postings(company.ats_platform, company.ats_slug, client)

    existing = {j["external_id"]: j for j in db.get_open_jobs(company.id)}
    seen_at = db.now_iso()

    # An empty board when we previously had roles is far more likely to be a
    # broken response than a company closing every req in one week. Never let
    # that mass-close the table.
    if not postings and existing:
        total, nyc = db.headcount(company.id)
        return {"signals": [], "headcount_total": total, "headcount_nyc": nyc,
                "opened": 0, "closed": 0, "skipped": "empty_board_response"}

    signals, opened = [], 0
    live_ids = set()

    for p in postings:
        live_ids.add(p.external_id)
        early, nyc_flag = is_early_career(p.title), is_nyc(p.location)
        if p.external_id in existing:
            db.touch_job(company.id, p.external_id, p, early, nyc_flag, seen_at)
            continue
        # Not currently open: either brand new, or a previously closed req reopening.
        inserted = db.insert_job(company.id, p, early, nyc_flag, seen_at)
        if not inserted:
            db.touch_job(company.id, p.external_id, p, early, nyc_flag, seen_at)
        opened += 1
        signals.append(Signal(
            company_id=company.id, type="job_new", title=p.title, url=p.url,
            published_at=seen_at,
            raw={"location": p.location, "department": p.department,
                 "is_early_career": early, "is_nyc": nyc_flag,
                 "external_id": p.external_id},
        ))

    gone = [eid for eid in existing if eid not in live_ids]
    db.close_jobs(company.id, gone, seen_at)
    for eid in gone:
        j = existing[eid]
        signals.append(Signal(
            company_id=company.id, type="job_closed", title=j["title"],
            url=j.get("url") or f"job:{company.id}:{eid}", published_at=seen_at,
            raw={"location": j.get("location"), "department": j.get("department"),
                 "is_early_career": j.get("is_early_career"), "is_nyc": j.get("is_nyc"),
                 "external_id": eid},
        ))

    total, nyc = db.headcount(company.id)
    return {"signals": signals, "headcount_total": total, "headcount_nyc": nyc,
            "opened": opened, "closed": len(gone), "skipped": None}
