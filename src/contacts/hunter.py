"""Hunter.io Domain Search + Email Verifier with cache-forever and a monthly quota guard.

- mode=mock (default): replays tests/fixtures/hunter/<domain>.json. Zero credits.
- mode=live: requires the --live-hunter flag AND quota below hard_stop. Counter is reserved atomically BEFORE
  the call. The full response is cached per domain, so re-ranking later costs nothing.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from src.common.config import HunterCfg
from src.common.logging import get_logger
from src.store.db import Database, HunterQuotaExceeded
from src.store.models import Contact, CostEntry, ReasonCode, Step

log = get_logger(__name__)
FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "hunter"
PERSONAL_DOMAINS = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "proton.me",
                    "protonmail.com", "aol.com", "live.com", "yandex.com"}


class HunterError(RuntimeError):
    def __init__(self, reason: ReasonCode, detail: str = ""):
        self.reason, self.detail = reason, detail
        super().__init__(f"{reason.value}: {detail}")


class HunterClient:
    BASE = "https://api.hunter.io/v2"

    def __init__(self, cfg: HunterCfg, db: Database, api_key: Optional[str], live_enabled: bool = False,
                 run_id: Optional[str] = None, fixture_dir: Path = FIXTURE_DIR, http: Optional[httpx.Client] = None):
        self.cfg = cfg
        self.db = db
        self.api_key = api_key
        self.live = cfg.mode == "live" and live_enabled
        self.run_id = run_id
        self.fixture_dir = fixture_dir
        self.http = http or httpx.Client(timeout=20)

    @property
    def mode(self) -> str:
        return "live" if self.live else "mock"

    def _params(self, domain: str) -> dict:
        f = self.cfg.filters
        p = {"domain": domain, "limit": self.cfg.results_limit}
        if f.type:
            p["type"] = f.type
        if f.seniority:
            p["seniority"] = ",".join(f.seniority)
        if f.department:
            p["department"] = ",".join(f.department)
        return p

    def _cached(self, domain: str, endpoint: str, params: dict) -> Optional[dict]:
        ph = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
        row = self.db.one("SELECT response_json FROM hunter_cache WHERE domain=? AND endpoint=? AND params_hash=?",
                          (domain, endpoint, ph))
        return json.loads(row["response_json"]) if row else None

    def _store(self, domain: str, endpoint: str, params: dict, data: dict) -> None:
        ph = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
        self.db.execute("INSERT OR REPLACE INTO hunter_cache (domain, endpoint, params_hash, response_json,"
                        " fetched_at) VALUES (?,?,?,?,?)",
                        (domain, endpoint, ph, json.dumps(data), datetime.now(timezone.utc).isoformat()))

    def account_searches_used(self) -> Optional[int]:
        """Hunter /v2/account (does not consume credits). None if unavailable."""
        try:
            r = self.http.get(f"{self.BASE}/account", params={"api_key": self.api_key})
            if r.status_code != 200:
                return None
            req = (r.json().get("data") or {}).get("requests") or {}
            used = (req.get("searches") or {}).get("used")
            return int(used) if used is not None else None
        except Exception:
            return None

    def domain_search(self, domain: str, refresh: bool = False) -> dict:
        params = self._params(domain)
        if not refresh:
            hit = self._cached(domain, "domain-search", params)
            if hit is not None:
                return hit
        if not self.live:
            fx = self.fixture_dir / f"{domain}.json"
            if not fx.exists():
                raise HunterError(ReasonCode.HUNTER_LIVE_NOT_ENABLED,
                                  f"mock mode and no fixture for {domain} (use --live-hunter to spend a credit)")
            data = json.loads(fx.read_text(encoding="utf-8"))
            self.db.add_cost(CostEntry(domain=domain, step=Step.CONTACTS, kind="hunter_search", calls=0,
                                       cost_usd=0.0, cached=True), run_id=self.run_id)
            return data
        if not self.api_key:
            raise HunterError(ReasonCode.HUNTER_ERROR, "HUNTER_API_KEY not set")
        # Authoritative guard: Hunter's own account counter (free call). Works on serverless, where the local
        # SQLite counter does not persist between instances.
        acct_used = self.account_searches_used()
        if acct_used is not None and acct_used >= self.cfg.hard_stop:
            raise HunterError(ReasonCode.HUNTER_QUOTA_EXHAUSTED,
                              f"Hunter account shows {acct_used} searches used this period; hard stop is "
                              f"{self.cfg.hard_stop}. Refusing live call (use config/manual_contacts.yaml).")
        try:
            used = self.db.reserve_hunter_call("search", self.cfg.hard_stop)
        except HunterQuotaExceeded as e:
            raise HunterError(ReasonCode.HUNTER_QUOTA_EXHAUSTED, str(e)) from e
        log.info("hunter live domain-search", extra={"domain": domain, "used_this_month": used,
                                                     "hard_stop": self.cfg.hard_stop})
        try:
            r = self.http.get(f"{self.BASE}/domain-search", params={**params, "api_key": self.api_key})
        except Exception as e:
            raise HunterError(ReasonCode.HUNTER_ERROR, type(e).__name__) from e
        if r.status_code == 429:
            raise HunterError(ReasonCode.HUNTER_QUOTA_EXHAUSTED, "Hunter usage limit reached (429)")
        if r.status_code == 403:
            raise HunterError(ReasonCode.HUNTER_ERROR, "Hunter rate limit (403)")
        if r.status_code >= 400:
            raise HunterError(ReasonCode.HUNTER_ERROR, f"HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        self._store(domain, "domain-search", params, data)
        self.db.add_cost(CostEntry(domain=domain, step=Step.CONTACTS, kind="hunter_search", calls=1, cost_usd=None),
                         run_id=self.run_id)
        return data

    def verify(self, domain: str, email: str) -> Optional[str]:
        """Return Hunter verification status, or None if skipped (mock/no credits/error)."""
        params = {"email": email}
        hit = self._cached(domain, "email-verifier", params)
        if hit is not None:
            return (hit.get("data") or {}).get("status")
        if not self.live or not self.cfg.verifier or not self.api_key:
            return None
        try:
            self.db.reserve_hunter_call("verify", self.cfg.verifier_monthly_budget)
        except HunterQuotaExceeded:
            return None
        try:
            r = self.http.get(f"{self.BASE}/email-verifier", params={"email": email, "api_key": self.api_key})
            if r.status_code == 202:
                return "pending"
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("hunter verify failed", extra={"domain": domain, "err": type(e).__name__})
            return None
        self._store(domain, "email-verifier", params, data)
        self.db.add_cost(CostEntry(domain=domain, step=Step.CONTACTS, kind="hunter_verify", calls=1, cost_usd=None),
                         run_id=self.run_id)
        return (data.get("data") or {}).get("status")


def parse_contacts(domain: str, data: dict) -> list[Contact]:
    out = []
    for e in (data.get("data") or {}).get("emails", []) or []:
        first, last = (e.get("first_name") or "").strip(), (e.get("last_name") or "").strip()
        name = " ".join(x for x in (first, last) if x)
        if not name:
            continue
        ver = e.get("verification") or {}
        out.append(Contact(name=name, first_name=first or None, last_name=last or None,
                           position=e.get("position") or e.get("position_raw"), seniority=e.get("seniority"),
                           department=e.get("department"), email=e.get("value"), confidence=e.get("confidence"),
                           verification_status=ver.get("status") if isinstance(ver, dict) else None,
                           source="hunter"))
    return out


def manual_contact(domain: str, manual: dict) -> Optional[Contact]:
    m = (manual or {}).get(domain)
    if not m or not m.get("name"):
        return None
    parts = m["name"].split()
    return Contact(name=m["name"], first_name=parts[0], last_name=" ".join(parts[1:]) or None,
                   position=m.get("title"), email=m.get("email"), confidence=None,
                   verification_status=None, source="apollo_manual")
