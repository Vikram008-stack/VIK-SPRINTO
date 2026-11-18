"""robots.txt checker (RFC 9309 semantics).

- Group selection: most specific matching user-agent group, else '*'.
- Rule matching: longest match wins; on a tie Allow wins; supports '*' and '$'.
- Availability: 2xx -> parse; 4xx -> allow all ("unavailable"); 5xx / network error -> disallow all.
- robots.txt is fetched directly (free) and cached for the cache TTL.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlparse

from src.common.logging import get_logger
from src.scrape.http_cache import HttpCache

log = get_logger(__name__)


@dataclass
class RobotsRules:
    groups: dict[str, list[tuple[str, str]]] = field(default_factory=dict)   # agent -> [(allow|disallow, path)]
    allow_all: bool = False
    disallow_all: bool = False
    source_status: Optional[int] = None

    def _rules_for(self, agent: str) -> list[tuple[str, str]]:
        agent = agent.lower()
        best = None
        for name in self.groups:
            if name != "*" and name in agent and (best is None or len(name) > len(best)):
                best = name
        return self.groups[best] if best is not None else self.groups.get("*", [])

    def allowed(self, url: str, agent: str = "*") -> bool:
        if self.disallow_all:
            return False
        if self.allow_all:
            return True
        p = urlparse(url)
        path = (p.path or "/") + (f"?{p.query}" if p.query else "")
        best_len, verdict = -1, True
        for kind, pattern in self._rules_for(agent):
            if pattern == "":
                continue  # empty Disallow = allow everything
            if _match(pattern, path):
                plen = len(pattern)
                if plen > best_len or (plen == best_len and kind == "allow"):
                    best_len, verdict = plen, (kind == "allow")
        return verdict


def _match(pattern: str, path: str) -> bool:
    anchored = pattern.endswith("$")
    core = pattern[:-1] if anchored else pattern
    regex = "^" + re.escape(core).replace(r"\*", ".*") + ("$" if anchored else "")
    return re.match(regex, path) is not None


def parse_robots(text: str) -> RobotsRules:
    rules = RobotsRules()
    current: list[str] = []
    last_was_agent = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, val = (s.strip() for s in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if not last_was_agent:
                current = []
            current.append(val.lower())
            rules.groups.setdefault(val.lower(), [])
            last_was_agent = True
        elif key in ("allow", "disallow"):
            for a in current:
                rules.groups.setdefault(a, []).append((key, val))
            last_was_agent = False
        else:
            last_was_agent = False
    return rules


RobotsFetch = Callable[[str], tuple[Optional[int], str]]   # url -> (status or None on network error, body)


class RobotsChecker:
    def __init__(self, fetch: RobotsFetch, cache: Optional[HttpCache] = None, agent: str = "*"):
        self.fetch = fetch
        self.cache = cache
        self.agent = agent
        self._mem: dict[str, RobotsRules] = {}

    def rules_for_host(self, scheme: str, host: str) -> RobotsRules:
        key = f"{scheme}://{host}"
        if key in self._mem:
            return self._mem[key]
        url = f"{key}/robots.txt"
        cached = self.cache.get(url, "direct") if self.cache else None
        if cached:
            status, body = cached.status_code, cached.body
        else:
            status, body = self.fetch(url)
            if self.cache is not None and status is not None:
                self.cache.put(url, "direct", status, url, body)
        if status is None or status >= 500:
            rules = RobotsRules(disallow_all=True, source_status=status)
        elif 400 <= status < 500:
            rules = RobotsRules(allow_all=True, source_status=status)
        else:
            rules = parse_robots(body)
            rules.source_status = status
        self._mem[key] = rules
        log.debug("robots loaded", extra={"host": host, "status": status})
        return rules

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        return self.rules_for_host(p.scheme or "https", p.netloc).allowed(url, self.agent)


def httpx_robots_fetch(timeout_s: int = 10) -> RobotsFetch:
    import httpx

    from src.scrape.transport import BROWSER_UA

    client = httpx.Client(timeout=timeout_s, follow_redirects=True, headers={"User-Agent": BROWSER_UA})

    def _fetch(url: str) -> tuple[Optional[int], str]:
        try:
            r = client.get(url)
            return r.status_code, (r.text if r.status_code < 400 else "")
        except Exception:
            return None, ""
    return _fetch
