"""Pluggable search. Default: Gemini grounded search. Optional: Serper. Selected by search.provider.

Provenance rule: a URL can only come from grounding metadata / the search API's result list, never from prose
the model wrote. Grounding redirect URLs are resolved to their final URL; both are stored.
Two-call pattern (Addendum A): (1) grounded call -> prose + citations; (2) separate un-grounded structured call
(src/pain/research.py) converts that into evidence items that reference citations by index only.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from string import Template
from typing import Optional, Protocol

import httpx

from src.common.logging import get_logger
from src.llm.gemini_client import BaseLlm, CallCtx, Citation, LlmError
from src.llm.prompts import RESEARCH_QUERY_V1
from src.scrape.transport import BROWSER_UA
from src.store.db import Database
from src.store.models import CostEntry, ReasonCode

log = get_logger(__name__)


@dataclass
class SearchResult:
    query: str
    text: str
    citations: list[Citation] = field(default_factory=list)
    provider: str = ""
    cached: bool = False

    def to_json(self) -> str:
        return json.dumps({"query": self.query, "text": self.text, "provider": self.provider,
                           "citations": [asdict(c) for c in self.citations]})

    @classmethod
    def from_json(cls, s: str) -> "SearchResult":
        d = json.loads(s)
        return cls(query=d["query"], text=d["text"], provider=d.get("provider", ""),
                   citations=[Citation(**c) for c in d.get("citations", [])], cached=True)


class SearchUnavailable(RuntimeError):
    pass


class SearchProvider(Protocol):
    name: str

    def search(self, ctx: CallCtx, domain: str, company: str, focus: str) -> SearchResult: ...


def resolve_url(url: str, client: Optional[httpx.Client] = None, timeout: int = 8) -> Optional[str]:
    """Follow redirects (grounding links are redirect URLs). Returns the final URL or None."""
    c = client or httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": BROWSER_UA})
    try:
        r = c.head(url)
        if r.status_code >= 400 or str(r.url) == url:
            r = c.get(url)
        return str(r.url)
    except Exception:
        return None


class CachedSearch:
    """Wraps a provider with the (domain, query) cache and the per-company search cap."""

    def __init__(self, inner: SearchProvider, db: Database, ttl_days: int, max_per_company: int):
        self.inner = inner
        self.db = db
        self.ttl = timedelta(days=ttl_days)
        self.max = max_per_company
        self.used: dict[str, int] = {}
        self.name = inner.name

    def remaining(self, domain: str) -> int:
        return max(0, self.max - self.used.get(domain, 0))

    def search(self, ctx: CallCtx, domain: str, company: str, focus: str) -> SearchResult:
        key = f"{company}|{focus}"
        row = self.db.one("SELECT * FROM search_cache WHERE domain=? AND query=? AND provider=?",
                          (domain, key, self.inner.name))
        if row and datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"]) <= self.ttl:
            return SearchResult.from_json(row["response_json"])
        if self.remaining(domain) <= 0:
            raise SearchUnavailable(f"search cap {self.max} reached for {domain}")
        self.used[domain] = self.used.get(domain, 0) + 1
        res = self.inner.search(ctx, domain, company, focus)
        self.db.execute("INSERT OR REPLACE INTO search_cache (domain, query, provider, response_json, fetched_at)"
                        " VALUES (?,?,?,?,?)",
                        (domain, key, self.inner.name, res.to_json(), datetime.now(timezone.utc).isoformat()))
        return res


class GeminiGroundedSearch:
    name = "gemini_grounded"

    def __init__(self, llm: BaseLlm, resolve: bool = True):
        self.llm = llm
        self.resolve = resolve
        self._client = httpx.Client(timeout=8, follow_redirects=True, headers={"User-Agent": BROWSER_UA})

    def search(self, ctx: CallCtx, domain: str, company: str, focus: str) -> SearchResult:
        prompt = Template(RESEARCH_QUERY_V1).substitute(domain=domain, company=company or domain, focus=focus)
        try:
            g = self.llm.grounded(ctx, prompt)
        except LlmError as e:
            raise SearchUnavailable(str(e)) from e
        cites = []
        for c in g.citations:
            if self.resolve:
                c.resolved_uri = resolve_url(c.uri, self._client)
            cites.append(c)
        return SearchResult(query=focus, text=g.text, citations=cites, provider=self.name)


class SerperProvider:
    name = "serper"
    URL = "https://google.serper.dev/search"

    def __init__(self, api_key: str, db: Database, run_id: Optional[str] = None):
        if not api_key:
            raise ValueError("SEARCH_API_KEY is not set")
        self.api_key = api_key
        self.db = db
        self.run_id = run_id

    def search(self, ctx: CallCtx, domain: str, company: str, focus: str) -> SearchResult:
        q = f"{company or domain} {focus}"
        try:
            r = httpx.post(self.URL, json={"q": q, "num": 8}, headers={"X-API-KEY": self.api_key}, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            raise SearchUnavailable(f"serper: {type(e).__name__}") from e
        cites, lines = [], []
        for i, item in enumerate(data.get("organic", [])[:8]):
            link = item.get("link")
            if not link:
                continue
            cites.append(Citation(index=len(cites), uri=link, title=item.get("title"), resolved_uri=link))
            lines.append(f"[{len(cites) - 1}] {item.get('title', '')} ({item.get('date', 'undated')}): "
                         f"{item.get('snippet', '')}")
        self.db.add_cost(CostEntry(domain=ctx.domain, step=ctx.step, kind="search", calls=1, cost_usd=None),
                         run_id=ctx.run_id)
        return SearchResult(query=q, text="\n".join(lines), citations=cites, provider=self.name)


class FakeSearch:
    """Offline tests: focus substring -> SearchResult."""
    name = "fake"

    def __init__(self, results: dict[str, SearchResult], fail: bool = False):
        self.results = results
        self.fail = fail
        self.calls: list[str] = []

    def search(self, ctx: CallCtx, domain: str, company: str, focus: str) -> SearchResult:
        self.calls.append(focus)
        if self.fail:
            raise SearchUnavailable("fake failure")
        for k, v in self.results.items():
            if k.lower() in focus.lower():
                return v
        return SearchResult(query=focus, text="No relevant results found.", citations=[], provider=self.name)


def search_failure_reason() -> ReasonCode:
    return ReasonCode.SEARCH_UNAVAILABLE
