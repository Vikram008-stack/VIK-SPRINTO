"""End-to-end: the whole pipeline offline (fixture sites, recorded model responses, mock Hunter, dry run)."""
import json

import pytest

from src import demo_offline
from src.common.config import load_settings
from src.factory import build_offline_demo
from src.pipeline import RunOptions
from src.report.summary import defence_sheets, eval_table, run_summary


@pytest.fixture
def run(db, tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_RECIPIENT_EMAIL", raising=False)
    s = load_settings()
    pipe = build_offline_demo(s, db, "t-run", reports_dir=tmp_path / "r", outcomes_dir=tmp_path / "o")
    outs = pipe.run(demo_offline.DOMAINS, RunOptions(dry_run=True))
    return {o.domain + ("#dup" if o.status == "DUPLICATE" else ""): o for o in outs}, pipe, db, tmp_path


def test_statuses(run):
    by, *_ = run
    assert by["acme-thin.io"].status == "DRAFTED"
    assert by["ledgerly.io"].status == "DRAFTED"
    assert by["brightpath.io"].status == "REJECTED"
    assert by["schedulr.io"].status == "NEEDS_RESEARCH"
    assert by["blocked-demo.io"].status == "NEEDS_RESEARCH"
    assert by["acme-thin.io#dup"].status == "DUPLICATE"


def test_consultancy_stops_at_stage_gate(run):
    by, *_ = run
    o = by["brightpath.io"]
    assert len(o.pages) == 1                                  # homepage only: no extra fetch, search or Hunter
    assert o.qualification["hard_disqualifier"] == "not_software"
    assert o.contact is None


def test_qualified_has_full_defensible_trail(run):
    by, *_ = run
    o = by["ledgerly.io"]
    q = o.qualification
    assert q["score"] == 90 and q["status"] == "QUALIFIED"
    assert all(c["source_urls"] for c in q["breakdown"]["components"] if c["points"])
    assert o.contact["chosen"]["name"] == "Arjun Mehta"
    assert len(o.pain_points) == 2
    assert o.draft and o.draft_attempts[-1]["errors"] == []
    assert o.send["status"] == "DRY_RUN"
    assert any(f["reason"] == "score_diff" for f in o.failures)      # model 92 vs Python 90 logged; Python wins


def test_pain_confidence_capped_by_evidence(run):
    by, *_ = run
    news = [p for p in by["acme-thin.io"].pain_points if "Series A" in p["signal"]][0]
    assert news["confidence"] == "MEDIUM"                    # model said HIGH; MEDIUM evidence caps it


def test_every_failure_has_reason_code(run):
    by, _pipe, db, _ = run
    rows = db.query("SELECT reason FROM failures")
    assert rows and all(r["reason"] for r in rows)
    assert db.one("SELECT COUNT(*) c FROM failures WHERE reason='captcha'")["c"] == 1


def test_rerun_is_idempotent_and_free(run, tmp_path):
    by, pipe, db, tp = run
    before = db.one("SELECT COUNT(*) c FROM pages")["c"]
    llm_calls = len(pipe.llm.calls)
    pipe.run_id = "t-run-2"
    again = pipe.run(demo_offline.DOMAINS, RunOptions(dry_run=True))
    assert db.one("SELECT COUNT(*) c FROM pages")["c"] == before      # nothing refetched
    assert len(pipe.llm.calls) == llm_calls                           # no model calls
    assert {o.domain: o.status for o in again}["ledgerly.io"] == "DRAFTED"


def test_reports_and_eval(run):
    by, pipe, db, tp = run
    assert (tp / "r" / "ledgerly.io.md").read_text(encoding="utf-8").startswith("# Ledgerly")
    data = [json.loads(o.to_json()) for o in by.values()]
    md = run_summary(data, db.hunter_usage(), 20)
    assert "Human review queue" in md and "blocked-demo.io" in md
    expected = {"ledgerly.io": {"status": "QUALIFIED"}, "brightpath.io": {"status": "REJECTED"},
                "schedulr.io": {"status": "NEEDS_RESEARCH"}, "blocked-demo.io": {"status": "NEEDS_RESEARCH|INSUFFICIENT_DATA"},
                "acme-thin.io": {"status": "QUALIFIED"}}
    table, acc = eval_table(data, expected, frozen=True)
    assert acc == 1.0 and "KILL" not in table
    sheets = defence_sheets(data)
    assert "QUALIFIED example" in sheets and "REJECTED example" in sheets


def test_console_html_is_self_contained(run, tmp_path):
    from src.report.console import build_console
    by, *_ = run
    data = [json.loads(o.to_json()) for o in by.values()]
    p = build_console(data, tmp_path / "c.html")
    html = p.read_text(encoding="utf-8")
    assert "<script src=" not in html and "<link" not in html     # no external requests
    assert "Outreach Review Desk" in html and "ledgerly.io" in html
