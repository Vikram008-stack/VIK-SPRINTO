"""Shared test helpers: fixture loading and an offline fetcher factory."""
from __future__ import annotations

from pathlib import Path

from src.common.config import load_settings
from src.scrape.fetcher import PageFetcher
from src.scrape.robots import RobotsChecker
from src.scrape.transport import FixtureTransport

FIX = Path(__file__).parent / "fixtures"


def html(name: str) -> str:
    return (FIX / "html" / name).read_text(encoding="utf-8")


def robots_from(mapping: dict[str, tuple]):
    """mapping: robots url -> (status, body). Missing -> 404 (allow all)."""
    def _fetch(url):
        return mapping.get(url, (404, ""))
    return _fetch


def make_fetcher(db, pages: dict, js_pages: dict | None = None, robots: dict | None = None, settings=None,
                 cache=None):
    s = settings or load_settings()
    transport = FixtureTransport(pages, js_pages)
    checker = RobotsChecker(robots_from(robots or {}))
    f = PageFetcher(s.pipeline, db, transport, checker, cache=cache, run_id=None, sleep=lambda _: None)
    return f, transport
