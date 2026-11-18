"""Single-fetch page fetcher (Step 1 + 1b).

Per URL:  cap check -> host check -> robots -> cache -> plain fetch -> (JS only if thin/empty) -> classify
          -> clean -> domain sanity -> persist page + cost.
Never crawls: callers ask for specific URLs. Every non-success carries exactly one ReasonCode.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urlparse

from src.common.budget import CompanyBudget
from src.common.config import PipelineCfg
from src.common.domains import same_entity
from src.common.logging import get_logger
from src.scrape.cleaner import CleanResult, clean_html, visible_word_count
from src.scrape.failures import classify, is_parked
from src.scrape.http_cache import HttpCache
from src.scrape.links import ATS_HOSTS, discover
from src.scrape.robots import RobotsChecker
from src.scrape.transport import RawResponse, Transport
from src.store.db import Database
from src.store.models import CostEntry, FetchMode, PageFetch, ReasonCode, Step

log = get_logger(__name__)

USABLE_MIN_WORDS = 30


@dataclass
class FetchedPage:
    meta: PageFetch
    clean: Optional[CleanResult] = None

    @property
    def ok(self) -> bool:
        return self.meta.ok


@dataclass
class FetchSession:
    """State for one company: enforces the per-company fetch cap and dedupes text across pages."""
    domain: str
    max_fetches: int
    fetch_count: int = 0
    credits: int = 0
    pages: list[FetchedPage] = field(default_factory=list)
    discovered: dict[str, list[str]] = field(default_factory=dict)
    seen_lines: set[str] = field(default_factory=set)
    base_host: Optional[str] = None
    scheme: str = "https"
    final_domain: Optional[str] = None
    flags: list[str] = field(default_factory=list)       # e.g. domain_mismatch, parked_domain, homepage_blocked

    @property
    def remaining(self) -> int:
        return max(0, self.max_fetches - self.fetch_count)

    def fetched_urls(self) -> set[str]:
        return {p.meta.url.rstrip("/") for p in self.pages}

    def fetched_types(self) -> set[str]:
        return {p.meta.page_type for p in self.pages if p.ok and p.meta.page_type}

    def chunks(self, page_types: Optional[set[str]] = None):
        out = []
        for p in self.pages:
            if p.ok and p.clean and (page_types is None or p.meta.page_type in page_types):
                out.extend(p.clean.chunks)
        return out


class PageFetcher:
    def __init__(self, cfg: PipelineCfg, db: Database, transport: Transport, robots: RobotsChecker,
                 cache: Optional[HttpCache] = None, run_id: Optional[str] = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.db = db
        self.transport = transport
        self.robots = robots
        self.cache = cache
        self.run_id = run_id
        self.sleep = sleep
        self.clock = clock
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ public
    def start(self, domain: str) -> FetchSession:
        return FetchSession(domain=domain, max_fetches=self.cfg.scrape.max_fetches_per_company)

    def fetch_homepage(self, s: FetchSession, budget: Optional[CompanyBudget] = None) -> FetchedPage:
        page = self.fetch(s, f"https://{s.domain}/", "homepage", budget=budget)
        if not page.ok and page.meta.failure_reason in (ReasonCode.DNS_ERROR, ReasonCode.TIMEOUT) and s.remaining:
            page = self.fetch(s, f"https://www.{s.domain}/", "homepage", budget=budget)
        if page.ok and page.clean:
            final = page.meta.final_url or page.meta.url
            p = urlparse(final)
            s.base_host, s.scheme = p.hostname, p.scheme or "https"
            s.final_domain = p.hostname
            s.discovered = discover(s.domain, page.clean.links)
        if not page.ok:
            s.flags.append("homepage_unusable")
        return page

    def fetch(self, s: FetchSession, url: str, page_type: str, round_no: int = 0,
              budget: Optional[CompanyBudget] = None) -> FetchedPage:
        if url.rstrip("/") in s.fetched_urls():
            return next(p for p in s.pages if p.meta.url.rstrip("/") == url.rstrip("/"))
        if s.fetch_count >= s.max_fetches:
            return self._record(s, self._fail(s, url, page_type, ReasonCode.FETCH_CAP_REACHED,
                                              f"cap {s.max_fetches} reached"), round_no, persist=False)
        host = (urlparse(url).hostname or "").lower()
        if not (same_entity(s.domain, url) or host in ATS_HOSTS):
            return self._record(s, self._fail(s, url, page_type, ReasonCode.DOMAIN_MISMATCH,
                                              "refusing to fetch another entity's host"), round_no)
        if self.cfg.scrape.respect_robots and not self.robots.allowed(url):
            log.info("robots disallowed", extra={"domain": s.domain, "url": url})
            return self._record(s, self._fail(s, url, page_type, ReasonCode.ROBOTS_DISALLOWED,
                                              "robots.txt disallows this path"), round_no)
        if budget:
            budget.check()

        s.fetch_count += 1
        raw, mode, from_cache = self._get(url, render_js=False)
        words = visible_word_count(raw.body)
        reason = classify(raw, words, thin_min_words=self.cfg.scrape.js_escalation_min_words)
        credits = 0 if from_cache else raw.credits

        can_js = self.transport.name != "direct"
        if reason in (ReasonCode.THIN_CONTENT, ReasonCode.EMPTY_SHELL) and can_js:
            if budget:
                budget.check()
            raw_js, mode_js, cached_js = self._get(url, render_js=True)
            words_js = visible_word_count(raw_js.body)
            reason_js = classify(raw_js, words_js, thin_min_words=self.cfg.scrape.js_escalation_min_words,
                                 after_js=True)
            credits += 0 if cached_js else raw_js.credits
            if raw_js.body and (words_js >= words or reason_js is None):
                raw, mode, from_cache, words, reason = raw_js, mode_js, cached_js, words_js, reason_js

        s.credits += credits
        meta = PageFetch(domain=s.domain, url=url, final_url=raw.final_url, page_type=page_type, mode=mode,
                         ok=False, status_code=raw.status_code, from_cache=from_cache, credits=credits,
                         fetched_at=datetime.now(timezone.utc), failure_detail=raw.error_detail)
        clean: Optional[CleanResult] = None
        if raw.body and reason not in (ReasonCode.BOT_BLOCKED, ReasonCode.CAPTCHA, ReasonCode.LOGIN_WALL,
                                       ReasonCode.GEO_BLOCKED, ReasonCode.NON_TEXT_CONTENT,
                                       ReasonCode.HTTP_ERROR, ReasonCode.RATE_LIMITED):
            clean = clean_html(raw.body, url, meta.fetched_at, page_type,
                               max_tokens=self.cfg.cleaner.max_tokens_per_group,
                               min_line_words=self.cfg.cleaner.min_line_words, seen=s.seen_lines)
            meta.raw_tokens, meta.clean_tokens, meta.text_hash = clean.raw_tokens, clean.clean_tokens, clean.text_hash
            if is_parked(raw.body, words):
                reason = ReasonCode.PARKED_DOMAIN
                s.flags.append("parked_domain")
        meta.failure_reason = reason
        meta.ok = clean is not None and clean.words >= USABLE_MIN_WORDS and reason not in (
            ReasonCode.PARKED_DOMAIN, ReasonCode.EMPTY_SHELL)

        # domain sanity: did we end up on someone else's site?
        if raw.final_url and not same_entity(s.domain, raw.final_url) and \
                (urlparse(raw.final_url).hostname or "") not in ATS_HOSTS:
            meta.failure_reason = ReasonCode.DOMAIN_MISMATCH
            meta.failure_detail = f"redirected to {urlparse(raw.final_url).hostname}"
            if page_type == "homepage":
                s.flags.append("domain_mismatch")

        if budget and credits:
            budget.add(cost_usd=self._credit_cost(credits), credits=credits)
        return self._record(s, FetchedPage(meta=meta, clean=clean), round_no)

    # ------------------------------------------------------------------ internals
    def _credit_cost(self, credits: int) -> Optional[float]:
        rate = self.cfg.scrape.usd_per_credit
        return None if rate is None else credits * rate

    def _mode(self, render_js: bool) -> FetchMode:
        if self.transport.name == "direct":
            return FetchMode.DIRECT
        return FetchMode.JS if render_js else FetchMode.PLAIN

    def _polite_wait(self, host: str) -> None:
        delay = self.cfg.run.per_domain_delay_s
        with self._lock:
            last = self._last_hit.get(host)
            now = self.clock()
            wait = 0.0 if last is None else max(0.0, delay - (now - last))
            self._last_hit[host] = now + wait
        if wait > 0:
            self.sleep(wait)

    def _get(self, url: str, render_js: bool) -> tuple[RawResponse, FetchMode, bool]:
        mode = self._mode(render_js)
        key_mode = "js" if render_js else "plain"
        if self.cache:
            hit = self.cache.get(url, key_mode)
            if hit:
                return (RawResponse(url=url, status_code=hit.status_code, final_url=hit.final_url, body=hit.body,
                                    content_type="text/html", credits=0), mode, True)
        self._polite_wait(urlparse(url).hostname or "")
        raw = self.transport.fetch(url, render_js=render_js)
        if self.cache and raw.error is None and raw.body:
            self.cache.put(url, key_mode, raw.status_code, raw.final_url, raw.body)
        return raw, mode, False

    def _fail(self, s: FetchSession, url: str, page_type: str, reason: ReasonCode, detail: str) -> FetchedPage:
        return FetchedPage(meta=PageFetch(domain=s.domain, url=url, page_type=page_type, mode=self._mode(False),
                                          ok=False, failure_reason=reason, failure_detail=detail))

    def _record(self, s: FetchSession, page: FetchedPage, round_no: int, persist: bool = True) -> FetchedPage:
        if page.meta.failure_reason != ReasonCode.FETCH_CAP_REACHED:
            s.pages.append(page)
        m = page.meta
        log.info("page", extra={"domain": s.domain, "url": m.url, "mode": m.mode.value, "ok": m.ok,
                                "reason": m.failure_reason.value if m.failure_reason else None,
                                "raw_tokens": m.raw_tokens, "clean_tokens": m.clean_tokens,
                                "credits": m.credits, "cache": m.from_cache})
        if persist:
            self.db.insert("pages", {
                "run_id": self.run_id, "domain": s.domain, "url": m.url, "final_url": m.final_url,
                "page_type": m.page_type, "mode": m.mode, "ok": m.ok, "status_code": m.status_code,
                "failure_reason": m.failure_reason, "failure_detail": m.failure_detail,
                "fetched_at": m.fetched_at, "from_cache": m.from_cache, "raw_tokens": m.raw_tokens,
                "clean_tokens": m.clean_tokens, "text_hash": m.text_hash, "credits": m.credits, "round": round_no,
            })
            if m.credits:
                self.db.add_cost(CostEntry(domain=s.domain, step=Step.SCRAPE, kind="scrapingbee",
                                           credits=m.credits, cost_usd=self._credit_cost(m.credits)),
                                 run_id=self.run_id)
        return page
