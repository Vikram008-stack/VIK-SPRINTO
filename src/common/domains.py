"""Domain normalisation + dedupe (idempotency starts here).

'https://WWW.Cal.com/' and 'www.cal.com' both become 'cal.com'; the second is reported as a duplicate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_DOMAIN_RE = re.compile(rf"^(?:{_LABEL}\.)+[a-z]{{2,63}}$")


class InvalidDomain(ValueError):
    pass


def normalise_domain(raw: str) -> str:
    s = (raw or "").strip().lower()
    if not s:
        raise InvalidDomain("empty domain")
    # Markdown links like [www.cal.com](https://www.cal.com) -> take the visible text
    m = re.match(r"^\[([^\]]+)\]\([^)]*\)$", s)
    if m:
        s = m.group(1)
    if "://" not in s:
        s = "http://" + s
    host = urlparse(s).hostname or ""
    host = host.rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not _DOMAIN_RE.match(host):
        raise InvalidDomain(f"not a valid domain: {raw!r}")
    return host


@dataclass
class DomainList:
    unique: list[tuple[str, str]] = field(default_factory=list)        # (normalised, raw)
    duplicates: list[tuple[str, str]] = field(default_factory=list)    # (normalised, raw)
    invalid: list[tuple[str, str]] = field(default_factory=list)       # (raw, error)


def parse_domains(lines: list[str]) -> DomainList:
    out = DomainList()
    seen: set[str] = set()
    for line in lines:
        raw = line.split("#", 1)[0].strip()
        if not raw:
            continue
        try:
            d = normalise_domain(raw)
        except InvalidDomain as e:
            out.invalid.append((raw, str(e)))
            continue
        if d in seen:
            out.duplicates.append((d, raw))
        else:
            seen.add(d)
            out.unique.append((d, raw))
    return out


def same_entity(input_domain: str, final_url_or_host: str) -> bool:
    """True if the post-redirect host is the input domain or one of its subdomains."""
    try:
        final = normalise_domain(final_url_or_host)
    except InvalidDomain:
        return False
    return final == input_domain or final.endswith("." + input_domain)
