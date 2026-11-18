"""Wire real (or offline-demo) components into a Pipeline."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from src.common.config import Settings
from src.contacts.hunter import HunterClient
from src.llm.gemini_client import FakeLlm, GeminiLlm
from src.mail.gmail_sender import GmailSender
from src.pain.search_provider import CachedSearch, GeminiGroundedSearch, SerperProvider
from src.pipeline import Pipeline
from src.scrape.fetcher import PageFetcher
from src.scrape.http_cache import HttpCache
from src.scrape.robots import RobotsChecker, httpx_robots_fetch
from src.scrape.transport import DirectTransport, FixtureTransport, ScrapingBeeTransport
from src.store.db import Database


class SetupError(RuntimeError):
    pass


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


def open_db(s: Settings) -> Database:
    db = Database(s.pipeline.paths.abs("db"))
    db.init()
    return db


def build_live(s: Settings, db: Database, run_id: str, *, dry_run: bool, live_hunter: bool,
               transport: Optional[str] = None, today: Optional[date] = None) -> Pipeline:
    p = s.pipeline
    sec = s.secrets
    missing = []
    if not sec.gemini_api_key:
        missing.append("GEMINI_API_KEY")
    tname = transport or p.scrape.transport
    if tname == "scrapingbee" and not sec.scrapingbee_api_key:
        missing.append("SCRAPINGBEE_API_KEY (or use --transport direct for a free dev run)")
    if missing:
        raise SetupError("Missing in .env: " + ", ".join(missing))
    cache = HttpCache(db, p.paths.abs("cache"), p.scrape.cache_ttl_days)
    tr = (ScrapingBeeTransport(sec.scrapingbee_api_key, p.scrape.timeout_s, p.scrape.credits)
          if tname == "scrapingbee" else DirectTransport(p.scrape.timeout_s))
    robots = RobotsChecker(httpx_robots_fetch(), cache, p.scrape.robots_user_agent)
    fetcher = PageFetcher(p, db, tr, robots, cache, run_id)
    llm = GeminiLlm(p.llm, db, sec.gemini_api_key)
    if p.search.provider == "serper":
        inner = SerperProvider(sec.search_api_key or "", db, run_id)
    else:
        inner = GeminiGroundedSearch(llm)
    search = CachedSearch(inner, db, p.search.cache_ttl_days, p.search.max_searches_per_company)
    hunter = HunterClient(p.hunter, db, sec.hunter_api_key, live_enabled=live_hunter, run_id=run_id)
    sender = GmailSender(sec.gmail_client_secret_file, sec.gmail_token_file, dry_run=dry_run,
                         outbox_dir=p.paths.abs("reports") / run_id / "outbox")
    return Pipeline(s, db, llm, fetcher, search, hunter, sender, run_id, today=today)


def build_offline_demo(s: Settings, db: Database, run_id: str, today: Optional[date] = None,
                       reports_dir: Optional[Path] = None, outcomes_dir: Optional[Path] = None) -> Pipeline:
    from src import demo_offline as demo
    p = s.pipeline
    tr = FixtureTransport(demo.pages(), simulate_credits=False)
    robots = RobotsChecker(lambda url: (404, ""))      # fixture sites publish no robots.txt -> allowed
    fetcher = PageFetcher(p, db, tr, robots, None, run_id, sleep=lambda _x: None)
    llm = FakeLlm(p.llm, db, handlers=demo.HANDLERS, grounded_handler=demo.fake_grounded)
    search = CachedSearch(GeminiGroundedSearch(llm, resolve=False), db, p.search.cache_ttl_days,
                          p.search.max_searches_per_company)
    p.hunter.mode = "mock"
    hunter = HunterClient(p.hunter, db, None, live_enabled=False, run_id=run_id)
    sender = GmailSender(None, None, dry_run=True, outbox_dir=(reports_dir or p.paths.abs("reports") / run_id)
                         / "outbox")
    return Pipeline(s, db, llm, fetcher, search, hunter, sender, run_id, today=today or date(2026, 11, 14),
                    reports_dir=reports_dir, outcomes_dir=outcomes_dir)
