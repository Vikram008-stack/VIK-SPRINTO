from datetime import datetime, timezone

import pytest

from src.scrape.cleaner import clean_html
from src.scrape.failures import classify
from src.scrape.http_cache import HttpCache
from src.scrape.links import discover
from src.scrape.robots import RobotsChecker, parse_robots
from src.scrape.transport import RawResponse
from src.store.models import ReasonCode
from tests.helpers import html, make_fetcher, robots_from

NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)


# ------------------------------------------------------------------ cleaner
def test_cleaner_nav_heavy_junk_to_signal():
    r = clean_html(html("nav_heavy.html"), "https://ledgerly.example/", NOW, "homepage")
    assert r.raw_tokens > 15000
    assert r.clean_tokens < 600, r.text
    t = r.text
    # signal survives
    assert "SOC 2 Type II audit" in t
    assert "Series B" in t
    assert "Enterprise plan" in t
    assert "Northwind Logistics" in t
    # junk is gone
    assert "Sub item" not in t and "Solution area" not in t       # mega menu
    assert "cookies" not in t.lower()                               # cookie banner
    assert "__DATA_" not in t and "margin:" not in t                # scripts/styles
    assert "Hidden promotional text" not in t                       # display:none
    assert t.count("Join thousands of teams") == 1                  # deduped
    # footer dropped, but compliance badge harvested
    assert "Copyright 2026" not in t
    assert "GDPR" in r.footer_compliance
    # provenance on every chunk
    assert r.chunks and all(c.source_url == "https://ledgerly.example/" and c.fetched_at == NOW for c in r.chunks)


def test_cleaner_keeps_job_titles_and_dates_on_careers():
    r = clean_html(html("careers_compliance.html"), "https://acme-thin.io/careers", NOW, "careers")
    assert "Head of Compliance" in r.text
    assert "2026-10-20" in r.text
    assert "Data Engineer" in r.text


def test_cleaner_token_cap():
    r = clean_html(html("nav_heavy.html"), "https://x.io/", NOW, "homepage", max_tokens=60)
    assert r.clean_tokens <= 70
    assert "SOC 2" in r.text  # highest-priority lines kept first


def test_cleaner_cross_page_dedupe():
    seen = set()
    a = clean_html(html("thin_homepage.html"), "https://acme-thin.io/", NOW, "homepage", seen=seen)
    b = clean_html(html("thin_homepage.html"), "https://acme-thin.io/again", NOW, "homepage", seen=seen)
    assert a.words > 40 and b.words == 0


def test_footer_cert_badge_detected():
    r = clean_html(html("certified_saas_home.html"), "https://schedulr.io/", NOW, "homepage")
    assert any("SOC 2" in c for c in r.footer_compliance)
    assert any("HIPAA" in c for c in r.footer_compliance)


# ------------------------------------------------------------------ links
def test_discover_links_from_homepage():
    r = clean_html(html("certified_saas_home.html"), "https://schedulr.io/", NOW, "homepage")
    d = discover("schedulr.io", r.links)
    assert d["security"] == ["https://schedulr.io/security"]
    assert d["careers"] == ["https://schedulr.io/careers"]
    assert d["pricing"] == ["https://schedulr.io/pricing"]


# ------------------------------------------------------------------ robots
ROBOTS = """
User-agent: *
Disallow: /private
Allow: /private/press
Disallow: /*.pdf$

User-agent: badbot
Disallow: /
"""


def test_robots_longest_match_and_wildcards():
    r = parse_robots(ROBOTS)
    assert r.allowed("https://a.io/about")
    assert not r.allowed("https://a.io/private/x")
    assert r.allowed("https://a.io/private/press/2026")
    assert not r.allowed("https://a.io/files/deck.pdf")
    assert r.allowed("https://a.io/files/deck.pdf?x=1")
    assert not r.allowed("https://a.io/about", agent="BadBot/1.0")


@pytest.mark.parametrize("status,expected", [(404, True), (403, True), (500, False), (None, False)])
def test_robots_availability_semantics(status, expected):
    c = RobotsChecker(lambda url: (status, ""))
    assert c.allowed("https://a.io/careers") is expected


# ------------------------------------------------------------------ failure taxonomy
def _raw(body, status=200, **kw):
    return RawResponse(url="https://a.io/", status_code=status, final_url="https://a.io/", body=body,
                       content_type="text/html", **kw)


@pytest.mark.parametrize("fixture,status,expected", [
    ("cloudflare_challenge.html", 200, ReasonCode.CAPTCHA),
    ("access_denied.html", 200, ReasonCode.BOT_BLOCKED),
    ("access_denied.html", 403, ReasonCode.BOT_BLOCKED),
    ("login_wall.html", 200, ReasonCode.LOGIN_WALL),
    ("spa_shell.html", 200, ReasonCode.EMPTY_SHELL),
    ("thin_homepage.html", 200, ReasonCode.THIN_CONTENT),
])
def test_classify_silent_failures(fixture, status, expected):
    from src.scrape.cleaner import visible_word_count
    body = html(fixture)
    assert classify(_raw(body, status), visible_word_count(body)) == expected


def test_classify_transport_errors():
    assert classify(RawResponse(url="u", status_code=None, final_url=None, body="", error="timeout"), 0) \
        == ReasonCode.TIMEOUT
    assert classify(RawResponse(url="u", status_code=None, final_url=None, body="", error="dns_error"), 0) \
        == ReasonCode.DNS_ERROR
    assert classify(_raw("", 429), 0) == ReasonCode.RATE_LIMITED
    assert classify(_raw("%PDF-1.7 ...", 200), 0) == ReasonCode.NON_TEXT_CONTENT
    assert classify(_raw("<p>x</p>", 451), 1) == ReasonCode.GEO_BLOCKED


# ------------------------------------------------------------------ fetcher
def test_fetcher_plain_then_js_escalation(db):
    pages = {"https://ledgerly.example/": html("spa_shell.html")}
    js = {"https://ledgerly.example/": html("nav_heavy.html")}
    f, t = make_fetcher(db, pages, js)
    s = f.start("ledgerly.example")
    p = f.fetch_homepage(s)
    assert t.calls == [("https://ledgerly.example/", False), ("https://ledgerly.example/", True)]
    assert p.ok and p.meta.mode.value == "js"
    assert p.meta.credits == 6          # 1 plain + 5 js
    assert db.one("SELECT mode, credits FROM pages")["mode"] == "js"


def test_fetcher_no_escalation_when_content_rich(db):
    f, t = make_fetcher(db, {"https://ledgerly.example/": html("nav_heavy.html")})
    s = f.start("ledgerly.example")
    p = f.fetch_homepage(s)
    assert t.calls == [("https://ledgerly.example/", False)]
    assert p.meta.mode.value == "plain" and p.meta.credits == 1


def test_fetcher_respects_robots(db):
    f, t = make_fetcher(db, {"https://a.io/careers": html("careers_compliance.html")},
                        robots={"https://a.io/robots.txt": (200, "User-agent: *\nDisallow: /careers")})
    s = f.start("a.io")
    p = f.fetch(s, "https://a.io/careers", "careers")
    assert not p.ok and p.meta.failure_reason == ReasonCode.ROBOTS_DISALLOWED
    assert t.calls == []                          # never fetched
    assert s.fetch_count == 0


def test_fetcher_blocked_site_reason(db):
    f, _ = make_fetcher(db, {"https://blocked.io/": html("cloudflare_challenge.html")},
                        js_pages={"https://blocked.io/": html("cloudflare_challenge.html")})
    s = f.start("blocked.io")
    p = f.fetch_homepage(s)
    assert not p.ok and p.meta.failure_reason == ReasonCode.CAPTCHA
    assert "homepage_unusable" in s.flags


def test_fetcher_domain_mismatch_on_redirect(db):
    f, _ = make_fetcher(db, {"https://oldco.io/": (200, html("nav_heavy.html"), "https://newowner.com/")})
    s = f.start("oldco.io")
    p = f.fetch_homepage(s)
    assert p.meta.failure_reason == ReasonCode.DOMAIN_MISMATCH and "domain_mismatch" in s.flags


def test_fetcher_parked_domain(db):
    f, _ = make_fetcher(db, {"https://example-parked.com/": html("parked.html")},
                        js_pages={"https://example-parked.com/": html("parked.html")})
    s = f.start("example-parked.com")
    p = f.fetch_homepage(s)
    assert not p.ok and "parked_domain" in s.flags


def test_fetcher_hard_cap(db):
    pages = {f"https://a.io/p{i}": html("nav_heavy.html") for i in range(10)}
    f, t = make_fetcher(db, pages)
    s = f.start("a.io")
    results = [f.fetch(s, f"https://a.io/p{i}", "other") for i in range(7)]
    assert s.fetch_count == 5
    assert results[5].meta.failure_reason == ReasonCode.FETCH_CAP_REACHED
    assert len(t.calls) == 5


def test_fetcher_refuses_other_hosts(db):
    f, t = make_fetcher(db, {})
    s = f.start("a.io")
    p = f.fetch(s, "https://linkedin.com/company/a", "news")
    assert p.meta.failure_reason == ReasonCode.DOMAIN_MISMATCH and t.calls == []


def test_cache_prevents_refetch_within_ttl(db, tmp_path):
    cache = HttpCache(db, tmp_path, ttl_days=7)
    pages = {"https://ledgerly.example/": html("nav_heavy.html")}
    f, t = make_fetcher(db, pages, cache=cache)
    f.fetch_homepage(f.start("ledgerly.example"))
    f2, t2 = make_fetcher(db, pages, cache=cache)
    p = f2.fetch_homepage(f2.start("ledgerly.example"))
    assert len(t.calls) == 1 and t2.calls == []
    assert p.meta.from_cache and p.meta.credits == 0
