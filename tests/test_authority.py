"""Tests for authority.classify_authority, from plan.md sections A, B and C.

Every test below is traceable to a numbered acceptance criterion in
.factory/plan.md. Nothing here is inferred from an implementation; it comes
straight from the plan's "Classification rules" and "Host matching" sections.
"""

import inspect
import os
import subprocess
import sys

import pytest

import authority
from models import Company, Person, Signal

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------
# A. The module and its signature (criteria 1-7)
# --------------------------------------------------------------------------

def test_classify_authority_exists_with_expected_signature():
    # criterion 1
    assert hasattr(authority, "classify_authority")
    sig = inspect.signature(authority.classify_authority)
    params = list(sig.parameters)
    assert params[:2] == ["signal", "company"]
    assert "people" in sig.parameters
    assert sig.parameters["people"].default is None


@pytest.mark.parametrize("case", [
    dict(type="blog", url="https://blog.acme.com/x", raw={}),
    dict(type="reddit", url="https://reddit.com/r/x", raw={}),
    dict(type="press", url="https://techcrunch.com/x", raw={}),
    dict(type="press", url="https://someguy.substack.com/p/x", raw={}),
])
def test_classify_authority_returns_one_of_three_strings(case):
    # criterion 2
    result = authority.classify_authority(case, {"name": "Acme", "website": "acme.com"})
    assert result in ("official", "outlet", "community")
    assert result is not None


@pytest.mark.parametrize("bad_signal", [
    {},
    {"type": None, "url": None, "raw": None},
    {"type": "youtube", "url": "", "raw": "not-a-dict"},
    {"type": "podcast", "url": None},
    None,
    object(),
    42,
])
@pytest.mark.parametrize("bad_company", [
    {},
    {"name": None, "website": None},
    None,
    object(),
])
def test_classify_authority_never_raises_on_malformed_input(bad_signal, bad_company):
    # criteria 2 and 19: malformed input must not raise, and must still return
    # one of the three valid strings.
    result = authority.classify_authority(bad_signal, bad_company)
    assert result in ("official", "outlet", "community")


def test_no_io_module_source_imports_nothing_forbidden():
    # criterion 3 (static half): the module's own import statements must not
    # pull in database, httpx, openai, sqlite3, or socket.
    src_path = os.path.join(REPO_ROOT, "authority.py")
    assert os.path.exists(src_path), "authority.py must exist at the repo root"
    with open(src_path, "r", encoding="utf-8") as f:
        source = f.read()
    import ast
    tree = ast.parse(source)
    forbidden = {"database", "httpx", "openai", "sqlite3", "socket", "requests", "urllib"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
    hit = imported & forbidden
    assert not hit, f"authority.py imports forbidden modules: {hit}"
    # Also must not import any fetcher.
    fetcher_imports = {m for m in imported if m.startswith("fetchers")}
    assert not fetcher_imports


def test_importing_module_pulls_in_nothing_forbidden_at_runtime():
    # criterion 3 (dynamic half): importing authority.py in a clean
    # interpreter must not cause database/httpx/openai to end up in
    # sys.modules.
    script = (
        "import sys\n"
        "import authority\n"
        "forbidden = {'database', 'httpx', 'openai'}\n"
        "hit = forbidden & set(sys.modules)\n"
        "assert not hit, hit\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=REPO_ROOT,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_classify_authority_does_no_filesystem_io(tmp_path, monkeypatch):
    # criterion 3: no file access. If classify_authority opened a file it
    # would fail against a bogus builtins.open.
    def _boom(*a, **k):
        raise AssertionError("authority.classify_authority attempted file I/O")

    monkeypatch.setattr("builtins.open", _boom)
    company = {"name": "Acme", "website": "https://acme.com"}
    signal = {"type": "press", "url": "https://techcrunch.com/story", "raw": {}}
    assert authority.classify_authority(signal, company) == "outlet"


def test_signal_as_dataclass_or_dict_agree():
    # criterion 4
    company = {"name": "Acme", "website": "https://acme.com"}
    sig_dc = Signal(company_id=1, type="blog", title="t", url="https://blog.acme.com/x")
    sig_dict = {"type": "blog", "url": "https://blog.acme.com/x", "raw": {}}
    assert (authority.classify_authority(sig_dc, company)
            == authority.classify_authority(sig_dict, company))


def test_company_as_dataclass_or_dict_agree():
    # criterion 5
    signal = {"type": "press", "url": "https://acme.com/news", "raw": {}}
    company_dc = Company(id=1, name="Acme", website="https://acme.com")
    company_dict = {"name": "Acme", "website": "https://acme.com"}
    assert (authority.classify_authority(signal, company_dc)
            == authority.classify_authority(signal, company_dict))


def test_people_forms_all_agree_and_fall_back_to_raw_person():
    # criterion 6
    company = {"name": "Cursor", "website": "https://cursor.com"}
    signal = {
        "type": "youtube",
        "url": "https://youtube.com/watch?v=1",
        "raw": {"mode": "search", "channel": "Michael Truell"},
    }
    # No people arg: falls back to raw["person"].
    signal_with_person = dict(signal, raw=dict(signal["raw"], person="Michael Truell"))
    assert authority.classify_authority(signal_with_person, company) == "official"

    # people as list of plain strings
    assert authority.classify_authority(
        signal, company, people=["Michael Truell"]
    ) == "official"
    # people as list of dicts with a name key
    assert authority.classify_authority(
        signal, company, people=[{"name": "Michael Truell"}]
    ) == "official"
    # people as list of models.Person
    person = Person(id=1, company_id=1, name="Michael Truell")
    assert authority.classify_authority(signal, company, people=[person]) == "official"

    # Omitted / empty people with no raw["person"] and unrelated channel -> community
    unrelated = {
        "type": "youtube",
        "url": "https://youtube.com/watch?v=2",
        "raw": {"mode": "search", "channel": "Lex Fridman Clips"},
    }
    assert authority.classify_authority(unrelated, company) == "community"
    assert authority.classify_authority(unrelated, company, people=[]) == "community"


def test_outlet_domains_collection():
    # criterion 7
    required = {
        "techcrunch.com", "theverge.com", "reuters.com", "bloomberg.com",
        "businesswire.com", "prnewswire.com", "wsj.com", "nytimes.com",
        "cnbc.com", "wired.com", "axios.com", "forbes.com", "arxiv.org",
    }
    assert hasattr(authority, "OUTLET_DOMAINS")
    domains = set(authority.OUTLET_DOMAINS)
    missing = required - domains
    assert not missing, f"OUTLET_DOMAINS missing: {missing}"
    # All entries should be lowercase bare domains (no scheme, no www.).
    for d in domains:
        assert d == d.lower()
        assert not d.startswith("http")
        assert not d.startswith("www.")


# --------------------------------------------------------------------------
# B. Classification rules, in order of precedence (criteria 8-15)
# --------------------------------------------------------------------------

ACME = {"name": "Acme", "website": "https://acme.com"}


def test_reddit_always_community_even_on_company_or_outlet_host():
    # criterion 8
    assert authority.classify_authority(
        {"type": "reddit", "url": "https://reddit.com/r/acme/comments/1", "raw": {}}, ACME
    ) == "community"
    assert authority.classify_authority(
        {"type": "reddit", "url": "https://acme.com/some/reddit/mirror", "raw": {}}, ACME
    ) == "community"
    assert authority.classify_authority(
        {"type": "reddit", "url": "https://techcrunch.com/whatever", "raw": {}}, ACME
    ) == "community"


@pytest.mark.parametrize("sig_type", ["blog", "changelog", "job_new", "job_closed", "launch"])
def test_official_types_are_official_regardless_of_host(sig_type):
    # criterion 9
    assert authority.classify_authority(
        {"type": sig_type, "url": "https://techcrunch.com/wherever", "raw": {}}, ACME
    ) == "official"
    assert authority.classify_authority(
        {"type": sig_type, "url": "https://random-unrelated-domain.xyz/x", "raw": {}}, ACME
    ) == "official"
    # Also unconditional even with no url at all (criterion 19 combined with 9).
    assert authority.classify_authority(
        {"type": sig_type, "url": "", "raw": {}}, ACME
    ) == "official"


def test_youtube_channel_mode_is_official():
    # criterion 10
    signal = {
        "type": "youtube", "url": "https://youtube.com/watch?v=x",
        "raw": {"mode": "channel", "channel": "Acme"},
    }
    assert authority.classify_authority(signal, ACME) == "official"


def test_youtube_search_mode_official_and_community_examples():
    # criterion 11, the exact Cursor example from the plan
    cursor = {"name": "Cursor", "website": "https://cursor.com"}
    official = {
        "type": "youtube", "url": "https://youtube.com/watch?v=1",
        "raw": {"mode": "search", "person": "Michael Truell", "channel": "Michael Truell"},
    }
    community = {
        "type": "youtube", "url": "https://youtube.com/watch?v=2",
        "raw": {"mode": "search", "person": "Michael Truell", "channel": "Lex Fridman Clips"},
    }
    assert authority.classify_authority(official, cursor) == "official"
    assert authority.classify_authority(community, cursor) == "community"

    # channel matches the company name itself
    company_channel = {
        "type": "youtube", "url": "https://youtube.com/watch?v=3",
        "raw": {"mode": "search", "channel": "Cursor"},
    }
    assert authority.classify_authority(company_channel, cursor) == "official"


def test_podcast_official_when_tracked_person_in_podcast_or_episode():
    # criterion 12
    company = {"name": "Acme", "website": "https://acme.com"}
    people = [{"name": "Jane Founder"}]

    in_podcast_name = {
        "type": "podcast", "url": "https://podcasts.example.com/e/1",
        "raw": {"podcast": "The Jane Founder Show", "episode": "Episode 1"},
    }
    in_episode_name = {
        "type": "podcast", "url": "https://podcasts.example.com/e/2",
        "raw": {"podcast": "Some Show", "episode": "Jane Founder on the future"},
    }
    unrelated = {
        "type": "podcast", "url": "https://podcasts.example.com/e/3",
        "raw": {"podcast": "Totally Unrelated Show", "episode": "Random guest"},
    }
    assert authority.classify_authority(in_podcast_name, company, people=people) == "official"
    assert authority.classify_authority(in_episode_name, company, people=people) == "official"
    assert authority.classify_authority(unrelated, company, people=people) == "community"

    # Unconditional for type == podcast: even hosted on the company's own
    # domain, an unmatched podcast is community (rule 12 fully decides before
    # any host-based rule gets a chance).
    on_own_domain = {
        "type": "podcast", "url": "https://acme.com/podcast/episode-9",
        "raw": {"podcast": "Some Show", "episode": "Random guest"},
    }
    assert authority.classify_authority(on_own_domain, company, people=people) == "community"


def test_host_match_on_company_website_is_official():
    # criterion 13
    signal = {"type": "press", "url": "https://blog.company.com/x", "raw": {}}
    company = {"name": "Company", "website": "https://company.com"}
    assert authority.classify_authority(signal, company) == "official"

    signal_exact = {"type": "press", "url": "https://company.com/news", "raw": {}}
    assert authority.classify_authority(signal_exact, company) == "official"


def test_host_match_on_outlet_domain_is_outlet():
    # criterion 14
    tc = {"type": "press", "url": "https://techcrunch.com/2026/story", "raw": {}}
    reuters = {"type": "press", "url": "https://www.reuters.com/business/story", "raw": {}}
    assert authority.classify_authority(tc, ACME) == "outlet"
    assert authority.classify_authority(reuters, ACME) == "outlet"


def test_unknown_host_is_community():
    # criterion 15
    signal = {"type": "press", "url": "https://someguy.substack.com/p/x", "raw": {}}
    assert authority.classify_authority(signal, ACME) == "community"


# --------------------------------------------------------------------------
# C. Host matching is exact on the domain (criteria 16-20)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://TechCrunch.com/story",
    "https://www.techcrunch.com/story",
    "http://techcrunch.com/story",
    "techcrunch.com/story",
    "https://techcrunch.com:443/story",
    "HTTPS://WWW.TECHCRUNCH.COM/STORY",
])
def test_host_matching_is_case_port_scheme_and_www_insensitive(url):
    # criterion 16
    signal = {"type": "press", "url": url, "raw": {}}
    assert authority.classify_authority(signal, ACME) == "outlet"


def test_lookalike_domain_does_not_match_company():
    # criterion 17
    signal = {"type": "press", "url": "https://notcompany.com/x", "raw": {}}
    company = {"name": "Company", "website": "https://company.com"}
    assert authority.classify_authority(signal, company) != "official"


def test_suffix_lookalikes_do_not_match_outlet_or_company():
    # criterion 18
    outlet_lookalike = {"type": "press", "url": "https://techcrunch.com.evil.net/x", "raw": {}}
    assert authority.classify_authority(outlet_lookalike, ACME) == "community"

    company = {"name": "Company", "website": "https://company.com"}
    company_lookalike = {"type": "press", "url": "https://company.com.evil.net/x", "raw": {}}
    assert authority.classify_authority(company_lookalike, company) != "official"
    assert authority.classify_authority(company_lookalike, company) == "community"


@pytest.mark.parametrize("signal,company,expected", [
    ({"type": "press", "url": "", "raw": {}}, {"name": "Acme", "website": "https://acme.com"}, "community"),
    ({"type": "press", "raw": {}}, {"name": "Acme", "website": "https://acme.com"}, "community"),
    ({"type": "press", "url": "https://techcrunch.com/x", "raw": {}}, {"name": "Acme", "website": ""}, "outlet"),
    ({"type": "press", "url": "https://someplace.com/x", "raw": {}}, {"name": "Acme", "website": None}, "community"),
    ({"type": "press", "url": "https://someplace.com/x", "raw": None}, {"name": "Acme", "website": "https://acme.com"}, "community"),
    ({"type": "press", "url": "https://someplace.com/x"}, {"name": "Acme", "website": "https://acme.com"}, "community"),
])
def test_missing_fields_are_handled_without_raising(signal, company, expected):
    # criterion 19
    assert authority.classify_authority(signal, company) == expected


def test_reddit_is_still_community_with_missing_fields():
    # criterion 19 combined with 8: an earlier rule (reddit) already decided,
    # so missing url/raw does not change the outcome.
    assert authority.classify_authority({"type": "reddit"}, {}) == "community"
    assert authority.classify_authority(
        {"type": "reddit", "url": None, "raw": None}, {"name": None, "website": None}
    ) == "community"


def test_name_matching_is_whole_word_case_insensitive():
    # criterion 20
    arc = {"name": "Arc", "website": "https://arc.com"}
    no_match = {
        "type": "youtube", "url": "https://youtube.com/watch?v=1",
        "raw": {"mode": "search", "channel": "Arcade Weekly"},
    }
    match_suffix_word = {
        "type": "youtube", "url": "https://youtube.com/watch?v=2",
        "raw": {"mode": "search", "channel": "Arc Browser"},
    }
    match_case_insensitive = {
        "type": "youtube", "url": "https://youtube.com/watch?v=3",
        "raw": {"mode": "search", "channel": "ARC weekly recap"},
    }
    assert authority.classify_authority(no_match, arc) == "community"
    assert authority.classify_authority(match_suffix_word, arc) == "official"
    assert authority.classify_authority(match_case_insensitive, arc) == "official"
