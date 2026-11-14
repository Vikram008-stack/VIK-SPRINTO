"""Logging: human-readable console + one JSONL file per run (machine-readable audit companion).

Usage:
    log = get_logger(__name__)
    log.info("fetched", extra={"domain": d, "url": u, "mode": "plain"})
Secrets are scrubbed from every record before it is written.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_STD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}
_SECRET_ENV = ("SCRAPINGBEE_API_KEY", "GEMINI_API_KEY", "HUNTER_API_KEY", "SEARCH_API_KEY")
_KEY_PARAM_RE = re.compile(r"(?i)(api_key|key|token)=([^&\s\"']+)")


def _scrub(text: str) -> str:
    for name in _SECRET_ENV:
        val = os.getenv(name)
        if val and len(val) >= 6:
            text = text.replace(val, f"<{name}>")
    return _KEY_PARAM_RE.sub(r"\1=<redacted>", text)


class _ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _scrub(str(record.msg))
        if record.args:
            record.args = tuple(_scrub(str(a)) for a in record.args)
        return True


class _JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _STD_ATTRS and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return _scrub(json.dumps(payload, default=str))


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = {k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS and not k.startswith("_")}
        dom = extras.pop("domain", None)
        prefix = f"[{dom}] " if dom else ""
        tail = " ".join(f"{k}={v}" for k, v in extras.items())
        line = f"{record.levelname[0]} {prefix}{record.getMessage()}" + (f"  {tail}" if tail else "")
        return _scrub(line)


_configured = False


def setup_logging(log_dir: Optional[Path] = None, run_id: Optional[str] = None,
                  level: int = logging.INFO) -> Optional[Path]:
    """Configure root 'sprinto' logger. Returns the JSONL path if a file handler was added."""
    global _configured
    root = logging.getLogger("sprinto")
    root.setLevel(level)
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler()
    console.setFormatter(_ConsoleFormatter())
    console.addFilter(_ScrubFilter())
    root.addHandler(console)

    path = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"run_{run_id or 'adhoc'}.jsonl"
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(_JsonlFormatter())
        fh.addFilter(_ScrubFilter())
        root.addHandler(fh)
    _configured = True
    return path


def get_logger(name: str) -> logging.Logger:
    if not _configured:
        setup_logging()
    short = name.removeprefix("src.")
    return logging.getLogger(f"sprinto.{short}")
