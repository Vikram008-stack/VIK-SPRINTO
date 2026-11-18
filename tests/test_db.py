import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from src.store.db import HunterQuotaExceeded
from src.store.models import CostEntry, Failure, ReasonCode, Step

EXPECTED_TABLES = {
    "runs", "companies", "pages", "extractions", "signals", "qualification_rounds", "qualifications",
    "contacts", "pain_points", "emails", "sends", "failures", "costs",
    "http_cache", "hunter_cache", "hunter_quota", "search_cache", "llm_cache",
}


def test_all_tables_created(db):
    assert EXPECTED_TABLES <= set(db.tables())


def test_init_is_idempotent(db):
    db.init()
    db.init()
    assert EXPECTED_TABLES <= set(db.tables())


def test_company_upsert_and_status(db):
    db.start_run("r1", "run", {}, True, "v1", "abc")
    db.upsert_company("cal.com", "www.cal.com", "r1")
    db.upsert_company("cal.com", "cal.com", "r1")
    assert len(db.query("SELECT * FROM companies")) == 1
    db.set_company_status("cal.com", "NEEDS_RESEARCH", "already_certified")
    row = db.company("cal.com")
    assert row["status"] == "NEEDS_RESEARCH" and row["status_reason"] == "already_certified"


def test_failure_and_cost_logging(db):
    db.log_failure(Failure(domain="x.io", step=Step.SCRAPE, reason=ReasonCode.BOT_BLOCKED, detail="403"))
    r = db.one("SELECT * FROM failures")
    assert r["reason"] == "bot_blocked" and r["step"] == "scrape"
    db.add_cost(CostEntry(domain="x.io", step=Step.EXTRACT, kind="llm", model="m", input_tokens=10, cost_usd=0.001))
    db.add_cost(CostEntry(domain="x.io", step=Step.EXTRACT, kind="llm", model="m", cost_usd=None))
    assert db.company_cost_usd("x.io") == pytest.approx(0.001)
    assert db.one("SELECT COUNT(*) c FROM costs WHERE cost_usd IS NULL")["c"] == 1


def test_signal_table_rejects_missing_source_url(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.insert("signals", {"domain": "x.io", "kind": "compliance", "signal": "s", "source_url": "",
                              "source_origin": "fetched_page", "observed_at": "2026-11-14", "quality": "WEAK"})


def test_hunter_quota_hard_stop(db):
    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    for i in range(1, 4):
        assert db.reserve_hunter_call("search", hard_stop=3, now=now) == i
    with pytest.raises(HunterQuotaExceeded):
        db.reserve_hunter_call("search", hard_stop=3, now=now)
    assert db.hunter_usage(now)["searches_used"] == 3
    # new calendar month resets
    assert db.reserve_hunter_call("search", hard_stop=3, now=datetime(2026, 12, 1, tzinfo=timezone.utc)) == 1
    # verifications tracked separately
    assert db.reserve_hunter_call("verify", hard_stop=3, now=now) == 1


def test_hunter_quota_is_thread_safe(file_db):
    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    ok, refused = [], []

    def worker():
        try:
            ok.append(file_db.reserve_hunter_call("search", hard_stop=20, now=now))
        except HunterQuotaExceeded:
            refused.append(1)

    threads = [threading.Thread(target=worker) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 20 and len(refused) == 10
    assert file_db.hunter_usage(now)["searches_used"] == 20


def test_purge_contacts(db):
    db.insert("contacts", {"domain": "a.io", "role": "chosen", "name": "A B", "source": "fixture",
                           "created_at": "2026-11-14"})
    assert db.purge_contacts("a.io") == 1


def test_concurrent_requests_share_one_file_lock(tmp_path):
    """Regression (live, 2026-11-18): each web request builds its own Database on the same /tmp file;
    parallel requests raised 'database is locked'. Separate objects must serialise writes per file."""
    from concurrent.futures import ThreadPoolExecutor

    from src.store.db import Database
    path = tmp_path / "shared.db"

    def request(i):
        d = Database(path)          # one object per "request", like the API
        d.init()
        for j in range(25):
            d.log_failure(Failure(domain=f"d{i}.io", step=Step.SCRAPE, reason=ReasonCode.TIMEOUT, detail=str(j)))
            d.upsert_company(f"d{i}.io", f"d{i}.io", None)
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(request, range(8)))
    assert Database(path).one("SELECT COUNT(*) c FROM failures")["c"] == 200
