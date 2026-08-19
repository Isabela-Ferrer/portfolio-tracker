"""Source-authority classifier.

classify_authority(signal, company, people=None) answers one question: did this
signal come from the company itself (or someone on its team), a recognised news
outlet, or somewhere else entirely? It never touches the network or the
database and it never raises. Purity is the point: the rules below are exactly
as testable as they are because nothing here depends on I/O, so every rule can
be pinned down with a plain dict in, a string out, no fixtures, no mocking.

Nothing is deleted or hidden because of what this module decides. It only
labels a signal `"official"`, `"outlet"`, or `"community"`; what happens with
that label (stored on every row, used to filter the digest) lives elsewhere.
"""

import re

# Recognised news outlets. A press or funding signal whose url host is one of
# these, or a subdomain of one, is "outlet" rather than "community". This is a
# starting list, not an exhaustive one; expanding it never changes the meaning
# of a rule, only how many articles clear it.
OUTLET_DOMAINS = frozenset({
    "techcrunch.com",
    "theverge.com",
    "reuters.com",
    "bloomberg.com",
    "businesswire.com",
    "prnewswire.com",
    "wsj.com",
    "nytimes.com",
    "cnbc.com",
    "wired.com",
    "axios.com",
    "forbes.com",
    "arxiv.org",
    "venturebeat.com",
    "fortune.com",
    "engadget.com",
    "arstechnica.com",
    "ft.com",
    "washingtonpost.com",
    "businessinsider.com",
    "fastcompany.com",
    "protocol.com",
    "semafor.com",
    "theinformation.com",
})

# Types that only ever originate from a registered Source (an RSS feed, a
# changelog page) or the company's own ATS board. Their host is irrelevant:
# nothing else in the pipeline emits a signal of these types.
_OFFICIAL_TYPES = frozenset({"blog", "changelog", "job_new", "job_closed", "launch"})

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


# --------------------------------------------------------------------------
# Field access that works on a dataclass instance or a plain dict alike.
# --------------------------------------------------------------------------

def _get(obj, key, default=None):
    """Read `key` off a dict or a dataclass instance, defaulting quietly.

    Anything that is neither (None, an int, a bare object()) just falls
    through to the default rather than raising, which is what lets the
    classifier accept malformed input without special-casing it everywhere.
    """
    try:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)
    except Exception:
        return default


def _person_name(entry) -> str:
    if entry is None:
        return ""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("name") or "")
    return str(getattr(entry, "name", "") or "")


def _tracked_names(raw: dict, people) -> list:
    """Names to check a channel/podcast/episode against.

    `people`, when given (non-empty), is the source of truth. Otherwise this
    falls back to raw["person"], which the youtube and podcast fetchers
    already write for exactly this purpose.
    """
    names = []
    if people:
        for p in people:
            name = _person_name(p)
            if name:
                names.append(name)
    if not names:
        person = raw.get("person") if isinstance(raw, dict) else None
        if person:
            names.append(str(person))
    return names


# --------------------------------------------------------------------------
# Host matching: case-insensitive, scheme/port/www-insensitive, exact or on a
# '.'-delimited suffix boundary only. No urllib: parsing by hand keeps this
# module free of anything that could open a connection.
# --------------------------------------------------------------------------

def _host(url) -> str:
    if not isinstance(url, str):
        return ""
    u = url.strip()
    if not u:
        return ""
    m = _SCHEME_RE.match(u)
    rest = u[m.end():] if m else u
    end = len(rest)
    for ch in ("/", "?", "#"):
        idx = rest.find(ch)
        if idx != -1:
            end = min(end, idx)
    authority = rest[:end]
    if "@" in authority:
        authority = authority.rsplit("@", 1)[-1]
    if authority.startswith("["):
        close = authority.find("]")
        host = authority[1:close] if close != -1 else authority.lstrip("[")
    elif ":" in authority:
        host = authority.split(":", 1)[0]
    else:
        host = authority
    host = host.strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _host_matches(host: str, domain: str) -> bool:
    """True on an exact match, or when `host` is a subdomain of `domain`.

    `techcrunch.com.evil.net` must not match `techcrunch.com`: the suffix has
    to land on a '.' boundary, not just be a trailing substring.
    """
    if not host or not domain:
        return False
    if host == domain:
        return True
    return host.endswith("." + domain)


def _name_in(needle: str, haystack: str) -> bool:
    """Case-insensitive, whole-word containment.

    "Arc" must not match "Arcade Weekly" but must match "Arc Browser".
    """
    if not needle or not haystack:
        return False
    try:
        pattern = r"\b" + re.escape(str(needle).strip()) + r"\b"
        return re.search(pattern, str(haystack), re.IGNORECASE) is not None
    except Exception:
        return False


# --------------------------------------------------------------------------
# The classifier
# --------------------------------------------------------------------------

def classify_authority(signal, company, people=None) -> str:
    """Classify one signal as "official", "outlet", or "community".

    Never raises: malformed input (missing fields, wrong types, None) is
    handled by falling through the rules to "community" rather than crashing
    a refresh or a digest.
    """
    try:
        return _classify(signal, company, people)
    except Exception:
        return "community"


def _classify(signal, company, people) -> str:
    sig_type = _get(signal, "type") or ""
    url = _get(signal, "url") or ""
    raw = _get(signal, "raw")
    if not isinstance(raw, dict):
        raw = {}

    company_name = str(_get(company, "name") or "")
    website = _get(company, "website") or ""

    # Rule 8: reddit is always community.
    if sig_type == "reddit":
        return "community"

    # Rule 9: these types only ever come from a registered Source or the
    # company's own ATS, whatever their url host says.
    if sig_type in _OFFICIAL_TYPES:
        return "official"

    # Rules 10-11: youtube.
    if sig_type == "youtube":
        mode = raw.get("mode")
        if mode == "channel":
            return "official"
        if mode == "search":
            channel = str(raw.get("channel") or "")
            candidates = [company_name] + _tracked_names(raw, people)
            for name in candidates:
                if _name_in(name, channel):
                    return "official"
            return "community"
        # Unrecognised mode: fall through to the host-based rules below.

    # Rule 12: podcast, decided entirely by whether a tracked person is
    # attached to the show or the episode. This is unconditional for the
    # type, so a podcast hosted on the company's own domain by an unrelated
    # person is still community.
    if sig_type == "podcast":
        podcast = str(raw.get("podcast") or "")
        episode = str(raw.get("episode") or "")
        names = _tracked_names(raw, people)
        for name in names:
            if _name_in(name, podcast) or _name_in(name, episode):
                return "official"
        return "community"

    host = _host(url)

    # Rule 13: the company's own domain, or a subdomain of it.
    website_host = _host(website)
    if _host_matches(host, website_host):
        return "official"

    # Rule 14: a recognised outlet, or a subdomain of one.
    for domain in OUTLET_DOMAINS:
        if _host_matches(host, domain):
            return "outlet"

    # Rule 15: everything else.
    return "community"
