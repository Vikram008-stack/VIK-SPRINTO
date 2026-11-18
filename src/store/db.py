"""SQLite access layer.

- WAL mode so the 3-worker thread pool can read while one thread writes.
- One connection per thread; all writes go through a process-wide lock.
- Small typed helpers for the audit tables used by every step.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional

from pydantic import BaseModel

from src.store.models import CostEntry, Failure

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(v: Any) -> Any:
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, BaseModel):
        return v.model_dump_json()
    if isinstance(v, (list, dict)):
        return json.dumps(v, default=str)
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


class HunterQuotaExceeded(RuntimeError):
    pass


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._memory_conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------ connections
    @property
    def conn(self) -> sqlite3.Connection:
        if str(self.path) == ":memory:":  # tests: share one connection
            if self._memory_conn is None:
                self._memory_conn = self._connect()
            return self._memory_conn
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        return c

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        if str(self.path) != ":memory:":
            c.execute("PRAGMA journal_mode = WAL")
            c.execute("PRAGMA busy_timeout = 30000")
        return c

    def init(self) -> None:
        with self._write_lock:
            self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None
        if self._memory_conn is not None:
            self._memory_conn.close()
            self._memory_conn = None

    # ------------------------------------------------------------ generic
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._write_lock:
            return self.conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._write_lock:   # cheap; also makes the shared :memory: connection safe across threads
            return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        with self._write_lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def insert(self, table: str, row: dict[str, Any]) -> int:
        cols = list(row)
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        cur = self.execute(sql, [_jsonable(row[c]) for c in cols])
        return int(cur.lastrowid)

    def tables(self) -> list[str]:
        return [r["name"] for r in self.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]

    # ------------------------------------------------------------ runs
    def start_run(self, run_id: str, command: str, args: dict, dry_run: bool,
                  rubric_version: str, config_hash: str) -> None:
        self.insert("runs", {
            "run_id": run_id, "started_at": _now(), "command": command, "args_json": args,
            "dry_run": dry_run, "rubric_version": rubric_version, "config_hash": config_hash,
        })

    def finish_run(self, run_id: str, summary: dict) -> None:
        self.execute("UPDATE runs SET finished_at=?, summary_json=? WHERE run_id=?",
                     (_now(), json.dumps(summary, default=str), run_id))

    # ------------------------------------------------------------ companies
    def upsert_company(self, domain: str, input_raw: str, run_id: Optional[str]) -> None:
        with self._write_lock:
            existing = self.one("SELECT domain FROM companies WHERE domain=?", (domain,))
            if existing:
                self.execute("UPDATE companies SET last_run_id=?, updated_at=? WHERE domain=?",
                             (run_id, _now(), domain))
            else:
                self.insert("companies", {
                    "domain": domain, "input_raw": input_raw, "first_run_id": run_id,
                    "last_run_id": run_id, "status": "PENDING", "updated_at": _now(),
                })

    def set_company_status(self, domain: str, status: str, reason: Optional[str] = None,
                           **extra: Any) -> None:
        fields = {"status": status, "status_reason": reason, "updated_at": _now(), **extra}
        sets = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE companies SET {sets} WHERE domain=?",
                     [_jsonable(v) for v in fields.values()] + [domain])

    def company(self, domain: str) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM companies WHERE domain=?", (domain,))

    # ------------------------------------------------------------ audit
    def log_failure(self, f: Failure, run_id: Optional[str] = None) -> int:
        return self.insert("failures", {
            "run_id": run_id, "domain": f.domain, "step": f.step, "reason": f.reason,
            "detail": f.detail, "at": f.at,
        })

    def add_cost(self, c: CostEntry, run_id: Optional[str] = None) -> int:
        return self.insert("costs", {"run_id": run_id, **c.model_dump(), "at": _now()})

    def company_cost_usd(self, domain: str, run_id: Optional[str] = None) -> float:
        sql = "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM costs WHERE domain=?"
        params: list[Any] = [domain]
        if run_id:
            sql += " AND run_id=?"
            params.append(run_id)
        return float(self.one(sql, params)["s"])

    # ------------------------------------------------------------ hunter quota
    @staticmethod
    def _month(now: Optional[datetime] = None) -> str:
        return (now or datetime.now(timezone.utc)).strftime("%Y-%m")

    def hunter_usage(self, now: Optional[datetime] = None) -> dict[str, int]:
        row = self.one("SELECT * FROM hunter_quota WHERE month=?", (self._month(now),))
        return {"searches_used": row["searches_used"] if row else 0,
                "verifications_used": row["verifications_used"] if row else 0}

    def reserve_hunter_call(self, kind: str, hard_stop: int, now: Optional[datetime] = None) -> int:
        """Atomically check-and-increment the monthly counter BEFORE a live call.

        kind: 'search' | 'verify'. Raises HunterQuotaExceeded if the call would exceed hard_stop.
        Returns the new used count.
        """
        col = {"search": "searches_used", "verify": "verifications_used"}[kind]
        month = self._month(now)
        with self._write_lock:
            self.execute("INSERT OR IGNORE INTO hunter_quota (month, updated_at) VALUES (?, ?)", (month, _now()))
            used = self.one(f"SELECT {col} AS u FROM hunter_quota WHERE month=?", (month,))["u"]
            if used + 1 > hard_stop:
                raise HunterQuotaExceeded(f"hunter {kind}: {used}/{hard_stop} used this month; refusing live call")
            self.execute(f"UPDATE hunter_quota SET {col}={col}+1, updated_at=? WHERE month=?", (_now(), month))
            return used + 1

    # ------------------------------------------------------------ privacy
    def purge_contacts(self, domain: Optional[str] = None) -> int:
        if domain:
            return self.execute("DELETE FROM contacts WHERE domain=?", (domain,)).rowcount
        return self.execute("DELETE FROM contacts").rowcount
