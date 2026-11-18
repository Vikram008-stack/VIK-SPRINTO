"""HTTP response cache: metadata in SQLite (http_cache), body on disk. TTL enforced here.

A re-run on unchanged input inside the TTL makes zero scraping calls.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from src.store.db import Database


@dataclass
class CachedResponse:
    url: str
    mode: str
    status_code: Optional[int]
    final_url: Optional[str]
    body: str
    fetched_at: datetime


def cache_key(url: str, mode: str) -> str:
    return hashlib.sha256(f"{mode}|{url}".encode()).hexdigest()


class HttpCache:
    def __init__(self, db: Database, cache_dir: Path, ttl_days: int):
        self.db = db
        self.dir = Path(cache_dir) / "http"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(days=ttl_days)

    def get(self, url: str, mode: str, now: Optional[datetime] = None) -> Optional[CachedResponse]:
        row = self.db.one("SELECT * FROM http_cache WHERE cache_key=?", (cache_key(url, mode),))
        if not row:
            return None
        fetched = datetime.fromisoformat(row["fetched_at"])
        if (now or datetime.now(timezone.utc)) - fetched > self.ttl:
            return None
        path = Path(row["body_path"]) if row["body_path"] else None
        if path is None or not path.exists():
            return None
        return CachedResponse(url=url, mode=mode, status_code=row["status_code"], final_url=row["final_url"],
                              body=path.read_text(encoding="utf-8", errors="replace"), fetched_at=fetched)

    def put(self, url: str, mode: str, status_code: Optional[int], final_url: Optional[str], body: str,
            fetched_at: Optional[datetime] = None) -> None:
        key = cache_key(url, mode)
        path = self.dir / f"{key}.html"
        path.write_text(body or "", encoding="utf-8")
        self.db.execute(
            "INSERT OR REPLACE INTO http_cache (cache_key, url, mode, status_code, final_url, body_path, fetched_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (key, url, mode, status_code, final_url, str(path),
             (fetched_at or datetime.now(timezone.utc)).isoformat()))
