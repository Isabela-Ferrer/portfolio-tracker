"""Blog and changelog fetcher.

Two source types from the sources table:

  blog_rss   RSS/Atom feed. If the stored URL turns out to serve HTML (plenty of
             these companies ship a Next.js blog with no feed at all), we fall
             back to pulling dated post links off the listing page.
  changelog  Changelog page. RSS is preferred when the page advertises one;
             otherwise the HTML is parsed for dated entries.

Per-domain changelog parsers live in _CHANGELOG_PARSERS and each call is wrapped
individually, so one site changing its markup can never take down the run.
"""

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse
from xml.etree.ElementTree import fromstring

import httpx
from selectolax.parser import HTMLParser

import content
from models import Signal

UA = {"User-Agent": "Mozilla/5.0 (compatible; dream-tracker/1.0)"}

# How far back a first run reaches. Dedupe on (company, type, url) means later
# runs only ever surface genuinely new entries.
LOOKBACK_DAYS = 30
MAX_ENTRIES_PER_SOURCE = 15
MAX_HTML_BYTES = 3_000_000

_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")

_DATE_TEXT_RE = re.compile(
    r"\b("
    r"\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{4}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"
    r"|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}"
    r")\b"
)

_DATE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
    "%d %B %Y", "%d %b %Y",
)

# Card chrome that listing pages glue onto the front of a post title.
_CARD_META_RE = re.compile(
    r"^(?:\s*(?:article|blog|guide|news|case study|customer story|podcast|video|"
    r"announcement|press|research|tutorial|new)\b|\s*\d+\s*min(?:ute)?s?\s*read\b)"
    r"[\s\-–—|·:]*",
    re.IGNORECASE,
)

# Listing paths that are navigation, not posts.
_NON_POST_SEGMENTS = ("/tag/", "/tags/", "/category/", "/categories/",
                      "/author/", "/authors/", "/topic/", "/topics/", "/page/")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _clean(text: str) -> str:
    return _SPACE_RE.sub(" ", _TAG_RE.sub(" ", text or "")).strip()


def parse_date(raw: str):
    """Best-effort date parse across RFC822, ISO 8601 and common written forms."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
        if dt:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    iso = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    m = _DATE_TEXT_RE.search(raw)
    if m:
        candidate = m.group(1).replace(".", "")
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _iso(dt) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") if dt else ""


def _recent(dt, days: int = LOOKBACK_DAYS) -> bool:
    """Undated entries are kept: dedupe stops them repeating, and a missing
    date is far more often a parsing gap than an old post."""
    if dt is None:
        return True
    return dt >= datetime.now(timezone.utc) - timedelta(days=days)


def _looks_like_xml(text: str, content_type: str) -> bool:
    if "xml" in (content_type or "").lower():
        return True
    head = (text or "")[:400].lstrip()
    return head.startswith("<?xml") or head.startswith("<rss") or head.startswith("<feed")


# --------------------------------------------------------------------------
# Feed parsing
# --------------------------------------------------------------------------

def parse_feed(xml_text: str) -> list:
    """Parse RSS 2.0 or Atom into [{title, url, published, guid}]."""
    root = fromstring(xml_text.strip())
    entries = []

    # RSS 2.0
    for item in root.findall(".//item"):
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        entries.append({
            "title": _clean(item.findtext("title") or ""),
            "url": link or guid,
            "published": (item.findtext("pubDate")
                          or item.findtext("{http://purl.org/dc/elements/1.1/}date")
                          or ""),
            "guid": guid or link,
        })

    # Atom
    for entry in root.findall(f".//{_ATOM_NS}entry"):
        link_el = None
        for cand in entry.findall(f"{_ATOM_NS}link"):
            rel = cand.get("rel") or "alternate"
            if rel == "alternate":
                link_el = cand
                break
        link_el = link_el if link_el is not None else entry.find(f"{_ATOM_NS}link")
        url = (link_el.get("href") if link_el is not None else "") or ""
        entries.append({
            "title": _clean(entry.findtext(f"{_ATOM_NS}title") or ""),
            "url": url.strip(),
            "published": (entry.findtext(f"{_ATOM_NS}published")
                          or entry.findtext(f"{_ATOM_NS}updated") or ""),
            "guid": (entry.findtext(f"{_ATOM_NS}id") or url).strip(),
        })

    return [e for e in entries if e["url"] and e["title"]]


# --------------------------------------------------------------------------
# HTML parsing
# --------------------------------------------------------------------------

def _node_date(node):
    """Date attached to a node: a <time> descendant, or date-shaped text."""
    for t in node.css("time"):
        dt = parse_date(t.attributes.get("datetime") or t.text(strip=True) or "")
        if dt:
            return dt
    m = _DATE_TEXT_RE.search(node.text(separator=" ", strip=True)[:400])
    return parse_date(m.group(1)) if m else None


def _first_link(node, base_url: str):
    for a in node.css("a"):
        href = (a.attributes.get("href") or "").strip()
        if href and not href.startswith(("#", "javascript:", "mailto:")):
            return urljoin(base_url, href)
    return None


def _heading_text(node) -> str:
    for sel in ("h1", "h2", "h3", "h4", "strong", "a"):
        el = node.css_first(sel)
        if el:
            # separator matters: a heading split across spans concatenates into
            # "Future(s) of WorkHow will AI change the way we work?" without it.
            text = _clean(el.text(separator=" ", strip=True))
            if text and len(text) > 2:
                return text[:200]
    return ""


def parse_html_entries(html: str, base_url: str) -> list:
    """Generic dated-entry extractor for changelog pages.

    Anchors on date markers, then walks up to the smallest ancestor that also
    carries a heading, which is the shape nearly every changelog uses.
    """
    tree = HTMLParser(html[:MAX_HTML_BYTES])
    entries, seen = [], set()

    anchors = tree.css("time") or []
    if not anchors:
        anchors = [n for n in tree.css("h1, h2, h3, h4")
                   if _DATE_TEXT_RE.search(n.text(strip=True) or "")]

    for anchor in anchors:
        node, container = anchor, None
        for _ in range(4):
            node = node.parent
            if node is None:
                break
            if node.css_first("h1, h2, h3, h4") is not None:
                container = node
                break
        container = container or anchor.parent
        if container is None:
            continue

        dt = _node_date(container)
        title = _heading_text(container)
        if not title:
            continue
        # A heading that is only a date is a section header, not an entry.
        if _DATE_TEXT_RE.fullmatch(title.strip()):
            body = container.text(separator=" ", strip=True)
            title = _clean(body.replace(title, "", 1))[:120]
            if not title:
                continue

        url = _first_link(container, base_url) or base_url
        key = (title.lower(), _iso(dt)[:10])
        if key in seen:
            continue
        seen.add(key)
        entries.append({"title": title, "url": url, "published": _iso(dt), "dated": dt is not None})
        if len(entries) >= MAX_ENTRIES_PER_SOURCE:
            break

    return entries


def _card_title(anchor) -> tuple:
    """Pull a clean title (and any date baked into the card) out of a post link.

    Listing cards concatenate chrome into the anchor text: "7/30/2026Three Tests
    to Run...", "Article - 4 min readAI spend moves fast...". Prefer a real
    heading inside the anchor, then the aria-label, and only then the raw text
    with the chrome stripped off the front.
    """
    for sel in ("h1", "h2", "h3", "h4"):
        el = anchor.css_first(sel)
        if el:
            text = _clean(el.text(separator=" ", strip=True))
            if text and len(text) > 8:
                return text[:200], None

    label = (anchor.attributes.get("aria-label") or "").strip()
    label = re.sub(r"^read (?:full )?article:\s*", "", label, flags=re.IGNORECASE)
    if len(label) > 8:
        return label[:200], None

    text = _clean(anchor.text(separator=" ", strip=True))
    dt = None
    m = _DATE_TEXT_RE.match(text)
    if m:
        dt = parse_date(m.group(1))
        text = text[m.end():]
    for _ in range(3):
        cleaned = _CARD_META_RE.sub("", text)
        if cleaned == text:
            break
        text = cleaned
    return text.strip()[:200], dt


def parse_html_post_links(html: str, base_url: str) -> list:
    """Fallback for feed-less blog index pages: pull post links off the listing."""
    tree = HTMLParser(html[:MAX_HTML_BYTES])
    path = urlparse(base_url).path.rstrip("/") or "/blog"
    entries, seen = [], set()

    for a in tree.css("a"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        # Only links that live under the listing path, and are not the listing itself.
        if not parsed.path.startswith(path) or parsed.path.rstrip("/") == path:
            continue
        if any(seg in parsed.path for seg in _NON_POST_SEGMENTS):
            continue
        clean_url = full.split("?")[0].split("#")[0]
        if clean_url in seen:
            continue

        title, card_date = _card_title(a)
        if not title or len(title) < 12 or len(title) > 200:
            continue

        seen.add(clean_url)
        node = a.parent if a.parent is not None else a
        dt = card_date or _node_date(node)
        entries.append({"title": title, "url": clean_url, "published": _iso(dt)})
        if len(entries) >= MAX_ENTRIES_PER_SOURCE:
            break

    return entries


# --------------------------------------------------------------------------
# Per-domain changelog parsers
# --------------------------------------------------------------------------

def _parse_elevenlabs(html: str, base_url: str) -> list:
    """The docs changelog groups entries under date headings."""
    tree = HTMLParser(html[:MAX_HTML_BYTES])
    entries = []
    for h in tree.css("h2, h3"):
        text = h.text(strip=True)
        dt = parse_date(text)
        if not dt:
            continue
        sib, bullets = h.next, []
        for _ in range(6):
            if sib is None:
                break
            if sib.tag in ("h2", "h3"):
                break
            bullets.append(sib.text(separator=" ", strip=True))
            sib = sib.next
        body = _clean(" ".join(bullets))[:180]
        entries.append({
            "title": body or f"Changelog {text}",
            "url": urljoin(base_url, "#" + (h.attributes.get("id") or "")),
            "published": _iso(dt),
            "dated": True,
        })
        if len(entries) >= MAX_ENTRIES_PER_SOURCE:
            break
    return entries


# domain -> parser. Anything not listed uses parse_html_entries.
_CHANGELOG_PARSERS = {
    "elevenlabs.io": _parse_elevenlabs,
}


def _domain(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "").lower()


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

async def _get(client: httpx.AsyncClient, url: str):
    r = await client.get(url, timeout=25, follow_redirects=True)
    r.raise_for_status()
    return r


async def _fetch_source(client: httpx.AsyncClient, company_id: int, source: dict) -> list:
    """Fetch one source. Returns Signals. Raises only on transport errors."""
    url, stype = source["url"], source["type"]
    r = await _get(client, url)
    content_type = r.headers.get("content-type", "")
    is_changelog = stype == "changelog"

    if _looks_like_xml(r.text, content_type):
        raw_entries = parse_feed(r.text)
        for e in raw_entries:
            e["dated"] = bool(e.get("published"))
    elif is_changelog:
        parser = _CHANGELOG_PARSERS.get(_domain(url), parse_html_entries)
        try:
            raw_entries = parser(r.text, url)
        except Exception as e:
            # An individual domain parser breaking must not kill the source,
            # let alone the run.
            print(f"[blogs] changelog parser failed for {url}: {e}")
            raw_entries = []
    else:
        raw_entries = parse_html_post_links(r.text, url)
        for e in raw_entries:
            e["dated"] = bool(e.get("published"))

    entries = raw_entries[:MAX_ENTRIES_PER_SOURCE]

    # Listing pages routinely render dates client side, so the card carries no
    # date at all. Rather than let an undated post inherit the time of the crawl,
    # open the post once and read its own metadata. content.fetch_many caches by
    # URL, so this costs one request per post ever, not one per week.
    undated = [e["url"] for e in entries if not e.get("published") and e.get("url")]
    if undated:
        try:
            resolved = await content.resolve_dates(undated, client)
            for e in entries:
                if not e.get("published"):
                    found = resolved.get(e["url"])
                    if found:
                        e["published"] = found
                        e["dated"] = True
        except Exception as exc:
            print(f"[blogs] date resolution failed for {url}: {exc}")

    signals = []
    for e in entries:
        dt = parse_date(e.get("published") or "")
        if not _recent(dt):
            continue
        # Cheap heuristic, no LLM: a dated entry on a changelog is a shipped thing.
        if is_changelog:
            sig_type = "launch" if e.get("dated") else "changelog"
        else:
            sig_type = "blog"
        signals.append(Signal(
            company_id=company_id,
            type=sig_type,
            title=e["title"],
            url=e["url"],
            published_at=_iso(dt),
            raw={"source_type": stype, "source_url": url, "guid": e.get("guid", "")},
        ))
    return signals


async def fetch(company, sources: list, client: httpx.AsyncClient = None) -> list:
    """Fetch every blog_rss and changelog source for a company.

    Each source is isolated: a 404, a timeout or a markup change on one site
    yields an empty list for that source and leaves the others untouched.
    """
    wanted = [s for s in sources if s["type"] in ("blog_rss", "changelog")]
    if not wanted:
        return []

    own_client = client is None
    client = client or httpx.AsyncClient(headers=UA, follow_redirects=True)
    out = []
    try:
        for source in wanted:
            try:
                out.extend(await _fetch_source(client, company.id, source))
            except Exception as e:
                print(f"[blogs] {company.name}: source {source['url']} failed: {e}")
    finally:
        if own_client:
            await client.aclose()
    return out
