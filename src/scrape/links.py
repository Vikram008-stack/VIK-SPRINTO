"""Discover real page paths from homepage links; fall back to common paths.

Only same-entity hosts are allowed, plus well-known applicant-tracking hosts for the careers page
(the company's own job board hosted on Greenhouse/Lever/Ashby/Workable is still the company's page).
"""
from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import urlparse

from src.common.domains import same_entity
from src.scrape.cleaner import Link

PAGE_PATTERNS: dict[str, re.Pattern] = {
    "about": re.compile(r"/(about|about-us|company|our-story|who-we-are)(/|$)", re.I),
    "product": re.compile(r"/(product|products|platform|solutions?|features|how-it-works)(/|$)", re.I),
    "pricing": re.compile(r"/(pricing|plans|price)(/|$)", re.I),
    "careers": re.compile(r"/(careers?|jobs|join-us|join|work-with-us|hiring|open-positions)(/|$)", re.I),
    "security": re.compile(r"/(security|trust|trust-center|compliance|soc-?2|privacy-security)(/|$)", re.I),
    "customers": re.compile(r"/(customers|case-studies|customer-stories|success-stories|testimonials)(/|$)", re.I),
    "news": re.compile(r"/(news|press|newsroom|media|announcements|blog)(/|$)", re.I),
}
TEXT_HINTS: dict[str, re.Pattern] = {
    "careers": re.compile(r"\b(careers?|jobs|we'?re hiring|join (us|the team))\b", re.I),
    "security": re.compile(r"\b(security|trust( center)?|compliance)\b", re.I),
    "pricing": re.compile(r"\bpricing|plans\b", re.I),
    "customers": re.compile(r"\b(customers|case studies)\b", re.I),
    "news": re.compile(r"\b(news|press|blog|newsroom)\b", re.I),
    "about": re.compile(r"\b(about( us)?|company)\b", re.I),
}
ATS_HOSTS = ("boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com",
             "apply.workable.com", "careers.smartrecruiters.com", "jobs.workable.com")

FALLBACK_PATHS: dict[str, list[str]] = {
    "about": ["/about"],
    "product": ["/product", "/platform"],
    "pricing": ["/pricing"],
    "careers": ["/careers", "/jobs"],
    "security": ["/security", "/trust"],
    "customers": ["/customers"],
    "news": ["/blog", "/news"],
}


def _allowed_host(domain: str, url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return same_entity(domain, url) or host in ATS_HOSTS


def discover(domain: str, links: Iterable[Link]) -> dict[str, list[str]]:
    """page_type -> candidate URLs (best first), from real homepage links."""
    found: dict[str, list[str]] = {k: [] for k in PAGE_PATTERNS}
    for link in links:
        url = link.href.split("#")[0]
        if not url.startswith("http") or not _allowed_host(domain, url):
            continue
        p = urlparse(url)
        path = p.path or "/"
        host = (p.hostname or "").lower()
        for ptype, pat in PAGE_PATTERNS.items():
            hit = bool(pat.search(path))
            if ptype == "careers" and host in ATS_HOSTS:
                hit = True
            if not hit and path.count("/") <= 2 and TEXT_HINTS.get(ptype, re.compile("$^")).search(link.text or ""):
                hit = True
            if hit and url not in found[ptype]:
                # prefer shallow paths (the index page, not one blog post)
                found[ptype].append(url)
        if host in ATS_HOSTS and url not in found["careers"]:
            found["careers"].append(url)
    for ptype in found:
        found[ptype].sort(key=lambda u: (urlparse(u).path.rstrip("/").count("/"), len(u)))
    return found


def candidates(domain: str, page_type: str, discovered: dict[str, list[str]], scheme: str = "https",
               base_host: Optional[str] = None) -> list[str]:
    host = base_host or domain
    out = list(discovered.get(page_type, []))[:2]
    for path in FALLBACK_PATHS.get(page_type, []):
        u = f"{scheme}://{host}{path}"
        if u not in out and u.rstrip("/") not in [x.rstrip("/") for x in out]:
            out.append(u)
    return out
