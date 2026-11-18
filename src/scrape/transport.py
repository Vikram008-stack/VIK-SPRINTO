"""Transports: how one URL is fetched. The fetcher never knows which one it has.

- ScrapingBeeTransport: the production path. Plain (render_js=false, 1 credit) or JS (5 credits).
  Uses json_response=true so the target's initial status code and resolved (post-redirect) URL come back.
- DirectTransport: dev-only plain HTTP (0 credits). Robots.txt is still enforced by the fetcher.
- FixtureTransport: offline tests; maps URL -> HTML string.
"""
from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from typing import Optional, Protocol

import httpx

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36 SprintoOutreachResearch/0.1")


@dataclass
class RawResponse:
    url: str
    status_code: Optional[int]          # the TARGET site's status code
    final_url: Optional[str]
    body: str
    content_type: Optional[str] = None
    credits: int = 0
    error: Optional[str] = None          # transport error: timeout | dns_error | rate_limited | provider_error
    error_detail: Optional[str] = None
    headers: dict = field(default_factory=dict)


class Transport(Protocol):
    name: str

    def fetch(self, url: str, render_js: bool) -> RawResponse: ...


def _exc_to_code(e: Exception) -> str:
    if isinstance(e, httpx.TimeoutException):
        return "timeout"
    text = str(e).lower()
    cause = e.__cause__ or e.__context__
    if isinstance(cause, socket.gaierror) or "getaddrinfo" in text or "name or service" in text \
            or "nodename" in text or "no address" in text:
        return "dns_error"
    return "provider_error"


class ScrapingBeeTransport:
    name = "scrapingbee"
    BASE = "https://app.scrapingbee.com/api/v1"

    def __init__(self, api_key: str, timeout_s: int = 30, credits: Optional[dict] = None,
                 client: Optional[httpx.Client] = None):
        if not api_key:
            raise ValueError("SCRAPINGBEE_API_KEY is not set")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.credits = credits or {"plain": 1, "js": 5}
        self.client = client or httpx.Client(timeout=timeout_s + 15)

    def fetch(self, url: str, render_js: bool) -> RawResponse:
        params = {
            "url": url,
            "render_js": "true" if render_js else "false",
            "block_resources": "true",
            "json_response": "true",
            "timeout": str(self.timeout_s * 1000),
        }
        if render_js:
            params["wait"] = "1500"
        default_credits = self.credits["js" if render_js else "plain"]
        try:
            r = self.client.get(self.BASE, params=params, headers={"Authorization": f"Bearer {self.api_key}"})
        except Exception as e:  # network failure reaching ScrapingBee itself
            return RawResponse(url=url, status_code=None, final_url=None, body="", error=_exc_to_code(e),
                               error_detail=type(e).__name__)
        if r.status_code == 401:
            return RawResponse(url=url, status_code=None, final_url=None, body="", error="provider_error",
                               error_detail="ScrapingBee rejected the API key (401)")
        if r.status_code == 429:
            return RawResponse(url=url, status_code=429, final_url=None, body="", error="rate_limited",
                               error_detail="ScrapingBee concurrency or credit limit (429)")
        try:
            data = r.json()
        except (json.JSONDecodeError, ValueError):
            # Non-JSON reply (e.g. ScrapingBee 500 when the target keeps failing)
            return RawResponse(url=url, status_code=r.status_code, final_url=None, body=r.text or "",
                               error="provider_error" if r.status_code >= 500 else None,
                               error_detail=f"ScrapingBee HTTP {r.status_code}", credits=0)
        if not isinstance(data, dict):
            return RawResponse(url=url, status_code=r.status_code, final_url=None, body="",
                               error="provider_error", error_detail="unexpected ScrapingBee payload")
        body = data.get("body") or ""
        if not isinstance(body, str):
            body = json.dumps(body)
        target_status = data.get("initial-status-code") or data.get("initial_status_code") or r.status_code
        final_url = data.get("resolved-url") or data.get("resolved_url") or url
        cost = data.get("cost")
        headers = data.get("headers") if isinstance(data.get("headers"), dict) else {}
        ctype = data.get("type") or headers.get("content-type") or headers.get("Content-Type")
        return RawResponse(url=url, status_code=int(target_status) if target_status else None, final_url=final_url,
                           body=body, content_type=ctype,
                           credits=int(cost) if isinstance(cost, (int, float)) else default_credits,
                           headers=headers)


class DirectTransport:
    """Dev-only: polite plain HTTP GET. No JS rendering; render_js requests return an error."""
    name = "direct"

    def __init__(self, timeout_s: int = 20, client: Optional[httpx.Client] = None):
        self.client = client or httpx.Client(timeout=timeout_s, follow_redirects=True,
                                             headers={"User-Agent": BROWSER_UA, "Accept-Language": "en"})

    def fetch(self, url: str, render_js: bool) -> RawResponse:
        if render_js:
            return RawResponse(url=url, status_code=None, final_url=None, body="", error="provider_error",
                               error_detail="direct transport cannot render JS")
        try:
            r = self.client.get(url)
        except Exception as e:
            return RawResponse(url=url, status_code=None, final_url=None, body="", error=_exc_to_code(e),
                               error_detail=type(e).__name__)
        ctype = r.headers.get("content-type", "")
        textual = (not ctype) or any(t in ctype for t in ("text", "html", "xml"))
        return RawResponse(url=url, status_code=r.status_code, final_url=str(r.url),
                           body=r.text if textual else "", content_type=ctype, credits=0)


class FixtureTransport:
    """Offline tests. pages: url -> html | (status, html) | (status, html, final_url). Missing url -> 404."""
    name = "fixture"

    def __init__(self, pages: dict, js_pages: Optional[dict] = None):
        self.pages = pages
        self.js_pages = js_pages or {}
        self.calls: list[tuple[str, bool]] = []

    def fetch(self, url: str, render_js: bool) -> RawResponse:
        self.calls.append((url, render_js))
        src = self.js_pages if (render_js and url in self.js_pages) else self.pages
        val = src.get(url, src.get(url.rstrip("/")))
        credits = 5 if render_js else 1
        if val is None:
            return RawResponse(url=url, status_code=404, final_url=url, body="<html><body>Not found</body></html>",
                               content_type="text/html", credits=credits)
        status, html, final = 200, val, url
        if isinstance(val, tuple):
            status, html = val[0], val[1]
            final = val[2] if len(val) > 2 else url
        return RawResponse(url=url, status_code=status, final_url=final, body=html, content_type="text/html",
                           credits=credits)
