"""HTML -> compact, sourced text chunks. This is where most pipelines fail.

Pipeline:
  1. Collect links (before nav is stripped - nav holds the links we need for discovery).
  2. Harvest compliance mentions from the footer (badges like "SOC 2 Type II") before dropping it.
  3. Strip scripts/styles/nav/header-with-nav/footer/cookie banners/hidden elements/forms.
  4. Turn <time datetime> into "text (YYYY-MM-DD)" so event dates survive.
  5. Split into lines; drop lines under min_line_words unless heading / key term / job title.
  6. Dedupe repeated blocks (also across pages via a shared `seen` set).
  7. If over the token cap, keep the highest-priority lines, restoring document order.
  8. Chunk (~300 tokens), each chunk carrying source_url + fetched_at.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, Tag

from src.store.models import Chunk

CERT_RE = re.compile(r"\b(SOC ?[123]( Type ?(I{1,2}|1|2))?|ISO[ /-]?(IEC )?27001|ISO[ /-]?27701|ISO[ /-]?42001|"
                     r"GDPR|HIPAA|PCI[ -]?DSS|CCPA|FedRAMP|CSA STAR|TISAX|Cyber Essentials|DPDP)\b", re.I)
KEY_TERMS = re.compile(
    r"SOC ?2|ISO ?27001|GDPR|HIPAA|PCI|complian|audit|security|trust|certif|enterprise|customers?|"
    r"series [a-f]\b|seed|funding|raised|investors?|backed by|valuation|acquired|acquisition|"
    r"pricing|per (user|seat|month)|/mo\b|\$\d|€\d|₹\d|free trial|book a demo|request a demo|"
    r"employees|team of|headcount|open (roles|positions)|we('| a)re hiring|careers|"
    r"saas|platform|api|b2b|teams|businesses|companies|consult|outsourc|services|agency|staffing", re.I)
# strongest qualification evidence: kept first when the token cap bites
SIGNAL_TERMS = re.compile(
    r"SOC ?2|ISO ?27001|audit|complian|certif|security questionnaire|procurement|"
    r"series [a-f]\b|raised|funding|acquired|acquisition|head of (security|compliance)|ciso|grc", re.I)
JOB_TITLE = re.compile(
    r"\b(engineer|developer|manager|head|director|lead|vp|vice president|chief|officer|analyst|architect|"
    r"designer|specialist|counsel|recruiter|scientist|administrator|representative|executive|associate|"
    r"intern|ciso|cto|grc|compliance|security|sre|devops)\b", re.I)
COOKIE_RE = re.compile(r"cookie|consent|onetrust|cookiebot|osano|termly|cc-window|cc-banner|gdpr-banner|"
                       r"truste|didomi|usercentrics|iubenda", re.I)
POPUP_RE = re.compile(r"newsletter-popup|modal|popup|intercom|drift-|hubspot-messages|chat-widget", re.I)

LEAF_BLOCKS = ["p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "dt", "dd", "blockquote",
               "figcaption", "label", "summary"]
NESTED_BLOCKS = ["p", "ul", "ol", "li", "table", "div", "section", "article", "h1", "h2", "h3", "h4"]

DROP_TAGS = ["script", "style", "noscript", "svg", "iframe", "template", "canvas", "picture", "video",
             "audio", "form", "button", "select", "input", "textarea", "link", "meta"]


MAX_HTML_CHARS = 1_500_000     # parse at most ~1.5 MB of HTML: huge pages exhausted serverless memory


def estimate_tokens(text: str) -> int:
    return max(0, len(text) // 4)


@dataclass
class Link:
    href: str
    text: str


@dataclass
class CleanResult:
    source_url: str
    fetched_at: datetime
    page_type: Optional[str]
    title: Optional[str]
    raw_tokens: int
    clean_tokens: int
    words: int
    text: str
    chunks: list[Chunk]
    links: list[Link] = field(default_factory=list)
    footer_compliance: list[str] = field(default_factory=list)
    text_hash: str = ""


def _attr_text(tag: Tag) -> str:
    if not isinstance(tag, Tag) or tag.attrs is None:
        return ""
    parts = [tag.get("id") or "", " ".join(tag.get("class") or []), tag.get("aria-label") or "",
             tag.get("role") or ""]
    return " ".join(str(p) for p in parts)


def _is_hidden(tag: Tag) -> bool:
    if tag.attrs is None:
        return False
    if tag.has_attr("hidden") or tag.get("aria-hidden") == "true":
        return True
    style = (tag.get("style") or "").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


def _norm(line: str) -> str:
    return re.sub(r"\W+", " ", line.lower()).strip()


def clean_html(html: str, source_url: str, fetched_at: datetime, page_type: Optional[str] = None, *,
               max_tokens: int = 2000, min_line_words: int = 8, seen: Optional[set[str]] = None,
               chunk_tokens: int = 300) -> CleanResult:
    html = html or ""
    raw_tokens = estimate_tokens(html)
    soup = BeautifulSoup(html[:MAX_HTML_CHARS], "lxml")
    seen = seen if seen is not None else set()

    title = soup.title.get_text(" ", strip=True) if soup.title else None
    meta_desc = None
    m = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if m and m.get("content"):
        meta_desc = m["content"].strip()

    # 1. links
    links: list[Link] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        links.append(Link(href=urljoin(source_url, href), text=a.get_text(" ", strip=True)[:80]))

    # 2. footer compliance badges (text + img alt)
    footer_compliance: list[str] = []
    for foot in soup.find_all(["footer"]) + soup.find_all(attrs={"role": "contentinfo"}):
        bits = [foot.get_text(" ", strip=True)] + [img.get("alt", "") for img in foot.find_all("img")]
        for bit in bits:
            for mm in CERT_RE.finditer(bit or ""):
                val = mm.group(0).strip()
                if val.lower() not in {x.lower() for x in footer_compliance}:
                    footer_compliance.append(val)
    # badges anywhere on the page via img alt (trust logos are often outside <footer>)
    for img in soup.find_all("img", alt=True):
        for mm in CERT_RE.finditer(img.get("alt") or ""):
            val = mm.group(0).strip()
            if val.lower() not in {x.lower() for x in footer_compliance}:
                footer_compliance.append(val)

    # 3. strip junk
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for t in soup.find_all(DROP_TAGS):
        t.decompose()
    for t in soup.find_all(["nav", "footer", "aside"]):
        t.decompose()
    for t in soup.find_all(attrs={"role": ["navigation", "contentinfo", "banner", "dialog", "search"]}):
        if t.name == "header" or t.get("role") != "banner" or t.find("nav") or len(t.find_all("a")) > 5:
            t.decompose()
    for t in soup.find_all("header"):
        if t.find("nav") or len(t.find_all("a")) > 5:
            t.decompose()
    for t in list(soup.find_all(True)):
        if t.attrs is None or getattr(t, "decomposed", False):
            continue
        attrs = _attr_text(t)
        if (COOKIE_RE.search(attrs) or POPUP_RE.search(attrs)) and t.name not in ("html", "body", "main"):
            t.decompose()
        elif _is_hidden(t):
            t.decompose()

    # 4. dates
    for tm in soup.find_all("time"):
        dt = (tm.get("datetime") or "").strip()
        label = tm.get_text(" ", strip=True)
        if dt:
            tm.replace_with(f"{label} ({dt[:10]})" if label and dt[:10] not in label else dt[:10])

    for br in soup.find_all("br"):
        br.replace_with(" ")
    # collapse each leaf text block to ONE line, so "<p>Text <a>link</a> more</p>" or a job row
    # "<li>Head of Compliance <span>Remote</span> <time>..</time></li>" is not split into fragments
    for blk in soup.find_all(LEAF_BLOCKS):
        if getattr(blk, "decomposed", False) or blk.find(NESTED_BLOCKS):
            continue
        txt = blk.get_text(" ", strip=True)
        blk.clear()
        blk.append(txt)
    headings = {_norm(h.get_text(" ", strip=True)) for h in soup.find_all(["h1", "h2", "h3", "h4"])}
    body = soup.body or soup
    text = body.get_text("\n", strip=True)

    # 5-6. filter + dedupe
    lines: list[tuple[int, str, int]] = []   # (index, line, priority)
    if meta_desc:
        text = meta_desc + "\n" + text
    for idx, raw_line in enumerate(text.splitlines()):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        n = _norm(line)
        if not n or n in seen:
            continue
        words = len(line.split())
        is_heading = n in headings
        has_key = bool(KEY_TERMS.search(line)) or bool(CERT_RE.search(line))
        is_job = page_type == "careers" and words <= 12 and bool(JOB_TITLE.search(line))
        if words < min_line_words and not (is_heading or has_key or is_job):
            continue
        seen.add(n)
        if SIGNAL_TERMS.search(line) or CERT_RE.search(line):
            priority = 3
        elif has_key or is_job:
            priority = 2
        else:
            priority = 1 if is_heading or idx < 40 else 0
        lines.append((idx, line, priority))

    if footer_compliance:
        badge_line = "Compliance badges/mentions on page: " + ", ".join(footer_compliance)
        if _norm(badge_line) not in seen:
            seen.add(_norm(badge_line))
            lines.insert(0, (-1, badge_line, 4))

    # 7. cap
    total = sum(estimate_tokens(l) + 1 for _, l, _ in lines)
    if total > max_tokens:
        chosen, budget = [], max_tokens
        for item in sorted(lines, key=lambda x: (-x[2], x[0])):
            cost = estimate_tokens(item[1]) + 1
            if cost <= budget:
                chosen.append(item)
                budget -= cost
        lines = sorted(chosen, key=lambda x: x[0])

    kept = [l for _, l, _ in lines]
    clean_text = "\n".join(kept)

    # 8. chunk
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_tokens = 0
    for line in kept:
        t = estimate_tokens(line) + 1
        if buf and buf_tokens + t > chunk_tokens:
            chunks.append(Chunk(text="\n".join(buf), source_url=source_url, fetched_at=fetched_at,
                                page_type=page_type, token_estimate=buf_tokens))
            buf, buf_tokens = [], 0
        buf.append(line)
        buf_tokens += t
    if buf:
        chunks.append(Chunk(text="\n".join(buf), source_url=source_url, fetched_at=fetched_at,
                            page_type=page_type, token_estimate=buf_tokens))

    return CleanResult(
        source_url=source_url, fetched_at=fetched_at, page_type=page_type, title=title,
        raw_tokens=raw_tokens, clean_tokens=estimate_tokens(clean_text), words=len(clean_text.split()),
        text=clean_text, chunks=chunks, links=links, footer_compliance=footer_compliance,
        text_hash=hashlib.sha256(clean_text.encode()).hexdigest()[:16],
    )


def visible_word_count(html: str) -> int:
    """Cheap word count of visible text, used by failure classification before full cleaning."""
    soup = BeautifulSoup((html or "")[:MAX_HTML_CHARS], "lxml")
    for t in soup.find_all(["script", "style", "noscript", "template", "svg"]):
        t.decompose()
    return len((soup.body or soup).get_text(" ", strip=True).split())
