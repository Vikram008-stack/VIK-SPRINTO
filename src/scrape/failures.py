"""Detect the many ways a fetch fails silently (200 OK with a challenge page, empty SPA shell, ...).

classify() returns exactly one ReasonCode or None. Order matters: the most specific cause wins.
"""
from __future__ import annotations

import re
from typing import Optional

from src.scrape.transport import RawResponse
from src.store.models import ReasonCode

_CAPTCHA = re.compile(r"g-recaptcha|h-captcha|hcaptcha|px-captcha|captcha-delivery|cf-turnstile|"
                      r"verify (that )?you are (a )?human|are you a robot|complete the security check", re.I)
_BOT = re.compile(r"just a moment\.\.\.|attention required! \| cloudflare|cf-browser-verification|"
                  r"access denied|request blocked|pardon our interruption|datadome|incapsula incident|"
                  r"you have been blocked|sorry, you have been blocked|bot detection", re.I)
_GEO = re.compile(r"not available in your (country|region)|unavailable in your (country|region)|"
                  r"geo.?blocked|this content is not available in your location", re.I)
_LOGIN = re.compile(r"<input[^>]+type=[\"']?password", re.I)
_LOGIN_WORDS = re.compile(r"\b(sign in|log in|login)\b", re.I)
_RATE = re.compile(r"too many requests|rate limit(ed)? exceeded", re.I)
_PARKED = re.compile(r"this domain (name )?(is|may be) for sale|buy this domain|domain is parked|"
                     r"hugedomains|sedoparking|dan\.com/buy-domain|parkingcrew|bodis\.com", re.I)
_SHELL = re.compile(r"<div[^>]+id=[\"'](root|app|__next|__nuxt|svelte)[\"'][^>]*>\s*</div>", re.I)


def classify(raw: RawResponse, visible_words: int, *, thin_min_words: int = 200,
             after_js: bool = False) -> Optional[ReasonCode]:
    """Return the single failure reason for this response, or None if it is usable."""
    if raw.error == "timeout":
        return ReasonCode.TIMEOUT
    if raw.error == "dns_error":
        return ReasonCode.DNS_ERROR
    if raw.error == "rate_limited" or raw.status_code == 429:
        return ReasonCode.RATE_LIMITED
    if raw.error == "provider_error" and not raw.body:
        return ReasonCode.HTTP_ERROR

    body = raw.body or ""
    head = body[:20000]
    ctype = (raw.content_type or "").lower()
    if body.startswith("%PDF") or (ctype and not any(t in ctype for t in ("html", "text", "xml", "json"))):
        return ReasonCode.NON_TEXT_CONTENT

    if raw.status_code == 451 or (_GEO.search(head) and visible_words < 150):
        return ReasonCode.GEO_BLOCKED
    if _CAPTCHA.search(head) and visible_words < 300:
        return ReasonCode.CAPTCHA
    if raw.status_code in (401, 407):
        return ReasonCode.LOGIN_WALL
    if raw.status_code == 403 or (_BOT.search(head) and visible_words < 300):
        return ReasonCode.BOT_BLOCKED
    if _RATE.search(head) and visible_words < 100:
        return ReasonCode.RATE_LIMITED
    if raw.status_code is not None and raw.status_code >= 400:
        return ReasonCode.HTTP_ERROR
    if _LOGIN.search(head) and _LOGIN_WORDS.search(head) and visible_words < 120:
        return ReasonCode.LOGIN_WALL
    if visible_words < 40 and (_SHELL.search(body) or body.count("<script") >= 3):
        return ReasonCode.EMPTY_SHELL
    if visible_words < thin_min_words:
        # before JS we only flag it so the fetcher escalates; after JS it is the final reason
        return ReasonCode.THIN_CONTENT
    return None


def is_parked(body: str, visible_words: int) -> bool:
    return bool(_PARKED.search(body[:30000])) and visible_words < 400
