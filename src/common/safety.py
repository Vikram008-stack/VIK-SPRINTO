"""Demo-send safety. The ONLY address this build may send to is DEMO_RECIPIENT_EMAIL.

- `demo_recipient()` reads it from the environment (never from CLI flags, never from contact data).
- `assert_safe_recipient(to)` is called by the Gmail sender immediately before every send,
  and by `startup_safety_check()` at the start of every run. Any mismatch aborts.
"""
from __future__ import annotations

import os
import re

_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[a-z]{2,}$", re.IGNORECASE)


class UnsafeRecipientError(RuntimeError):
    pass


def demo_recipient() -> str:
    addr = (os.getenv("DEMO_RECIPIENT_EMAIL") or "").strip()
    if not addr:
        raise UnsafeRecipientError("DEMO_RECIPIENT_EMAIL is not set in .env")
    if not _EMAIL_RE.match(addr):
        raise UnsafeRecipientError("DEMO_RECIPIENT_EMAIL is not a valid email address")
    return addr


def assert_safe_recipient(to: str) -> str:
    """Raise unless `to` is exactly the demo recipient (case-insensitive). Returns the safe address."""
    safe = demo_recipient()
    if (to or "").strip().lower() != safe.lower():
        raise UnsafeRecipientError("refusing to send: 'to' is not DEMO_RECIPIENT_EMAIL")
    return safe


def startup_safety_check(sending: bool) -> None:
    """Fail the run early if sending is requested but the demo recipient is missing/invalid."""
    if sending:
        assert_safe_recipient(demo_recipient())
