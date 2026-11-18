"""Addendum D: offline agent-loop demo with zero credits.

Thin homepage (~80 words, no stage/compliance info) -> round 0 flags missing stage + trigger
-> round 1 fetches ONLY /careers -> stage + trigger filled -> score recomputed -> QUALIFIED.
"""
from datetime import date

from src.common.config import load_settings
from src.extract.extractor import extract
from src.llm.gemini_client import CallCtx, FakeLlm
from src.qualify.loop import LoopDeps, run_loop
from src.store.models import QualStatus, Step
from tests.helpers import html, make_fetcher

TODAY = date(2026, 11, 14)
HOME = "https://acme-thin.io/"
CAREERS = "https://acme-thin.io/careers"


def fake_extract(system, user):
    if "SOURCE https://acme-thin.io/careers" in user:
        return {
            "funding_stage": {"value": "series_a", "source_url": CAREERS,
                              "snippet": "We are a Series A company backed by Example Capital"},
            "signals": [{
                "kind": "compliance", "signal": "Hiring a Head of Compliance to lead the first SOC 2 Type II audit",
                "snippet": "You will own our SOC 2 and ISO 27001 programme end to end and lead our first SOC 2 Type II audit",
                "source_url": CAREERS, "event_date": "2026-10-20", "evidence_quality": "MEDIUM",
                "trigger_type": "grc_leader_hire"}],
            "hiring": [{"title": t, "source_url": CAREERS, "event_date": d} for t, d in [
                ("Head of Compliance", "2026-10-20"), ("Senior Backend Engineer", "2026-10-02"),
                ("Product Designer", "2026-09-28"), ("Account Executive, Enterprise", "2026-10-11"),
                ("Customer Success Manager", "2026-09-15"), ("Data Engineer", "2026-10-05"),
                ("Frontend Engineer", "2026-10-09")]],
        }
    return {
        "company_name": {"value": "Acme Thin", "source_url": HOME, "snippet": "Acme Thin is a cloud subscription product"},
        "product_description": {"value": "Invoice automation for B2B finance teams", "source_url": HOME,
                                "snippet": "Invoice automation for B2B finance teams"},
        "business_model": {"value": "cloud_subscription", "source_url": HOME,
                           "snippet": "Acme Thin is a cloud subscription product that reads supplier invoices"},
        "buyer_type": {"value": "business", "source_url": HOME,
                       "snippet": "Finance teams at growing B2B software companies use Acme Thin"},
        "funding_stage": None,
        "signals": [],
        "hiring": [],
    }


def test_thin_homepage_triggers_only_careers_fetch(db):
    s = load_settings()
    pages = {HOME: html("thin_homepage.html"), CAREERS: html("careers_compliance.html")}
    fetcher, transport = make_fetcher(db, pages, settings=s)
    llm = FakeLlm(s.pipeline.llm, db, handlers={"extract": fake_extract})
    session = fetcher.start("acme-thin.io")
    home = fetcher.fetch_homepage(session)
    assert home.ok
    ex = extract(llm, CallCtx("acme-thin.io", Step.EXTRACT), "acme-thin.io", home.clean.chunks, TODAY)
    assert ex.facts.funding_stage is None and ex.facts.business_model == "cloud_subscription"

    deps = LoopDeps(settings=s, db=db, fetcher=fetcher, llm=llm, search=None, today=TODAY)
    out = run_loop(deps, session, ex.facts)

    r0, r1 = out.rounds[0], out.rounds[1]
    assert set(r0.missing_after) == {"stage", "trigger"}            # round 0 flags missing stage + trigger
    assert r1.fetched_urls == [CAREERS]                              # ONLY /careers fetched
    assert r1.missing_after == []                                    # both filled
    assert r1.score > r0.score                                       # score recomputed
    fetched = [u for u, _js in transport.calls]
    assert set(fetched) == {HOME, CAREERS}
    assert len(out.rounds) == 2                                      # stopped early: nothing missing
    assert out.decision.status == QualStatus.QUALIFIED
    # 25 SaaS + 25 GRC-leader hire + 15 Series A + 4 B2B unclear + 10 recency (25 days old)
    assert out.decision.score == 79
    trig = out.decision.breakdown.get("compliance_trigger")
    assert trig.source_urls == [CAREERS]
    # iteration logging is persisted per round by the pipeline; the loop exposes it
    assert r1.missing_before == r0.missing_after


def test_loop_rejects_hallucinated_source(db):
    s = load_settings()
    fetcher, _ = make_fetcher(db, {HOME: html("thin_homepage.html")}, settings=s)
    session = fetcher.start("acme-thin.io")
    home = fetcher.fetch_homepage(session)

    def lying(system, user):
        return {"funding_stage": {"value": "series_b", "source_url": "https://techcrunch.com/acme",
                                  "snippet": "Acme raised a Series B"},
                "signals": [{"kind": "compliance", "signal": "SOC 2 audit planned", "snippet": "planning SOC 2 audit",
                             "source_url": HOME, "event_date": "2026-11-01", "evidence_quality": "STRONG",
                             "trigger_type": "explicit_audit_target"}]}
    llm = FakeLlm(s.pipeline.llm, db, handlers={"extract": lying})
    ex = extract(llm, CallCtx("acme-thin.io", Step.EXTRACT), "acme-thin.io", home.clean.chunks, TODAY)
    assert ex.facts.funding_stage is None          # URL we never fetched -> rejected
    assert ex.facts.compliance_signals == []       # snippet not in page text -> rejected
    assert any("url_provenance_rejected" in d for d in ex.dropped)
    assert any("snippet_not_found" in d for d in ex.dropped)
